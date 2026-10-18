from pathlib import Path

import duckdb
import pandas as pd
from streamlit.testing.v1 import AppTest

from workbench.migration import Mapping, MappingPlan, MigrationService
from workbench.migration_runtime import materialize_and_validate
from workbench.settings import Settings
from workbench.workspace import read_json


def app_for(root):
    code = f"from workbench.settings import Settings\nfrom ui.migration import render_migration\nrender_migration(Settings(workspace_root={str(root)!r}))\n"
    return AppTest.from_string(code, default_timeout=15)


def button(app, label):
    return next(item for item in app.button if item.label == label)


def approved_missing_workspace(root):
    service = MigrationService(Settings(workspace_root=root))
    workspace = service.create('Partial migration', b'identifier\nX1\n', b'column_name,data_type,required,description\nnew_identifier,VARCHAR,true,Unavailable field\n')
    plan = MappingPlan(mappings=[Mapping(target_column='new_identifier', source_columns=[], mapping_type='MISSING', status='MISSING', reason='No semantic source provided')])
    service.manager.write(workspace, 'generated/mapping.json', plan.model_dump())
    service.approve(workspace.name, plan)
    return service, workspace, plan


def materialize_partial_report(service, workspace, plan):
    _, target = service.metadata(workspace.name)
    with duckdb.connect(str(workspace / 'database/workspace.duckdb')) as con:
        report = materialize_and_validate(con, pd.DataFrame({'new_identifier': [None]}),
            [field.model_dump() for field in target], [mapping.model_dump() for mapping in plan.mappings], 1)
    service.manager.write(workspace, 'results/validation.json', report)
    report = service._verify_report(workspace, target, plan)
    service.manager.write(workspace, 'metadata/migration_result.json', report)
    service.manager.status(workspace, 'MIGRATION_INCOMPLETE', error=None)
    (workspace / 'results/migrated_data.csv').write_text('new_identifier\n""\n', encoding='utf-8')


def test_migration_page_offers_uploads_and_synthetic_example(tmp_path):
    app = app_for(tmp_path / 'workspaces').run()
    assert not app.exception
    assert app.title[0].value == 'Migration Studio'
    assert button(app, 'Create migration workspace')
    assert button(app, 'Load synthetic payroll example')
    button(app, 'Create migration workspace').click().run()
    assert any('Upload source data' in item.value for item in app.error)


def test_synthetic_example_loads_once_and_shows_schema(tmp_path):
    root = tmp_path / 'workspaces'
    app = app_for(root).run()
    button(app, 'Load synthetic payroll example').click().run()
    assert not app.exception
    assert len(list(root.glob('uc_*'))) == 1
    assert app.session_state['migration_workspace_id'].startswith('uc_')
    assert button(app, 'Generate semantic mappings')
    assert len(app.dataframe) >= 3
    app.run()
    assert len(list(root.glob('uc_*'))) == 1


def test_review_requires_confirmation_and_persists_partial_approval(tmp_path):
    root = tmp_path / 'workspaces'
    service = MigrationService(Settings(workspace_root=root))
    workspace = service.create('UI review', b'identifier\nX1\n', b'column_name,data_type,required,description\nnew_identifier,VARCHAR,true,Unavailable field\n')
    proposal = MappingPlan(mappings=[Mapping(target_column='new_identifier', source_columns=[], mapping_type='MISSING', reason='No semantic source provided')])
    service.manager.write(workspace, 'generated/mapping.json', proposal.model_dump())
    app = app_for(root)
    app.session_state['migration_workspace_id'] = workspace.name
    app.run()
    assert not app.exception
    button(app, 'Approve reviewed mapping plan').click().run()
    assert not (workspace / 'metadata/mapping_approval.json').exists()
    assert any('confirm approval' in item.value for item in app.error)
    app.checkbox[0].check()
    button(app, 'Approve reviewed mapping plan').click().run()
    assert not app.exception
    approved = read_json(workspace / 'metadata/mapping_approval.json')
    assert approved, [item.value for item in app.error]
    assert approved['human_approved'] and approved['fit_gap']['unresolved_required_fields'] == ['new_identifier']
    assert button(app, 'Generate and execute approved migration')


