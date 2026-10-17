from pathlib import Path

from streamlit.testing.v1 import AppTest

from workbench.migration import Mapping, MappingPlan, MigrationService
from workbench.settings import Settings
from workbench.workspace import read_json


def app_for(root):
    code = f"from workbench.settings import Settings\nfrom ui.migration import render_migration\nrender_migration(Settings(workspace_root={str(root)!r}))\n"
    return AppTest.from_string(code, default_timeout=15)


def button(app, label):
    return next(item for item in app.button if item.label == label)


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
