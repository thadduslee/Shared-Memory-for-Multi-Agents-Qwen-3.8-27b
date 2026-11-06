"""Retrospective only: read retained historical evidence without mutating it.

Run this file with the repository on PYTHONPATH. Benchmark case selection and
saved model outputs are analysis inputs, never runtime identity evidence.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from agentic_memory.privacy.permissions import PermissionBootstrap, PermissionUpdater, PermissionProposalRejected
from agentic_memory.privacy.revocation_v19 import context, validate_batch


def readonly(path):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    return db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--card', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    card = json.loads(args.card.read_text())
    attempt = next(a for a in card['selected_control_attempts'] if a['source_turn'] == 't138')
    policy = readonly(args.run_dir / 'memory.privacy.sqlite')
    memory = readonly(args.run_dir / 'memory.sqlite')
    try:
        job = policy.execute('SELECT * FROM permission_jobs WHERE id=?', (attempt['job_id'],)).fetchone()
        ids = json.loads(job['inputs'])
        sources = {}
        missing = []
        for sid in ids:
            row = memory.execute('SELECT payload FROM resources WHERE id=?', (sid,)).fetchone()
            if row:
                sources[sid] = json.loads(row['payload'])
            else:
                missing.append(sid)
        boot = policy.execute('SELECT body FROM permission_bootstrap WHERE compartment=?',
            (job['compartment'],)).fetchone()
        # Only the stored raw-stream bootstrap and source payloads go to the
        # resolver. No checkpoint labels, entity roster, or saved model target.
        updater = PermissionUpdater.__new__(PermissionUpdater)
        updater.bootstrap = PermissionBootstrap(**json.loads(boot[0]))
        updater.version = '19'
        result = context(updater, sources, job['source_id'])
        saved = attempt['proposal']['control_review'][0]['recipient']
        assert result['status'] == 'unresolved' or all(
            saved not in request['recipients'] for request in result['requests'])
        cutoff = policy.execute('SELECT MIN(rowid) FROM permission_records WHERE job_id=?', (job['id'],)).fetchone()[0]
        assert cutoff is not None, 'No retained publication boundary for historical source'
        state = {kind: {} for kind in ('assignment', 'activity', 'restriction')}
        for row in policy.execute('SELECT kind,id,body FROM permission_records WHERE compartment=? AND rowid<? ORDER BY rowid',
                (job['compartment'], cutoff)):
            state[row['kind']][row['id']] = json.loads(row['body'])
        saved_revokes = [op for op in attempt['proposal']['permission_changes'] if op['op'] == 'revoke_assignment']
        try:
            # Isolate the saved wrong-recipient revokes from the independent
            # family-denial operation when checking the recipient invariant.
            validate_batch(updater, saved_revokes, state, sources, job['source_id'])
        except PermissionProposalRejected as exc:
            rejected = str(exc)
        else:
            raise AssertionError('Historical wrong-recipient revokes unexpectedly validated')
        report = {'historical_run': str(args.run_dir), 'evidence_card': str(args.card),
            'evidence_card_sha256': hashlib.sha256(args.card.read_bytes()).hexdigest(),
            'method': 'Read-only immutable SQLite; retained sources within saved job prefix; no processor calls or publications.',
            'prefix_sources': len(ids), 'available_source_rows': len(sources), 'missing_source_rows': missing,
            'source_id': job['source_id'], 'source_text': sources[job['source_id']]['text'],
            'saved_model_selected_recipient': saved,
            'saved_revoked_assignment_ids': [op['assignment_id'] for op in attempt['proposal']['permission_changes']
                if op['op'] == 'revoke_assignment'], 'new_resolution': result,
            'prepublication_assignment_count': len(state['assignment']),
            'saved_wrong_recipient_revokes_rejected': rejected,
            'limits': ['Retained end-of-run rows are not an exact pre-turn snapshot; later erasure may have scrubbed evidence.',
                'This verifies recipient resolution, not publication of the compound future-family restriction or historical utility recovery.',
                'The evidence establishes the wrong model-selected recipient, not a session.actor_id fallback cause.',
                'No historical run was restarted; no paid evaluation was launched.']}
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'prefix_sources': len(ids), 'saved_recipient': saved,
            'resolution_status': result['status'],
            'resolved_recipients': [r['recipients'] for r in result['requests']]}))
    finally:
        policy.close()
        memory.close()


if __name__ == '__main__':
    main()
