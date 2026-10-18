import datetime
import json

import duckdb
import pytest

from workbench.migration import (
    Mapping, MappingPlan, MetadataField, MigrationService, SqlTransformationPlan,
    TransformationExpression, TransformationPlan, build_transformation,
)
from workbench.migration_runtime import execute_sql_transformation, materialize_and_validate
from workbench.settings import Settings
from workbench.workspace import atomic_json, read_json


def date_plan():
    return MappingPlan(mappings=[Mapping(
        target_column='hire_date', source_columns=['joined_on'], mapping_type='TRANSFORMATION',
        status='APPROVED', transformation_description='Parse DD-MM-YYYY input as a date; flag invalid values.',
        reason='Approved correction to the source date format',
    )])


def build_date(expression, approved=None, target=None):
    target = target or [MetadataField(column_name='hire_date', data_type='DATE', required=False, description='Hire date')]
    build = build_transformation(TransformationPlan(language='sql', python_code='', expressions=[
        TransformationExpression(target_column='hire_date', sql_expression=expression),
    ]), approved or date_plan(), target)
    files = {file.path: file.content for file in build.files}
    return files['generated/transformation.sql'], json.loads(files['generated/migration_spec.json'])


@pytest.mark.parametrize('mask,value,expected', [
    ('DD-MM-YYYY', '14-08-2024', datetime.date(2024, 8, 14)),
    ('YYYY/MM/DD', '2024/08/14', datetime.date(2024, 8, 14)),
    ('MM/DD/YYYY', '08/14/2024', datetime.date(2024, 8, 14)),
    ('DD-MM-YYYY', '04-05-2024', datetime.date(2024, 5, 4)),
    ('%d-%m-%Y', '14-08-2024', datetime.date(2024, 8, 14)),
])
def test_known_date_masks_parse_without_reinterpreting_day_month_order(mask, value, expected):
    sql, spec = build_date(f"STRPTIME(joined_on, '{mask}')")
    with duckdb.connect(':memory:') as con:
        for schema in ('source', 'staging', 'results'):
            con.execute(f'CREATE SCHEMA {schema}')
        con.execute('CREATE TABLE source.data (joined_on VARCHAR)')
        con.executemany('INSERT INTO source.data VALUES (?)', [(value,), ('not-a-date',)])
        frame, errors = execute_sql_transformation(con, sql, ['hire_date'], spec['sql_expressions'])
        report = materialize_and_validate(con, frame, spec['target'], spec['mappings'], 2, errors)
        assert con.execute('SELECT hire_date FROM results.migrated_data').fetchall() == [(expected,), (None,)]
        assert report['valid_records'] == 1 and report['invalid_records'] == 1
        assert report['field_validation'][0]['failed_transformations'] == 1


@pytest.mark.parametrize('expression', [
    "STRPTIME(joined_on, 'unknown format')", "STRPTIME(joined_on, '%Q')",
    'MAX(joined_on)', 'LAG(joined_on) OVER ()',
])
def test_invalid_format_or_non_scalar_expression_requires_builder_repair(expression):
    with pytest.raises(ValueError):
        build_date(expression)


class DateModel:
    def __init__(self):
        self.payload = self.system = None

    def structured(self, schema, system, prompt):
        if schema is MappingPlan:
            return date_plan()
        assert schema is SqlTransformationPlan
        self.payload, self.system = json.loads(prompt), system
        return SqlTransformationPlan(expressions=[TransformationExpression(
            target_column='hire_date', sql_expression="STRPTIME(joined_on, 'DD-MM-YYYY')")])


def date_workspace(tmp_path):
    model = DateModel()
    service = MigrationService(Settings(workspace_root=tmp_path), llm=model)
    workspace = service.create('Date migration', b'joined_on\n14-08-2024\nnot-a-date\n',
        b'column_name,data_type,required,description,transformation_hint\nhire_date,DATE,false,Hire date,Parse ISO YYYY-MM-DD date\n')
    proposal = service.propose(workspace.name)
    proposal.mappings[0].status = 'APPROVED'
    return service, workspace, proposal, model


def test_builder_receives_approved_correction_and_actual_input_type(tmp_path):
    service, workspace, approved, model = date_workspace(tmp_path)
    service.approve(workspace.name, approved)
    source, target = service.metadata(workspace.name)
    service._generate(workspace, approved, source, target)
    assert model.payload['approved_transformations'][0]['transformation_description'].startswith('Parse DD-MM-YYYY')
    assert model.payload['source_runtime_types'] == {'joined_on': 'VARCHAR'}
    assert model.payload['source_profile']['row_count'] == 2
    assert 'including corrections to metadata hints' in model.system


@pytest.mark.parametrize('decision', [{'mapping_type': 'MISSING'}, {'status': 'MISSING'}])
def test_approval_missing_choice_clears_stale_source_fields(tmp_path, decision):
    service, workspace, proposal, _ = date_workspace(tmp_path)
    row = {**proposal.mappings[0].model_dump(), **decision}
    result = service.approve(workspace.name, [row])
    mapping = result['plan']['mappings'][0]
    assert mapping['source_columns'] == []
    assert mapping['status'] == mapping['mapping_type'] == 'MISSING'


def test_host_verifies_optional_transformation_errors_and_rejects_lost_evidence(tmp_path):
    service, workspace, approved, _ = date_workspace(tmp_path)
    service.approve(workspace.name, approved)
    _, target = service.metadata(workspace.name)
    sql, spec = build_date("STRPTIME(joined_on, 'DD-MM-YYYY')", approved, target)
    with duckdb.connect(str(workspace / 'database/workspace.duckdb')) as con:
        frame, errors = execute_sql_transformation(con, sql, ['hire_date'], spec['sql_expressions'])
        report = materialize_and_validate(con, frame, spec['target'], spec['mappings'], 2, errors)
    atomic_json(workspace / 'results/validation.json', report)
    verified = service._verify_report(workspace, target, approved)
    assert not verified['complete'] and verified['invalid_records'] == 1
    assert verified['field_validation'][0]['failed_transformations'] == 1
    with duckdb.connect(str(workspace / 'database/workspace.duckdb')) as con:
        con.execute("DELETE FROM results.validation WHERE issue='TRANSFORMATION_FAILED'")
    altered = read_json(workspace / 'results/validation.json')
    altered.update(invalid_records=0, valid_records=2)
    atomic_json(workspace / 'results/validation.json', altered)
    with pytest.raises(ValueError, match='Transformation failure validation evidence'):
        service._verify_report(workspace, target, approved)
