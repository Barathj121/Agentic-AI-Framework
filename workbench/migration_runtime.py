"""Trusted migration executor copied into the existing Docker sandbox.

Only ``main`` executes transformations. It refuses to run outside Docker. This
file contains no model-generated instructions; those are read as data/code from
the approved workspace and are executed exclusively inside that container.
"""
from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import duckdb
import pandas as pd


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def enforce_approved_mappings(frame, source, target, mappings):
    """Supply every target field without inventing values for unresolved maps."""
    if not isinstance(frame, pd.DataFrame) or len(frame) != len(source):
        raise ValueError('Migration transformations must preserve source row count')
    by_target = {mapping['target_column']: mapping for mapping in mappings}
    names = [field['column_name'] for field in target]
    frame = frame.copy()
    for name in names:
        mapping = by_target.get(name)
        if not mapping or mapping['status'] != 'APPROVED' or mapping['mapping_type'] in {'MISSING', 'AMBIGUOUS'}:
            frame[name] = None
        elif mapping['mapping_type'] == 'DIRECT':
            frame[name] = source[mapping['source_columns'][0]].values
        elif name not in frame.columns:
            raise ValueError(f'Approved transformation did not return target column: {name}')
    return frame[names]


def execute_sql_transformation(con, sql, target_names, expressions):
    """Keep recoverable value errors local to their target field and record.

    Valid input takes the normal single-query path. If a conversion fails, use
    the builder's validated scalar expressions to isolate the offending values.
    SQL syntax, binding and infrastructure failures still reach the repair loop.
    No alternate date format or replacement business value is guessed here.
    """
    value_errors = (duckdb.ConversionException, duckdb.InvalidInputException)
    try:
        return con.execute(sql).fetchdf(), []
    except value_errors as original_error:
        if set(expressions) != set(target_names):
            raise
        row_ids = [row[0] for row in con.execute('SELECT rowid FROM source.data ORDER BY rowid').fetchall()]
        columns, errors = {}, []
        for name in target_names:
            expression = expressions[name]
            query = f'SELECT ({expression}) AS {quote(name)} FROM source.data'
            try:
                columns[name] = con.execute(query + ' ORDER BY rowid').fetchdf()[name]
            except value_errors:
                values = []
                for record_number, row_id in enumerate(row_ids, start=1):
                    try:
                        value = con.execute(query + ' WHERE rowid = ?', [row_id]).fetchone()[0]
                    except value_errors as exc:
                        value = None
                        errors.append({'record_number': record_number, 'target_column': name,
                                       'detail': f'{type(exc).__name__}: {exc}'[:2000]})
                    values.append(value)
                # Nullable integer inference would otherwise use float64 and
                # round valid identifiers greater than 2**53 after one failure.
                columns[name] = pd.Series(values, dtype=object)
        if not errors:
            # A failure that cannot be reproduced per field must not become a
            # successful migration merely because its SELECT was decomposed.
            raise original_error
        return pd.DataFrame(columns, columns=target_names), errors


