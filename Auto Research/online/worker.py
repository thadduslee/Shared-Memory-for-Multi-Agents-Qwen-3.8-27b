"""Subprocess boundary: receives only a header, a delta OR a visible query.

Executed with the selected version workspace on sys.path. It has no scoring
imports and never receives a dataset path or the orchestration checkpoint DB.
"""
import json
import sys


def main():
    payload = json.load(sys.stdin)
    sys.path.insert(0, payload['workspace'])
    import memory_system.agent as implementation
    if hasattr(implementation, 'MemoryAgent'):
        agent = implementation.MemoryAgent(payload['database'], policy=payload.get('trusted_policy'))
    else:  # Immutable workspaces from older run manifests.
        agent = implementation.GateMemAgent(payload['database'])
    try:
        agent.open_episode(payload['episode'])
        for turn in payload.get('delta', []):
            agent.ingest(turn)
        if 'checkpoint' in payload:
            result = (agent.query(payload['checkpoint'], additional_evidence=payload['additional_evidence'])
                      if payload.get('additional_evidence') else agent.query(payload['checkpoint']))
        else:
            result = {}
        agent.store.conn.commit()
    finally:
        agent.store.close()
    print('ONLINE_RESULT=' + json.dumps(result))


if __name__ == '__main__':
    main()
