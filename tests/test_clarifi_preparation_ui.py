import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from workbench.agents.clarifi.ingest import sample_input
from workbench.agents.clarifi.preparation import FinancialPreparation, read_sources
from workbench.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def financial_app(tmp_path, monkeypatch):
    import streamlit as st
    from workbench.service import Service
    monkeypatch.setenv('WORKSPACE_ROOT', str(tmp_path))
    monkeypatch.setenv('CLARIFI_DEFAULT_PROVIDER', 'local')
    monkeypatch.delenv('CLARIFI_API_URL', raising=False)
    monkeypatch.setattr(Service, 'diagnostics', lambda self: {})
    st.cache_resource.clear()
    yield tmp_path
    st.cache_resource.clear()


def app():
    instance = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=30)
    instance.session_state['page'] = 'ClariFi Agent'
    instance.session_state['clarifi_intake_mode'] = 'Prepare with Migration'
    return instance


def button(instance, label):
    return next(item for item in instance.button if item.label == label)


def test_preparation_has_separate_review_flow_and_does_not_run_on_render(financial_app):
    instance = app().run()
    assert not instance.exception
    assert button(instance, 'Create financial preparation').disabled
    assert not any(item.label == 'Run Investigation' for item in instance.button)
    assert not list(financial_app.glob('clarifi-pipelines/uc_*'))
    assert any('PDFs need extraction' in item.value for item in instance.caption)


def test_routed_json_creates_preparation_and_requires_mappings(financial_app):
    instance = app()
    instance.session_state['clarifi_capability'] = 'Combined investigation'
    instance.session_state['clarifi_routed_files'] = [('export.json', sample_input('combined').model_dump_json().encode())]
    instance.run()
    assert not instance.exception
    button(instance, 'Create financial preparation').click().run()
    assert not instance.exception
    assert instance.session_state['clarifi_preparation_id']
    assert button(instance, 'Convert and validate in sandbox').disabled
    assert not any(item.label == 'Run prepared ClariFi investigation' for item in instance.button)
    assert any(item.label.startswith('Propose mappings') for item in instance.button)
    count = len(list(financial_app.glob('clarifi-pipelines/uc_*')))
    instance.run()
    assert len(list(financial_app.glob('clarifi-pipelines/uc_*'))) == count


def test_general_workbench_instruction_routes_before_general_proposals(financial_app, monkeypatch):
    import streamlit as st
    from workbench.service import Service
    from workbench.migration import MigrationService
    calls = []
    data = sample_input('combined').model_dump_json().encode()
    upload = SimpleNamespace(name='export.json', getvalue=lambda: data)
    def uploader(label, *args, **kwargs):
        if label == 'Dataset':
            return upload
        return [] if kwargs.get('accept_multiple_files') else None
    monkeypatch.setattr(st, 'file_uploader', uploader)
    monkeypatch.setattr(Service, 'create_workspace', lambda *args: calls.append('general'))
    monkeypatch.setattr(MigrationService, 'propose', lambda *args: calls.append('model'))
    instance = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=30)
    instance.session_state['page'] = 'New workspace'
    instance.run()
    next(item for item in instance.text_input if item.label == 'Workspace name').set_value('Financial review')
    next(item for item in instance.text_area if item.label == 'What do you want to solve?').set_value('Use ClariFi agent to investigate this export')
    button(instance, 'Create & profile workspace').click().run()
    assert not instance.exception
    assert instance.header[0].value == 'ClariFi Agent'
    assert instance.session_state['clarifi_intake_mode'] == 'Prepare with Migration'
    assert instance.session_state['clarifi_routed_files'][0][0] == 'export.json'
    assert not calls
    assert not list(financial_app.glob('uc_*'))


def test_valid_contract_can_skip_conversion_without_automatic_execution(financial_app, monkeypatch):
    calls = []
    monkeypatch.setattr(FinancialPreparation, 'create', lambda *a, **k: calls.append('migration'))
    monkeypatch.setattr('workbench.agents.clarifi.sandbox.FinancialSandbox.__call__', lambda *a, **k: calls.append('sandbox'))
    instance = app()
    instance.session_state['clarifi_capability'] = 'Combined investigation'
    instance.session_state['clarifi_routed_files'] = [('export.json', sample_input('combined').model_dump_json().encode())]
    instance.run()
    assert not instance.exception
    assert any('already match' in x.value for x in instance.success)
    next(x for x in instance.checkbox if x.label == 'Use the validated format directly').check().run()
    assert not instance.exception
    assert button(instance, 'Run Investigation').disabled
    assert not calls
    assert not list(financial_app.glob('clarifi-pipelines/uc_*'))
    assert list(financial_app.glob('clarifi-valid-inputs/uc_*'))
