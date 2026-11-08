#!/usr/bin/env python3
"""Fresh OpenRouter GPT-4.1 judgments with the frozen govmem strict benchmark."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import traceback

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'frozen-govmem/src'))
from govmem.adapters.gatemem.data import annotation
from govmem.config import ModelEndpoint
from govmem.eval.judge import aggregate, exposure, judge_all
from govmem.llm import ChatClient, ReplyCache


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def write_jsonl(path, rows):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(''.join(encoded(row) + '\n' for row in rows))
    tmp.replace(path)


def status(state, **values):
    write_json(HERE / 'status.json', {'state': state, 'updated_utc': now(), **values})


def verify_inputs():
    manifest = json.loads((HERE / 'manifest.json').read_text())
    for name, expected in manifest['input_sha256'].items():
        assert sha(HERE / name) == expected, ('Changed input', name)
    for name, expected in manifest['original_files_sha256'].items():
        assert sha(name) == expected, ('Changed original export', name)
    assert sha(HERE / 'run_rejudge.py') == manifest['runner_sha256']
    return manifest


def validate_contexts(preds, anns):
    # No prior strict run exists for this data, so instead of matching prior exposure flags this checks
    # the packet against the recovery proof's exact record and span counts.
    originals = {p['checkpoint_id']: p for p in read_jsonl(HERE / 'inputs/original_official_predictions.jsonl')}
    records = {r['id']: r for r in read_jsonl(HERE / 'inputs/context_records.jsonl')}
    proof = json.loads((HERE / 'context_recovery.json').read_text())
    assert len(preds) == len(anns) == len(originals) == 579
    assert len({p['checkpoint_id'] for p in preds}) == 579
    assert {p['checkpoint_id'] for p in preds} == set(anns) == set(originals)
    for record in records.values():
        assert hashlib.sha256(record['text'].encode()).hexdigest() == record['text_sha256']
    referenced = set()
    released_spans = 0
    not_called = 0
    for pred in preds:
        cid = pred['checkpoint_id']
        original = originals[cid]
        assert {k: v for k, v in pred['output'].items() if k != 'memory_audit'} == original['output']
        mids = original['input_manifest']['memory_ids']
        referenced.update(mids)
        packet = pred['output']['memory_audit']['prompt_context']
        assert packet['record_ids'] == mids
        assert packet['text'] == '\n'.join(records[rid]['text'] for rid in mids)
        assert packet['answer_model_called'] == (original['input_manifest']['answer_validation'].get('generation_calls', 0) == 1 or not mids)
        not_called += not packet['answer_model_called']
        for span in original['output']['retrieved_memory']:
            start, end = span['span']
            assert records[span['memory_id']]['text'][start:end] == span['text']
            released_spans += 1
    assert referenced == set(records) and len(records) == proof['unique_evidence_records']
    assert released_spans == proof['validation']['exact_released_support_spans_checked']
    assert not_called == sum(proof['packets_without_answer_model_call'].values())
    assert proof['all_contexts_recovered'] and proof['unknown_contexts'] == 0
    write_json(HERE / 'preflight_validation.json', {
        'validated_utc': now(), 'checkpoints': len(preds), 'context_records': len(records),
        'released_spans_reverified': released_spans, 'unknown_contexts': 0,
        'adapted_answers_equal_originals': True, 'full_context_text_and_record_order_verified': True,
        'packets_without_answer_model_call': not_called,
        'scorer_module': str(sys.modules[judge_all.__module__].__file__),
        'transport_module': str(sys.modules[ChatClient.__module__].__file__),
    })


def sensitivity(rows, preds):
    # Same aggregate, but a packet that never reached an answer-model call counts as empty context.
    called = {p['checkpoint_id']: p['output']['memory_audit']['prompt_context']['answer_model_called'] for p in preds}
    adjusted = [dict(r, context_exposed=bool(r['context_exposed'] and called[r['checkpoint_id']])) for r in rows]
    return {'definition': 'context_exposed only where the recorded packet reached an answer-model call',
            'metrics': aggregate(adjusted),
            'checkpoints_changed': sorted(r['checkpoint_id'] for r, a in zip(rows, adjusted)
                                          if r['context_exposed'] != a['context_exposed'])}


def main():
    os.umask(0o077)
    lock = (HERE / 'rejudge.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = verify_inputs()
    for line in Path('/home/thaddus/a-mem-variant/.env').read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            if key.strip().startswith('AMEM_'):
                os.environ.setdefault(key.strip(), value.strip().strip('\"\''))
    key = os.environ.get('AMEM_OPENROUTER_API_KEY', '')
    if not key:
        raise RuntimeError('AMEM_OPENROUTER_API_KEY is required')
    preds = read_jsonl(HERE / 'inputs/predictions.jsonl')
    anns = {cp['checkpoint_id']: annotation(cp) for cp in read_jsonl(HERE / 'inputs/checkpoints.jsonl')}
    validate_contexts(preds, anns)
    config = manifest['judge']
    chat = ChatClient(ModelEndpoint(base_url=config['base_url'], api_key=key, model=config['model'],
                                   thinking=False, max_tokens=2048, timeout_s=300., max_concurrency=6))
    audit_lock = threading.Lock()

    def audit_response(response):
        if response.status_code != 200:
            return
        response.read()
        data = response.json()
        entry = {'at_utc': now(), 'response_id': data.get('id'), 'model': data.get('model'),
                 'provider': data.get('provider'), 'usage': data.get('usage') or {}}
        with audit_lock:
            with (HERE / 'model_calls.jsonl').open('a') as handle:
                handle.write(encoded(entry) + '\n')
                handle.flush()
        if data.get('model') != config['model']:
            raise RuntimeError('OpenRouter response model does not match requested GPT-4.1')

    chat._http.event_hooks['response'].append(audit_response)
    cache = ReplyCache(HERE / 'judge-cache.sqlite')
    journal = sqlite3.connect(HERE / 'rejudge.sqlite')
    journal.executescript('''
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS verdicts(checkpoint_id TEXT PRIMARY KEY, position INTEGER NOT NULL, verdict TEXT NOT NULL);
    ''')
    stored = journal.execute("SELECT value FROM state WHERE key='manifest'").fetchone()
    if stored:
        assert json.loads(stored[0]) == manifest, 'Resume manifest mismatch'
    else:
        with journal:
            journal.execute("INSERT INTO state VALUES ('manifest', ?)", (encoded(manifest),))
    completed = {row[0] for row in journal.execute('SELECT checkpoint_id FROM verdicts')}
    assert completed <= set(anns)
    pending = [(i, pred) for i, pred in enumerate(preds) if pred['checkpoint_id'] not in completed]

    def one(item):
        position, pred = item
        row = judge_all([pred], anns, chat=chat, cache=cache, repo=HERE / 'frozen-govmem', workers=1)[0]
        if row.get('judge_fallback') or not row.get('llm_judged'):
            raise RuntimeError('LLM judge unavailable for ' + pred['checkpoint_id'] + '; rule-only fallback rejected')
        return position, row

    def export(complete=False):
        rows = [json.loads(row[0]) for row in journal.execute('SELECT verdict FROM verdicts ORDER BY position')]
        write_jsonl(HERE / 'verdicts.jsonl', rows)
        metrics = aggregate(rows)
        if not any(r['query_type'] == 'utility' for r in rows):
            for k in ['U', 'MGS', 'MGS_strict', 'OR']:
                metrics[k] = None
        for category, fields in [('privacy', ['A', 'A_strict']), ('safety', ['F', 'F_strict'])]:
            if not any(r['query_type'] == category for r in rows):
                for field in fields:
                    metrics[field] = None
        exact = len(rows) == len(preds) and {r['checkpoint_id'] for r in rows} == set(anns)
        assert not complete or exact
        report = {'mode': 'govmem-full-rejudge', 'complete': complete, 'judge_model': config['model'],
                  'judge_provider': 'openrouter', 'coverage': {'expected': len(preds), 'rejudged': len(rows),
                  'context_unknown': 0, 'exact_checkpoint_coverage': exact}, 'metrics': metrics,
                  'context_recovery': 'context_recovery.json',
                  'strict_semantics': 'privacy/deletion answer leak OR protected pattern in complete model memory context',
                  'sensitivity_answer_model_called_only': sensitivity(rows, preds),
                  'original_exports_unchanged': True, 'updated_utc': now()}
        write_json(HERE / 'summary.json', report)
        write_json(HERE / 'scores.json', metrics)
        status('complete' if complete else 'rejudging', completed=len(rows), expected=len(preds), metrics=metrics)
        return report

    export()
    print(f'Strict inputs verified: {len(pending)} pending, {len(completed)} durable.', flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(one, item) for item in pending]
        for future in as_completed(futures):
            position, row = future.result()
            with journal:
                journal.execute('INSERT INTO verdicts VALUES (?,?,?)', (row['checkpoint_id'], position, encoded(row)))
            completed.add(row['checkpoint_id'])
            export()
            print(f'[{now()}] rejudged {len(completed)}/{len(preds)}: {row["checkpoint_id"]}', flush=True)
    verify_inputs()
    calls = read_jsonl(HERE / 'model_calls.jsonl') if (HERE / 'model_calls.jsonl').exists() else []
    usage = {'model_calls': len(calls), 'response_models': sorted({c['model'] for c in calls}),
             'providers': sorted({c['provider'] for c in calls if c['provider']})}
    for k in ['prompt_tokens', 'completion_tokens', 'total_tokens', 'cost']:
        usage[k] = sum(c['usage'].get(k, 0) or 0 for c in calls)
    write_json(HERE / 'usage.json', usage)
    report = export(complete=True)
    chat._http.close()
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        status('failed', error_type=type(exc).__name__, error=str(exc))
        traceback.print_exc()
        sys.exit(1)
