import csv
import io
import json
import zipfile

import duckdb
import pandas as pd
import pytest

from workbench.migration import (
    Mapping, MappingPlan, MetadataField, MigrationService, PythonTransformationPlan,
    SqlTransformationPlan, TransformationExpression, TransformationPlan, build_transformation, fit_gap, infer_source_metadata,
    normalize_type, parse_metadata, validate_mappings,
)
from workbench.migration_runtime import materialize_and_validate
from workbench.settings import Settings
from workbench.workspace import atomic_json, read_json


SOURCE = b'sku,stock_count,unit_cost\nA01,12,2.5\nB02,invalid,3.75\n'
TARGET = b'column_name,data_type,required,description\nproduct_id,VARCHAR,true,Product identifier\nunits,INTEGER,true,Units in inventory\n'


class InventoryModel:
    def __init__(self):
        self.calls = []

    def structured(self, schema, system, prompt):
        self.calls.append(schema)
        return MappingPlan(mappings=[
            Mapping(target_column='product_id', source_columns=['sku'], mapping_type='DIRECT', confidence='HIGH', reason='Product identifier'),
            Mapping(target_column='units', source_columns=['stock_count'], mapping_type='DIRECT', confidence='HIGH', reason='Inventory units'),
        ])


@pytest.fixture
def migration(tmp_path):
    model = InventoryModel()
    settings = Settings(workspace_root=tmp_path / 'workspaces', max_repair_attempts=0)
    service = MigrationService(settings, llm=model)
    workspace = service.create('Inventory migration', SOURCE, TARGET)
    return service, workspace, model


def approve_all(service, workspace):
    plan = service.propose(workspace.name)
    for mapping in plan.mappings:
        mapping.status = 'APPROVED'
    service.approve(workspace.name, plan)
    return plan


def test_metadata_scalar_types_and_aliases():
    fields = parse_metadata(TARGET)
    assert fields[1].data_type == 'INTEGER' and fields[1].required
    assert normalize_type('decimal (18, 2)') == 'DECIMAL(18,2)'
    assert normalize_type('string') == 'VARCHAR'


@pytest.mark.parametrize('data', [
    b'name,type\na,int\n',
    b'column_name,data_type,required,description\na,BIGINT,maybe,number\n',
    b'column_name,data_type,required,description\na,BIGINT,true,number\nA,BIGINT,false,duplicate\n',
    b'column_name,data_type,required,description\na,INTEGER);DROP SCHEMA raw,true,number\n',
    b'column_name,data_type,required,description\n__record_number,BIGINT,true,reserved\n',
])
def test_invalid_target_metadata_rejected(data):
    with pytest.raises(ValueError):
        parse_metadata(data)


def test_allowed_values_and_defaults_are_explicit():
    fields = parse_metadata(b'column_name,data_type,required,description,allowed_values,default_value\ncountry,VARCHAR,true,Country,IN|SG,IN\n')
    assert fields[0].allowed_values == ['IN', 'SG'] and fields[0].default_value == 'IN'


def test_unconvertible_allowed_value_is_rejected():
    with pytest.raises(ValueError, match='cannot be converted'):
        MetadataField(column_name='n', data_type='INTEGER', required=True, description='Number', allowed_values=['word'])


@pytest.mark.parametrize('dtype,values,allowed', [('BOOLEAN', [True, False], ['True', 'False']), ('DECIMAL(18,2)', [1, 2], ['1', '2'])])
def test_allowed_values_compare_in_target_type(dtype, values, allowed):
    target = [MetadataField(column_name='value', data_type=dtype, required=True, description='Value', allowed_values=allowed).model_dump()]
    mappings = [{'target_column': 'value', 'mapping_type': 'DIRECT', 'status': 'APPROVED'}]
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE SCHEMA staging')
        con.execute('CREATE SCHEMA results')
        report = materialize_and_validate(con, pd.DataFrame({'value': values}), target, mappings, 2)
    assert report['complete'] and report['invalid_records'] == 0


def test_source_metadata_inference_does_not_invent_business_meaning():
    fields = infer_source_metadata({'columns': [{'name': 'count', 'type': 'BIGINT', 'null_count': 0}]})
    assert fields[0].required is False
    assert 'Business meaning is not inferred' in fields[0].description