def test_marking_mapping_type_missing_normalizes_status_and_ignores_empty_sources(tmp_path):
    root = tmp_path / 'workspaces'
    service = MigrationService(Settings(workspace_root=root))
    workspace = service.create('Change mapping', b'identifier\nX1\n', b'column_name,data_type,required,description\nnew_identifier,VARCHAR,true,Target identifier\n')
    plan = MappingPlan(mappings=[Mapping(target_column='new_identifier', source_columns=['identifier'], mapping_type='DIRECT', reason='Suggested identifier')])
    service.manager.write(workspace, 'generated/mapping.json', plan.model_dump())
    app = app_for(root)
    app.session_state['migration_workspace_id'] = workspace.name
    app.run()
    editor = next(item for item in app.dataframe if item.key and item.key.startswith('mapping_editor_'))
    app.session_state[editor.key] = {'edited_rows': {0: {'Mapping Type': 'MISSING', 'Source Field(s)': '',
        'Transformation': 'Uppercase the identifier'}}, 'added_rows': [], 'deleted_rows': []}
    app.checkbox[0].check()
    button(app, 'Approve reviewed mapping plan').click().run()
    assert not app.exception and not app.error
    approved = read_json(workspace / 'metadata/mapping_approval.json')
    mapping = approved['plan']['mappings'][0]
    assert mapping['mapping_type'] == mapping['status'] == 'MISSING'
    assert mapping['source_columns'] == []
    assert any(tab.label == 'Artifacts and export' for tab in app.tabs)


def test_review_instructions_survive_confirmation_and_promote_direct_mapping(tmp_path, monkeypatch):
    root = tmp_path / 'workspaces'
    service = MigrationService(Settings(workspace_root=root))
    workspace = service.create('Apply reviewer instructions', b'identifier\n x1 \n', b'column_name,data_type,required,description\nnew_identifier,VARCHAR,true,Target identifier\n')
    plan = MappingPlan(mappings=[Mapping(target_column='new_identifier', source_columns=['identifier'], mapping_type='DIRECT',
        transformation_description='Copy the identifier unchanged.', reason='Matching identifier')])
    service.manager.write(workspace, 'generated/mapping.json', plan.model_dump())
    submitted = []
    original_approve = MigrationService.approve

    def capture_approval(self, identifier, mappings):
        submitted.extend(dict(mapping) for mapping in mappings)
        return original_approve(self, identifier, mappings)

    monkeypatch.setattr(MigrationService, 'approve', capture_approval)
    app = app_for(root)
    app.session_state['migration_workspace_id'] = workspace.name
    app.run()
    editor = next(item for item in app.dataframe if item.key and item.key.startswith('mapping_editor_'))
    instruction = 'Trim surrounding whitespace and uppercase the identifier. Preserve null values.'
    app.session_state[editor.key] = {'edited_rows': {0: {'Transformation': instruction}}, 'added_rows': [], 'deleted_rows': []}
    button(app, 'Approve reviewed mapping plan').click().run()
    assert any('confirm approval' in item.value for item in app.error)
    assert not submitted
    assert app.session_state[editor.key]['edited_rows'][0]['Transformation'] == instruction
    app.checkbox[0].check()
    button(app, 'Approve reviewed mapping plan').click().run()
    assert not app.exception and not app.error
    assert submitted[0]['mapping_type'] == 'TRANSFORMATION'
    assert submitted[0]['transformation_description'] == instruction
    approved = read_json(workspace / 'metadata/mapping_approval.json')
    assert approved['plan']['mappings'][0]['mapping_type'] == 'TRANSFORMATION'
    assert approved['plan']['mappings'][0]['transformation_description'] == instruction
    assert approved['plan']['mappings'][0]['source_columns'] == ['identifier']
    assert not (workspace / 'metadata/migration_result.json').exists()
    assert button(app, 'Generate and execute approved migration')


