"""Native Streamlit interface for the shared Migration Studio backend."""
from __future__ import annotations

import hashlib
import json
import re
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
    st.caption('Edit source fields as a JSON list, e.g. ["monthly_salary"]. Use Transformation / review instructions for the changes you want applied, e.g. trim and uppercase a code, convert kilograms to grams, or parse DD-MM-YYYY input as a date. Adding or changing instructions on an approved DIRECT mapping makes it a TRANSFORMATION when you approve. Marking a field MISSING keeps its output null regardless of other instructions. Resolve ambiguous mappings before approval. Table edits are drafts until you approve below.')
    rows = []
    for mapping in plan.mappings:
        status = ('MISSING' if mapping.mapping_type == 'MISSING' or mapping.status == 'MISSING'
                  else mapping.status if mapping.status in {'APPROVED', 'REJECTED'}
                  else 'PENDING' if mapping.mapping_type == 'AMBIGUOUS' else 'APPROVED')
        rows.append({'Target Field': mapping.target_column, 'Source Field(s)': json.dumps(mapping.source_columns),
                     'Mapping Type': mapping.mapping_type, 'Transformation': mapping.transformation_description,
                     'Confidence': mapping.confidence, 'Reason': mapping.reason,
                     'Status': status})
    # Preserve submitted edits across validation reruns, but reset them when a
    # fresh proposal changes the values or row order of this workspace's table.
    proposal_key = hashlib.sha256(json.dumps(plan.model_dump(), sort_keys=True).encode()).hexdigest()[:12]
    with st.form('mapping_review_' + workspace.name):
        edited = st.data_editor(pd.DataFrame(rows), width='stretch', hide_index=True, num_rows='fixed',
            key=f'mapping_editor_{workspace.name}_{proposal_key}',
            disabled=['Target Field', 'Confidence', 'Reason'], column_config={
                'Mapping Type': st.column_config.SelectboxColumn(options=['DIRECT', 'TRANSFORMATION', 'DERIVED', 'AMBIGUOUS', 'MISSING'], required=True),
                'Transformation': st.column_config.TextColumn('Transformation / review instructions', help='Your approved instructions determine the transformation. Describe the input, required output and handling of invalid values.'),
                'Status': st.column_config.SelectboxColumn(options=['APPROVED', 'REJECTED', 'MISSING', 'PENDING'], required=True),
            })
        confirm = st.checkbox('I reviewed these mappings and approve their use. Unresolved fields may appear as null in a partial preview.')
        approved = st.form_submit_button('Approve reviewed mapping plan', type='primary')
    if approved:
        if not confirm:
            st.error('Review the mapping table and confirm approval before continuing.')
            return
        mappings = []
        proposed_instructions = {mapping.target_column: mapping.transformation_description.strip() for mapping in plan.mappings}
        for row in edited.to_dict('records'):
            missing = row['Status'] == 'MISSING' or row['Mapping Type'] == 'MISSING'
            instruction = row['Transformation'] if isinstance(row['Transformation'], str) else ''
            mapping_type = 'MISSING' if missing else row['Mapping Type']
            if (not missing and mapping_type == 'DIRECT' and row['Status'] == 'APPROVED'
                    and instruction.strip() and instruction.strip() != proposed_instructions[row['Target Field']]):
                mapping_type = 'TRANSFORMATION'
            if missing:
                source_columns = []
            else:
                try:
                    source_columns = json.loads(row['Source Field(s)'])
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ValueError(f"{row['Target Field']}: source fields must be a JSON list") from exc
                if not isinstance(source_columns, list) or any(not isinstance(value, str) for value in source_columns):
                    raise ValueError('Source fields must be a JSON list of column names')
            mappings.append(Mapping(target_column=row['Target Field'], source_columns=source_columns,
                mapping_type=mapping_type,
                transformation_description=instruction, confidence=row['Confidence'], reason=row['Reason'],
                requires_review=False, status='MISSING' if missing else row['Status']).model_dump())
        service.approve(workspace.name, mappings)
        st.rerun()


