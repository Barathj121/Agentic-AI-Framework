import csv
import datetime
import json

import duckdb
import pandas as pd
import pytest

from workbench.migration_runtime import enforce_approved_mappings, execute_sql_transformation, export_results, materialize_and_validate


@pytest.fixture
def connection():
    with duckdb.connect(':memory:') as con:
        for schema in ('source', 'staging', 'results'):
            con.execute(f'CREATE SCHEMA {schema}')
        yield con


def execute(con, expressions):
    sql = 'SELECT ' + ', '.join(f'({expression}) AS "{name}"' for name, expression in expressions.items())
    return execute_sql_transformation(con, sql + ' FROM source.data ORDER BY rowid', list(expressions), expressions)


def test_bad_date_isolated_without_losing_good_values_or_record_identity(connection):
    con = connection
    con.execute("CREATE TABLE source.data AS SELECT * FROM (VALUES ('E001', '14-08-2024', '2'), ('removed', '01-01-2024', '0'), ('E003', 'not-a-date', '3'), ('E004', '16-08-2024', 'invalid')) AS v(worker, joined_on, units)")
    con.execute("DELETE FROM source.data WHERE worker = 'removed'")
    expressions = {'employee_id': 'worker', 'hire_date': "CAST(STRPTIME(joined_on, '%d-%m-%Y') AS DATE)",
                   'quantity': 'CAST(units AS INTEGER)', 'missing': 'NULL'}
    frame, errors = execute(con, expressions)
    assert frame['employee_id'].tolist() == ['E001', 'E003', 'E004']
    assert frame['hire_date'].iloc[0] == datetime.date(2024, 8, 14)
    assert pd.isna(frame['hire_date'].iloc[1])
    assert frame['hire_date'].iloc[2] == datetime.date(2024, 8, 16)
    assert frame['quantity'].iloc[:2].tolist() == [2, 3]
    assert pd.isna(frame['quantity'].iloc[2])
    assert frame['missing'].isna().all()
    assert [(e['record_number'], e['target_column']) for e in errors] == [(2, 'hire_date'), (3, 'quantity')]


def test_optional_transformation_failure_is_invalid_and_explained(connection):
    con = connection
    con.execute("CREATE TABLE source.data AS SELECT * FROM (VALUES ('14-08-2024'), ('not-a-date'), (NULL)) AS v(joined_on)")
    frame, errors = execute(con, {'hire_date': "CAST(STRPTIME(joined_on, '%d-%m-%Y') AS DATE)"})
    target = [{'column_name': 'hire_date', 'data_type': 'DATE', 'required': False}]
    mappings = [{'target_column': 'hire_date', 'mapping_type': 'TRANSFORMATION', 'status': 'APPROVED'}]
    report = materialize_and_validate(con, frame, target, mappings, 3, errors)
    assert report['valid_records'] == 2 and report['invalid_records'] == 1
    assert report['field_validation'][0]['failed_transformations'] == 1
    assert report['field_validation'][0]['failed_conversions'] == 0
    assert report['issues_preview'][0]['issue'] == 'TRANSFORMATION_FAILED'
    assert report['issues_preview'][0]['record_number'] == 2
    assert 'not-a-date' in report['issues_preview'][0]['detail']
    assert not report['complete']
    assert con.execute('SELECT hire_date FROM results.migrated_data').fetchall() == [(datetime.date(2024, 8, 14),), (None,), (None,)]


def test_recovery_preserves_large_integer_precision(connection):
    connection.execute("CREATE TABLE source.data AS SELECT * FROM (VALUES ('9007199254740993'), ('invalid')) AS v(value)")
    frame, errors = execute(connection, {'value': 'CAST(value AS BIGINT)'})
    assert frame['value'].iloc[0] == 9007199254740993
    target = [{'column_name': 'value', 'data_type': 'BIGINT', 'required': False}]
    mappings = [{'target_column': 'value', 'mapping_type': 'TRANSFORMATION', 'status': 'APPROVED'}]
    materialize_and_validate(connection, frame, target, mappings, 2, errors)
    assert connection.execute('SELECT value FROM results.migrated_data').fetchall() == [(9007199254740993,), (None,)]


