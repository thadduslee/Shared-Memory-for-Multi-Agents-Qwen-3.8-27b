"""Trusted security controls and preservation during gap recovery."""
import json
import re
import sqlite3
import tempfile
from pathlib import Path

from templates.memory_system.agent import GateMemAgent, MemoryAgent

NEGATIVE_CONTROL = re.compile(r'\b(?:delete|remove|erase|forget|purge|scrub|revoke|revoked|withdraw|stop sharing|do not share|must not share)\b', re.IGNORECASE)


def apply_controls(blob, header, turns, *, preceding_turn_ids=None, policy=None):
    """Denials/deletions bypass a failed ordinary parser using trusted baseline.

    Never install an allow from a quarantined batch. This path can conservatively
    deny extra access; it cannot expand permissions or release new facts.
    """
    controls = [t for t in turns if NEGATIVE_CONTROL.search(str(t.get('text') or '')) or t.get('operation')=='delete']
    if not controls and blob is not None:
        return blob
    with tempfile.TemporaryDirectory(prefix='security-controls-') as tmp:
        path = Path(tmp) / 'memory.sqlite'
        if blob:
            path.write_bytes(blob)
        agent = MemoryAgent(str(path),policy=policy)
        try:
            agent.open_episode(header)
            conn = agent.store.conn
            for turn in controls:
                # Keep the control source for replay, but never derive ordinary
                # memories or grant permissions on this path.
                eid = header['episode_id']
                speaker = str((turn.get('speaker') or {}).get('principal_id') or '')
                text = str(turn.get('text') or '')
                if (speaker!=agent.owner_id and (turn.get('operation')=='delete' or
                        speaker not in agent.trusted_policy.get('permission_authority_ids',[]))):
                    # An invalid control must not become effective on a later
                    # replay merely because its original ingestion failed.
                    continue
                if turn.get('operation')=='delete' and speaker==agent.owner_id:
                    target=conn.execute('SELECT observation_id FROM observations WHERE episode_id=? AND turn_id=?',
                                        (eid,str(turn.get('target_turn_id') or ''))).fetchone()
                    if target:
                        agent.store.tombstone(target[0],speaker,'explicit structured deletion')
                if re.search(r'\b(?:revoke|withdraw|stop sharing|do not share|must not share)\b', text, re.IGNORECASE):
                    conn.execute("UPDATE permissions SET status='revoked' WHERE episode_id=? AND resource_owner_id=?", (eid, agent.owner_id))
                if re.search(r'\b(?:delete|remove|erase|forget|purge|scrub)\b', text, re.IGNORECASE):
                    agent.store._honor_deletion_request(eid, text, speaker)
                # Record control history outside the ordinary ingestion map.
                turn = dict(turn)
                known = [r[0] for r in conn.execute('SELECT turn_id FROM observations WHERE episode_id=?', (eid,))]
                prefix = []
                for incoming in turns:
                    prefix.append(incoming['turn_id'])
                    if incoming['turn_id'] == turn['turn_id']:
                        break
                ordered = list(preceding_turn_ids if preceding_turn_ids is not None else known + prefix)
                if turn['turn_id'] in ordered:
                    ordered = ordered[:ordered.index(turn['turn_id']) + 1]
                turn['_control_preceding_turn_ids'] = ordered
                turn['_control_owner_id'] = agent.owner_id
                conn.execute('INSERT OR REPLACE INTO store_meta VALUES (?,?)',
                    ('security_control:' + str(turn.get('turn_id')), json.dumps(turn).encode()))
            conn.commit()
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            agent.store.close()
        return path.read_bytes()


def retain_controls(before, rebuilt):
    """Reapply shredding and denials after replay; no resurrection at backfill."""
    if before is None:
        return rebuilt
    with tempfile.TemporaryDirectory(prefix='retain-controls-') as tmp:
        old_path, path = Path(tmp) / 'before', Path(tmp) / 'after'
        old_path.write_bytes(before)
        path.write_bytes(rebuilt)
        old = sqlite3.connect(old_path)
        new = GateMemAgent(str(path))
        try:
            for target, requester, reason in old.execute('SELECT target_id,requested_by,reason FROM tombstones'):
                new.store.tombstone(target, requester, reason)
            for row in old.execute('SELECT * FROM tombstones'):
                new.store.conn.execute('INSERT OR REPLACE INTO tombstones VALUES (?,?,?,?,?,?)', tuple(row))
            for (pid,) in old.execute("SELECT permission_id FROM permissions WHERE status='revoked'"):
                new.store.conn.execute("UPDATE permissions SET status='revoked' WHERE permission_id=?", (pid,))
            for key, value in old.execute("SELECT key,value FROM store_meta WHERE key LIKE 'security_control:%'"):
                # A failed earlier batch may not have existed when a deletion
                # arrived. Reapply the retained request to newly backfilled
                # sources as well as copying already-established tombstones.
                control = json.loads(value)
                text = str(control.get('text') or '')
                episode = new.store.conn.execute("SELECT value FROM store_meta WHERE key='online_episode'").fetchone()
                eid = bytes(episode[0]).decode() if episode else ''
                speaker = str((control.get('speaker') or {}).get('principal_id') or '')
                if control.get('operation')=='delete' and speaker == control.get('_control_owner_id'):
                    target=new.store.conn.execute('SELECT observation_id FROM observations WHERE episode_id=? AND turn_id=?',
                        (eid,str(control.get('target_turn_id') or ''))).fetchone()
                    if target:
                        new.store.tombstone(target[0],str((control.get('speaker') or {}).get('principal_id') or ''),
                                            'retained structured deletion')
                if re.search(r'\b(?:delete|remove|erase|forget|purge|scrub)\b', text, re.IGNORECASE):
                    new.store._honor_deletion_request(eid, text,
                        str((control.get('speaker') or {}).get('principal_id') or ''))
                if re.search(r'\b(?:revoke|withdraw|stop sharing|do not share|must not share)\b', text, re.IGNORECASE):
                    preceding = control.get('_control_preceding_turn_ids')
                    # Legacy controls have no source ordering: retain the
                    # conservative denial. New controls never revoke a later
                    # independently authenticated regrant during backfill.
                    for pid, tid in new.store.conn.execute('SELECT p.permission_id,o.turn_id FROM permissions p '
                            'JOIN observations o ON o.observation_id=p.source_observation_id WHERE p.episode_id=?', (eid,)).fetchall():
                        if preceding is None or tid in preceding:
                            new.store.conn.execute("UPDATE permissions SET status='revoked' WHERE permission_id=?", (pid,))
                new.store.conn.execute('INSERT OR REPLACE INTO store_meta VALUES (?,?)', (key, value))
            new.store.conn.commit()
            new.store.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            old.close()
            new.store.close()
        return path.read_bytes()