def _execution_service_unavailable(error):
    message = str(error or '').lower()
    return (bool(re.search(r'hyper-v\s*socket', message))
            or any(text in message for text in ('cannot connect to the docker daemon', 'cannot connect to docker daemon',
                                                'is the docker daemon running', 'docker daemon is not running'))
            or (any(text in message for text in ('failed to connect to backend', 'error during connect', 'connection refused', 'no such file or directory'))
                and any(text in message for text in ('docker', 'rancher', 'containerd'))))


def _execution_error(error):
    """Explain recovery without treating generated-code errors as bad input."""
    if _execution_service_unavailable(error):
        st.error('The local execution service is unavailable. Your source data and approved mappings are saved.')
        st.info('Start or restore Rancher Desktop / Docker and wait for its container engine to be ready, then choose Retry approved migration.')
    elif 'unapproved source column' in error.lower():
        target = re.search(r'([^:\n]+):\s*expression references\b', error, flags=re.IGNORECASE)
        field = ' for ' + target[1].strip() if target else ''
        st.error(f'The generated transformation{field} uses a field outside the approved mapping. The check stopped that transformation; your source data has not been changed.')
        st.info('Choose Retry approved migration to generate the transformation again. To correct the mapping, choose Edit approved mappings. If no source field can supply this target, mark it MISSING, approve again, and run a partial migration.')
    else:
        st.error('Migration could not finish. Your source data and approved mappings are saved.')
        st.info('Retry the approved migration, or choose Edit approved mappings to correct source fields or transformation instructions and approve the updated plan.')
    with st.expander('Technical details'):
        st.text(str(error)[:6000])


def _previous_approvals(workspace):
    directory = safe_path(workspace, 'metadata/mapping_revisions')
    if not directory.is_dir():
        return
    revisions = []
    for candidate in directory.iterdir():
        if not re.fullmatch(r'[a-f0-9]{32}', candidate.name):
            continue
        try:
            archive = safe_path(workspace, 'metadata/mapping_revisions/' + candidate.name)
            revision = read_json(safe_path(archive, 'revision.json'))
            approval_path = safe_path(archive, 'metadata/mapping_approval.json')
            approval = read_json(approval_path)
            if not isinstance(revision, dict) or revision.get('current') is not False or not isinstance(approval, dict) or approval.get('human_approved') is not True:
                continue
            plan = MappingPlan.model_validate(approval['plan'])
        except (OSError, ValueError, KeyError):
            continue
        revisions.append((approval.get('approved_at', ''), candidate.name, revision, approval, approval_path, plan))
    if not revisions:
        return
    with st.expander('Previous mapping approvals'):
        st.caption('Historical approvals are retained for reference. They are not the current mapping or evidence of the current migration result.')
        for approved_at, revision_id, revision, approval, path, plan in sorted(revisions, reverse=True):
            number = approval.get('revision', revision.get('approval_revision', 1))
            st.write(f'Revision {number} · Approved {approved_at or "date unavailable"}')
            st.dataframe([mapping.model_dump() for mapping in plan.mappings], width='stretch', hide_index=True)
            st.download_button(f'Download previous approval JSON (revision {number})', path.read_bytes(),
                file_name=f'mapping_approval_revision_{number}.json', mime='application/json', key=workspace.name + '_approval_' + revision_id)


