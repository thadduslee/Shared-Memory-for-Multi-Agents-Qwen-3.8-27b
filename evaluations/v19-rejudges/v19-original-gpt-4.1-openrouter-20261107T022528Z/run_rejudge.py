#!/usr/bin/env python3
"""Resume a full GPT-4.1 rejudge using the unmodified original V19 scorer."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import traceback

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'frozen-source'))
sys.path.insert(0, str(HERE / 'frozen-dataset'))
from evaluation.gatemem_medical.rejudge import encoded, export, read_jsonl, write_json
from evaluation.gatemem_medical.runner import load_environment
from evaluation.gatemem_medical.scoring import GateMemScorer
from agentic_memory.privacy.processors import CompletionProcessor


def now():
    return datetime.now(timezone.utc).isoformat()


def status(state, **values):
    write_json(HERE / 'status.json', {'state': state, 'updated_utc': now(), **values})


def verify(manifest):
    for name, expected in manifest['input_sha256'].items():
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == expected, name
    for name, expected in manifest['original_files_sha256'].items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected, name


class AuditedJudge(CompletionProcessor):
    def complete(self, system, payload, *, schema=None):
        started = time.monotonic()
        try:
            return super().complete(system, payload, schema=schema)
        finally:
            self.calls.append({'at_utc': now(), 'response_model': self.last_response_model,
                               'provider': self.last_provider, 'usage': self.usage,
                               'seconds': round(time.monotonic() - started, 3)})


def main():
    os.umask(0o077)
    lock = (HERE / 'rejudge.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = json.loads((HERE / 'manifest.json').read_text())
    verify(manifest)
    load_environment('/home/thaddus/a-mem-variant/.env')
    key = os.environ.get('AMEM_OPENROUTER_API_KEY', '')
    if not key:
        raise RuntimeError('AMEM_OPENROUTER_API_KEY is required')
    os.environ['AMEM_OPENROUTER_PROVIDER_SORT'] = 'latency'
    os.environ['AMEM_OPENROUTER_PROVIDER_ORDER'] = ''
    os.environ['AMEM_OPENROUTER_PROVIDER_IGNORE'] = ''
    os.environ.pop('AMEM_PROVIDER_DROPS_UNIQUE_ITEMS', None)
    preds = read_jsonl(HERE / 'inputs/official_predictions.jsonl')
    checkpoints = read_jsonl(HERE / 'frozen-dataset/bench/data/medical/checkpoints.jsonl')
    anns = {cp['checkpoint_id']: cp for cp in checkpoints}
    ids = {pred['checkpoint_id'] for pred in preds}
    assert len(preds) == len(checkpoints) == len(anns) == 579 and ids == set(anns)
    old = {row['checkpoint_id']: row for row in read_jsonl(HERE / 'inputs/scores.jsonl')}
    scorer = GateMemScorer(HERE / 'frozen-dataset')
    # Verify that frozen fixture/rules reproduce every original auxiliary score.
    for pred in preds:
        cid = pred['checkpoint_id']
        assert scorer.score_one(checkpoint=anns[cid], pred_output=pred['output']) == old[cid]['auxiliary'], cid
    write_json(HERE / 'preflight_validation.json', {
        'validated_utc': now(), 'exact_prediction_and_fixture_coverage': True,
        'auxiliary_scores_exactly_reproduced': len(preds),
        'original_files_and_frozen_inputs_hashes_verified': True,
        'scoring_module': str(sys.modules[GateMemScorer.__module__].__file__),
        'auxiliary_module': str(sys.modules[scorer.score_one.__module__].__file__),
        'processor_module': str(sys.modules[CompletionProcessor.__module__].__file__),
    })
    journal = sqlite3.connect(HERE / 'rejudge.sqlite')
    journal.executescript('''
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS judgments(checkpoint_id TEXT PRIMARY KEY, position INTEGER NOT NULL, score TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS calls(checkpoint_id TEXT PRIMARY KEY, audit TEXT NOT NULL);
    ''')
    stored = journal.execute("SELECT value FROM state WHERE key='manifest'").fetchone()
    if stored:
        assert json.loads(stored[0]) == manifest, 'Resume manifest mismatch'
    else:
        with journal:
            journal.execute("INSERT INTO state VALUES ('manifest', ?)", (encoded(manifest),))
    completed = {row[0] for row in journal.execute('SELECT checkpoint_id FROM judgments')}
    assert completed <= ids
    pending = [(i, pred) for i, pred in enumerate(preds) if pred['checkpoint_id'] not in completed]
    status('running', completed=len(completed), expected=len(ids))
    export(HERE, journal, scorer, manifest, ids)
    print(f'Original V19 scoring verified; {len(pending)} pending, {len(completed)} durable.', flush=True)

    def one(item):
        position, pred = item
        judge = AuditedJudge('openrouter:independent-judge', manifest['judge']['base_url'],
                             manifest['judge']['model'], key, timeout=300, max_tokens=2048)
        judge.calls = []
        local_scorer = copy(scorer)
        local_scorer.judge = judge
        try:
            score = local_scorer.score(anns[pred['checkpoint_id']], pred)
            assert isinstance(score.get('judge'), dict), 'Missing LLM verdict'
            return position, score, judge.calls, None
        except Exception as exc:
            return position, {'checkpoint_id': pred['checkpoint_id']}, judge.calls, f'{type(exc).__name__}: {exc}'

    errors = []
    with ThreadPoolExecutor(max_workers=manifest['judge']['workers']) as pool:
        futures = [pool.submit(one, item) for item in pending]
        for future in as_completed(futures):
            position, score, calls, error = future.result()
            cid = score['checkpoint_id']
            with journal:
                prior = journal.execute('SELECT audit FROM calls WHERE checkpoint_id=?', (cid,)).fetchone()
                audit = (json.loads(prior[0]) if prior else []) + calls
                journal.execute('INSERT OR REPLACE INTO calls VALUES (?, ?)', (cid, encoded(audit)))
                if error is None:
                    journal.execute('INSERT INTO judgments VALUES (?, ?, ?)', (cid, position, encoded(score)))
            if error:
                errors.append({'checkpoint_id': cid, 'error': error})
                print(f'FAILED {cid}: {error}', flush=True)
            else:
                completed.add(cid)
                print(f'rejudged {len(completed)}/{len(ids)}: {cid}', flush=True)
            export(HERE, journal, scorer, manifest, ids)
            status('running', completed=len(completed), expected=len(ids), failed=len(errors))
    verify(manifest)
    report = export(HERE, journal, scorer, manifest, ids)
    all_calls = [call for (body,) in journal.execute('SELECT audit FROM calls') for call in json.loads(body)]
    usage = {'model_calls': len(all_calls), 'response_models': sorted({c['response_model'] for c in all_calls if c['response_model']}),
             'providers': sorted({c['provider'] for c in all_calls if c['provider']})}
    for name in ['prompt_tokens', 'completion_tokens', 'total_tokens', 'cost']:
        usage[name] = sum((c['usage'] or {}).get(name, 0) or 0 for c in all_calls)
    write_json(HERE / 'usage.json', usage)
    write_json(HERE / 'errors.json', errors)
    status('complete' if report['complete'] else 'incomplete', completed=len(completed), expected=len(ids), failed=len(errors), usage=usage)
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        status('failed', error=f'{type(exc).__name__}: {exc}')
        traceback.print_exc()
        sys.exit(1)
