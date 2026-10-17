"""Live inference rejects invalid state and runs model bytes only in Docker."""
import hashlib
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from workbench.prediction import FEATURE_NAMES, MODEL_LOCATION, get_prediction_spec, predict_alert
from workbench.schemas import ExecutionResult
from workbench.settings import Settings
from workbench.workspace import WorkspaceManager, atomic_json


@pytest.fixture
def prediction_workspace(tmp_path):
    settings = Settings(workspace_root=tmp_path / 'workspaces')
    manager = WorkspaceManager(settings.workspace_root)
    workspace = manager.create('Predictive example', 'Synthetic supervised model')
    (workspace / MODEL_LOCATION).write_bytes(b'not a pickle; must never be loaded on host')
    atomic_json(workspace / 'metadata/manifest.json', {'status': 'COMPLETED', 'artifacts': [{'location': MODEL_LOCATION, 'type': 'model', 'validation_status': 'PASSED', 'size_bytes': (workspace / MODEL_LOCATION).stat().st_size, 'sha256': hashlib.sha256((workspace / MODEL_LOCATION).read_bytes()).hexdigest()}, {'location': 'results/metrics.json', 'type': 'json', 'validation_status': 'PASSED'}]})
    atomic_json(workspace / 'metadata/workspace.json', {'status': 'COMPLETED'})
    atomic_json(workspace / 'metadata/validation.json', {'passed': True})
    atomic_json(workspace / 'results/metrics.json', {'features': FEATURE_NAMES, 'model_artifact': MODEL_LOCATION, 'provenance': {'actual_model_training': True}, 'selection': {'model': 'Logistic regression'}, 'threshold': {'value': .4}})
    return settings, workspace


def values(workspace):
    return get_prediction_spec(workspace)['presets']['Typical activity'].copy()


def mock_run(monkeypatch, *, score=.7, fail=False, mutate=None, cleanup=False):
    calls = []
    def run(self, job, contract, mode, label):
        calls.append(job)
        assert mode == 'execute' and contract['entrypoint'] == 'generated/solution.py'
        assert (job / 'input/model.joblib').read_bytes().startswith(b'not a pickle')
        assert not (job / 'database/workspace.duckdb').exists()
        request = json.loads((job / 'input/request.json').read_text())
        result = {key: request[key] for key in ['request_id', 'model_sha256', 'input_sha256', 'threshold']}
        result.update(score=score, review_recommended=score >= request['threshold'], model=request['model_name'])
        if mutate:
            mutate(result)
        (job / 'results/prediction.json').write_text(json.dumps(result))
        atomic_json(job / 'metadata/sandbox.json', {'cleanup_failed': cleanup, 'container_name': 'unconfirmed' if cleanup else None})
        return ExecutionResult(success=not fail, exit_code=1 if fail else 0, execution_time_seconds=.25, stdout_path='logs/prediction.stdout.log', stderr_path='logs/prediction.stderr.log', error='Docker unavailable' if fail else None)
    monkeypatch.setattr('workbench.prediction.SandboxManager._run', run)
    return calls


def test_spec_never_deserializes_and_requires_validated_outputs(prediction_workspace, monkeypatch):
    _, workspace = prediction_workspace
    monkeypatch.setitem(sys.modules, 'joblib', SimpleNamespace(load=lambda *args, **kwargs: pytest.fail('Host must never deserialize models')))
    spec = get_prediction_spec(workspace)
    assert spec['feature_names'] == FEATURE_NAMES and spec['threshold'] == .4
    atomic_json(workspace / 'metadata/validation.json', {'passed': False})
    assert get_prediction_spec(workspace) is None


