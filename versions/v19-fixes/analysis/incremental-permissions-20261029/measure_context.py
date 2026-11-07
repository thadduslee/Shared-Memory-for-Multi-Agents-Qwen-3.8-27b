"""Offline request-size comparison using the permission security-test fixture.

Run from the repository root: python3 analysis/incremental-permissions-20261029/measure_context.py
No remote model calls or existing run/store mutations.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from security_tests.test_permission_inference import PermissionInference
from agentic_memory.privacy.permissions import INSTRUCTION, compact_input, proposal_schema


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())


def main():
    case = PermissionInference()
    case.setUp()
    try:
        case.establish()
        for i in range(80):
            case.apply(case.ingest(f'Unrelated stream observation {i}. ' +
                                   'Background detail without a permission change. ' * 8))
        job = case.ingest('Pickup is at 4 PM.')
        canonical = case.updater.inspect()
        case.apply(job, [case.binding(job['source_id'])])
        incremental = case.model.calls[-1]
        baseline = deepcopy(incremental)
        baseline.pop('context')
        baseline['sources'] = [{'id': row['id'], **json.loads(row['payload'])}
                               for row in case.core.db.execute(
                                   "SELECT id,payload FROM resources WHERE kind='observation' "
                                   "AND state='active' ORDER BY created,id")]
        baseline['permission_state'] = canonical
        baseline['binding_authorizations'] = {
            aid: a['evidence']['information_scope'] for aid, a in canonical['assignment'].items()
            if a['status'] in {'active', 'proposed'}}

        def wire(payload, incremental_mode=False):
            result, schema, _ = compact_input(payload, proposal_schema(
                revision=payload['revision'], bootstrap=case.bootstrap,
                source_ids=[s['id'] for s in payload['sources']],
                allow_context_requests=incremental_mode))
            return result, schema

        old_wire, old_schema = wire(baseline)
        new_wire, new_schema = wire(incremental, True)
        old_instruction = INSTRUCTION.split('\nThe input is an INCREMENTAL context,')[0]
        old_bytes = size({'instruction': old_instruction, 'payload': old_wire, 'schema': old_schema})
        new_bytes = size({'instruction': INSTRUCTION, 'payload': new_wire, 'schema': new_schema})
        print(json.dumps({
            'live_turns': len(baseline['sources']), 'loaded_turns': len(incremental['sources']),
            'old_request_bytes': old_bytes, 'incremental_request_bytes': new_bytes,
            'reduction_percent': round(100 * (1 - new_bytes / old_bytes), 1),
            'model_calls_for_update': case.core.db.execute(
                'SELECT attempts FROM permission_jobs WHERE id=?', (job['job_id'],)).fetchone()[0],
            'assignment_status': case.updater.inspect()['assignment']['access']['status'],
            'authorized_facts': [r['text'] for r in case.selected()],
        }, indent=2))
    finally:
        case.tearDown()


if __name__ == '__main__':
    main()