def test_ingestion_materializes_metadata_and_source(migration):
    service, workspace, _ = migration
    with duckdb.connect(str(workspace / 'database/workspace.duckdb'), read_only=True) as con:
        assert con.execute('SELECT count(*) FROM source.data').fetchone() == (2,)
        assert con.execute('SELECT count(*) FROM source.metadata').fetchone() == (3,)
        assert con.execute('SELECT required FROM target.metadata WHERE column_name=?', ['units']).fetchone() == (True,)
        assert con.execute('SELECT count(*) FROM raw.original').fetchone() == (2,)
    assert (workspace / 'input/source_data.csv').read_bytes() == SOURCE
    assert read_json(workspace / 'metadata/workspace.json')['kind'] == 'migration'


def test_source_metadata_unknown_column_rejected(tmp_path):
    service = MigrationService(Settings(workspace_root=tmp_path))
    with pytest.raises(ValueError, match='exactly'):
        service.create('bad', SOURCE, TARGET, b'column_name,data_type,required,description\nunknown,VARCHAR,true,No such source column\n')


def test_semantic_proposal_is_persisted_and_requires_review(migration):
    service, workspace, model = migration
    service.manager.status(workspace, 'MAPPING_FAILED', error='Previous model request failed')
    plan = service.propose(workspace.name)
    assert model.calls == [MappingPlan]
    assert all(m.status == 'PENDING' and m.requires_review for m in plan.mappings)
    assert (workspace / 'generated/mapping.json').is_file()
    assert read_json(workspace / 'metadata/workspace.json')['error'] is None
    with duckdb.connect(str(workspace / 'database/workspace.duckdb'), read_only=True) as con:
        assert con.execute('SELECT count(*) FROM mapping.proposals').fetchone() == (2,)


def test_invented_source_columns_rejected(migration):
    service, workspace, _ = migration
    source, target = service.metadata(workspace.name)
    proposal = InventoryModel().structured(None, '', '')
    proposal.mappings[0].source_columns = ['invented']
    with pytest.raises(ValueError, match='unknown source'):
        validate_mappings(proposal, source, target)


@pytest.mark.parametrize('explanation', [{}, {'reason': ''}, {'reason': ' \t\n '}])
def test_structured_mapping_rejects_missing_or_blank_explanations(explanation):
    # The shared structured client catches Pydantic validation errors and makes
    # a bounded repair request before any proposal is persisted or approved.
    payload = {'mappings': [{'target_column': 'units', 'source_columns': ['stock_count'],
                            'mapping_type': 'DIRECT', **explanation}]}
    with pytest.raises(ValueError, match='reason'):
        MappingPlan.model_validate_json(json.dumps(payload))


def test_structured_mapping_requires_explicit_source_columns_even_for_missing_field():
    payload = {'mappings': [{'target_column': 'tax_identifier', 'mapping_type': 'MISSING',
                            'reason': 'No source field contains the required tax identifier'}]}
    with pytest.raises(ValueError, match='source_columns'):
        MappingPlan.model_validate_json(json.dumps(payload))
    payload['mappings'][0]['source_columns'] = []
    assert MappingPlan.model_validate_json(json.dumps(payload)).mappings[0].source_columns == []


def test_missing_target_and_duplicate_mappings_rejected(migration):
    service, workspace, _ = migration
    source, target = service.metadata(workspace.name)
    mapping = Mapping(target_column='units', source_columns=['stock_count'], mapping_type='DIRECT', reason='Both fields represent inventory unit counts')
    with pytest.raises(ValueError, match='exactly one'):
        validate_mappings(MappingPlan(mappings=[mapping, mapping]), source, target)


def test_execute_and_approval_require_explicit_checkpoints(migration):
    service, workspace, model = migration
    with pytest.raises(ValueError, match='Human mapping approval'):
        service.execute(workspace.name)
    assert model.calls == []
    with pytest.raises(ValueError, match='Generate a mapping'):
        service.approve(workspace.name, InventoryModel().structured(None, '', ''))
    pending = service.propose(workspace.name)
    with pytest.raises(ValueError, match='choose APPROVED'):
        service.approve(workspace.name, pending)