@pytest.mark.parametrize('change', [lambda v: v.update(confirmed_escalation=1), lambda v: v.pop('amount_log'), lambda v: v.update(amount_log=float('nan')), lambda v: v.update(amount_log=float('inf')), lambda v: v.update(amount_log=True), lambda v: v.update(cross_border_ratio_30d=1.1), lambda v: v.update(transfers_24h=2.5)])
def test_bad_features_are_rejected_before_docker(prediction_workspace, monkeypatch, change):
    settings, workspace = prediction_workspace
    payload = values(workspace)
    change(payload)
    calls = mock_run(monkeypatch)
    with pytest.raises(ValueError):
        predict_alert(settings, workspace, payload)
    assert calls == []


def test_current_request_runs_in_fresh_isolated_job_and_records_real_result(prediction_workspace, monkeypatch):
    settings, workspace = prediction_workspace
    calls = mock_run(monkeypatch)
    payload = values(workspace)
    one = predict_alert(settings, workspace, payload)
    two = predict_alert(settings, workspace, payload)
    assert one['request_id'] != two['request_id'] and len(calls) == 2
    assert one['score'] == .7 and one['review_recommended'] is True
    assert one['ground_truth'] == 'not_yet_observed' and one['inputs'] == payload
    assert calls[0].is_relative_to(workspace / 'metadata/predictions')
    assert json.loads((calls[0] / 'metadata/result.json').read_text()) == one


def test_changed_pinned_model_is_rejected(prediction_workspace, monkeypatch):
    settings, workspace = prediction_workspace
    payload = values(workspace)
    atomic_json(workspace / 'metadata/prediction_model.json', {'model_location': MODEL_LOCATION, 'model_sha256': '0' * 64})
    calls = mock_run(monkeypatch)
    with pytest.raises(ValueError, match='fingerprint'):
        predict_alert(settings, workspace, payload)
    assert not calls


def test_old_model_without_verified_hash_is_not_enabled(prediction_workspace):
    _, workspace = prediction_workspace
    path = workspace / 'metadata/manifest.json'
    manifest = json.loads(path.read_text())
    manifest['artifacts'][0].pop('sha256')
    atomic_json(path, manifest)
    assert get_prediction_spec(workspace) is None


def test_same_size_changed_model_rejected_by_validation_hash(prediction_workspace):
    _, workspace = prediction_workspace
    path = workspace / MODEL_LOCATION
    path.write_bytes(b'x' * path.stat().st_size)
    with pytest.raises(ValueError, match='fingerprint'):
        get_prediction_spec(workspace)


@pytest.mark.parametrize('mutate', [lambda r: r.update(score=float('nan')), lambda r: r.update(score=1.1), lambda r: r.update(request_id='stale'), lambda r: r.update(input_sha256='old'), lambda r: r.update(review_recommended=False), lambda r: r.update(threshold=.9)])
def test_invalid_or_stale_sandbox_result_is_rejected(prediction_workspace, monkeypatch, mutate):
    settings, workspace = prediction_workspace
    mock_run(monkeypatch, mutate=mutate)
    with pytest.raises(RuntimeError):
        predict_alert(settings, workspace, values(workspace))


def test_failed_docker_never_returns_a_cached_score(prediction_workspace, monkeypatch):
    settings, workspace = prediction_workspace
    mock_run(monkeypatch, fail=True)
    with pytest.raises(RuntimeError, match='Docker unavailable'):
        predict_alert(settings, workspace, values(workspace))


def test_unconfirmed_cleanup_blocks_next_request(prediction_workspace, monkeypatch):
    settings, workspace = prediction_workspace
    calls = mock_run(monkeypatch, cleanup=True)
    with pytest.raises(RuntimeError, match='cleanup'):
        predict_alert(settings, workspace, values(workspace))
    with pytest.raises(RuntimeError, match='cleanup'):
        predict_alert(settings, workspace, values(workspace))
    assert len(calls) == 1


def test_foreign_workspace_is_rejected(prediction_workspace, tmp_path, monkeypatch):
    settings, workspace = prediction_workspace
    calls = mock_run(monkeypatch)
    other = tmp_path / 'foreign' / workspace.name
    other.mkdir(parents=True)
    with pytest.raises(ValueError, match='different root'):
        predict_alert(settings, other, values(workspace))
    assert not calls