def test_required_missing_mapping_can_materialize_with_transformation_errors(connection):
    con = connection
    con.execute("CREATE TABLE source.data AS SELECT 'not-a-date' AS joined_on")
    frame, errors = execute(con, {'hire_date': "STRPTIME(joined_on, '%d-%m-%Y')", 'tax_id': 'NULL'})
    target = [{'column_name': 'hire_date', 'data_type': 'DATE', 'required': True},
              {'column_name': 'tax_id', 'data_type': 'VARCHAR', 'required': True}]
    mappings = [{'target_column': 'hire_date', 'mapping_type': 'TRANSFORMATION', 'status': 'APPROVED'},
                {'target_column': 'tax_id', 'mapping_type': 'MISSING', 'status': 'MISSING'}]
    report = materialize_and_validate(con, frame, target, mappings, 1, errors)
    assert report['target_record_count'] == 1
    assert report['unresolved_required_fields'] == ['tax_id']
    assert report['invalid_records'] == 1 and report['issue_count'] == 3
    assert not report['complete']


def test_intentional_null_transformation_does_not_become_an_error(connection):
    con = connection
    con.execute("CREATE TABLE source.data AS SELECT 'withheld' AS value")
    frame, errors = execute(con, {'value': "CASE WHEN value = 'withheld' THEN NULL ELSE value END"})
    report = materialize_and_validate(con, frame, [{'column_name': 'value', 'data_type': 'VARCHAR', 'required': False}],
                                     [{'target_column': 'value', 'mapping_type': 'TRANSFORMATION', 'status': 'APPROVED'}], 1, errors)
    assert errors == [] and report['complete']


@pytest.mark.parametrize('expression,exception', [('unknown_column', duckdb.BinderException), ('CAST(', duckdb.ParserException)])
def test_invalid_sql_still_raises_for_repair(connection, expression, exception):
    connection.execute('CREATE TABLE source.data AS SELECT 1 AS value')
    with pytest.raises(exception):
        execute(connection, {'value': expression})


def test_legacy_spec_cannot_silently_recover_without_expression_evidence(connection):
    connection.execute("CREATE TABLE source.data AS SELECT 'bad' AS value")
    with pytest.raises(duckdb.ConversionException):
        execute_sql_transformation(connection, 'SELECT CAST(value AS INTEGER) AS value FROM source.data', ['value'], {})


def test_previous_transformation_error_evidence_is_cleared(connection):
    target = [{'column_name': 'value', 'data_type': 'INTEGER', 'required': False}]
    mappings = [{'target_column': 'value', 'mapping_type': 'TRANSFORMATION', 'status': 'APPROVED'}]
    materialize_and_validate(connection, pd.DataFrame({'value': [None]}), target, mappings, 1,
                             [{'record_number': 1, 'target_column': 'value', 'detail': 'Invalid input'}])
    report = materialize_and_validate(connection, pd.DataFrame({'value': [7]}), target, mappings, 1)
    assert report['complete'] and report['issue_count'] == 0
    assert connection.execute('SELECT count(*) FROM staging.transformation_errors').fetchone()[0] == 0