def test_unchanged_direct_description_does_not_become_transformation(tmp_path):
    root = tmp_path / 'workspaces'
    service = MigrationService(Settings(workspace_root=root))
    workspace = service.create('Keep direct mapping', b'identifier\nX1\n', b'column_name,data_type,required,description\nnew_identifier,VARCHAR,true,Target identifier\n')
    plan = MappingPlan(mappings=[Mapping(target_column='new_identifier', source_columns=['identifier'], mapping_type='DIRECT',
        transformation_description='Copy the identifier unchanged.', reason='Matching identifier')])
    service.manager.write(workspace, 'generated/mapping.json', plan.model_dump())
    app = app_for(root)
    app.session_state['migration_workspace_id'] = workspace.name
    app.run()
    app.checkbox[0].check()
    button(app, 'Approve reviewed mapping plan').click().run()
    assert not app.exception and not app.error
    approved = read_json(workspace / 'metadata/mapping_approval.json')
    assert approved['plan']['mappings'][0]['mapping_type'] == 'DIRECT'


def test_fresh_proposal_resets_previous_draft_instructions(tmp_path):
    root = tmp_path / 'workspaces'
    service = MigrationService(Settings(workspace_root=root))
    workspace = service.create('Replace proposal', b'identifier\nX1\n', b'column_name,data_type,required,description\nnew_identifier,VARCHAR,true,Target identifier\n')
    plan = MappingPlan(mappings=[Mapping(target_column='new_identifier', source_columns=['identifier'], mapping_type='DIRECT', reason='Matching identifier')])
    service.manager.write(workspace, 'generated/mapping.json', plan.model_dump())
    app = app_for(root)
    app.session_state['migration_workspace_id'] = workspace.name
    app.run()
    previous_editor = next(item for item in app.dataframe if item.key and item.key.startswith('mapping_editor_'))
    app.session_state[previous_editor.key] = {'edited_rows': {0: {'Transformation': 'Discarded draft instruction'}}, 'added_rows': [], 'deleted_rows': []}
    button(app, 'Approve reviewed mapping plan').click().run()
    plan.mappings[0].transformation_description = 'Copy the identifier unchanged.'
    service.manager.write(workspace, 'generated/mapping.json', plan.model_dump())
    app.run()
    current_editor = next(item for item in app.dataframe if item.key and item.key.startswith('mapping_editor_'))
    assert current_editor.key != previous_editor.key
    app.checkbox[0].check()
    button(app, 'Approve reviewed mapping plan').click().run()
    assert not app.exception and not app.error
    approved = read_json(workspace / 'metadata/mapping_approval.json')
    assert approved['plan']['mappings'][0]['transformation_description'] == 'Copy the identifier unchanged.'
    assert approved['plan']['mappings'][0]['mapping_type'] == 'DIRECT'


def test_failed_execution_keeps_results_and_diagnostics_accessible_without_stale_exports(tmp_path, monkeypatch):
    root = tmp_path / 'workspaces'
    service, workspace, _ = approved_missing_workspace(root)
    for relative in ('results/migrated_data.csv', 'results/validation.json', 'results/connector.zip', 'notebooks/migration.ipynb'):
        (workspace / relative).write_bytes(b'stale output from an earlier attempt')

    def failed_execution(self, identifier):
        current = self.workspace(identifier)
        (current / 'generated/transformation.sql').write_text('SELECT invalid_expression FROM source.data', encoding='utf-8')
        (current / 'logs/execution_1.stderr.log').write_text('Cannot parse 14-08-2024 as YYYY-MM-DD', encoding='utf-8')
        self.manager.status(current, 'MIGRATION_FAILED', error='Cannot parse 14-08-2024 as YYYY-MM-DD')
        raise ValueError('Cannot parse 14-08-2024 as YYYY-MM-DD')

    monkeypatch.setattr(MigrationService, 'execute', failed_execution)
    app = app_for(root)
    app.session_state['migration_workspace_id'] = workspace.name
    app.run()
    button(app, 'Generate and execute approved migration').click().run()
    assert not app.exception
    assert any('Migration could not finish' in item.value for item in app.error)
    details = next(item for item in app.expander if item.label == 'Technical details')
    assert any('Cannot parse 14-08-2024' in item.value for item in details.text)
    assert [tab.label for tab in app.tabs] == ['Target data', 'Mapping report', 'Validation report', 'Artifacts and export']
    downloads = {item.proto.label for item in app.get('download_button')}
    assert {'Download approved mapping JSON', 'Download transformation SQL', 'Download execution_1.stderr.log', 'Download agent.jsonl'} <= downloads
    assert not {'Download target CSV', 'Download validation report', 'Download connector ZIP', 'Download migration notebook'} & downloads
    assert button(app, 'Retry approved migration')
    assert button(app, 'Edit approved mappings')
    app.run()
    assert any('Migration could not finish' in item.value for item in app.error)
    assert any(tab.label == 'Artifacts and export' for tab in app.tabs)


