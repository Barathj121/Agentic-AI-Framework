"""Native Streamlit interface for the shared Migration Studio backend."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from workbench.migration import DEMO_PROMPT, Mapping, MappingPlan, MigrationService, fit_gap
from workbench.workspace import read_json, safe_path


def _preview(value):
    st.dataframe(pd.DataFrame(value['rows'], columns=value['columns']), width='stretch', hide_index=True)
    if value.get('truncated'):
        st.caption('Preview limited to 100 rows. Download the CSV for all records.')


def _fit_gap(summary):
    columns = st.columns(4)
    columns[0].metric('Target fields', summary['total_target_fields'])
    columns[1].metric('Direct', summary['direct_mappings'])
    columns[2].metric('Transformable / derived', summary['transformable_mappings'] + summary['derived_mappings'])
    columns[3].metric('Missing / unresolved', len(summary['missing_fields']))
    if summary['unresolved_required_fields']:
        st.warning('Required fields still unresolved: ' + ', '.join(summary['unresolved_required_fields']) + '. A partial preview is allowed; these values remain null.')
    st.caption(f"{len(summary['fields_requiring_review'])} fields need review. Confidence labels are qualitative model suggestions, not measured probabilities.")


def _review(service, workspace, plan, target):
    st.subheader('Review mappings')
    _fit_gap(fit_gap(plan, target))
    for observation in plan.observations:
        st.write(observation)
    st.caption('Edit source fields as a JSON list, e.g. ["monthly_salary"]. Change ambiguous mappings to a resolved mapping type or mark them missing. Table edits are drafts until you approve below.')
    rows = []
    for mapping in plan.mappings:
        rows.append({'Target Field': mapping.target_column, 'Source Field(s)': json.dumps(mapping.source_columns),
                     'Mapping Type': mapping.mapping_type, 'Transformation': mapping.transformation_description,
                     'Confidence': mapping.confidence, 'Reason': mapping.reason,
                     'Status': 'MISSING' if mapping.mapping_type == 'MISSING' else 'PENDING' if mapping.mapping_type == 'AMBIGUOUS' else 'APPROVED'})
    with st.form('mapping_review_' + workspace.name):
        edited = st.data_editor(pd.DataFrame(rows), width='stretch', hide_index=True, num_rows='fixed',
            disabled=['Target Field', 'Confidence', 'Reason'], column_config={
                'Mapping Type': st.column_config.SelectboxColumn(options=['DIRECT', 'TRANSFORMATION', 'DERIVED', 'AMBIGUOUS', 'MISSING'], required=True),
                'Status': st.column_config.SelectboxColumn(options=['APPROVED', 'REJECTED', 'MISSING', 'PENDING'], required=True),
            })
        confirm = st.checkbox('I reviewed these mappings and approve their use. Unresolved fields may appear as null in a partial preview.')
        approved = st.form_submit_button('Approve reviewed mapping plan', type='primary')
    if approved:
        if not confirm:
            st.error('Review the mapping table and confirm approval before continuing.')
            return
        mappings = []
        for row in edited.to_dict('records'):
            try:
                source_columns = json.loads(row['Source Field(s)'])
            except json.JSONDecodeError as exc:
                raise ValueError(f"{row['Target Field']}: source fields must be a JSON list") from exc
            if not isinstance(source_columns, list) or any(not isinstance(value, str) for value in source_columns):
                raise ValueError('Source fields must be a JSON list of column names')
            if row['Status'] == 'MISSING' or row['Mapping Type'] == 'MISSING':
                source_columns = []
            mappings.append(Mapping(target_column=row['Target Field'], source_columns=source_columns,
                mapping_type='MISSING' if row['Status'] == 'MISSING' else row['Mapping Type'],
                transformation_description=row['Transformation'] or '', confidence=row['Confidence'], reason=row['Reason'],
                requires_review=False, status=row['Status']).model_dump())
        service.approve(workspace.name, mappings)
        st.rerun()


def _results(service, workspace, report):
    st.subheader('Migration results')
    if report['complete']:
        st.success('Migration passed the configured data checks. Review business semantics before using the export.')
    else:
        st.warning('Incomplete migration — actual target data is available as a partial preview. Review missing required fields and invalid records before reuse.')
    columns = st.columns(4)
    for column, label, key in zip(columns, ['Source records', 'Target records', 'Valid records', 'Invalid records'], ['source_record_count', 'target_record_count', 'valid_records', 'invalid_records']):
        column.metric(label, report[key])
    st.caption(f"Mapped fields: {report['mapped_fields']}. Missing/unresolved fields: {', '.join(report['missing_fields']) or 'none'}.")
    tabs = st.tabs(['Target data', 'Mapping report', 'Validation report', 'Artifacts and export'])
    with tabs[0]:
        _preview(service.preview(workspace.name))
        st.caption('Generic target-schema export. No Oracle HCM or other vendor import compatibility is claimed.')
    with tabs[1]:
        approval = read_json(safe_path(workspace, 'metadata/mapping_approval.json'))
        st.dataframe(approval['plan']['mappings'], width='stretch', hide_index=True)
    with tabs[2]:
        st.dataframe(report['field_validation'], width='stretch', hide_index=True)
        if report.get('issues_preview'):
            st.dataframe(report['issues_preview'], width='stretch', hide_index=True)
            st.caption(f"Showing at most {report['issues_preview_limit']} of {report['issue_count']} validation issues. All issues are persisted in results.validation.")
        else:
            st.info('No record-level issues were found by the configured checks.')
        with st.expander('Full validation summary'):
            st.json(report)
    with tabs[3]:
        files = [('results/migrated_data.csv', 'Download target CSV', 'text/csv'),
                 ('generated/approved_mapping.json', 'Download approved mapping JSON', 'application/json'),
                 ('results/validation.json', 'Download validation report', 'application/json'),
                 ('results/connector.zip', 'Download connector ZIP', 'application/zip'),
                 ('generated/transformation.sql', 'Download transformation SQL', 'text/plain'),
                 ('generated/transformation.py', 'Download transformation Python', 'text/plain'),
                 ('notebooks/migration.ipynb', 'Download migration notebook', 'application/x-ipynb+json')]
        for relative, label, mime in files:
            path = safe_path(workspace, relative)
            if path.is_file():
                st.download_button(label, data=path.read_bytes(), file_name=path.name, mime=mime, key=workspace.name + relative)
        for relative in ('generated/transformation.sql', 'generated/transformation.py', 'generated/solution.py'):
            path = safe_path(workspace, relative)
            if path.is_file():
                with st.expander('Inspect ' + relative):
                    st.code(path.read_text(encoding='utf-8'), language='sql' if path.suffix == '.sql' else 'python')
        for path in sorted(safe_path(workspace, 'logs').glob('*.log')):
            with st.expander(path.name):
                st.text(path.read_text(encoding='utf-8', errors='replace')[-15000:])
                st.download_button('Download ' + path.name, path.read_bytes(), file_name=path.name, key=workspace.name + path.name)


def render_migration(settings, manager=None):
    """Render in the existing app; session selection is migration_workspace_id."""
    service = MigrationService(settings)
    manager = manager or service.manager
    st.title('Migration Studio')
    st.write('Assess source-to-target fit, review mappings, and execute approved transformations in Docker.')
    workspaces = [w for w in manager.list() if w.get('kind') == 'migration' or w.get('application') == 'migration_studio']
    choices = ['New migration', *[w['id'] for w in workspaces]]
    labels = {w['id']: f"{w['name']} · {w['status']}" for w in workspaces}
    selected = st.session_state.get('migration_workspace_id')
    choice = st.selectbox('Migration workspace', choices, index=choices.index(selected) if selected in choices else 0, format_func=lambda value: labels.get(value, value))
    if choice != 'New migration':
        st.session_state['migration_workspace_id'] = choice
    try:
        if choice == 'New migration':
            with st.form('migration_upload'):
                name = st.text_input('Workspace name', value='Application migration')
                problem = st.text_area('Migration objective', value='Assess whether the source data can satisfy the target application requirements. Identify gaps and transform approved fields without inventing missing values.')
                source = st.file_uploader('Source application data CSV', type=['csv'], key='migration_source_upload')
                source_metadata = st.file_uploader('Source metadata CSV (optional)', type=['csv'], key='migration_source_metadata_upload')
                target_metadata = st.file_uploader('Target metadata CSV', type=['csv'], key='migration_target_metadata_upload')
                st.caption('Metadata headers: column_name, data_type, required, description. Optional: default_value, allowed_values, business_glossary_term, transformation_hint. allowed_values accepts a JSON array or a pipe-separated list.')
                create = st.form_submit_button('Create migration workspace', type='primary')
            if create:
                if source is None or target_metadata is None:
                    st.error('Upload source data and target metadata.')
                else:
                    workspace = service.create(name, source.getvalue(), target_metadata.getvalue(), source_metadata.getvalue() if source_metadata else None, problem)
                    st.session_state['migration_workspace_id'] = workspace.name
                    st.rerun()
            examples = Path(__file__).resolve().parents[1] / 'examples/migration'
            if st.button('Load synthetic payroll example'):
                workspace = service.create('Synthetic payroll migration', (examples / 'legacy_payroll_data.csv').read_bytes(),
                    (examples / 'target_payroll_metadata.csv').read_bytes(), (examples / 'legacy_payroll_metadata.csv').read_bytes(), DEMO_PROMPT)
                st.session_state['migration_workspace_id'] = workspace.name
                st.rerun()
            st.caption('The example contains synthetic employee data, ambiguous fields, conversion errors and a missing required field. It intentionally demonstrates a partial migration.')
            return
        workspace = service.workspace(choice)
        source, target = service.metadata(choice)
        info = manager.read(workspace, 'metadata/workspace.json')
        st.caption(f"Workspace: {workspace.name} · Status: {info['status']}")
        with st.expander('Source and target schemas', expanded=info['status'] == 'MIGRATION_READY'):
            left, right = st.columns(2)
            with left:
                st.write('Source metadata')
                st.dataframe([f.model_dump() for f in source], width='stretch', hide_index=True)
            with right:
                st.write('Target metadata')
                st.dataframe([f.model_dump() for f in target], width='stretch', hide_index=True)
            st.write('Source data preview')
            _preview(service.preview(choice, target=False))
            st.caption('Model prompts use masked data-profile samples; originals stay in this local workspace.')
        approval = manager.read(workspace, 'metadata/mapping_approval.json')
        proposed = manager.read(workspace, 'generated/mapping.json')
        if not approval:
            if st.button('Generate semantic mappings', type='primary'):
                with st.spinner('Google ADK is asking the configured local model for structured mappings…'):
                    service.propose(choice)
                st.rerun()
            if proposed:
                _review(service, workspace, MappingPlan.model_validate(proposed), target)
        else:
            st.success('Human mapping approval saved. Transformations run only when you start execution below.')
            with st.expander('Approved mapping and fit-gap'):
                _fit_gap(approval['fit_gap'])
                st.dataframe(approval['plan']['mappings'], width='stretch', hide_index=True)
            if st.button('Generate and execute approved migration', type='primary'):
                with st.spinner('Building transformations, executing in Docker, and validating materialized target data…'):
                    service.execute(choice)
                st.rerun()
        report = manager.read(workspace, 'metadata/migration_result.json')
        if report:
            _results(service, workspace, report)
        elif info.get('error'):
            st.error(info['error'])
    except Exception as exc:
        st.error(f'{type(exc).__name__}: {str(exc)[:3000]}')
