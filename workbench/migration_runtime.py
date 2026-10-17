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


def materialize_and_validate(con, frame, target, mappings, source_count):
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
    field_reports = []
    for field in target:
        name, dtype = field['column_name'], field['data_type']
        col = quote(name)
        conversion = f'{col} IS NOT NULL AND TRY_CAST({col} AS {dtype}) IS NULL'
        con.execute(f'INSERT INTO results.validation SELECT __record_number, ?, ?, ? FROM staging.migration_values WHERE {conversion}', [name, 'FAILED_CONVERSION', f'Value cannot be converted to {dtype}'])
        conversion_count = con.execute(f'SELECT count(*) FROM staging.migration_values WHERE {conversion}').fetchone()[0]
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
        field_reports.append({'column_name': name, 'data_type': dtype, 'required': field['required'], 'null_values': null_count, 'failed_conversions': conversion_count, 'invalid_allowed_values': invalid_allowed})
    invalid = con.execute('SELECT count(DISTINCT record_number) FROM results.validation').fetchone()[0]
    issues = con.execute('SELECT record_number, target_column, issue, detail FROM results.validation ORDER BY record_number, target_column LIMIT 500').fetchall()
    missing = [m['target_column'] for m in mappings if m['status'] != 'APPROVED' or m['mapping_type'] in {'MISSING', 'AMBIGUOUS'}]
    required_missing = [f['column_name'] for f in target if f['required'] and f['column_name'] in missing]
    return {'source_record_count': source_count, 'target_record_count': len(frame), 'valid_records': source_count - invalid, 'invalid_records': invalid,
            'mapped_fields': len(target) - len(missing), 'missing_fields': missing, 'unresolved_required_fields': required_missing,
            'complete': not required_missing and invalid == 0, 'field_validation': field_reports,
            'issue_count': con.execute('SELECT count(*) FROM results.validation').fetchone()[0],
            'issues_preview': [dict(zip(['record_number', 'target_column', 'issue', 'detail'], row)) for row in issues],
            'issues_preview_limit': 500, 'record_count_preserved': True,
            'scope': 'Computed data/type/required-value validation; business semantics still require human review.'}


def main():
    if not Path('/.dockerenv').exists():
        raise RuntimeError('Migration transformations must run through the Docker sandbox')
    root = Path('/workspace')
    spec = json.loads((root / 'generated/migration_spec.json').read_text(encoding='utf-8'))
    con = duckdb.connect(str(root / 'database/workspace.duckdb'), config={'enable_external_access': False, 'autoload_known_extensions': False, 'autoinstall_known_extensions': False, 'threads': 2, 'memory_limit': '1GB'})
    try:
        source = con.execute('SELECT * FROM source.data ORDER BY rowid').fetchdf()
        target, mappings = spec['target'], spec['mappings']
        if spec['language'] == 'sql':
            sql = (root / 'generated/transformation.sql').read_text(encoding='utf-8')
            statements = con.extract_statements(sql)
            if len(statements) != 1 or str(statements[0].type).split('.')[-1] != 'SELECT':
                raise ValueError('Transformation SQL must be one SELECT statement')
            frame = con.execute(sql).fetchdf()
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
        if len(frame) != len(source):
            raise ValueError('Migration transformations must preserve source row count')
        # A missing/rejected field can never acquire invented values from code.
        # Direct mappings are enforced from the approved source, in source order.
        for mapping in mappings:
            name = mapping['target_column']
            if mapping['status'] != 'APPROVED' or mapping['mapping_type'] in {'MISSING', 'AMBIGUOUS'}:
                frame[name] = None
            elif mapping['mapping_type'] == 'DIRECT':
                frame[name] = source[mapping['source_columns'][0]].values
        frame = frame[[field['column_name'] for field in target]]
        report = materialize_and_validate(con, frame, target, mappings, len(source))
        result = root / 'results'
        result.mkdir(exist_ok=True)
        with (result / 'migrated_data.csv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            cursor = con.execute('SELECT * FROM results.migrated_data')
            writer.writerow([c[0] for c in cursor.description])
            while rows := cursor.fetchmany(4096):
                writer.writerows(rows)
        (result / 'validation.json').write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
        print(json.dumps(report, indent=2))
    finally:
        con.close()


if __name__ == '__main__':
    main()
