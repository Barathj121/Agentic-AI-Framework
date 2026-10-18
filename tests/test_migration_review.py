import csv
import json
from pathlib import Path

import duckdb
import pytest

from workbench.migration import (Mapping, MappingPlan, MetadataField, MigrationService, SqlTransformationPlan,
                                 TransformationExpression, TransformationPlan, build_transformation)
from workbench.settings import Settings
from workbench.schemas import ExecutionResult
from workbench.workspace import atomic_json, read_json


TARGET = b'column_name,data_type,required,description\ncode,VARCHAR,true,Employee code\ntax_id,VARCHAR,true,Tax identifier absent from source\n'


class ReviewModel:
    def __init__(self, empty=False):
        self.empty = empty
        self.calls = []
        self.systems = []

    def structured(self, schema, system, prompt):
        self.calls.append((schema, json.loads(prompt)))
        self.systems.append(system)
        if schema is MappingPlan:
            return MappingPlan(mappings=[] if self.empty else [Mapping(
                target_column='code', source_columns=['raw_code'], mapping_type='DIRECT',
                transformation_description='Copy the employee code', reason='Source employee code')])
        assert schema is SqlTransformationPlan
        return SqlTransformationPlan(expressions=[
            TransformationExpression(target_column='code', sql_expression='UPPER(TRIM(raw_code))'),
            # The model cannot override a missing field managed by the framework.
            TransformationExpression(target_column='tax_id', sql_expression="'invented'"),
        ])


def workspace_for(tmp_path, model):
    service = MigrationService(Settings(workspace_root=tmp_path), llm=model)
    workspace = service.create('Review instructions', b'raw_code\n  a001  \n  b002  \n', TARGET)
    return service, workspace


@pytest.mark.parametrize('empty', [False, True])
def test_omitted_proposal_targets_become_explicit_missing_rows_for_review(tmp_path, empty):
    service, workspace = workspace_for(tmp_path, ReviewModel(empty))
    proposal = service.propose(workspace.name)
    assert [mapping.target_column for mapping in proposal.mappings] == ['code', 'tax_id']
    assert all(mapping.status == 'PENDING' for mapping in proposal.mappings)
    assert proposal.mappings[1].mapping_type == 'MISSING'
    assert 'No mapping was proposed' in proposal.mappings[1].reason
    if empty:
        assert all(mapping.mapping_type == 'MISSING' for mapping in proposal.mappings)
        approval = service.approve(workspace.name, proposal)
        assert all(mapping['status'] == 'MISSING' for mapping in approval['plan']['mappings'])
    else:
        with pytest.raises(ValueError, match='choose APPROVED'):
            service.approve(workspace.name, proposal)


def test_review_instruction_promoted_to_transform_and_missing_model_output_ignored(tmp_path):
    model = ReviewModel()
    service, workspace = workspace_for(tmp_path, model)
    proposal = service.propose(workspace.name)
    rows = [mapping.model_dump() for mapping in proposal.mappings]
    rows[0].update(status='APPROVED', transformation_description='Trim whitespace and uppercase the employee code.')
    rows[1].update(status='APPROVED', source_columns=['outdated_selection'])
    approval = service.approve(workspace.name, rows)
    approved = MappingPlan.model_validate(approval['plan'])
    assert approved.mappings[0].mapping_type == 'TRANSFORMATION'
    assert approved.mappings[1].mapping_type == approved.mappings[1].status == 'MISSING'
    assert approved.mappings[1].source_columns == []
    source, target = service.metadata(workspace.name)
    generated = service._generate(workspace, approved, source, target)
    assert [entry.target_column for entry in generated.expressions] == ['code']
    payload = model.calls[-1][1]
    assert payload['approved_transformations'][0]['transformation_description'] == rows[0]['transformation_description']


def test_unchanged_direct_description_does_not_create_unrequested_transformation(tmp_path):
    model = ReviewModel()
    service, workspace = workspace_for(tmp_path, model)
    proposal = service.propose(workspace.name)
    proposal.mappings[0].status = 'APPROVED'
    proposal.mappings[1].status = 'MISSING'
    approval = service.approve(workspace.name, proposal)
    approved = MappingPlan.model_validate(approval['plan'])
    source, target = service.metadata(workspace.name)
    generated = service._generate(workspace, approved, source, target)
    assert generated.expressions == [] and len(model.calls) == 1