def test_approval_persists_actual_table_and_is_immutable(migration):
    service, workspace, _ = migration
    plan = approve_all(service, workspace)
    assert read_json(workspace / 'metadata/mapping_approval.json')['human_approved'] is True
    with duckdb.connect(str(workspace / 'database/workspace.duckdb'), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM mapping.approved WHERE status='APPROVED'").fetchone() == (2,)
    with pytest.raises(ValueError, match='immutable'):
        service.approve(workspace.name, plan)
    with pytest.raises(ValueError, match='already approved'):
        service.propose(workspace.name)


def test_fit_gap_uses_actual_mappings_and_required_flags():
    target = [MetadataField(column_name='identifier', data_type='VARCHAR', required=True, description='Required'),
              MetadataField(column_name='label', data_type='VARCHAR', required=False, description='Optional')]
    plan = MappingPlan(mappings=[Mapping(target_column='identifier', source_columns=[], mapping_type='MISSING', reason='The source contains no identifier field'),
                                Mapping(target_column='label', source_columns=['name'], mapping_type='DIRECT', reason='Source name supplies the requested display label')])
    result = fit_gap(plan, target)
    assert result['total_target_fields'] == 2 and result['direct_mappings'] == 1
    assert result['unresolved_required_fields'] == ['identifier']
    assert len(result['fields_requiring_review']) == 2
    assert not any('percent' in key for key in result)


def test_direct_transform_build_needs_no_generated_model_code(migration):
    service, workspace, model = migration
    plan = approve_all(service, workspace)
    source, target = service.metadata(workspace.name)
    generated = service._generate(workspace, plan, source, target)
    build = build_transformation(generated, plan, target)
    assert len(model.calls) == 1
    sql = next(f.content for f in build.files if f.path.endswith('.sql'))
    assert '"sku"' in sql and 'FROM source.data' in sql
    assert build.entrypoint == 'generated/solution.py'
    assert 'Docker sandbox' in next(f.content for f in build.files if f.path == build.entrypoint)


def test_transform_generation_preserves_expression_and_rejects_unapproved_references():
    mapping = Mapping(target_column='total', source_columns=['unit_cost', 'units'], mapping_type='DERIVED', status='APPROVED', transformation_description='Multiply units and unit cost', reason='Inventory value derives from the approved unit cost and inventory unit count')
    plan = MappingPlan(mappings=[mapping])
    target = [MetadataField(column_name='total', data_type='DOUBLE', required=True, description='Inventory value')]
    generated = TransformationPlan(language='sql', expressions=[TransformationExpression(target_column='total', sql_expression='"unit_cost" * "units"')], python_code='')
    assert '"unit_cost" * "units"' in build_transformation(generated, plan, target).files[0].content
    for expression in ('invented * 2', 'units; DROP TABLE raw.source', '(SELECT 1 FROM source.data)'):
        with pytest.raises(ValueError):
            build_transformation(TransformationPlan(language='sql', expressions=[TransformationExpression(target_column='total', sql_expression=expression)], python_code=''), plan, target)


def test_python_transform_requires_defined_entrypoint():
    plan = MappingPlan(mappings=[Mapping(target_column='value', source_columns=['n'], mapping_type='DIRECT', status='APPROVED', reason='The numeric source measurement supplies the requested value')])
    target = [MetadataField(column_name='value', data_type='DOUBLE', required=True, description='Value')]
    with pytest.raises(ValueError, match='define transform'):
        build_transformation(TransformationPlan(language='python', expressions=[], python_code='print(1)'), plan, target)
    build = build_transformation(TransformationPlan(language='python', expressions=[], python_code='def transform(source):\n    return source.rename(columns={"n": "value"})\n'), plan, target)
    assert any(f.path == 'generated/transformation.py' for f in build.files)


@pytest.mark.parametrize('payload', [
    {'notes': ['Transformation is ready']},
    {'language': 'python', 'expressions': [], 'python_code': ''},
    {'language': 'python', 'expressions': [], 'python_code': ' \n\t '},
    {'language': 'python', 'expressions': [{'target_column': 'total', 'sql_expression': 'units * price'}],
     'python_code': 'def transform(source):\n    return source\n'},
    {'language': 'sql', 'expressions': [], 'python_code': 'def transform(source):\n    return source\n'},
])
def test_structured_transformation_rejects_nonexecutable_or_mixed_language_response(payload):
    # Reject these at the same schema boundary used by the local model client's
    # bounded repair loop, before a build can be marked ready for Docker.
    with pytest.raises(ValueError):
        TransformationPlan.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize('python_needed', [False, True])
def test_builder_uses_narrow_sql_contract_and_explicit_python_route(tmp_path, python_needed):
    calls = []

    class BuilderFixture:
        def structured(self, schema, system, prompt):
            payload = json.loads(prompt)
            calls.append((schema, payload))
            if schema is SqlTransformationPlan:
                if python_needed:
                    return SqlTransformationPlan(expressions=[], unsupported_targets=['total'])
                return SqlTransformationPlan(expressions=[TransformationExpression(target_column='total', sql_expression='units * unit_cost')])
            assert schema is PythonTransformationPlan
            return PythonTransformationPlan(python_code='import pandas as pd\ndef transform(source):\n    return pd.DataFrame({"product_id": source["sku"], "total": source["units"] * source["unit_cost"]}, index=source.index)\n')

    service = MigrationService(Settings(workspace_root=tmp_path), llm=BuilderFixture())
    source = [MetadataField(column_name=name, data_type='VARCHAR' if name == 'sku' else 'DOUBLE', required=True, description=name) for name in ['sku', 'units', 'unit_cost']]
    target = [MetadataField(column_name='product_id', data_type='VARCHAR', required=True, description='Product identifier'),
              MetadataField(column_name='total', data_type='DOUBLE', required=True, description='Inventory value')]
    approved = MappingPlan(mappings=[Mapping(target_column='product_id', source_columns=['sku'], mapping_type='DIRECT', status='APPROVED', reason='Stock identifier'),
        Mapping(target_column='total', source_columns=['units', 'unit_cost'], mapping_type='DERIVED', status='APPROVED', transformation_description='Multiply units by unit cost', reason='Inventory value is quantity times unit cost')])
    result = service._generate(tmp_path, approved, source, target)
    assert calls[0][0] is SqlTransformationPlan
    assert calls[0][1]['required_complex_targets'] == ['total']
    assert {field['column_name'] for field in calls[0][1]['source_metadata']} == {'units', 'unit_cost'}
    assert [mapping['target_column'] for mapping in calls[0][1]['approved_transformations']] == ['total']
    if python_needed:
        assert result.language == 'python' and result.python_code and result.expressions == []
        assert len(calls) == 2 and calls[1][0] is PythonTransformationPlan
        assert calls[1][1]['ordered_target_columns'] == ['product_id', 'total']
        assert {field['column_name'] for field in calls[1][1]['source_metadata']} == {'sku', 'units', 'unit_cost'}
    else:
        assert len(calls) == 1 and result.language == 'sql' and result.python_code == ''
        assert result.expressions[0].target_column == 'total'


def test_sql_only_model_contract_rejects_script_leak_and_empty_implementation():
    with pytest.raises(ValueError):
        SqlTransformationPlan.model_validate({'expressions': [], 'python_code': 'import pandas as pd'})
    with pytest.raises(ValueError, match='executable SQL'):
        SqlTransformationPlan(expressions=[])
    with pytest.raises(ValueError):
        PythonTransformationPlan(python_code=' \n ')


def test_actual_record_validation_counts_conversion_null_and_allowed_values():
    target = [MetadataField(column_name='quantity', data_type='INTEGER', required=True, description='Quantity').model_dump(),
              MetadataField(column_name='status', data_type='VARCHAR', required=True, description='Status', allowed_values=['OPEN']).model_dump()]
    mappings = [{'target_column': f['column_name'], 'mapping_type': 'DIRECT', 'status': 'APPROVED'} for f in target]
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE SCHEMA staging')
        con.execute('CREATE SCHEMA results')
        report = materialize_and_validate(con, pd.DataFrame({'quantity': ['1', 'bad', None, '4'], 'status': ['OPEN', 'OPEN', 'CLOSED', 'OPEN']}), target, mappings, 4)
        assert report['valid_records'] == 2 and report['invalid_records'] == 2
        assert report['field_validation'][0]['failed_conversions'] == 1
        assert report['field_validation'][0]['null_values'] == 2
        assert report['field_validation'][1]['invalid_allowed_values'] == 1
        assert con.execute('SELECT quantity FROM results.migrated_data').fetchall() == [(1,), (None,), (None,), (4,)]
        assert con.execute('SELECT count(*) FROM results.validation').fetchone()[0] == 4
        assert not report['complete']


def test_missing_required_field_cannot_be_complete():
    target = [MetadataField(column_name='tax_id', data_type='VARCHAR', required=True, description='Missing').model_dump()]
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE SCHEMA staging')
        con.execute('CREATE SCHEMA results')
        report = materialize_and_validate(con, pd.DataFrame({'tax_id': [None]}), target,
            [{'target_column': 'tax_id', 'mapping_type': 'MISSING', 'status': 'MISSING'}], 1)
    assert report['unresolved_required_fields'] == ['tax_id'] and report['invalid_records'] == 1
    assert not report['complete']


def test_host_recomputes_readiness_instead_of_trusting_json(migration):
    service, workspace, _ = migration
    approved = approve_all(service, workspace)
    _, target = service.metadata(workspace.name)
    with duckdb.connect(str(workspace / 'database/workspace.duckdb')) as con:
        report = materialize_and_validate(con, pd.DataFrame({'product_id': ['A01', 'B02'], 'units': ['12', 'invalid']}),
            [field.model_dump() for field in target], [mapping.model_dump() for mapping in approved.mappings], 2)
    report.update(complete=True, missing_fields=['invented'], unresolved_required_fields=['invented'], mapped_fields=999)
    atomic_json(workspace / 'results/validation.json', report)
    verified = service._verify_report(workspace, target, approved)
    assert not verified['complete'] and verified['invalid_records'] == 1
    assert verified['missing_fields'] == [] and verified['unresolved_required_fields'] == []
    assert verified['mapped_fields'] == 2


def test_host_detects_missing_required_validation_evidence(migration):
    service, workspace, _ = migration
    approved = approve_all(service, workspace)
    _, target = service.metadata(workspace.name)
    with duckdb.connect(str(workspace / 'database/workspace.duckdb')) as con:
        report = materialize_and_validate(con, pd.DataFrame({'product_id': ['A01', 'B02'], 'units': ['12', 'invalid']}),
            [field.model_dump() for field in target], [mapping.model_dump() for mapping in approved.mappings], 2)
        con.execute("DELETE FROM results.validation WHERE issue='REQUIRED_VALUE_MISSING'")
    atomic_json(workspace / 'results/validation.json', report)
    with pytest.raises(ValueError, match='required-value validation evidence'):
        service._verify_report(workspace, target, approved)


@pytest.mark.docker
def test_docker_nonpayroll_materialization_exports_and_connector(migration):
    service, workspace, model = migration
    approve_all(service, workspace)
    result = service.execute(workspace.name)
    assert result['source_record_count'] == result['target_record_count'] == 2
    assert result['valid_records'] == 1 and result['invalid_records'] == 1
    assert result['field_validation'][1]['failed_conversions'] == 1
    rows = list(csv.DictReader(io.StringIO((workspace / 'results/migrated_data.csv').read_text())))
    assert rows == [{'product_id': 'A01', 'units': '12'}, {'product_id': 'B02', 'units': ''}]
    assert (workspace / 'notebooks/migration.ipynb').is_file()
    with zipfile.ZipFile(workspace / 'results/connector.zip') as archive:
        assert {'connector.json', 'mapping.json', 'transformation.sql', 'solution.py', 'migration_spec.json', 'README.md'} <= set(archive.namelist())
        config = json.loads(archive.read('connector.json'))
        assert config['target_metadata'][0]['column_name'] == 'product_id'
        assert not any(name.endswith('.csv') for name in archive.namelist())
    assert model.calls == [MappingPlan]
