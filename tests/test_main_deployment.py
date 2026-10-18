"""Deployment policy and orchestration checks; never connect to a live VM."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import io

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40


def test_workflow_main_automatic_trigger_and_explicit_manual_source():
    # BaseLoader avoids YAML 1.1 interpreting GitHub's `on` as a boolean.
    workflow = yaml.load((ROOT / '.github/workflows/deploy-main.yml').read_text(), Loader=yaml.BaseLoader)
    assert workflow['on']['push'] == {'branches': ['main']}
    assert set(workflow['on']) == {'push', 'workflow_dispatch'}
    assert workflow['on']['workflow_dispatch']['inputs']['source_ref']['default'] == 'main'
    assert set(workflow['jobs']) == {'verify', 'deploy'}
    job = workflow['jobs']['deploy']
    assert job['needs'] == 'verify'
    assert job['env']['DEPLOY_REVISION'] == '${{ needs.verify.outputs.revision }}'
    checkouts = [step for step in job['steps'] if step.get('uses', '').startswith('actions/checkout@')]
    assert checkouts[0]['with']['ref'] == '${{ needs.verify.outputs.revision }}'
    assert "github.event_name == 'push'" in job['if']
    assert "github.ref == 'refs/heads/main'" in job['if']
    assert "github.repository == 'Barathj121/Agentic-AI-Framework'" in job['if']
    assert job['concurrency']['cancel-in-progress'] == 'false'
    actions = [step.get('uses', '') for step in job['steps']]
    assert 'google-github-actions/auth@v3' in actions
    assert 'google-github-actions/setup-gcloud@v3' in actions
    commands = '\n'.join(step.get('run', '') for step in job['steps'])
    assert '--revision "$DEPLOY_REVISION"' in commands
    assert "github.event_name == 'workflow_dispatch'" in job['if']
    assert '/git/ref/heads/main' in commands
    main_check = next(step for step in job['steps'] if step.get('name') == 'Validate configuration and requested revision')
    assert main_check['env']['GH_TOKEN'] == '${{ github.token }}'
    assert 'bash deploy/gcp/update.sh' in commands
    assert 'bootstrap.sh' not in commands and 'deploy/gcp/deploy.sh' not in commands
    verification = '\n'.join(step.get('run', '') for step in workflow['jobs']['verify']['steps'])
    assert "pytest -m 'not live_ollama'" in verification
    assert 'workbench-launcher-demo:latest' in verification
    assert 'agentic-workbench-sandbox:latest' in verification
    assert workflow['jobs']['verify'].get('permissions', workflow['permissions']) == {'contents': 'read'}


def test_committed_archive_ignores_dirty_and_untracked_content(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('main_package', ROOT / 'deploy/gcp/package.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path).decode().strip()
    git('init', '-q')
    (tmp_path / 'app.py').write_text('committed source')
    (tmp_path / '.env').write_text('fixture-secret')
    (tmp_path / 'examples').mkdir()
    (tmp_path / 'examples/linked.py').symlink_to(tmp_path / '.env')
    git('add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
    revision = git('rev-parse', 'HEAD')
    (tmp_path / 'app.py').write_text('dirty source must not ship')
    (tmp_path / 'examples/untracked.py').write_text('must not ship')
    module.package(tmp_path / 'release.tar.gz', revision=revision)
    with tarfile.open(tmp_path / 'release.tar.gz') as archive:
        assert archive.getnames() == ['app.py']
        assert archive.extractfile('app.py').read() == b'committed source'
    with pytest.raises(ValueError):
        module.package(tmp_path / 'bad.tar.gz', revision='main')


@pytest.fixture
def deployment_tools(tmp_path):
    gcloud = tmp_path / 'gcloud'
    gcloud.write_text('''#!/usr/bin/env python3
import json,os,sys
with open(os.environ['CALLS'],'a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1:4]==['compute','instances','describe']:print('RUNNING')
''')
    gcloud.chmod(0o755)
    curl = tmp_path / 'curl'
    curl.write_text('''#!/usr/bin/env python3
import json,os,sys
with open(os.environ['CALLS'],'a') as f:f.write(json.dumps(['curl']+sys.argv[1:])+'\\n')
''')
    curl.chmod(0o755)
    archive = tmp_path / 'source.tar.gz'
    archive.write_bytes(b'fixture archive')
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'], 'CALLS': str(tmp_path / 'calls'),
           'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': SHA,
           'GITHUB_ACTIONS':'true', 'GITHUB_REPOSITORY': 'Barathj121/Agentic-AI-Framework', 'PROJECT_ID': 'test-project',
           'ZONE': 'us-central1-a', 'VM': 'test-vm', 'PUBLIC_URL': 'https://test-web.run.app', 'RUNNER_TEMP': str(tmp_path)}
    return archive, env


@pytest.mark.parametrize('key,value', [('GITHUB_REF','refs/heads/feature/test'), ('GITHUB_REF','refs/tags/v1'),
                                     ('GITHUB_EVENT_NAME','pull_request'), ('GITHUB_EVENT_NAME','schedule'),
                                     ('GITHUB_REPOSITORY','fork/Agentic-AI-Framework'), ('GITHUB_SHA','b'*40)])
def test_update_rejects_unapproved_event_before_any_cloud_call(deployment_tools, key, value):
    archive, env = deployment_tools
    env[key] = value
    result = subprocess.run(['bash', str(ROOT / 'deploy/gcp/update.sh'), str(archive), SHA], env=env, capture_output=True)
    assert result.returncode != 0
    assert not Path(env['CALLS']).exists()


def test_update_targets_existing_vm_and_gateway_without_provisioning(deployment_tools):
    archive, env = deployment_tools
    result = subprocess.run(['bash', str(ROOT / 'deploy/gcp/update.sh'), str(archive), SHA], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in Path(env['CALLS']).read_text().splitlines()]
    assert [call[:3] for call in calls[:3]] == [['compute','instances','describe'],['compute','scp',str(archive)],['compute','ssh','test-vm']]
    command = next(part for part in calls[2] if part.startswith('--command='))
    assert 'sha256sum --check --status' in command and 'sudo bash' in command
    assert all('--tunnel-through-iap' in call for call in calls[1:3])
    assert calls[-1][-1] == 'https://test-web.run.app/_stcore/health'
    assert not any(token in {'create','delete','start','deploy','set-machine-type'} for call in calls for token in call)


@pytest.mark.parametrize('name,kind', [('../escape','file'),('/tmp/escape','file'),('examples/link','symlink'),
                                      ('.env','file'),('workspaces/private.json','file')])
def test_vm_rejects_unsafe_archive_before_writing_any_member(tmp_path, name, kind):
    script = (ROOT / 'deploy/gcp/update-vm.sh').read_text().split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    path = tmp_path / 'release.tar.gz'
    with tarfile.open(path, 'w:gz') as archive:
        allowed = tarfile.TarInfo('app.py'); allowed.size = 4
        archive.addfile(allowed, io.BytesIO(b'pass'))
        member = tarfile.TarInfo(name)
        if kind == 'symlink': member.type = tarfile.SYMTYPE; member.linkname = '/etc/passwd'
        archive.addfile(member)
    destination = tmp_path / 'release'; destination.mkdir()
    result = subprocess.run([sys.executable, '-', str(path), str(destination)], input=script, text=True, capture_output=True)
    assert result.returncode != 0
    assert list(destination.iterdir()) == []


def test_update_scripts_have_valid_shell_syntax():
    for name in ['update.sh', 'update-vm.sh', 'setup-main-identity.sh', 'manual.sh']:
        result = subprocess.run(['bash', '-n', str(ROOT / 'deploy/gcp' / name)], capture_output=True)
        assert result.returncode == 0, result.stderr


def test_release_smoke_check_imports_packaged_runtime_from_workspace(tmp_path):
    """Exercise the actual smoke-check code with the sandbox's file layout."""
    import shlex
    import shutil

    runtime_root = tmp_path / 'opt/workbench'
    package = runtime_root / 'clarifi_runtime'
    package.mkdir(parents=True)
    for name in ['__init__.py', 'schemas.py', 'tools.py']:
        shutil.copyfile(ROOT / 'workbench/agents/clarifi' / name, package / name)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    script = (ROOT / 'deploy/gcp/update-vm.sh').read_text()
    smoke = script[script.index('docker run --rm'):].split('\n# Preserve', 1)[0]
    arguments = shlex.split(smoke.replace('\\\n', ' '))
    command = arguments[arguments.index('python'):]
    command[0] = sys.executable
    # Substitute only the container filesystem root, preserving the actual check.
    command[-1] = command[-1].replace('/opt/workbench', str(runtime_root))
    result = subprocess.run(command, cwd=workspace, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('recovery_fails', [False, True])
@pytest.mark.parametrize('had_env', [False, True])
def test_failed_activation_restores_release_and_reports_failed_recovery(tmp_path, recovery_fails, had_env):
    import shlex
    text = (ROOT / 'deploy/gcp/update-vm.sh').read_text()
    rollback = text[text.index('rollback() {'):text.index('\ntrap rollback ERR')]
    previous = tmp_path / 'old'; previous.mkdir()
    current_release = tmp_path / 'new'; current_release.mkdir()
    current = tmp_path / 'current'; current.symlink_to(current_release, target_is_directory=True)
    state = tmp_path / 'state'; state.mkdir()
    (state / 'previous.env').write_text('SANDBOX_IMAGE=fixture:old\n')
    release_env = tmp_path / 'release.env'; release_env.write_text('SANDBOX_IMAGE=fixture:new\n')
    systemctl = tmp_path / 'systemctl'
    systemctl.write_text('#!/bin/sh\nexit ' + ('1' if recovery_fails else '0') + '\n'); systemctl.chmod(0o755)
    curl = tmp_path / 'curl'; curl.write_text('#!/bin/sh\nexit 0\n'); curl.chmod(0o755)
    variables = {'PREVIOUS':str(previous), 'CURRENT':str(current), 'STATE':str(state), 'ENV_FILE':str(release_env),
                 'HAD_ENV':'true' if had_env else 'false', 'SWITCHED':'true', 'STOPPED':'true'}
    program = 'set -Eeuo pipefail\n' + '\n'.join(f'{k}={shlex.quote(v)}' for k,v in variables.items())
    program += '\n' + rollback + '\ntrap rollback ERR\nfalse\n'
    result = subprocess.run(['bash'], input=program, text=True, capture_output=True,
                            env={**os.environ,'PATH':str(tmp_path)+os.pathsep+os.environ['PATH']})
    assert result.returncode == 1
    assert current.resolve() == previous
    assert release_env.read_text() == ('SANDBOX_IMAGE=fixture:old\n' if had_env else '')
    if recovery_fails:
        assert 'automatic recovery failed' in result.stderr
        assert 'health checks restored' not in result.stderr
    else:
        assert 'previous application and health checks restored' in result.stderr


def test_manual_workflow_can_deploy_explicit_commit_from_main_controller(deployment_tools):
    archive, env = deployment_tools
    env.update(GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_SHA='b'*40, DEPLOY_REVISION=SHA)
    result = subprocess.run(['bash', str(ROOT / 'deploy/gcp/update.sh'), str(archive), SHA], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert Path(env['CALLS']).exists()


def test_explicit_operator_mode_can_deploy_feature_commit_without_push(deployment_tools):
    archive, env = deployment_tools
    env.update(MANUAL_DEPLOY='true', GITHUB_ACTIONS='false', GITHUB_REF='refs/heads/feature/review', GITHUB_SHA='b'*40)
    result = subprocess.run(['bash', str(ROOT / 'deploy/gcp/update.sh'), str(archive), SHA], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in Path(env['CALLS']).read_text().splitlines()]
    assert not any('push' in call for call in calls)


def test_ci_cannot_use_operator_mode_to_bypass_branch_restriction(deployment_tools):
    archive, env = deployment_tools
    env.update(MANUAL_DEPLOY='true', GITHUB_ACTIONS='true', GITHUB_REF='refs/heads/feature/review')
    result = subprocess.run(['bash', str(ROOT / 'deploy/gcp/update.sh'), str(archive), SHA], env=env, capture_output=True)
    assert result.returncode != 0
    assert not Path(env['CALLS']).exists()