@pytest.mark.parametrize('language', ['sql', 'python'])
@pytest.mark.parametrize('all_missing', [False, True])
def test_missing_targets_export_with_all_headers_types_and_source_rows(connection, tmp_path, language, all_missing):
    con = connection
    con.execute("CREATE TABLE source.data AS SELECT * FROM (VALUES ('E001', 100), ('E002', 200)) AS v(worker_id, monthly_pay)")
    source = con.execute('SELECT * FROM source.data ORDER BY rowid').fetchdf()
    target = [{'column_name': name, 'data_type': dtype, 'required': required} for name, dtype, required in [
        ('employee_id', 'VARCHAR', True), ('annual_total', 'DECIMAL(18,2)', True),
        ('department_number', 'INTEGER', True), ('is_active', 'BOOLEAN', False),
        ('tax_id', 'VARCHAR', True), ('started_at', 'TIMESTAMP', False)]]
    mappings = [
        {'target_column': 'employee_id', 'mapping_type': 'DIRECT', 'status': 'APPROVED', 'source_columns': ['worker_id']},
        {'target_column': 'annual_total', 'mapping_type': 'DERIVED', 'status': 'APPROVED'},
        {'target_column': 'department_number', 'mapping_type': 'MISSING', 'status': 'APPROVED'},
        {'target_column': 'is_active', 'mapping_type': 'AMBIGUOUS', 'status': 'APPROVED'},
        {'target_column': 'tax_id', 'mapping_type': 'DIRECT', 'status': 'REJECTED', 'source_columns': ['worker_id']},
        # A legacy/incomplete plan omits started_at entirely. It must still
        # exist as a typed NULL target, and be reported as an unresolved map.
    ]
    if all_missing:
        for mapping in mappings:
            mapping.update(mapping_type='MISSING', status='APPROVED')
    if language == 'sql':
        expressions = {'unused_model_output': 'monthly_pay'} if all_missing else {'annual_total': 'monthly_pay * 12'}
        frame, errors = execute(con, expressions)
    else:
        source.index = pd.Index(['source_record_0', 'source_record_1'], name='migration_record_identity')

        def fixture_transform(frame):
            result = pd.DataFrame(index=frame.index)
            if not all_missing:
                result['annual_total'] = frame['monthly_pay'] * 12
            # A generated frame may omit every missing/direct target; its
            # proposed value for a missing field must never reach the export.
            result['department_number'] = 999
            return result

        frame, errors = fixture_transform(source.copy()), []
        assert frame.index.equals(source.index)
    frame = enforce_approved_mappings(frame, source, target, mappings)
    report = materialize_and_validate(con, frame, target, mappings, len(source), errors)
    export_results(con, tmp_path / 'results', report)

    expected_names = [field['column_name'] for field in target]
    assert list(frame.columns) == expected_names
    assert con.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='results' AND table_name='migrated_data' ORDER BY ordinal_position").fetchall() == [(field['column_name'], field['data_type']) for field in target]
    with (tmp_path / 'results/migrated_data.csv').open(newline='') as stream:
        rows = list(csv.reader(stream))
    assert rows[0] == expected_names
    assert rows[1:] == ([[''] * len(target)] * 2 if all_missing else [
        ['E001', '1200.00', '', '', '', ''], ['E002', '2400.00', '', '', '', '']])
    exported_report = json.loads((tmp_path / 'results/validation.json').read_text())
    assert exported_report['target_record_count'] == exported_report['source_record_count'] == 2
    assert exported_report['missing_fields'] == (expected_names if all_missing else expected_names[2:])
    assert exported_report['mapped_fields'] == (0 if all_missing else 2)
    assert exported_report['invalid_records'] == 2 and not exported_report['complete']


def test_omitted_approved_transformation_still_requires_repair():
    source = pd.DataFrame({'monthly_pay': [100, 200]})
    with pytest.raises(ValueError, match='Approved transformation did not return target column: annual_total'):
        enforce_approved_mappings(pd.DataFrame(index=source.index), source,
                                  [{'column_name': 'annual_total', 'data_type': 'DECIMAL(18,2)', 'required': True}],
                                  [{'target_column': 'annual_total', 'mapping_type': 'DERIVED', 'status': 'APPROVED'}])


def test_missing_targets_do_not_hide_a_changed_source_row_count():
    with pytest.raises(ValueError, match='preserve source row count'):
        enforce_approved_mappings(pd.DataFrame(), pd.DataFrame({'id': [1, 2]}),
                                  [{'column_name': 'unmapped', 'data_type': 'VARCHAR', 'required': False}], [])