def test_execution_service_connection_errors_offer_runtime_recovery(tmp_path):
    root = tmp_path / 'workspaces'
    service, workspace, _ = approved_missing_workspace(root)
    app = app_for(root)
    app.session_state['migration_workspace_id'] = workspace.name
    for error in (
        'RuntimeError: failed to connect to backend: timed out dialing Hyper-V socket',
        'RuntimeError: failed to connect to backend timed out dialing Hyper-Vsocket',
        'Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?',
        'error during connect: open //./pipe/docker_engine: The system cannot find the file specified.',
    ):
        service.manager.status(workspace, 'MIGRATION_FAILED', error=error)
        app.run()
        assert not app.exception
        assert any('local execution service is unavailable' in item.value for item in app.error)
        assert any('Rancher Desktop / Docker' in item.value and 'Retry approved migration' in item.value for item in app.info)
        assert not any('edit' in item.value.lower() or 'correct source fields' in item.value.lower() for item in app.info)
        assert button(app, 'Retry approved migration')
        details = next(item for item in app.expander if item.label == 'Technical details')
        assert details.text[0].value == error
        assert any(item.proto.label == 'Download approved mapping JSON' for item in app.get('download_button'))


def test_failed_generated_mapping_can_be_revised_and_rerun_with_existing_source(tmp_path, monkeypatch):
    root = tmp_path / 'workspaces'
    service = MigrationService(Settings(workspace_root=root))
    source = b'identifier,worker_class\nX1,CONTRACTOR\n'
    workspace = service.create('Recover approved mapping', source,
        b'column_name,data_type,required,description\nemployment_status,VARCHAR,true,Current employment status\nemployee_code,VARCHAR,true,Employee identifier\nlegacy_note,VARCHAR,false,Optional legacy note\n')
    instruction = 'Do not infer current employment status from engagement category.'
    plan = MappingPlan(mappings=[
        Mapping(target_column='employment_status', source_columns=['worker_class'], mapping_type='TRANSFORMATION',
            status='APPROVED', transformation_description=instruction, reason='Reviewer must confirm status availability'),
        Mapping(target_column='employee_code', source_columns=['identifier'], mapping_type='DIRECT', status='APPROVED', reason='Employee identifier'),
        Mapping(target_column='legacy_note', source_columns=['identifier'], mapping_type='DIRECT', status='REJECTED', reason='Reviewer rejected this relationship'),
    ])
    service.manager.write(workspace, 'generated/mapping.json', plan.model_dump())
    service.approve(workspace.name, plan)
    raw_error = 'ValueError: employment_status: expression references an unapproved source column: ACTIVE. Approved source columns: worker_class.'
    service.manager.status(workspace, 'MIGRATION_FAILED', error=raw_error)
    executions = []

    def execute_reviewed(self, identifier):
        current = self.workspace(identifier)
        approval = self.manager.read(current, 'metadata/mapping_approval.json')
        approved = MappingPlan.model_validate(approval['plan'])
        executions.append(approved)
        _, target = self.metadata(identifier)
        values = pd.DataFrame({'employment_status': [None], 'employee_code': ['X1'], 'legacy_note': [None]})
        with duckdb.connect(str(current / 'database/workspace.duckdb')) as con:
            report = materialize_and_validate(con, values, [field.model_dump() for field in target],
                [mapping.model_dump() for mapping in approved.mappings], 1)
        self.manager.write(current, 'results/validation.json', report)
        verified = self._verify_report(current, target, approved)
        self.manager.write(current, 'metadata/migration_result.json', verified)
        self.manager.status(current, 'MIGRATION_INCOMPLETE', error=None)
        (current / 'results/migrated_data.csv').write_text(values.to_csv(index=False), encoding='utf-8')
        return verified

    monkeypatch.setattr(MigrationService, 'execute', execute_reviewed)
    app = app_for(root)
    app.session_state['migration_workspace_id'] = workspace.name
    app.run()
    assert not app.exception
    assert any('transformation for employment_status' in item.value and 'source data has not been changed' in item.value for item in app.error)
    assert not any('ValueError' in item.value or 'ACTIVE' in item.value for item in app.error)
    details = next(item for item in app.expander if item.label == 'Technical details')
    assert details.text[0].value == raw_error
    assert button(app, 'Retry approved migration')
    assert any(tab.label == 'Artifacts and export' for tab in app.tabs)
    button(app, 'Edit approved mappings').click().run()
    assert not app.exception and not app.error
    assert not executions
    assert app.session_state['migration_workspace_id'] == workspace.name
    assert len(list(root.glob('uc_*'))) == 1
    assert (workspace / 'input/source_data.csv').read_bytes() == source
    assert not (workspace / 'metadata/mapping_approval.json').exists()
    assert app.checkbox[0].value is False
    history = next(item for item in app.expander if item.label == 'Previous mapping approvals')
    assert history.dataframe[0].value.loc[0, 'transformation_description'] == instruction
    assert history.dataframe[0].value.loc[0, 'mapping_type'] == 'TRANSFORMATION'
    assert any('Historical approvals' in item.value for item in history.caption)
    assert any(item.proto.label == 'Download previous approval JSON (revision 1)' for item in app.get('download_button'))
    editor = next(item for item in app.dataframe if item.key and item.key.startswith('mapping_editor_'))
    assert editor.value.loc[0, 'Transformation'] == instruction
    assert editor.value.loc[0, 'Source Field(s)'] == '["worker_class"]'
    assert editor.value.loc[2, 'Status'] == 'REJECTED'
    app.session_state[editor.key] = {'edited_rows': {0: {'Mapping Type': 'MISSING'}}, 'added_rows': [], 'deleted_rows': []}
    app.checkbox[0].check()
    button(app, 'Approve reviewed mapping plan').click().run()
    assert not app.exception and not app.error
    updated = read_json(workspace / 'metadata/mapping_approval.json')['plan']['mappings']
    assert updated[0]['mapping_type'] == updated[0]['status'] == 'MISSING'
    assert updated[0]['source_columns'] == []
    assert updated[2]['status'] == 'REJECTED'
    history = next(item for item in app.expander if item.label == 'Previous mapping approvals')
    assert history.dataframe[0].value.loc[0, 'mapping_type'] == 'TRANSFORMATION'
    assert not executions
    button(app, 'Generate and execute approved migration').click().run()
    assert not app.exception and not app.error
    assert len(executions) == 1
    assert (workspace / 'input/source_data.csv').read_bytes() == source
    assert any('Incomplete migration' in item.value for item in app.warning)
    assert any(item.proto.label == 'Download target CSV' for item in app.get('download_button'))
    with duckdb.connect(str(workspace / 'database/workspace.duckdb'), read_only=True) as con:
        assert con.execute('SELECT * FROM source.data').fetchall() == [('X1', 'CONTRACTOR')]
        assert con.execute('SELECT * FROM results.migrated_data').fetchall() == [(None, 'X1', None)]


