"""ClariFi's request deadline must match the operator's configuration."""
import pytest

from workbench.agents.clarifi import models
from workbench.llm import LLMError, OllamaClient


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(models, '__file__', str(tmp_path / 'workbench/agents/clarifi/models.py'))
    for key in ('CLARIFI_LLM_TIMEOUT_SECONDS', 'LLM_TIMEOUT_SECONDS', 'CLARIFI_OLLAMA_BASE_URL', 'OLLAMA_BASE_URL'):
        monkeypatch.delenv(key, raising=False)
    return tmp_path / '.env'


@pytest.mark.parametrize('generic,specific,expected', [(None, None, 600), ('480', None, 480), ('480', '720', 720)])
def test_timeout_precedence(isolated_config, monkeypatch, generic, specific, expected):
    if generic is not None:
        monkeypatch.setenv('LLM_TIMEOUT_SECONDS', generic)
    if specific is not None:
        monkeypatch.setenv('CLARIFI_LLM_TIMEOUT_SECONDS', specific)
    reasoner = models.LocalReasoner('gemma3:4b')
    assert reasoner.settings.llm_timeout_seconds == expected
    client = OllamaClient(reasoner.settings)
    try:
        assert client._client.timeout.read == expected
    finally:
        client.close()


def test_ignored_dotenv_configuration_and_environment_override(isolated_config, monkeypatch):
    isolated_config.write_text('CLARIFI_LLM_TIMEOUT_SECONDS=700\nOLLAMA_BASE_URL=http://127.0.0.1:11435\n')
    reasoner = models.LocalReasoner('gemma3:4b')
    assert reasoner.settings.llm_timeout_seconds == 700
    assert reasoner.settings.ollama_base_url == 'http://127.0.0.1:11435'
    monkeypatch.setenv('CLARIFI_LLM_TIMEOUT_SECONDS', '800')
    assert models.LocalReasoner('gemma3:4b').settings.llm_timeout_seconds == 800


@pytest.mark.parametrize('value', ['0', '-1', 'not-a-number'])
def test_invalid_timeout_fails_before_inference(isolated_config, monkeypatch, value):
    monkeypatch.setenv('CLARIFI_LLM_TIMEOUT_SECONDS', value)
    with pytest.raises(ValueError, match='positive integer'):
        models.LocalReasoner('gemma3:4b')


def test_timeout_guidance_names_effective_clarifi_setting(isolated_config, monkeypatch):
    monkeypatch.setenv('CLARIFI_LLM_TIMEOUT_SECONDS', '720')
    def timeout(*args, **kwargs):
        raise LLMError('Ollama request timed out after 720s.')
    monkeypatch.setattr(OllamaClient, 'structured', timeout)
    with pytest.raises(LLMError, match='720s.*CLARIFI_LLM_TIMEOUT_SECONDS'):
        models.LocalReasoner('gemma3:4b').plan({'available_tools': ['insider_trading']})


def test_local_drafts_cover_each_finding_in_bounded_batches(isolated_config, monkeypatch):
    reasoner = models.LocalReasoner('gemma3:4b')
    findings = [{'finding_id': f'insider:T{i}:E1', 'evidence_ids': [f'trades:T{i}']} for i in range(7)]
    batches = []
    def generate(schema, instruction, packet):
        batches.append(packet)
        return schema.model_validate({'claims': {item['finding_id']: {
            'explanation': 'Synthetic evidence requires analyst review.', 'evidence_ids': item['evidence_ids']}
            for item in packet['findings']}, 'next_steps': ['Review evidence.']})
    monkeypatch.setattr(reasoner, '_generate', generate)
    narrative = reasoner.summarize({'findings': findings, 'total_findings': 7, 'drafted_findings': 7})
    assert [len(packet['findings']) for packet in batches] == [5, 2]
    assert [claim.finding_id for claim in narrative.claims] == [item['finding_id'] for item in findings]
    assert narrative.next_steps == ['Review evidence.']


@pytest.mark.parametrize('corruption', ['missing_finding', 'foreign_evidence'])
def test_local_draft_schema_rejects_omissions_and_foreign_citations(isolated_config, monkeypatch, corruption):
    from pydantic import ValidationError
    reasoner = models.LocalReasoner('gemma3:4b')
    def generate(schema, instruction, packet):
        claims = {'shell:C1': {'explanation': 'Review supplied evidence.', 'evidence_ids': ['entities:C1']}}
        if corruption == 'missing_finding':
            claims = {}
        else:
            claims['shell:C1']['evidence_ids'] = ['entities:OTHER']
        return schema.model_validate({'claims': claims, 'next_steps': []})
    monkeypatch.setattr(reasoner, '_generate', generate)
    with pytest.raises(ValidationError):
        reasoner.summarize({'findings': [{'finding_id': 'shell:C1', 'evidence_ids': ['entities:C1']}]})


def test_secret_manager_requires_pinned_resource_and_redacts_errors(monkeypatch):
    import pytest
    from workbench.agents.clarifi.models import _secret_manager_key, _cached_secret_key
    _cached_secret_key.cache_clear()
    with pytest.raises(ValueError, match='pinned'):
        _secret_manager_key('https://untrusted.example/key')
    monkeypatch.setattr('google.auth.default', lambda **kwargs: (_ for _ in ()).throw(RuntimeError('sensitive credential')))
    with pytest.raises(ValueError) as error:
        _secret_manager_key('projects/fixture-project/secrets/gemini/versions/1')
    assert 'sensitive credential' not in str(error.value)
    _cached_secret_key.cache_clear()
