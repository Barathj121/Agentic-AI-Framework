import copy
import json
from pathlib import Path

import pytest

from workbench.agents.clarifi.ingest import focused_input, excel_template
from workbench.agents.clarifi.models import RulesReasoner
from workbench.agents.clarifi.preparation import canonical_input, read_sources
from workbench.agents.clarifi.screening import screen, draft


def test_scores_match_full_agent_without_any_model(monkeypatch):
    from workbench.agents.clarifi.agent import ClariFiAgent
    source = focused_input('combined')
    expected = ClariFiAgent(mode='rules').run(source, approved=True)
    monkeypatch.setattr(RulesReasoner, 'plan', lambda *a: pytest.fail('AI-style planning is unnecessary'))
    monkeypatch.setattr(RulesReasoner, 'summarize', lambda *a: pytest.fail('No draft before request'))
    report = screen(source, approved=True)
    assert report['findings'] == expected['findings']
    assert len(report['findings']) == 5
    assert report['status'] == 'SCREENED'
    assert report['provenance']['ai_used'] is False
    assert report['narrative']['claims'] == []
    with pytest.raises(ValueError):
        screen(source)


def test_draft_requires_approval_and_untampered_input_before_model(monkeypatch):
    source = focused_input('combined')
    report = screen(source, approved=True)
    monkeypatch.setattr('workbench.agents.clarifi.agent.GeminiAPIReasoner', lambda *a: pytest.fail('Must not call model'))
    with pytest.raises(ValueError):
        draft(source, report)
    tampered = copy.deepcopy(report)
    tampered['findings'][0]['score'] = 1
    with pytest.raises(ValueError, match='evidence changed'):
        draft(source, tampered, approved=True)
    other = source.model_copy(update={'case_id': 'different-case'})
    with pytest.raises(ValueError, match='does not match'):
        draft(other, report, approved=True)


def test_draft_skips_planning_preserves_scores_and_validates_citations(monkeypatch):
    class Reasoner(RulesReasoner):
        provider = 'gemini_api'
        model = 'fixture-gemini'
        def plan(self, context):
            pytest.fail('Fast flow must skip model planning')
    monkeypatch.setattr('workbench.agents.clarifi.agent.GeminiAPIReasoner', lambda *a: Reasoner())
    source = focused_input('combined')
    report = screen(source, approved=True)
    original = copy.deepcopy(report)
    result = draft(source, report, approved=True, model='fixture-gemini')
    assert result['status'] == 'COMPLETED'
    assert result['findings'] == report['findings']
    assert result['provenance']['screening_id'] == report['investigation_id']
    assert result['provenance']['ai_used'] is True
    assert report == original
    assert len(result['narrative']['claims']) == 5
    def invalid(self, context):
        value = RulesReasoner.summarize(self, context)
        value.claims[0].evidence_ids = ['invented:source']
        return value
    monkeypatch.setattr(Reasoner, 'summarize', invalid)
    with pytest.raises(ValueError, match='outside'):
        draft(source, report, approved=True, model='fixture-gemini')
    assert report == original


def test_sandbox_executor_is_retained_and_cannot_return_invalid_scores():
    source = focused_input('combined')
    expected = screen(source, approved=True)
    calls = []
    def executor(data, tools):
        calls.append(tools)
        return {'findings': expected['findings'], 'trace': [{'stage': 'tool', 'execution': 'docker_sandbox'}]}
    assert screen(source, approved=True, tool_executor=executor)['provenance']['execution'] == 'docker_sandbox'
    assert calls == [source.tools]
    def invalid(data, tools):
        altered = copy.deepcopy(expected['findings'])
        altered[0]['score'] = 1
        return {'findings': altered, 'trace': []}
    with pytest.raises(ValueError, match='reconcile'):
        screen(source, approved=True, tool_executor=invalid)


@pytest.mark.parametrize('extension', ['json', 'xlsx'])
def test_canonical_format_skips_mapping_but_rejects_schema_and_reference_errors(extension):
    source = focused_input('combined')
    content = source.model_dump_json().encode() if extension == 'json' else excel_template('combined')
    sources = read_sources([('input.' + extension, content)])
    assignments = {item.key: item.label for item in sources}
    result = canonical_input(sources, assignments, case_id='validated', tool='combined')
    assert result.tools == ['insider_trading', 'shell_companies']
    with pytest.raises(ValueError):
        canonical_input(sources, {}, case_id='validated', tool='combined')
    payload = source.model_dump(mode='json')
    payload['trades'][0]['account_id'] = 'missing-account'
    invalid = read_sources([('bad.json', json.dumps(payload).encode())])
    with pytest.raises(ValueError):
        canonical_input(invalid, {item.key: item.label for item in invalid}, case_id='validated', tool='combined')


def test_scores_first_ui_survives_model_failure(financial_ui, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from workbench.service import Service
    monkeypatch.setattr(Service, 'run_agent', lambda *a, **k: pytest.fail('No full agent in fast flow'))
    monkeypatch.setattr('workbench.agents.clarifi.screening.draft', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('private failure detail')))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=30)
    app.session_state['page'] = 'ClariFi Agent'
    app.run()
    assert not app.exception
    next(x for x in app.checkbox if x.label == 'I have reviewed the input and approve this investigation').check().run()
    next(x for x in app.button if x.label == 'Run Investigation').click().run()
    assert not app.exception
    assert app.metric
    assert len(list((financial_ui / 'workspaces/clarifi').glob('*.json'))) == 1
    next(x for x in app.checkbox if x.label == 'Generate an AI analyst draft using the selected model').check().run()
    next(x for x in app.button if x.label == 'Generate AI analyst draft').click().run()
    assert not app.exception
    assert app.metric
    assert any('Your verified screening is saved' in x.value for x in app.error)
    assert not any('private failure detail' in x.value for x in app.error)
    assert len(list((financial_ui / 'workspaces/clarifi').glob('*.json'))) == 1

from test_clarifi_ui import financial_ui


def test_financial_hosted_mapping_adapter_is_explicit_and_redacts_failures(monkeypatch):
    from workbench.agents.clarifi.models import FinancialGeminiMappingClient
    from workbench.migration import MappingPlan
    from workbench.llm import LLMError
    class Hosted:
        def __init__(self, model):
            assert model == 'fixture-gemini'
        def _generate(self, schema, system, payload):
            assert schema is MappingPlan
            assert payload == {'source_metadata': []}
            raise ValueError('Sensitive sample values')
    monkeypatch.setattr('workbench.agents.clarifi.models.GeminiAPIReasoner', Hosted)
    with pytest.raises(LLMError) as error:
        FinancialGeminiMappingClient('fixture-gemini').structured(MappingPlan, 'mapping instructions', '{"source_metadata": []}')
    assert 'Sensitive sample values' not in str(error.value)


def test_credential_failure_does_not_block_screening(financial_ui, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('CLARIFI_DEFAULT_PROVIDER', 'gemini')
    monkeypatch.setattr('workbench.agents.clarifi.models.gemini_api_config', lambda: (_ for _ in ()).throw(ValueError('private secret detail')))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=30)
    app.session_state['page'] = 'ClariFi Agent'
    app.run()
    assert not app.exception
    assert any('still run scores-first screening' in x.value for x in app.warning)
    next(x for x in app.checkbox if x.label == 'I have reviewed the input and approve this investigation').check().run()
    next(x for x in app.button if x.label == 'Run Investigation').click().run()
    assert not app.exception
    assert app.metric
    assert not any('private secret detail' in x.value for x in app.warning)