def _results(service, workspace, report=None, error=None):
    st.subheader('Migration results')
    if error:
        _execution_error(error)
    if report:
        if error:
            st.warning('The latest execution could not finish. The saved results below are from the previous validated execution.')
        if report['complete']:
            st.success('Migration passed the configured data checks. Review business semantics before using the export.')
        else:
            st.warning('Incomplete migration — actual target data is available as a partial preview. Review missing required fields and invalid records before reuse.')
        columns = st.columns(4)
        for column, label, key in zip(columns, ['Source records', 'Target records', 'Valid records', 'Invalid records'], ['source_record_count', 'target_record_count', 'valid_records', 'invalid_records']):
            column.metric(label, report[key])
        st.caption(f"Mapped fields: {report['mapped_fields']}. Missing/unresolved fields: {', '.join(report['missing_fields']) or 'none'}.")
    elif error:
        st.warning('Execution did not produce validated results. Review the error and saved artifacts below, then retry execution. Missing mappings are allowed as null values in a partial migration.')
    else:
        st.info('Start execution above to produce target data and validation results. The approved mapping is available to download now.')
    tabs = st.tabs(['Target data', 'Mapping report', 'Validation report', 'Artifacts and export'])
    with tabs[0]:
        if report:
            try:
                _preview(service.preview(workspace.name))
            except Exception as exc:
                st.error(f'Target preview could not be loaded: {str(exc)[:3000]}')
        else:
            st.info('No validated target data is available for this run yet.')
        st.caption('Generic target-schema export. No Oracle HCM or other vendor import compatibility is claimed.')
    with tabs[1]:
        approval = read_json(safe_path(workspace, 'metadata/mapping_approval.json'))
        st.dataframe(approval['plan']['mappings'], width='stretch', hide_index=True)
    with tabs[2]:
        if report:
            st.dataframe(report['field_validation'], width='stretch', hide_index=True)
            if report.get('issues_preview'):
                st.dataframe(report['issues_preview'], width='stretch', hide_index=True)
                st.caption(f"Showing at most {report['issues_preview_limit']} of {report['issue_count']} validation issues. All issues are persisted in results.validation.")
            else:
                st.info('No record-level issues were found by the configured checks.')
            with st.expander('Full validation summary'):
                st.json(report)
        else:
            st.info('Validation results become available after the transformation finishes successfully. A partial migration can still report missing fields and invalid records.')
    with tabs[3]:
        files = [('generated/approved_mapping.json', 'Download approved mapping JSON', 'application/json'),
                 ('generated/transformation.sql', 'Download transformation SQL', 'text/plain'),
                 ('generated/transformation.py', 'Download transformation Python', 'text/plain')]
        if report:
            files = [('results/migrated_data.csv', 'Download target CSV', 'text/csv'),
                     ('results/validation.json', 'Download validation report', 'application/json'),
                     ('results/connector.zip', 'Download connector ZIP', 'application/zip'),
                     ('notebooks/migration.ipynb', 'Download migration notebook', 'application/x-ipynb+json'), *files]
        else:
            st.caption('Target CSV, validation report and connector exports become available after a validated execution, including an incomplete migration. Saved mappings, generated source and logs are available below.')
        for relative, label, mime in files:
            path = safe_path(workspace, relative)
            if path.is_file():
                st.download_button(label, data=path.read_bytes(), file_name=path.name, mime=mime, key=workspace.name + relative)
        for relative in ('generated/transformation.sql', 'generated/transformation.py', 'generated/solution.py'):
            path = safe_path(workspace, relative)
            if path.is_file():
                with st.expander('Inspect ' + relative):
                    st.code(path.read_text(encoding='utf-8'), language='sql' if path.suffix == '.sql' else 'python')
        for path in sorted(path for path in safe_path(workspace, 'logs').iterdir() if path.suffix in {'.log', '.jsonl'}):
            with st.expander(path.name):
                st.text(path.read_text(encoding='utf-8', errors='replace')[-15000:])
                st.download_button('Download ' + path.name, path.read_bytes(), file_name=path.name, key=workspace.name + path.name)


