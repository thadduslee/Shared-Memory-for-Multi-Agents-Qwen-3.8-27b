"""Copy a completed, identical evolution-off prefix into a full-schedule run.

This operator utility leaves the original paired experiment untouched. It verifies
the exact source prefix and current controls before widening only the dataset
manifest. The ordinary runner then resumes with its normal manifest checks.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evaluation.gatemem_medical import runner


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    source, destination = args.source.resolve(), args.destination.resolve()
    require(not destination.exists(), 'Destination must be new')
    with (source / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = json.loads((source / 'manifest.json').read_text())
        summary = json.loads((source / 'summary.json').read_text())
        prefix = json.loads((source / 'schedule.json').read_text())
        require(summary['complete'], 'Source must be complete')
        require(not manifest['offline'] and not manifest['evolution'],
                'Source must be a real-model evolution-off run')
        episodes, checkpoints = runner.load_data(Path('/home/thaddus/GateMem/bench/data/medical'))
        events = runner.schedule(episodes, checkpoints)
        require((len(episodes), len(checkpoints), len(events)) == (21, 579, 567),
                'Unexpected full dataset')
        require(0 < len(prefix) < len(events) and events[:len(prefix)] == prefix,
                'Source schedule is not an exact full-schedule prefix')
        selected = {event['episode_id'] for event in prefix}
        prefix_episodes = [episode for episode in episodes if episode['episode_id'] in selected]
        prefix_queries = [query for query in checkpoints if query['episode_id'] in selected]
        require(runner.digest({'episodes': prefix_episodes, 'checkpoints': prefix_queries})
                == manifest['dataset_digest'], 'Prefix source data changed')
        require(runner.schedule(prefix_episodes, prefix_queries) == prefix,
                'Source does not include whole prefix episodes')
        code_digest = runner.digest({
            str(path.relative_to(runner.ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in (runner.ROOT / 'agentic_memory/privacy',
                           runner.ROOT / 'evaluation/gatemem_medical')
            for path in sorted(folder.iterdir())
            if path.is_file() and (path.suffix == '.py' or path.name == 'fixture_policy.json')})
        require(code_digest == manifest['code_digest'], 'Pinned implementation changed')
        runner.load_environment(runner.ROOT / '.env')
        embedder, llm, judge = runner.processors()
        require(embedder.fingerprint == manifest['embedding_fingerprint'], 'Embedding changed')
        require(manifest['models'] == {'embedding': embedder.space_id,
                'memory': llm.model, 'judge': judge.model}, 'Models changed')
        # Endpoint locality is pinned explicitly for this continuation.
        require(llm.base_url == 'http://127.0.0.1:8000/v1' and
                judge.base_url == 'http://127.0.0.1:8001/v1', 'Expected local model servers')
        source_journal = sqlite3.connect(f'file:{source}/evaluation.sqlite?mode=ro', uri=True)
        require(json.loads(source_journal.execute(
            "SELECT value FROM state WHERE key='manifest'").fetchone()[0]) == manifest,
            'Source journal manifest mismatch')
        before = source_journal.execute(
            'SELECT id,boundary,prediction,score FROM official ORDER BY boundary,id').fetchall()
        require({row[0] for row in before} == {q['checkpoint_id'] for q in prefix_queries}
                and all(row[3] is not None for row in before), 'Incomplete source judgments')
        boundaries = source_journal.execute(
            'SELECT id,state,delta_digest FROM boundaries ORDER BY id').fetchall()
        require(len(boundaries) == len(prefix), 'Incomplete source boundaries')
        by_episode = {episode['episode_id']: episode for episode in episodes}
        for (number, state, delta_digest), event in zip(boundaries, prefix):
            turns = by_episode[event['episode_id']]['turns'][event['start']:event['end']]
            require(number == event['boundary'] and state == 'complete' and
                    delta_digest == runner.digest(turns), 'Boundary prefix mismatch')
        destination.mkdir(mode=0o700)
        for name in ('evaluation.sqlite', 'memory.sqlite', 'memory.privacy.sqlite'):
            with sqlite3.connect(f'file:{source}/{name}?mode=ro', uri=True) as original:
                with sqlite3.connect(destination / name) as copied:
                    original.backup(copied)
                    require(copied.execute('PRAGMA integrity_check').fetchone()[0] == 'ok',
                            f'Invalid copied database: {name}')
        source_journal.close()
        full_manifest = {**manifest, 'dataset_digest': runner.digest({
            'episodes': episodes, 'checkpoints': checkpoints})}
        with sqlite3.connect(destination / 'evaluation.sqlite') as journal:
            require(journal.execute('SELECT id,boundary,prediction,score FROM official '
                                    'ORDER BY boundary,id').fetchall() == before,
                    'Copied predictions or scores changed')
            journal.execute("UPDATE state SET value=? WHERE key='manifest'",
                            (runner.encoded(full_manifest),))
        runner.write_json(destination / 'manifest.json', full_manifest)
        runner.write_json(destination / 'schedule.json', events)
        runner.write_json(destination / 'continuation.json', {
            'prepared_at_utc': datetime.now(timezone.utc).isoformat(),
            'source_run': str(source), 'source_manifest': manifest,
            'preserved_predictions_and_scores': len(before),
            'preserved_boundaries': len(prefix),
            'remaining_checkpoints': len(checkpoints) - len(before),
            'remaining_boundaries': len(events) - len(prefix),
            'checks': 'Exact episode data, schedule prefix, boundary deltas, current code, '
                      'models, embedding, database integrity, and unchanged prediction/score rows',
            'manifest_change': 'Only dataset_digest widened to the validated full dataset'})
        # Produce an honest full-schedule progress report before the runner starts.
        with sqlite3.connect(destination / 'evaluation.sqlite') as journal:
            journal.row_factory = sqlite3.Row
            core = runner.PrivacyMemorySystem(destination / 'memory.sqlite', embedder)
            try:
                report = runner.export_run(destination, journal, events, episodes,
                    checkpoints, runner.GateMemScorer(Path('/home/thaddus/GateMem'), judge),
                    core, False)
            finally:
                core.close()
        require(not report['complete'], 'Full continuation cannot already be complete')
        print(json.dumps({'run_dir': str(destination), 'coverage': report['coverage']}, indent=2))


if __name__ == '__main__':
    main()
