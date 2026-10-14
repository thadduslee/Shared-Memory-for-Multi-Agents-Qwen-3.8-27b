"""Subprocess boundary: receives only a header, a delta OR a visible query.

Executed with the selected version workspace on sys.path. It has no scoring
imports and never receives a dataset path or the orchestration checkpoint DB.
"""
import json
import sys


def main():
    payload = json.load(sys.stdin)
    sys.path.insert(0, payload['workspace'])
    from memory_system.agent import GateMemAgent
    agent = GateMemAgent(payload['database'])
    try:
        agent.open_episode(payload['episode'])
        for turn in payload.get('delta', []):
            agent.ingest(turn)
        result = agent.query(payload['checkpoint']) if 'checkpoint' in payload else {}
        agent.store.conn.commit()
    finally:
        agent.store.close()
    print('ONLINE_RESULT=' + json.dumps(result))


if __name__ == '__main__':
    main()