def test_missing_review_does_not_approve_omitted_target_decisions(tmp_path):
    service, workspace = workspace_for(tmp_path, ReviewModel())
    proposal = service.propose(workspace.name)
    proposal.mappings[0].status = 'APPROVED'
    with pytest.raises(ValueError, match='exactly one'):
        service.approve(workspace.name, [proposal.mappings[0].model_dump()])


def test_proposal_prompt_treats_business_prohibitions_as_constraints(tmp_path):
    model = ReviewModel()
    service, workspace = workspace_for(tmp_path, model)
    service.propose(workspace.name)
    assert 'A prohibition such as "do not infer X from Y" is a binding constraint' in model.systems[0]
    assert 'propose MISSING or AMBIGUOUS for human review' in model.systems[0]
    assert 'Never propose a TRANSFORMATION that contradicts its own reason' in model.systems[0]


@pytest.mark.parametrize('expression,unexpected', [('ACTIVE', 'ACTIVE'), ('source.worker_class', 'source.worker_class')])
def test_unapproved_reference_error_names_target_actual_reference_and_allowed_sources(expression, unexpected):
    approved = MappingPlan(mappings=[Mapping(target_column='employment_status', source_columns=['worker_class'],
        mapping_type='TRANSFORMATION', status='APPROVED', transformation_description='Do not infer employment status from engagement category',
        reason='Category does not establish employment status')])
    target = [MetadataField(column_name='employment_status', data_type='VARCHAR', required=False, description='Employment status')]
    generated = TransformationPlan(language='sql', expressions=[TransformationExpression(target_column='employment_status', sql_expression=expression)], python_code='')
    with pytest.raises(ValueError) as failure:
        build_transformation(generated, approved, target)
    message = str(failure.value)
    assert 'employment_status: expression references an unapproved source column' in message
    assert f'Unexpected references: {unexpected}' in message
    assert 'Approved source columns: ["worker_class"]' in message
    assert 'Do not replace an unknown name with an invented constant' in message


def test_reopen_archives_approval_and_outputs_without_changing_source_or_calling_model(tmp_path):
    model = ReviewModel()
    service, workspace = workspace_for(tmp_path, model)
    plan = service.propose(workspace.name)
    plan.mappings[0].status = 'APPROVED'
    plan.mappings[1].status = 'MISSING'
    approval = service.approve(workspace.name, plan)
    original_source = (workspace / 'input/source_data.csv').read_bytes()
    artifacts = {'results/migrated_data.csv': 'code,tax_id\nold,\n', 'results/connector.zip': 'old connector',
                 'notebooks/migration.ipynb': 'old notebook', 'generated/transformation.sql': 'SELECT 1',
                 'generated/migration_spec.json': '{}', 'connector/mapping.json': '{}'}
    for relative, content in artifacts.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    atomic_json(workspace / 'metadata/migration_result.json', {'complete': True})
    atomic_json(workspace / 'metadata/validation.json', {'passed': True})
    atomic_json(workspace / 'metadata/manifest.json', {'workspace_id': workspace.name, 'validation_passed': True,
                                                     'artifacts': [{'location': 'results/migrated_data.csv'}]})
    with duckdb.connect(str(workspace / 'database/workspace.duckdb')) as con:
        con.execute("CREATE TABLE results.migrated_data AS SELECT 'old' AS code")
        con.execute("CREATE TABLE audit.workbench_artifacts AS SELECT 'old' AS artifact_id")
    calls_before = len(model.calls)
    reopened = service.reopen_review(workspace.name)
    assert len(model.calls) == calls_before
    assert (workspace / 'input/source_data.csv').read_bytes() == original_source
    assert [m.status for m in reopened.mappings] == ['APPROVED', 'MISSING']
    assert all(m.requires_review for m in reopened.mappings)
    assert reopened.mappings[0].transformation_description == 'Copy the employee code'
    assert read_json(workspace / 'generated/mapping.json') == reopened.model_dump()
    manifest = read_json(workspace / 'metadata/manifest.json')
    assert manifest['artifacts'] == [] and manifest['validation_passed'] is False
    assert manifest['status'] == 'MAPPING_REVIEW'
    for relative in [*artifacts, 'metadata/mapping_approval.json', 'metadata/migration_result.json', 'metadata/validation.json']:
        assert not (workspace / relative).exists()
    review = read_json(workspace / 'metadata/mapping_review.json')
    archive = workspace / review['previous_revision_path']
    assert read_json(archive / 'metadata/mapping_approval.json') == approval
    assert read_json(archive / 'metadata/migration_result.json') == {'complete': True}
    for relative, content in artifacts.items():
        assert (archive / relative).read_text(encoding='utf-8') == content
    with duckdb.connect(str(archive / 'workspace.duckdb'), read_only=True) as con:
        assert con.execute('SELECT * FROM results.migrated_data').fetchall() == [('old',)]
    with duckdb.connect(str(workspace / 'database/workspace.duckdb'), read_only=True) as con:
        assert con.execute('SELECT count(*) FROM source.data').fetchone()[0] == 2
        assert con.execute('SELECT count(*) FROM audit.workbench_artifacts').fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='results' AND table_name='migrated_data'").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='mapping' AND table_name='approved'").fetchone()[0] == 0
    with pytest.raises(ValueError, match='Human mapping approval'):
        service.execute(workspace.name)
    second = service.approve(workspace.name, reopened)
    assert second['revision'] == 2 and second['plan']['mappings'][0]['mapping_type'] == 'DIRECT'
    assert not (workspace / 'metadata/migration_result.json').exists()
    assert read_json(archive / 'metadata/mapping_approval.json') == approval


