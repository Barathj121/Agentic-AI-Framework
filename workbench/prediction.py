"""On-demand inference with a registered model, deserialized only inside Docker."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import uuid
from pathlib import Path

from filelock import FileLock

from .sandbox import SandboxManager
from .schemas import ExecutionResult
from .workspace import WorkspaceManager, atomic_json, now, safe_path

MODEL_LOCATION = 'models/alert_prioritizer.joblib'
MAX_MODEL_BYTES = 64 * 1024 * 1024
FIELD_DEFINITIONS = [
    ('amount_log', 'Log of alert amount', 7.0, 0., 20., .1, 'number', 'Natural log of one plus amount, in comparable synthetic units.'),
    ('amount_to_median', 'Amount / historical median', 1., 0., 100., .1, 'number', 'Amount relative to the customer baseline known at alert time.'),
    ('transfers_24h', 'Transfers in previous 24 hours', 4, 0, 1000, 1, 'integer', 'Count preceding this alert.'),
    ('unique_recipients_7d', 'Recipients in previous 7 days', 5, 0, 1000, 1, 'integer', 'Distinct recipients preceding this alert.'),
    ('cross_border_ratio_30d', 'Cross-border share', .1, 0., 1., .01, 'number', 'Share in the previous 30 days; 0 to 1.'),
    ('new_recipient_ratio_7d', 'New-recipient share', .2, 0., 1., .01, 'number', 'Share in the previous 7 days; 0 to 1.'),
    ('night_transfer_ratio_7d', 'Night-time transfer share', .12, 0., 1., .01, 'number', 'Share in the previous 7 days; 0 to 1.'),
    ('account_age_days', 'Account age in days', 700, 0, 36500, 1, 'integer', 'Age at the time of the alert.'),
    ('prior_alerts_180d', 'Earlier alerts in 180 days', 0, 0, 1000, 1, 'integer', 'Only alerts known before the current one.'),
    ('inflow_outflow_ratio_7d', 'Incoming / outgoing amount', 1., 0., 100., .1, 'number', 'Ratio from the previous 7 days.'),
]
FEATURE_NAMES = [item[0] for item in FIELD_DEFINITIONS]

# This constant is written as data on the host and runs only in the sandbox.
PREDICTOR_SOURCE = '''from pathlib import Path
import hashlib, json, math
import joblib
import numpy as np
import pandas as pd
assert Path('/.dockerenv').exists()
request = json.loads(Path('input/request.json').read_text())
model_path = Path('input/model.joblib')
assert hashlib.sha256(model_path.read_bytes()).hexdigest() == request['model_sha256']
canonical = json.dumps(request['values'], sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
assert hashlib.sha256(canonical).hexdigest() == request['input_sha256']
bundle = joblib.load(model_path)
assert isinstance(bundle, dict) and bundle['features'] == request['feature_names']
assert bundle['selected_model'] == request['model_name']
threshold = float(bundle['threshold'])
assert math.isfinite(threshold) and 0 <= threshold <= 1 and threshold == request['threshold']
model = bundle['model']
assert np.asarray(model.classes_).tolist() == [0, 1]
frame = pd.DataFrame([{name: request['values'][name] for name in request['feature_names']}], columns=request['feature_names'])
probabilities = np.asarray(model.predict_proba(frame), dtype=float)
assert probabilities.shape == (1, 2) and np.isfinite(probabilities).all()
assert ((probabilities >= 0) & (probabilities <= 1)).all() and abs(float(probabilities.sum())-1) < 1e-7
score = float(probabilities[0, 1])
result = {'request_id': request['request_id'], 'input_sha256': request['input_sha256'], 'model_sha256': request['model_sha256'], 'model': request['model_name'], 'score': score, 'threshold': threshold, 'review_recommended': score >= threshold}
Path('results/prediction.json').write_text(json.dumps(result, allow_nan=False))
print(json.dumps(result, allow_nan=False))
'''


def _json(workspace: Path, location: str, limit: int = 1024 * 1024):
    path = safe_path(workspace, location)
    if not path.is_file():
        return None
    if path.stat().st_size > limit:
        raise ValueError('Prediction metadata exceeds the supported size')
    return json.loads(path.read_text(encoding='utf-8'))


def _digest(path: Path) -> str:
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_MODEL_BYTES:
        raise ValueError('Registered prediction model is missing, empty or too large')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def get_prediction_spec(workspace: Path) -> dict | None:
    """Read validated JSON and hash model bytes; never import or unpickle a model."""
    workspace = Path(workspace)
    if workspace.is_symlink() or (hasattr(workspace, 'is_junction') and workspace.is_junction()):
        raise ValueError('Workspace links are not allowed')
    manifest = _json(workspace, 'metadata/manifest.json') or {}
    metadata = _json(workspace, 'metadata/workspace.json') or {}
    validation = _json(workspace, 'metadata/validation.json') or {}
    if metadata.get('status') != 'COMPLETED' or manifest.get('status') != 'COMPLETED' or validation.get('passed') is not True:
        return None
    artifacts = manifest.get('artifacts', [])
    registered = {item.get('location'): item for item in artifacts if isinstance(item, dict) and item.get('validation_status') == 'PASSED'}
    if registered.get(MODEL_LOCATION, {}).get('type') != 'model' or registered.get('results/metrics.json', {}).get('type') != 'json':
        return None
    metrics = _json(workspace, 'results/metrics.json') or {}
    if metrics.get('features') != FEATURE_NAMES or metrics.get('model_artifact') != MODEL_LOCATION:
        return None
    if metrics.get('provenance', {}).get('actual_model_training') is not True:
        return None
    threshold = metrics.get('threshold', {}).get('value')
    model_name = metrics.get('selection', {}).get('model')
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError('Saved prediction threshold is invalid')
    if not isinstance(model_name, str) or not 1 <= len(model_name) <= 100:
        raise ValueError('Saved prediction model name is invalid')
    model = safe_path(workspace, MODEL_LOCATION)
    if model.stat().st_size != registered[MODEL_LOCATION].get('size_bytes'):
        raise ValueError('Registered prediction model size changed')
    digest = _digest(model)
    pin = _json(workspace, 'metadata/prediction_model.json')
    if pin and (pin.get('model_sha256') != digest or pin.get('model_location') != MODEL_LOCATION):
        raise ValueError('Registered model changed after its fingerprint was recorded')
    optional_registry_hash = registered[MODEL_LOCATION].get('sha256')
    if optional_registry_hash and optional_registry_hash != digest:
        raise ValueError('Registered model fingerprint does not match its file')
    if not pin and not optional_registry_hash:
        # Older models need a separately established, verified fingerprint.
        # Merely reading an arbitrary current file must not bless changed bytes.
        return None
    fields = [dict(zip(('name', 'label', 'default', 'min', 'max', 'step', 'kind', 'help'), item)) for item in FIELD_DEFINITIONS]
    defaults = {field['name']: field['default'] for field in fields}
    higher = dict(zip(FEATURE_NAMES, [9., 3.5, 20, 18, .35, .55, .3, 90, 3, 2.7]))
    return {'model_name': model_name, 'threshold': float(threshold), 'score_label': 'Predicted escalation score', 'fields': fields, 'feature_names': FEATURE_NAMES.copy(), 'presets': {'Typical activity': defaults, 'Unusual activity': higher}, 'model_sha256': digest, 'model_location': MODEL_LOCATION, 'synthetic_only': True, 'fingerprint_basis': pin.get('basis', 'Previously verified fingerprint') if pin else 'SHA256 recorded during artifact validation'}


def _validate_values(values: dict) -> dict:
    if not isinstance(values, dict) or set(values) != set(FEATURE_NAMES):
        raise ValueError('Provide exactly the ten supported prediction features; labels and extra fields are forbidden')
    normalized = {}
    for name, _, _, low, high, _, kind, _ in FIELD_DEFINITIONS:
        value = values[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{name} must be a finite number between {low} and {high}')
        if kind == 'integer' and not float(value).is_integer():
            raise ValueError(f'{name} must be an integer')
        normalized[name] = int(value) if kind == 'integer' else float(value)
    return normalized


def predict_alert(settings, workspace: Path, values: dict) -> dict:
    """Score a new request in a fresh isolated sandbox and persist its real evidence."""
    values = _validate_values(values)
    workspace = Path(workspace)
    if workspace.is_symlink() or (hasattr(workspace, 'is_junction') and workspace.is_junction()):
        raise ValueError('Workspace links are not allowed')
    manager = WorkspaceManager(settings.workspace_root)
    owned = manager.get(workspace.name)
    if workspace.resolve() != owned.resolve():
        raise ValueError('Prediction workspace belongs to a different root')
    with FileLock(str(safe_path(owned, 'metadata/workflow.lock')), timeout=2):
        spec = get_prediction_spec(owned)
        if spec is None:
            raise ValueError('This completed workspace has no supported validated prediction model')
        parent_sandbox = _json(owned, 'metadata/sandbox.json') or {}
        if parent_sandbox.get('cleanup_failed') or parent_sandbox.get('container_name'):
            raise RuntimeError('Workspace sandbox cleanup is unconfirmed')
        previous = _json(owned, 'metadata/prediction_active.json') or {}
        if previous.get('job_location'):
            old_job = safe_path(owned, previous['job_location'])
            old_state = _json(old_job, 'metadata/sandbox.json') or {}
            if old_state.get('cleanup_failed') or old_state.get('container_name'):
                raise RuntimeError('A previous prediction container has unconfirmed cleanup')
        pin = _json(owned, 'metadata/prediction_model.json')
        if not pin:
            atomic_json(safe_path(owned, 'metadata/prediction_model.json'), {'model_location': MODEL_LOCATION, 'model_sha256': spec['model_sha256'], 'recorded_at': now(), 'basis': 'Matched SHA256 recorded during artifact validation'})
        request_id = uuid.uuid4().hex
        job_location = 'metadata/predictions/' + request_id
        job = safe_path(owned, job_location)
        for folder in ('input', 'generated', 'results', 'models', 'notebooks', 'database', 'logs', 'metadata'):
            safe_path(owned, job_location + '/' + folder).mkdir(parents=True, exist_ok=False)
        source = safe_path(owned, MODEL_LOCATION)
        destination = safe_path(owned, job_location + '/input/model.joblib')
        if _digest(source) != spec['model_sha256']:
            raise ValueError('Model changed before the inference snapshot')
        shutil.copyfile(source, destination)
        if _digest(destination) != spec['model_sha256'] or _digest(source) != spec['model_sha256']:
            raise ValueError('Model changed while making the inference snapshot')
        input_hash = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        request = {'request_id': request_id, 'values': values, 'feature_names': FEATURE_NAMES, 'model_name': spec['model_name'], 'threshold': spec['threshold'], 'model_sha256': spec['model_sha256'], 'input_sha256': input_hash}
        atomic_json(safe_path(job, 'input/request.json'), request)
        safe_path(job, 'generated/solution.py').write_text(PREDICTOR_SOURCE, encoding='utf-8')
        atomic_json(safe_path(owned, 'metadata/prediction_active.json'), {'job_location': job_location, 'request_id': request_id, 'started_at': now()})
        try:
            execution = SandboxManager(settings)._run(job, {'entrypoint': 'generated/solution.py', 'required_dependencies': ['joblib', 'pandas', 'scikit-learn']}, 'execute', 'prediction')
            execution = ExecutionResult.model_validate(execution)
            atomic_json(safe_path(job, 'metadata/execution.json'), execution.model_dump(mode='json'))
            if not execution.success:
                raise RuntimeError('Prediction failed in Docker: ' + (execution.error or f'exit code {execution.exit_code}'))
            cleanup = _json(job, 'metadata/sandbox.json') or {}
            if cleanup.get('cleanup_failed') or cleanup.get('container_name'):
                raise RuntimeError('Prediction container cleanup is unconfirmed')
            result = _json(job, 'results/prediction.json', 32 * 1024)
            if not isinstance(result, dict):
                raise RuntimeError('Prediction sandbox produced no valid result')
            score = result.get('score')
            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                raise RuntimeError('Prediction sandbox returned an invalid score')
            for key in ('request_id', 'model_sha256', 'input_sha256'):
                if result.get(key) != request[key]:
                    raise RuntimeError('Prediction result does not match the current request')
            if result.get('model') != spec['model_name'] or type(result.get('threshold')) not in (int, float) or result['threshold'] != spec['threshold']:
                raise RuntimeError('Prediction result changed the saved model or threshold')
            if type(result.get('review_recommended')) is not bool or result['review_recommended'] != (score >= spec['threshold']):
                raise RuntimeError('Prediction decision does not match its score and threshold')
            if _digest(source) != spec['model_sha256'] or _digest(destination) != spec['model_sha256']:
                raise RuntimeError('Model fingerprint changed during inference')
            result.update(score=float(score), scored_at=now(), execution_seconds=execution.execution_time_seconds, inputs=values, ground_truth='not_yet_observed', decision='PRIORITIZE FOR ANALYST REVIEW' if result['review_recommended'] else 'BELOW THIS QUEUE THRESHOLD', synthetic_only=True)
            atomic_json(safe_path(job, 'metadata/result.json'), result)
            return result
        except Exception as exc:
            atomic_json(safe_path(job, 'metadata/failure.json'), {'request_id': request_id, 'failed_at': now(), 'error': str(exc)[:2000]})
            raise
