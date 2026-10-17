"""Actual ADK invocation tests with bounded local HTTP fixtures, no inference API."""
import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel

from workbench.llm import LLMError, create_llm
from workbench.settings import Settings
from workbench.workspace import WorkspaceManager


class MappingAnswer(BaseModel):
    target_column: str
    source_columns: list[str]


def test_structured_response_really_traverses_adk_agent_runner(tmp_path, monkeypatch):
    from google.adk.runners import Runner
    observed = []
    original = Runner.run_async

    async def observe(self, **kwargs):
        observed.append((self.agent.name, type(self.agent.model).__name__, kwargs['run_config'].max_llm_calls))
        async for event in original(self, **kwargs):
            yield event

    monkeypatch.setattr(Runner, 'run_async', observe)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'message': {'content': '{"target_column":"employee_id","source_columns":["emp_id"]}'}})

    settings = Settings(workspace_root=tmp_path, ollama_model='gemma-fixture:4b')
    workspace = WorkspaceManager(tmp_path).create('ADK fixture', 'Map fields')
    client = create_llm(settings, client=httpx.Client(transport=httpx.MockTransport(respond)))
    client.bind_workspace(workspace)
    result = client.structured(MappingAnswer, 'Map metadata. Preserve literal {braces}.', 'Use emp_id for employee_id')
    assert result.source_columns == ['emp_id']
    assert observed == [('mapping_agent', 'LocalModel', 1)]
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert '{braces}' in payload['messages'][0]['content']
    assert 'tools' not in payload
    events = [json.loads(line) for line in (workspace / 'logs/adk-reasoning.jsonl').read_text().splitlines()]
    assert any(e['author'] == 'mapping_agent' and e.get('invocation_id') for e in events)


def test_auto_detects_installed_gemma_without_downloading_or_cloud_fallback(tmp_path):
    requested = []

    def respond(request):
        requested.append(str(request.url))
        return httpx.Response(200, json={'models': [
            {'name': 'llama3.1:8b'}, {'name': 'gemma3:12b'},
            {'name': 'gemma3:4b'}, {'name': 'gemma-super:cloud'},
        ]})

    client = create_llm(Settings(workspace_root=tmp_path), client=httpx.Client(transport=httpx.MockTransport(respond)))
    report = client.health()
    assert report['model_available']
    assert report['model'] == 'gemma3:4b'
    assert report['gemma_models'] == ['gemma3:4b', 'gemma3:12b']
    assert requested == ['http://localhost:11434/api/tags']


def test_auto_selection_precedes_first_adk_agent_and_records_concrete_model(tmp_path, monkeypatch):
    from google.adk.runners import Runner
    observed_models, requests = [], []
    original = Runner.run_async

    async def observe(self, **kwargs):
        observed_models.append(self.agent.model.model)
        async for event in original(self, **kwargs):
            yield event

    monkeypatch.setattr(Runner, 'run_async', observe)

    def respond(request):
        requests.append(request)
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [
                {'name': 'banking-local:qat', 'details': {'family': 'gemma3'}},
            ]})
        return httpx.Response(200, json={'message': {'content': '{"target_column":"id","source_columns":["id"]}'}})

    workspace = WorkspaceManager(tmp_path).create('Auto model trace', 'Map fields')
    client = create_llm(Settings(workspace_root=tmp_path), client=httpx.Client(transport=httpx.MockTransport(respond)))
    client.bind_workspace(workspace)
    assert client.structured(MappingAnswer, 'Map fields', 'Map id').target_column == 'id'
    assert observed_models == ['banking-local:qat']
    assert [request.url.path for request in requests] == ['/api/tags', '/api/chat']
    assert json.loads(requests[-1].content)['model'] == 'banking-local:qat'
    events = [json.loads(line) for line in (workspace / 'logs/adk-reasoning.jsonl').read_text().splitlines()]
    assert any(event.get('custom_metadata', {}).get('model') == 'banking-local:qat' for event in events)


def test_auto_does_not_substitute_non_gemma_when_missing(tmp_path):
    client = create_llm(Settings(workspace_root=tmp_path), client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'models': [{'name': 'llama3.1:8b'}]}))))
    report = client.health()
    assert not report['model_available']
    assert report['model'] == 'auto'
    with pytest.raises(LLMError, match='No installed Gemma'):
        client.structured(MappingAnswer, 'Map', 'Map id')


def test_auto_recognizes_custom_gemma_tags_using_ollama_family_metadata(tmp_path):
    client = create_llm(Settings(workspace_root=tmp_path), client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'models': [
            {'name': 'banking-local:qat', 'size': 2_000_000, 'details': {'family': 'gemma4', 'families': ['gemma4']}},
            {'name': 'gemma3:4b', 'size': 3_000_000},
        ]}))))
    report = client.health()
    assert report['model'] == 'banking-local:qat'
    assert report['gemma_models'] == ['banking-local:qat', 'gemma3:4b']


def test_explicit_local_model_remains_explicit(tmp_path):
    client = create_llm(Settings(workspace_root=tmp_path, ollama_model='owner/custom-gemma:qat'),
                        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
                            200, json={'models': [{'name': 'gemma3:4b'}]}))))
    report = client.health()
    assert not report['model_available']
    assert report['model'] == 'owner/custom-gemma:qat'


def test_tool_capabilities_are_reported_without_requiring_model_tool_calls(tmp_path):
    client = create_llm(Settings(workspace_root=tmp_path, ollama_model='gemma-fixture:4b'),
                        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
                            200, json={'capabilities': ['completion', 'vision']}))))
    report = client.capabilities()
    assert report['native_tools_reported'] is False
    assert 'deterministic approved tools' in report['execution_mode']


def test_adapter_works_from_existing_async_loop(tmp_path):
    client = create_llm(Settings(workspace_root=tmp_path, ollama_model='gemma-fixture:4b'),
                        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
                            200, json={'message': {'content': '{"target_column":"id","source_columns":["id"]}'}}))))

    async def call():
        return client.structured(MappingAnswer, 'Map', 'Map id')

    assert asyncio.run(call()).target_column == 'id'


def test_framework_configuration_cannot_reenable_langgraph(tmp_path):
    assert Settings(workspace_root=tmp_path).agent_framework == 'google_adk'
    with pytest.raises(ValueError, match='google_adk'):
        Settings(workspace_root=tmp_path, agent_framework='langgraph')