def test_reopened_review_preserves_rejected_decision_and_promotes_only_new_instructions(tmp_path):
    service, workspace = workspace_for(tmp_path, ReviewModel())
    plan = service.propose(workspace.name)
    plan.mappings[0].status = 'REJECTED'
    plan.mappings[1].status = 'MISSING'
    service.approve(workspace.name, plan)
    reopened = service.reopen_review(workspace.name)
    assert [m.status for m in reopened.mappings] == ['REJECTED', 'MISSING']
    reopened.mappings[0].status = 'APPROVED'
    reopened.mappings[0].transformation_description = 'Trim whitespace and uppercase the code.'
    approved = service.approve(workspace.name, reopened)
    assert approved['plan']['mappings'][0]['mapping_type'] == 'TRANSFORMATION'
    assert approved['plan']['mappings'][1]['status'] == 'MISSING'
    service.reopen_review(workspace.name)
    assert len(list((workspace / 'metadata/mapping_revisions').iterdir())) == 2
    with pytest.raises(ValueError, match='no approved mapping'):
        service.reopen_review(workspace.name)


@pytest.mark.parametrize('locked_file', ['metadata/mapping_approval.json', 'results/migrated_data.csv'])
def test_interrupted_reopen_revokes_certification_and_can_resume(tmp_path, monkeypatch, locked_file):
    service, workspace = workspace_for(tmp_path, ReviewModel())
    plan = service.propose(workspace.name)
    plan.mappings[0].status = 'APPROVED'
    plan.mappings[1].status = 'MISSING'
    original_approval = service.approve(workspace.name, plan)
    atomic_json(workspace / 'metadata/migration_result.json', {'complete': True})
    atomic_json(workspace / 'metadata/manifest.json', {'validation_passed': True, 'artifacts': [{'location': 'results/migrated_data.csv'}]})
    (workspace / 'results/migrated_data.csv').write_text('old result', encoding='utf-8')
    original_unlink = Path.unlink

    def locked_unlink(path, *args, **kwargs):
        if path == workspace / locked_file:
            raise PermissionError('File is locked')
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', locked_unlink)
        with pytest.raises(PermissionError, match='File is locked'):
            service.reopen_review(workspace.name)
    review = read_json(workspace / 'metadata/mapping_review.json')
    assert review['invalidation_pending'] is True
    archive = workspace / review['previous_revision_path']
    assert read_json(archive / 'metadata/mapping_approval.json') == original_approval
    assert read_json(workspace / 'metadata/manifest.json')['artifacts'] == []
    assert read_json(workspace / 'metadata/manifest.json')['validation_passed'] is False
    with pytest.raises(ValueError, match='Resume mapping review'):
        service.execute(workspace.name)
    with pytest.raises(ValueError, match='Resume mapping review'):
        service.approve(workspace.name, plan)
    with pytest.raises(ValueError, match='no longer current'):
        service.preview(workspace.name)
    assert len(service.preview(workspace.name, target=False)['rows']) == 2
    resumed = service.reopen_review(workspace.name)
    assert read_json(workspace / 'metadata/mapping_review.json')['invalidation_pending'] is False
    assert len(list((workspace / 'metadata/mapping_revisions').iterdir())) == 1
    assert not (workspace / 'metadata/migration_result.json').exists()
    assert not (workspace / 'results/migrated_data.csv').exists()
    new_approval = service.approve(workspace.name, resumed)
    assert new_approval['revision'] == 2
    assert read_json(workspace / 'metadata/manifest.json')['validation_passed'] is False
    assert (archive / 'results/migrated_data.csv').read_text(encoding='utf-8') == 'old result'