def materialize_and_validate(con, frame, target, mappings, source_count, transformation_errors=None):
    """Deterministic casting and record-level validation of an actual result."""
    names = [field['column_name'] for field in target]
    if len(frame) != source_count:
        raise ValueError(f'Transformation changed record count: {source_count} -> {len(frame)}')
    if list(frame.columns) != names:
        raise ValueError('Transformation must return exactly the ordered target columns')
    con.register('_migration_frame', frame)
    con.execute('CREATE OR REPLACE TABLE staging.migration_values AS SELECT row_number() OVER () AS __record_number, * FROM _migration_frame')
    expressions = [f'TRY_CAST({quote(f["column_name"])} AS {f["data_type"]}) AS {quote(f["column_name"])}' for f in target]
    con.execute('CREATE OR REPLACE TABLE results.migrated_data AS SELECT ' + ', '.join(expressions) + ' FROM staging.migration_values ORDER BY __record_number')
    con.execute('CREATE OR REPLACE TABLE results.validation (record_number BIGINT, target_column VARCHAR, issue VARCHAR, detail VARCHAR)')
    con.execute('CREATE OR REPLACE TABLE staging.transformation_errors (record_number BIGINT, target_column VARCHAR, detail VARCHAR)')
    seen_errors = set()
    for error in transformation_errors or []:
        key = (error['record_number'], error['target_column'])
        if key[0] < 1 or key[0] > source_count or key[1] not in names or key in seen_errors:
            raise ValueError('Transformation failure evidence must identify a unique source record and target field')
        seen_errors.add(key)
        con.execute('INSERT INTO staging.transformation_errors VALUES (?, ?, ?)', [*key, error['detail']])
    con.execute("INSERT INTO results.validation SELECT record_number, target_column, 'TRANSFORMATION_FAILED', detail FROM staging.transformation_errors")
    field_reports = []
    for field in target:
        name, dtype = field['column_name'], field['data_type']
        col = quote(name)
        conversion = f'{col} IS NOT NULL AND TRY_CAST({col} AS {dtype}) IS NULL'
        con.execute(f'INSERT INTO results.validation SELECT __record_number, ?, ?, ? FROM staging.migration_values WHERE {conversion}', [name, 'FAILED_CONVERSION', f'Value cannot be converted to {dtype}'])
        conversion_count = con.execute(f'SELECT count(*) FROM staging.migration_values WHERE {conversion}').fetchone()[0]
        transformation_count = con.execute('SELECT count(*) FROM staging.transformation_errors WHERE target_column = ?', [name]).fetchone()[0]
        null_count = con.execute(f'SELECT count(*) FROM results.migrated_data WHERE {col} IS NULL').fetchone()[0]
        if field['required']:
            con.execute(f'INSERT INTO results.validation SELECT __record_number, ?, ?, ? FROM staging.migration_values WHERE TRY_CAST({col} AS {dtype}) IS NULL', [name, 'REQUIRED_VALUE_MISSING', 'Required target value is null'])
        allowed = field.get('allowed_values') or []
        invalid_allowed = 0
        if allowed:
            placeholders = ','.join(f'TRY_CAST(? AS {dtype})' for _ in allowed)
            condition = f'TRY_CAST({col} AS {dtype}) IS NOT NULL AND TRY_CAST({col} AS {dtype}) NOT IN ({placeholders})'
            con.execute(f'INSERT INTO results.validation SELECT __record_number, ?, ?, ? FROM staging.migration_values WHERE {condition}', [name, 'INVALID_ALLOWED_VALUE', 'Value is outside target allowed_values', *allowed])
            invalid_allowed = con.execute(f'SELECT count(*) FROM staging.migration_values WHERE {condition}', allowed).fetchone()[0]
        field_reports.append({'column_name': name, 'data_type': dtype, 'required': field['required'], 'null_values': null_count, 'failed_conversions': conversion_count, 'failed_transformations': transformation_count, 'invalid_allowed_values': invalid_allowed})
    invalid = con.execute('SELECT count(DISTINCT record_number) FROM results.validation').fetchone()[0]
    issues = con.execute('SELECT record_number, target_column, issue, detail FROM results.validation ORDER BY record_number, target_column LIMIT 500').fetchall()
    by_target = {mapping['target_column']: mapping for mapping in mappings}
    missing = [name for name in names if name not in by_target or by_target[name]['status'] != 'APPROVED' or by_target[name]['mapping_type'] in {'MISSING', 'AMBIGUOUS'}]
    required_missing = [f['column_name'] for f in target if f['required'] and f['column_name'] in missing]
    return {'source_record_count': source_count, 'target_record_count': len(frame), 'valid_records': source_count - invalid, 'invalid_records': invalid,
            'mapped_fields': len(target) - len(missing), 'missing_fields': missing, 'unresolved_required_fields': required_missing,
            'complete': not required_missing and invalid == 0, 'field_validation': field_reports,
            'issue_count': con.execute('SELECT count(*) FROM results.validation').fetchone()[0],
            'issues_preview': [dict(zip(['record_number', 'target_column', 'issue', 'detail'], row)) for row in issues],
            'issues_preview_limit': 500, 'record_count_preserved': True,
            'scope': 'Computed data/type/required-value validation; business semantics still require human review.'}


def export_results(con, result, report):
    """Export the materialized target schema even when validation is incomplete."""
    result.mkdir(exist_ok=True)
    with (result / 'migrated_data.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        cursor = con.execute('SELECT * FROM results.migrated_data')
        writer.writerow([column[0] for column in cursor.description])
        while rows := cursor.fetchmany(4096):
            writer.writerows(rows)
    (result / 'validation.json').write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')


def main():
    if not Path('/.dockerenv').exists():
        raise RuntimeError('Migration transformations must run through the Docker sandbox')
    root = Path('/workspace')
    spec = json.loads((root / 'generated/migration_spec.json').read_text(encoding='utf-8'))
    con = duckdb.connect(str(root / 'database/workspace.duckdb'), config={'enable_external_access': False, 'autoload_known_extensions': False, 'autoinstall_known_extensions': False, 'threads': 2, 'memory_limit': '1GB'})
    try:
        source = con.execute('SELECT * FROM source.data ORDER BY rowid').fetchdf()
        target, mappings = spec['target'], spec['mappings']
        transformation_errors = []
        needs_transformation = any(mapping['status'] == 'APPROVED' and mapping['mapping_type'] in {'TRANSFORMATION', 'DERIVED'} for mapping in mappings)
        if not needs_transformation:
            # Direct and missing fields are completely determined by the
            # approved mapping; no generated code needs to supply these rows.
            frame = pd.DataFrame(index=source.index)
        elif spec['language'] == 'sql':
            sql = (root / 'generated/transformation.sql').read_text(encoding='utf-8')
            statements = con.extract_statements(sql)
            if len(statements) != 1 or str(statements[0].type).split('.')[-1] != 'SELECT':
                raise ValueError('Transformation SQL must be one SELECT statement')
            frame, transformation_errors = execute_sql_transformation(con, sql, [field['column_name'] for field in target], spec.get('sql_expressions', {}))
        else:
            # A preserved non-RangeIndex detects accidental sorting followed by
            # reset_index(drop=True), which would otherwise silently misalign
            # derived values with the direct identifiers enforced below.
            source.index = pd.Index([f'source_record_{i}' for i in range(len(source))], name='migration_record_identity')
            module_spec = importlib.util.spec_from_file_location('approved_migration', root / 'generated/transformation.py')
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
            frame = module.transform(source.copy(deep=True))
            if not isinstance(frame, pd.DataFrame) or not frame.index.equals(source.index):
                raise ValueError('Python transform must return a DataFrame with unchanged row index')
        # A missing/rejected field can never acquire invented values from code.
        # Direct mappings are enforced from the approved source, in source order.
        frame = enforce_approved_mappings(frame, source, target, mappings)
        report = materialize_and_validate(con, frame, target, mappings, len(source), transformation_errors)
        export_results(con, root / 'results', report)
        print(json.dumps(report, indent=2))
    finally:
        con.close()


if __name__ == '__main__':
    main()
