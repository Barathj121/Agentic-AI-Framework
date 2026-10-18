import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile


ROOT = Path(__file__).resolve().parents[1]


def test_source_archive_excludes_runtime_data_credentials_and_symlinks(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("gcp_package", ROOT / "deploy/gcp/package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = ["app.py", "ui/clarifi.py", "examples/financial.json", ".env", "examples/.env",
             "examples/private.key", "workspaces/customer.json", "outputs/data.xlsx", ".venv/bin/python"]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    (tmp_path / "examples/linked.json").symlink_to(tmp_path / ".env")
    names.append("examples/linked.json")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module.subprocess, "check_output", lambda *args, **kwargs: "\0".join(names).encode())
    output = tmp_path / "source.tar.gz"
    module.package(output)
    with tarfile.open(output) as archive:
        assert set(archive.getnames()) == {"app.py", "ui/clarifi.py", "examples/financial.json"}


def test_fresh_deployment_exposes_gateway_and_preserves_private_backend(tmp_path):
    # Exercise orchestration only: the fake gcloud makes no network/resource changes.
    fake = tmp_path / "gcloud"
    fake.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["GCLOUD_CALLS"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:4] == ["run", "services", "describe"]:
    print("https://test-123.us-central1.run.app")
if sys.argv[1:4] == ["compute", "instances", "describe"]:
    print("10.42.0.2")
''')
    fake.chmod(0o755)
    fake_curl = tmp_path / "curl"
    fake_curl.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["CURL_CALLS"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
''')
    fake_curl.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    curl_log = tmp_path / "curl.jsonl"
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "GCLOUD_CALLS": str(log), "CURL_CALLS": str(curl_log), "TMPDIR": str(tmp_path), "PYTHON": sys.executable,
           "PROJECT_ID": "test-project", "REGION": "us-central1", "ZONE": "us-central1-a",
           "PREFIX": "test-workbench", "TAG": "test-build", "CLARIFI_MODE": "gemini",
           "CLARIFI_API_MODEL": "gemini-test", "CLARIFI_API_KEY_SECRET": "test-secret:1",
           "CLARIFI_EVIDENCE_BUCKET": "test-evidence-bucket", "OLLAMA_IMAGE": "ollama/ollama:test-release"}
    result = subprocess.run(["bash", "deploy/gcp/deploy.sh"], cwd=ROOT, env=env,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    vm = next(call for call in calls if call[:3] == ["compute", "instances", "create"])
    assert "--no-boot-disk-auto-delete" in vm
    firewall = next(call for call in calls if call[:3] == ["compute", "firewall-rules", "create"])
    assert "--allow=tcp:22" in firewall
    assert "--source-ranges=35.235.240.0/20" in firewall
    api = next(call for call in calls if call[:2] == ["run", "deploy"])
    assert "--no-allow-unauthenticated" in api
    assert "--set-secrets=GEMINI_API_KEY=test-secret:1" in api
    builds = [call for call in calls if call[:2] == ["builds", "submit"]]
    assert len(builds) == 2
    assert all("--service-account=projects/test-project/serviceAccounts/test-workbench-build@test-project.iam.gserviceaccount.com" in call for call in builds)
    assert all("--gcs-source-staging-dir=gs://test-project-test-workbench-builds/source" in call for call in builds)
    web = next(call for call in calls if call[:3] == ["run", "deploy", "test-workbench-web"])
    assert "--allow-unauthenticated" in web
    assert "--network-tags=test-workbench-web" in web
    assert "--vpc-egress=private-ranges-only" in web
    assert "--set-env-vars=WORKBENCH_UPSTREAM=10.42.0.2:8080" in web
    internal = next(call for call in calls if call[:4] == ["compute", "firewall-rules", "create", "test-workbench-web-internal"])
    assert "--source-tags=test-workbench-web" in internal
    assert "--allow=tcp:8080" in internal
    assert not any("0.0.0.0/0" in part for call in calls for part in call)
    bootstrap = next(call for call in calls if call[:2] == ["compute", "ssh"])
    assert "--tunnel-through-iap" in bootstrap
    assert any("CLARIFI_REMOTE_MODEL='gemini-test'" in part for part in bootstrap)
    assert any("PUBLIC_HOST='test-123.us-central1.run.app'" in part for part in bootstrap)
    assert "Public judge URL: https://test-123.us-central1.run.app" in result.stdout
    probes = [json.loads(line)[-1] for line in curl_log.read_text().splitlines()]
    assert probes == ["https://test-123.us-central1.run.app/_stcore/health",
                      "https://test-123.us-central1.run.app/apps/demo/"]
    assert not any("delete" in call for call in calls)


def test_deployment_shell_syntax():
    result = subprocess.run(["bash", "-n", str(ROOT / "deploy/gcp/deploy.sh")], capture_output=True)
    assert result.returncode == 0
    result = subprocess.run(["bash", "-n", str(ROOT / "deploy/gcp/bootstrap.sh")], capture_output=True)
    assert result.returncode == 0