def test_missing_required_field_partial_result_remains_previewable_and_exportable(tmp_path):
    root = tmp_path / 'workspaces'
    service, workspace, plan = approved_missing_workspace(root)
    materialize_partial_report(service, workspace, plan)
    app = app_for(root)
    app.session_state['migration_workspace_id'] = workspace.name
    app.run()
    assert not app.exception and not app.error
    assert any('Incomplete migration' in item.value for item in app.warning)
    assert any(list(item.value.columns) == ['new_identifier'] for item in app.dataframe)
    assert any(item.label == 'Invalid records' and item.value == '1' for item in app.metric)
    downloads = {item.proto.label for item in app.get('download_button')}
    assert {'Download target CSV', 'Download validation report', 'Download approved mapping JSON'} <= downloads


def test_preview_error_does_not_hide_mapping_validation_or_export(tmp_path, monkeypatch):
    root = tmp_path / 'workspaces'
    service, workspace, plan = approved_missing_workspace(root)
    materialize_partial_report(service, workspace, plan)
    original_preview = MigrationService.preview

    def unavailable_preview(self, identifier, target=True):
        if target:
            raise RuntimeError('Preview is temporarily unavailable')
        return original_preview(self, identifier, target=target)

    monkeypatch.setattr(MigrationService, 'preview', unavailable_preview)
    app = app_for(root)
    app.session_state['migration_workspace_id'] = workspace.name
    app.run()
    assert not app.exception
    assert any('Target preview could not be loaded' in item.value for item in app.error)
    assert any(list(item.value.columns)[:2] == ['target_column', 'source_columns'] for item in app.dataframe)
    assert any(item.proto.label == 'Download target CSV' for item in app.get('download_button'))


