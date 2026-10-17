"""Run the payroll migration through the shared backend and real Docker sandbox.

The optional fixture model is explicitly a mocked reasoning test. The Docker
execution, validation, exports and notebook are real in either mode.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.migration import Mapping, MappingPlan, MigrationService, SqlTransformationPlan, TransformationExpression
from workbench.settings import Settings


def demo_mapping():
    direct = [('employee_id', 'worker_code'), ('employee_name', 'person_name'), ('hire_date', 'joined_on'),
              ('department_number', 'dept_code'), ('company_email', 'office_email'), ('work_country', 'country')]
    mappings = [Mapping(target_column=target, source_columns=[source], mapping_type='DIRECT', confidence='HIGH', reason='Explicit semantic correspondence in synthetic fixture metadata') for target, source in direct]
    mappings.extend([
        Mapping(target_column='annual_base_salary', source_columns=['monthly_base_pay'], mapping_type='TRANSFORMATION',
                transformation_description='Multiply monthly contractual base salary by 12; same currency and exclusion of bonus/overtime.', confidence='HIGH', reason='Source and target metadata explicitly describe the salary period'),
        Mapping(target_column='employment_status', source_columns=['worker_class'], mapping_type='AMBIGUOUS', confidence='LOW', reason='Engagement category does not establish current active/inactive status'),
        Mapping(target_column='tax_identifier', source_columns=[], mapping_type='MISSING', confidence='HIGH', reason='No tax identifier is present in source data'),
    ])
    return MappingPlan(mappings=mappings, observations=['This mapping is an explicit mocked model response for repeatable integration testing.'])


class FixtureModel:
    def structured(self, schema, system, prompt):
        if schema is MappingPlan:
            return demo_mapping()
        if schema is SqlTransformationPlan:
            return SqlTransformationPlan(expressions=[TransformationExpression(target_column='annual_base_salary', sql_expression='"monthly_base_pay" * 12')], notes=['Mocked model expression; results still computed in Docker.'])
        raise ValueError('Unexpected fixture request')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture-model', action='store_true', help='Use explicitly mocked model responses; Docker execution remains real')
    parser.add_argument('--approve-demo', action='store_true', help='Approve this synthetic demo mapping; unresolved fields remain missing')
    args = parser.parse_args()
    settings = Settings.from_env()
    service = MigrationService(settings, llm=FixtureModel() if args.fixture_model else None)
    folder = Path(__file__).resolve().parents[1] / 'examples/migration'
    workspace = service.create('Payroll migration — ' + ('mocked model / real Docker' if args.fixture_model else 'live local model'),
        (folder / 'legacy_payroll_data.csv').read_bytes(), (folder / 'target_payroll_metadata.csv').read_bytes(), (folder / 'legacy_payroll_metadata.csv').read_bytes())
    print(f'Workspace: {workspace}', flush=True)
    proposal = service.propose(workspace.name)
    print(proposal.model_dump_json(indent=2), flush=True)
    if not args.approve_demo:
        print('Mappings saved. Review and approve in Migration Studio before execution.', flush=True)
        return
    for mapping in proposal.mappings:
        if mapping.mapping_type in {'MISSING', 'AMBIGUOUS'}:
            mapping.status, mapping.mapping_type, mapping.source_columns = 'MISSING', 'MISSING', []
        else:
            mapping.status = 'APPROVED'
    service.approve(workspace.name, proposal)
    result = service.execute(workspace.name)
    print(json.dumps({'model_mode': 'mocked fixture' if args.fixture_model else 'configured local model through ADK', 'workspace': str(workspace), 'result': result}, indent=2), flush=True)


if __name__ == '__main__':
    main()