def test_repair_prompt_retains_exact_allowlists_and_prohibitions(tmp_path):
    requests = []

    class RepairModel:
        def structured(self, schema, system, prompt):
            requests.append((system, json.loads(prompt)))
            return SqlTransformationPlan(expressions=[TransformationExpression(target_column='employment_status', sql_expression='NULL')])

    service = MigrationService(Settings(workspace_root=tmp_path), llm=RepairModel())
    approved = MappingPlan(mappings=[Mapping(target_column='employment_status', source_columns=['worker_class'],
        mapping_type='TRANSFORMATION', status='APPROVED', transformation_description='Do not infer employment status from engagement category',
        reason='Category does not establish employment status')])
    source = [MetadataField(column_name='worker_class', data_type='VARCHAR', required=False, description='Engagement category')]
    target = [MetadataField(column_name='employment_status', data_type='VARCHAR', required=False, description='Employment status')]
    generated = service._generate(tmp_path, approved, source, target, 'Unexpected references: ACTIVE')
    system, payload = requests[0]
    assert payload['allowed_source_columns_by_target'] == {'employment_status': ['worker_class']}
    assert payload['repair_error'] == 'Unexpected references: ACTIVE'
    assert 'A prohibition such as "do not infer" is binding' in system
    assert generated.expressions[0].sql_expression == 'NULL'


@pytest.mark.parametrize('exit_code,expected_attempts,error_type', [(None, 1, RuntimeError), (1, 3, ValueError)])
def test_prelaunch_sandbox_failure_does_not_regenerate_sql_but_code_failure_can_retry(tmp_path, exit_code, expected_attempts, error_type):
    model = ReviewModel()
    service, workspace = workspace_for(tmp_path, model)
    proposal = service.propose(workspace.name)
    proposal.mappings[0].status = 'APPROVED'
    proposal.mappings[0].transformation_description = 'Trim whitespace and uppercase the code.'
    proposal.mappings[1].status = 'MISSING'
    service.approve(workspace.name, proposal)
    attempts = []
    failure = ('Error response from daemon: failed to connect to the backend: timed out dialing Hyper-V socket'
               if exit_code is None else 'Generated code failed with an invalid operation')

    class FailedSandbox:
        def execute(self, workspace, build, attempt):
            attempts.append(attempt)
            stdout = f'logs/attempt-{attempt}.stdout.log'
            stderr = f'logs/attempt-{attempt}.stderr.log'
            (workspace / stdout).write_text('', encoding='utf-8')
            (workspace / stderr).write_text(failure, encoding='utf-8')
            return ExecutionResult(success=False, exit_code=exit_code, execution_time_seconds=0,
                                   stdout_path=stdout, stderr_path=stderr, error=failure)

    service.sandbox = FailedSandbox()
    with pytest.raises(error_type, match='Hyper-V socket' if exit_code is None else 'Generated code failed'):
        service.execute(workspace.name)
    assert attempts == list(range(1, expected_attempts + 1))
    assert sum(schema is SqlTransformationPlan for schema, _ in model.calls) == expected_attempts
    status = read_json(workspace / 'metadata/workspace.json')
    assert status['status'] == 'MIGRATION_FAILED' and failure in status['error']
    assert not (workspace / 'metadata/migration_result.json').exists()
    assert (workspace / 'metadata/mapping_approval.json').is_file()


@pytest.mark.docker
@pytest.mark.parametrize('empty', [False, True])
def test_real_sandbox_exports_approved_missing_columns_even_when_all_missing(tmp_path, empty):
    service, workspace = workspace_for(tmp_path, ReviewModel(empty))
    proposal = service.propose(workspace.name)
    for mapping in proposal.mappings:
        mapping.status = 'APPROVED'
        if mapping.mapping_type == 'DIRECT':
            mapping.transformation_description = 'Trim whitespace and uppercase the code.'
    service.approve(workspace.name, proposal)
    report = service.execute(workspace.name)
    assert report['target_record_count'] == 2 and not report['complete']
    assert 'tax_id' in report['unresolved_required_fields']
    with (workspace / 'results/migrated_data.csv').open(newline='', encoding='utf-8') as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        assert reader.fieldnames == ['code', 'tax_id']
    assert [row['tax_id'] for row in rows] == ['', '']
    assert [row['code'] for row in rows] == (['', ''] if empty else ['A001', 'B002'])
    assert (workspace / 'results/connector.zip').is_file()
    assert (workspace / 'notebooks/migration.ipynb').is_file()