def test_interrupted_review_hides_old_results_and_resumes_before_approval(tmp_path, monkeypatch):
    root = tmp_path / 'workspaces'
    service, workspace, plan = approved_missing_workspace(root)
    materialize_partial_report(service, workspace, plan)
    service.manager.write(workspace, 'metadata/mapping_review.json', {'invalidation_pending': True})
    resumed = []

    def resume_review(self, identifier):
        resumed.append(identifier)
        current = self.workspace(identifier)
        (current / 'metadata/mapping_approval.json').unlink()
        (current / 'metadata/migration_result.json').unlink()
        self.manager.write(current, 'metadata/mapping_review.json', {'invalidation_pending': False})
        self.manager.status(current, 'MAPPING_REVIEW', error=None)
        return plan

    monkeypatch.setattr(MigrationService, 'reopen_review', resume_review)
    app = app_for(root)
    app.session_state['migration_workspace_id'] = workspace.name
    app.run()
    assert not app.exception and not app.error
    assert any('Mapping review was interrupted' in item.value for item in app.warning)
    assert not any(item.label in {'Approve reviewed mapping plan', 'Generate and execute approved migration'} for item in app.button)
    assert not any(item.proto.label == 'Download target CSV' for item in app.get('download_button'))
    assert not any(item.value == 'Migration results' for item in app.subheader)
    assert not resumed
    button(app, 'Resume mapping review').click().run()
    assert not app.exception and not app.error
    assert resumed == [workspace.name]
    assert button(app, 'Approve reviewed mapping plan')
    assert app.checkbox[0].value is False
    assert (workspace / 'input/source_data.csv').read_bytes() == b'identifier\nX1\n'


def test_new_result_clears_stale_browser_execution_errors(tmp_path):
    root = tmp_path / 'workspaces'
    service, workspace, plan = approved_missing_workspace(root)
    materialize_partial_report(service, workspace, plan)
    app = app_for(root)
    app.session_state['migration_workspace_id'] = workspace.name
    error_key = 'migration_execution_error_' + workspace.name
    for stale_error in ('ValueError: old execution failed', {'error': 'ValueError: old execution failed', 'workspace_updated_at': 'old version'}):
        app.session_state[error_key] = stale_error
        app.run()
        assert not app.exception and not app.error
        assert error_key not in app.session_state
        assert not any(item.label == 'Retry approved migration' for item in app.button)
        assert any(item.proto.label == 'Download target CSV' for item in app.get('download_button'))
