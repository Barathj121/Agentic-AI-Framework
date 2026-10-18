"""Contract tests use explicit fakes; real Docker tests are marked separately."""
import csv
import io
import json
from dataclasses import replace

import duckdb
import pytest

from workbench.agents.clarifi.agent import ClariFiAgent
from workbench.agents.clarifi.ingest import IntakeError, sample_input
from workbench.agents.clarifi.preparation import FinancialPreparation, digest, read_sources, requests_clarifi, target_metadata
from workbench.agents.clarifi.sandbox import FinancialSandbox
from workbench.migration import Mapping, MappingPlan, parse_metadata
from workbench.sandbox import SandboxManager
from workbench.schemas import ExecutionResult
from workbench.settings import Settings
from workbench.workspace import atomic_json, read_json, safe_path


class MappingModel:
    def structured(self, schema, system, prompt):
        assert schema is MappingPlan
        packet = json.loads(prompt)
        source = {item['column_name'] for item in packet['source_metadata']}
        return MappingPlan(mappings=[Mapping(target_column=item['column_name'],
            source_columns=[item['column_name']] if item['column_name'] in source else [],
            mapping_type='DIRECT' if item['column_name'] in source else 'MISSING',
            reason='Fixture source schema correspondence, subject to review') for item in packet['target_metadata']])


def fixture_pipeline(tmp_path, tool='combined', payload=None, sandbox=None):
    source = sample_input(tool).model_dump(mode='json') if payload is None else payload
    tables = read_sources([('source.json', json.dumps(source).encode())])
    service = FinancialPreparation(Settings(workspace_root=tmp_path), llm=MappingModel(), sandbox=sandbox)
    workspace = service.create('case-1', 'Use ClariFi agent to review these records', tool, tables, {s.key: s.label for s in tables})
    return service, workspace


def approve(service, workspace):
    migration = service.migration(workspace.name)
    for table, item in service.state(workspace.name)['tables'].items():
        plan = migration.propose(item['workspace_id'])
        for mapping in plan.mappings:
            mapping.status = 'MISSING' if mapping.mapping_type == 'MISSING' else 'APPROVED'
        migration.approve(item['workspace_id'], plan)


