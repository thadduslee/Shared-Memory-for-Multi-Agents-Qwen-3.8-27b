"""Reconstruct permission authority from preserved sources, outside candidates."""
import json

from templates.memory_system.agent import MemoryAgent

MAX_AUTHORITY_SOURCES = 2048


def reconstruct(source, header, policy, seq):
    """Candidate permissions/relationships cannot become their own authority.

    Header policy/identity is supplied by the authenticated source adapter.
    Raw permission evidence is interpreted by the trusted baseline, with a
    finite source budget. More elaborate permission formats require a trusted
    adapter update; a learned specialization cannot grant new privileges.
    """
    rows = source.execute('SELECT observation_id,episode_id,turn_id,observer_id,observed_role,kind,raw_text,'
                          'raw_payload_json,observed_at,seq,content_hash '
                          'FROM observations WHERE episode_id=? AND seq<=? ORDER BY seq LIMIT ?',
                          (header['episode_id'], seq, MAX_AUTHORITY_SOURCES+1)).fetchall()
    if len(rows) > MAX_AUTHORITY_SOURCES:
        return None
    agent = MemoryAgent(policy=policy)
    agent.open_episode(header)
    store = agent.store
    for row in rows:
        if row['turn_id'] == '__episode_metadata__':
            continue
        store.conn.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?)', tuple(row))
        payload=json.loads(row['raw_payload_json'])
        store._derive_explicit_permissions(header['episode_id'], row['observation_id'],
            row['raw_text'], agent.owner_id, str((payload.get('speaker') or {}).get('principal_id') or ''),
            str(payload.get('timestamp') or ''))
    # Explicit current revocations and shredding only narrow reconstructed access.
    for (pid,) in source.execute("SELECT permission_id FROM permissions WHERE status='revoked'"):
        store.conn.execute("UPDATE permissions SET status='revoked' WHERE permission_id=?", (pid,))
    store.conn.commit()
    return store