def render_migration(settings, manager=None):
    """Render in the existing app; session selection is migration_workspace_id."""
    service = MigrationService(settings)
    manager = manager or service.manager
    st.title('Migration Studio')
    st.write('Assess source-to-target fit, review mappings, and execute approved transformations in Docker.')
    if st.button('Migration history', help='Find and reopen saved migrations, including failed runs and mappings awaiting review.'):
        st.session_state['workspace_history_filter'] = 'Migrations'
        st.session_state['next_page'] = 'Workspace history'
        st.rerun()
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
        _previous_approvals(workspace)
        review = manager.read(workspace, 'metadata/mapping_review.json', {}) or {}
        if review.get('invalidation_pending'):
            st.warning('Mapping review was interrupted. Your source data is saved. Resume the review before approving or running this migration again.')
            if st.button('Resume mapping review', type='primary'):
                try:
                    service.reopen_review(choice)
                except Exception as exc:
                    st.error('Mapping review could not resume. Close any open migration export files and try again.')
                    with st.expander('Technical details'):
                        st.text(f'{type(exc).__name__}: {str(exc)[:3000]}')
                else:
                    st.session_state.pop('migration_execution_error_' + workspace.name, None)
                    st.rerun()
            return
        approval = manager.read(workspace, 'metadata/mapping_approval.json')
        proposed = manager.read(workspace, 'generated/mapping.json')
        execution_error_key = 'migration_execution_error_' + workspace.name
        saved_error = st.session_state.get(execution_error_key)
        if isinstance(saved_error, dict):
            execution_error = saved_error.get('error') if saved_error.get('workspace_updated_at') == info.get('updated_at') else None
        else:
            execution_error = saved_error if info['status'] not in {'MIGRATION_COMPLETED', 'MIGRATION_INCOMPLETE'} else None
        if saved_error and not execution_error:
            st.session_state.pop(execution_error_key, None)
        if not approval:
            if st.button('Generate semantic mappings', type='primary'):
                with st.spinner('Google ADK is asking the configured local model for structured mappings…'):
                    service.propose(choice)
                st.rerun()
            if proposed:
                st.info('Your source data is already loaded. Review this plan and approve it before running the migration.')
                _review(service, workspace, MappingPlan.model_validate(proposed), target)
        else:
            retry = info['status'] == 'MIGRATION_FAILED' or bool(execution_error)
            if retry and _execution_service_unavailable(execution_error or info.get('error')):
                st.info('Approval is saved. Restore Rancher Desktop / Docker, then retry the approved migration using the source data already loaded.')
            elif retry:
                st.info('The last execution failed. You can retry or edit the approved mappings using the source data already loaded.')
            else:
                st.success('Human mapping approval saved. Transformations run only when you start execution below.')
            with st.expander('Approved mapping and fit-gap'):
                _fit_gap(approval['fit_gap'])
                st.dataframe(approval['plan']['mappings'], width='stretch', hide_index=True)
            run, edit = st.columns(2)
            with run:
                execute = st.button('Retry approved migration' if retry else 'Generate and execute approved migration', type='primary')
            with edit:
                if st.button('Edit approved mappings', help='Keep the uploaded source data and reopen your previous decisions for review. A new approval is required before execution.'):
                    service.reopen_review(choice)
                    st.session_state.pop(execution_error_key, None)
                    st.rerun()
            if execute:
                try:
                    with st.spinner('Building transformations, executing in Docker, and validating materialized target data…'):
                        service.execute(choice)
                except Exception as exc:
                    current = manager.read(workspace, 'metadata/workspace.json')
                    st.session_state[execution_error_key] = {'error': f'{type(exc).__name__}: {str(exc)[:3000]}',
                        'workspace_updated_at': current.get('updated_at')}
                else:
                    st.session_state.pop(execution_error_key, None)
                st.rerun()
        info = manager.read(workspace, 'metadata/workspace.json')
        report = manager.read(workspace, 'metadata/migration_result.json')
        if approval or report:
            _results(service, workspace, report, execution_error or info.get('error'))
        elif info.get('error'):
            st.error(info['error'])
    except Exception as exc:
        st.error(f'{type(exc).__name__}: {str(exc)[:3000]}')