def fake_materialization(monkeypatch):
    """Materialize reviewed direct fields with trusted static code, no generated imports.

    This deliberately replaces Docker execution, so it is not an isolation test.
    """
    from workbench.migration import MigrationService
    from workbench.migration_runtime import materialize_and_validate

    def execute(self, identifier):
        child = self.workspace(identifier)
        config = read_json(child / 'metadata/migration.json')
        approval = read_json(child / 'metadata/mapping_approval.json')
        with duckdb.connect(str(child / 'database/workspace.duckdb')) as con:
            source = con.execute('SELECT * FROM source.data ORDER BY rowid').fetchdf()
            import pandas as pd
            frame = pd.DataFrame({m['target_column']: source[m['source_columns'][0]] if m['mapping_type'] == 'DIRECT' else [None] * len(source)
                                  for m in approval['plan']['mappings']})
            report = materialize_and_validate(con, frame, config['target'], approval['plan']['mappings'], len(source))
            cursor = con.execute('SELECT * FROM results.migrated_data')
            with (child / 'results/migrated_data.csv').open('w', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow([col[0] for col in cursor.description])
                writer.writerows(cursor.fetchall())
        atomic_json(child / 'results/validation.json', report)
        return self._verify_report(child, *self.metadata(identifier)[1:], MappingPlan.model_validate(approval['plan']))
    # Explicit args avoid conflating a container's report with a trusted success.
    monkeypatch.setattr(MigrationService, 'execute', execute)


@pytest.mark.parametrize('text,expected', [('Use ClariFi agent', True), ('please use the clarifi agent for this', True),
    ('use Clarify agent', True), ('investigate payments', False), ('ClariFi agent is interesting', False)])
def test_explicit_agent_routing(text, expected):
    assert requests_clarifi(text) is expected


def test_raw_intake_preserves_ids_offsets_and_arbitrary_sheet_names():
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = 'Broker export'
    sheet.append(['acct', 'timestamp'])
    sheet.append(['00007', '2026-01-01T12:00:00+05:30'])
    output = io.BytesIO()
    book.save(output)
    result = read_sources([('broker.xlsx', output.getvalue())])
    assert result[0].label == 'Broker export'
    assert b'00007' in result[0].content and b'+05:30' in result[0].content
    assert result[0].original_sha256 == digest(output.getvalue())


def test_intake_rejects_formulas_nested_objects_and_excess_input():
    from openpyxl import Workbook
    book = Workbook()
    book.active.append(['identifier'])
    book.active.append(['=1+1'])
    output = io.BytesIO()
    book.save(output)
    with pytest.raises(IntakeError, match='formulas'):
        read_sources([('bad.xlsx', output.getvalue())])
    with pytest.raises(IntakeError, match='nested'):
        read_sources([('bad.json', b'[{"a":{"b":1}}]')])
    with pytest.raises(IntakeError, match='5 MiB'):
        read_sources([('large.csv', b'x' * (5 * 1024 * 1024 + 1))])
    with pytest.raises(IntakeError, match='PDF'):
        read_sources([('source.pdf', b'%PDF')])
    with pytest.raises(IntakeError, match='match'):
        read_sources([('bad.csv', b'a,b\n1\n')])


def test_target_contract_does_not_coerce_identifiers_or_timezone_text():
    fields = parse_metadata(target_metadata('trades'))
    assert all(field.data_type == 'VARCHAR' for field in fields)
    assert next(field for field in fields if field.column_name == 'occurred_at').required
    assert 'UTC offset' in next(field for field in fields if field.column_name == 'occurred_at').description


def test_table_assignment_and_both_approval_boundaries(tmp_path):
    service, workspace = fixture_pipeline(tmp_path)
    with pytest.raises(IntakeError, match='approve mappings'):
        service.prepare(workspace.name)
    with pytest.raises(IntakeError, match='approve this investigation'):
        service.investigate(workspace.name)
    with pytest.raises(IntakeError, match='Complete migration'):
        service.investigate(workspace.name, approved=True, mode='rules')
    sources = read_sources([('data.json', sample_input('combined').model_dump_json().encode())])
    with pytest.raises(IntakeError, match='each required table'):
        service.create('case-2', 'Use ClariFi agent', 'combined', sources, {s.key: 'entities' for s in sources})
    assert not (workspace / 'results/investigation.json').exists()


@pytest.mark.parametrize('tool', ['insider_trading', 'shell_companies', 'combined'])
def test_reviewed_multi_table_handoff_matches_direct_financial_results(tmp_path, monkeypatch, tool):
    fake_materialization(monkeypatch)
    service, workspace = fixture_pipeline(tmp_path, tool)
    approve(service, workspace)
    prepared = service.prepare(workspace.name)
    assert prepared.model_dump(mode='json') == {**sample_input(tool).model_dump(mode='json'), 'case_id': 'case-1'}
    assert read_json(workspace / 'metadata/workspace.json')['status'] == 'READY_FOR_INVESTIGATION'
    handoff = read_json(workspace / 'results/handoff.json')
    assert all(item['target_sha256'] and item['mapping_sha256'] and item['original_sha256'] for item in handoff['tables'].values())
    for table, item in service.state(workspace.name)['tables'].items():
        child = service.migration(workspace.name).workspace(item['workspace_id'])
        with duckdb.connect(str(child / 'database/workspace.duckdb'), read_only=True) as con:
            assert all(col[1] == 'VARCHAR' for col in con.execute('DESCRIBE source.data').fetchall())
    # Explicit fake at the process boundary; evidence and score checks still run.
    from workbench.agents.clarifi.tools import TOOLS
    def fake_run(self, source, tools):
        return {'findings': [f.model_dump(mode='json') for tool in tools for f in TOOLS[tool](source)],
                'trace': [{'stage': 'tool', 'tool': tool, 'execution': 'docker_sandbox'} for tool in tools]}
    monkeypatch.setattr(FinancialSandbox, '__call__', fake_run)
    report = service.investigate(workspace.name, approved=True, mode='rules')
    baseline = ClariFiAgent(mode='rules').run(prepared, approved=True)
    assert report['findings'] == baseline['findings']
    assert report['provenance']['execution'] == 'docker_sandbox'
    assert report['trace'][0]['stage'] == 'migration_handoff'
    assert read_json(workspace / 'results/investigation.json')['report']['investigation_id'] == report['investigation_id']


@pytest.mark.parametrize('corruption', ['reference', 'timestamp', 'amount', 'duplicate'])
def test_invalid_financial_data_never_reaches_investigation(tmp_path, monkeypatch, corruption):
    fake_materialization(monkeypatch)
    data = sample_input('combined').model_dump(mode='json')
    if corruption == 'reference':
        data['payments'][0]['sender_id'] = 'unknown'
    elif corruption == 'timestamp':
        data['trades'][0]['occurred_at'] = '2026-10-18T12:00:00'
    elif corruption == 'amount':
        data['payments'][0]['amount'] = -1
    else:
        data['entities'].append(data['entities'][0])
    service, workspace = fixture_pipeline(tmp_path, payload=data)
    approve(service, workspace)
    with pytest.raises(ValueError):
        service.prepare(workspace.name)
    assert not (workspace / 'results/handoff.json').exists()
    with pytest.raises(IntakeError, match='Complete migration'):
        service.investigate(workspace.name, approved=True, mode='rules')


@pytest.mark.parametrize('path', ['results/prepared.json', 'results/migrated_data.csv', 'metadata/mapping_approval.json'])
def test_changed_evidence_invalidates_handoff(tmp_path, monkeypatch, path):
    fake_materialization(monkeypatch)
    service, workspace = fixture_pipeline(tmp_path, 'shell_companies')
    approve(service, workspace)
    service.prepare(workspace.name)
    target = workspace if path == 'results/prepared.json' else service.migration(workspace.name).workspace(next(iter(service.state(workspace.name)['tables'].values()))['workspace_id'])
    with (target / path).open('a') as stream:
        stream.write(' ')
    with pytest.raises(IntakeError, match='changed'):
        service.prepared(workspace.name)


def test_financial_sandbox_reuses_restrictions_and_never_falls_back(tmp_path, monkeypatch):
    service, workspace = fixture_pipeline(tmp_path, 'shell_companies')
    sandbox = SandboxManager(service.settings)
    command = sandbox._command(workspace, tmp_path, 'adw-test', 'clarifi_tools')
    assert '--read-only' in command and '--privileged' not in command
    assert command[command.index('--network') + 1] == 'none'
    assert '--cpus' in command and '--memory' in command
    monkeypatch.setattr(sandbox, '_recover_cleanup', lambda workspace: None)
    monkeypatch.setattr(sandbox, '_run', lambda *args: ExecutionResult(success=False, exit_code=None,
        execution_time_seconds=0, stdout_path='logs/out', stderr_path='logs/err', error='Docker unavailable'))
    old = workspace / 'results/clarifi-tools.json'
    old.write_text('{"findings": []}')
    with pytest.raises(RuntimeError, match='sandbox did not complete'):
        FinancialSandbox(service.settings, workspace, sandbox=sandbox)(sample_input('shell_companies'), ['shell_companies'])
    assert not old.exists()


@pytest.mark.docker
@pytest.mark.parametrize('tool', ['insider_trading', 'shell_companies', 'combined'])
def test_real_docker_migration_and_financial_tools(tmp_path, tool):
    service, workspace = fixture_pipeline(tmp_path, tool)
    approve(service, workspace)
    prepared = service.prepare(workspace.name)
    report = service.investigate(workspace.name, approved=True, mode='rules')
    expected = ClariFiAgent(mode='rules').run(prepared, approved=True)
    assert report['findings'] == expected['findings']
    execution = read_json(workspace / 'metadata/clarifi_execution.json')
    assert execution['success'] and execution['exit_code'] == 0


def test_financial_mapping_contract_requires_every_target_and_real_source_columns(tmp_path):
    calls = []
    class ExactMappingModel:
        def structured(self, schema, system, prompt):
            packet = json.loads(prompt)
            assert 'OBJECT keyed' in system
            targets = [field['column_name'] for field in packet['target_metadata']]
            available = {field['column_name'] for field in packet['source_metadata']}
            decisions = {name: {'source_columns': [name] if name in available else [],
                'mapping_type': 'DIRECT' if name in available else 'MISSING', 'reason': 'Fixture schema correspondence'} for name in targets}
            with pytest.raises(ValueError):
                schema.model_validate({'mappings': {}})
            wrong = {name: dict(value) for name, value in decisions.items()}
            wrong[targets[0]]['source_columns'] = ['invented_source']
            with pytest.raises(ValueError):
                schema.model_validate({'mappings': wrong})
            calls.append(schema.__name__)
            return schema.model_validate({'mappings': decisions})
    service, workspace = fixture_pipeline(tmp_path, 'shell_companies')
    service.llm = ExactMappingModel()
    plan = service.propose(workspace.name, 'entities')
    assert calls == ['FinancialMappingPlan']
    assert {mapping.target_column for mapping in plan.mappings} == set(type(sample_input('shell_companies').entities[0]).model_fields)
    assert all(mapping.status == 'PENDING' for mapping in plan.mappings)


@pytest.mark.parametrize('corruption', ['hash', 'tools', 'score'])
def test_sandbox_evidence_is_bound_to_approved_input(tmp_path, monkeypatch, corruption):
    source = sample_input('shell_companies')
    service, workspace = fixture_pipeline(tmp_path, 'shell_companies')
    sandbox = SandboxManager(service.settings)
    monkeypatch.setattr(sandbox, '_recover_cleanup', lambda workspace: None)
    def fake_run(workspace, contract, mode, label):
        report = ClariFiAgent(mode='rules').run(source, approved=True)
        result = {'input_sha256': digest((workspace / 'input/clarifi.json').read_bytes()), 'tools': source.tools,
                  'findings': report['findings'], 'trace': [{'tool': tool} for tool in source.tools]}
        if corruption == 'hash':
            result['input_sha256'] = 'stale'
        elif corruption == 'tools':
            result['tools'] = []
        else:
            result['findings'][0]['score'] = 101
        atomic_json(workspace / 'results/clarifi-tools.json', result)
        return ExecutionResult(success=True, exit_code=0, execution_time_seconds=0,
                               stdout_path='logs/out', stderr_path='logs/err')
    monkeypatch.setattr(sandbox, '_run', fake_run)
    with pytest.raises(ValueError):
        FinancialSandbox(service.settings, workspace, sandbox=sandbox)(source, source.tools)


def test_reused_identifier_roles_require_explicit_resolution(tmp_path):
    class ConfusedModel:
        def structured(self, schema, system, prompt):
            packet = json.loads(prompt)
            available = {field['column_name'] for field in packet['source_metadata']}
            decisions = {field['column_name']: {'source_columns': [field['column_name']] if field['column_name'] in available else [],
                'mapping_type': 'DIRECT' if field['column_name'] in available else 'MISSING', 'reason': 'Model suggestion'} for field in packet['target_metadata']}
            decisions['entity_id']['source_columns'] = ['owner_entity_id']
            return schema.model_validate({'mappings': decisions})
    service, workspace = fixture_pipeline(tmp_path, 'shell_companies')
    service.llm = ConfusedModel()
    plan = service.propose(workspace.name, 'entities')
    conflicts = [mapping for mapping in plan.mappings if mapping.target_column in {'entity_id', 'owner_entity_id'}]
    assert all(mapping.mapping_type == 'AMBIGUOUS' for mapping in conflicts)
    assert plan.observations
    for mapping in plan.mappings:
        mapping.status = 'APPROVED' if mapping.mapping_type != 'MISSING' else 'MISSING'
    child = service.state(workspace.name)['tables']['entities']['workspace_id']
    with pytest.raises(ValueError, match='resolve ambiguity'):
        service.migration(workspace.name).approve(child, plan)


def test_migration_export_must_match_materialized_evidence(tmp_path, monkeypatch):
    fake_materialization(monkeypatch)
    from workbench.migration import MigrationService
    original = MigrationService.execute
    def inconsistent_export(self, identifier):
        report = original(self, identifier)
        child = self.workspace(identifier)
        path = child / 'results/migrated_data.csv'
        # Still parseable, but it is not the independently validated table.
        rows = list(csv.reader(io.StringIO(path.read_text())))
        rows[1][0] += '-altered'
        output = io.StringIO(newline='')
        csv.writer(output).writerows(rows)
        path.write_text(output.getvalue())
        return report
    monkeypatch.setattr(MigrationService, 'execute', inconsistent_export)
    service, workspace = fixture_pipeline(tmp_path, 'shell_companies')
    approve(service, workspace)
    with pytest.raises(IntakeError, match='disagrees'):
        service.prepare(workspace.name)
    assert not (workspace / 'results/handoff.json').exists()
