"""Isolated scripted reproduction. Never opens or modifies the native run."""
from dataclasses import replace
import json

from agentic_memory.privacy import PermissionUpdater
from security_tests import test_incremental_permissions as fixtures
from security_tests.test_workflow_v20 import fail_and_quarantine


def main():
    h = fixtures.IncrementalTests()
    h.setUp()
    try:
        h.bootstrap = replace(h.bootstrap, owner_read_actions=('search', 'read', 'derive'))
        h.updater = PermissionUpdater(h.core, h.host, 'work', h.bootstrap, h.model, version='21')
        h.classify = lambda p, s: 'instruction'
        h.turn('My appointment is Tuesday at four.')
        failed = fail_and_quarantine(h)
        h.turn('Bring the printed referral to my appointment.')
        before = next(a for a in h.updater.inspect()['assignment'].values() if a.get('ownership'))
        assert before['status'] == 'active'
        result = h.updater.service_pending(max_batches=1)
        after = h.updater.inspect()['assignment'][before['id']]
        child = h.core.db.execute("SELECT proposal FROM permission_jobs WHERE id LIKE 'host-recovery:%' AND status='complete'").fetchone()
        operations = json.loads(child[0])['operations']
        assert operations == []
        assert after['status'] == 'suspended'
        print(json.dumps({'reproduced': True, 'external_model_calls': False,
            'live_run_modified': False, 'recovery_result': result['status'],
            'before_ownership_status': before['status'],
            'after_ownership_status': after['status'], 'proposed_operations': operations,
            'failed_original_job': failed['job_id']}, indent=2))
    finally:
        h.tearDown()


if __name__ == '__main__':
    main()
