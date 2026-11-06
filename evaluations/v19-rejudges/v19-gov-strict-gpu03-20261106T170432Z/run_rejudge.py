#!/usr/bin/env python3
"""Recover V19's recorded evidence packet, then run unchanged govmem judging."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import traceback
import urllib.request

HERE = Path(__file__).resolve().parent
HOME = Path('/home/thaddus')
REPO = HOME / 'a-mem-variant'
SOURCE = REPO / 'evaluations/v19/v19-model-controls-full-soclaas-20261104T103839Z/results'
ORIGINAL = HOME / 'v19-evaluations/v19-model-controls-full-soclaas-20261104T103839Z'
GOV = HOME / 'a-mem-gov'
MODEL = 'qwen3.8:27b'
URL = 'http://127.0.0.1:18003/v1'


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def write_jsonl(path, values):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(''.join(encoded(value) + '\n' for value in values))
    temporary.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def status(state, **values):
    write_json(HERE / 'status.json', {'state': state, 'updated_utc': now(), **values})


def ro(path):
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)


def prepare():
    if (HERE / 'manifest.json').exists():
        verify_inputs()
        return
    status('preparing_contexts')
    inputs = HERE / 'inputs'
    inputs.mkdir(exist_ok=True)
    frozen = HERE / 'frozen-govmem'
    shutil.copytree(GOV / 'govmem/src', frozen / 'src', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (frozen / 'bench/prompts').mkdir(parents=True, exist_ok=True)
    shutil.copy2(GOV / 'bench/prompts/judge_prompt.txt', frozen / 'bench/prompts/judge_prompt.txt')
    for name in ['summary.json', 'manifest.json', 'scores.jsonl', 'official_predictions.jsonl']:
        shutil.copy2(SOURCE / name, inputs / ('original_' + name))
    fixture = ORIGINAL / 'frozen-dataset/bench/data/medical'
    for name in ['episodes.jsonl', 'checkpoints.jsonl']:
        shutil.copy2(fixture / name, inputs / name)
    original_annotations = read_jsonl(inputs / 'checkpoints.jsonl')
    gov_annotations = read_jsonl(GOV / 'bench/data/medical/checkpoints.jsonl')
    anns = {c['checkpoint_id']: c for c in original_annotations}
    assert anns == {c['checkpoint_id']: c for c in gov_annotations}, 'Fixture mismatch'
    preds = read_jsonl(inputs / 'original_official_predictions.jsonl')
    assert len(preds) == len(anns) == 579
    assert {p['checkpoint_id'] for p in preds} == set(anns)
    episodes = read_jsonl(inputs / 'episodes.jsonl')
    sources = {}
    positions = {}
    for episode in episodes:
        eid = episode['episode_id']
        compartment = 'encounter:' + digest(eid)[:24]
        for position, turn in enumerate(episode['turns']):
            sid = 'permission-source:' + digest([compartment, turn['turn_id']])
            sources[sid] = {'text': turn['text'], 'episode_id': eid,
                            'turn_id': turn['turn_id'], 'position': position}
            positions[(eid, turn['turn_id'])] = position
    content = ro(ORIGINAL / 'results/memory.sqlite')
    privacy = ro(ORIGINAL / 'results/memory.privacy.sqlite')
    history = {(cid, version): json.loads(body)['candidate'] for cid, version, body in
               privacy.execute('SELECT candidate_id,version,body FROM attribution_history_v19')}
    views = defaultdict(list)
    for cid, version, rid in privacy.execute('SELECT candidate_id,version,resource_id FROM attribution_views_v19'):
        views['note:' + rid].append((cid, version))
    ids = {rid for pred in preds for rid in pred['input_manifest']['memory_ids']}
    records = {}
    counts = Counter()
    for rid in sorted(ids):
        choices = []
        for key in views[rid]:
            metadata = history.get(key)
            if metadata and metadata.get('source_id') in sources:
                source = sources[metadata['source_id']]
                assert all(0 <= a < b <= len(source['text']) for a, b in metadata['spans'])
                text = '; '.join(source['text'][a:b] for a, b in metadata['spans'])
                choices.append((text, metadata, source))
        assert choices and len({choice[0] for choice in choices}) == 1, ('Ambiguous context', rid)
        text, metadata, source = choices[0]
        row = content.execute('SELECT payload,state,kind FROM resources WHERE id=?', (rid,)).fetchone()
        assert row and row[2] == 'memory'
        saved = json.loads(row[0]).get('text')
        if saved is not None:
            assert saved == text, ('Projection does not match stored packet bytes', rid)
            method = 'exact_match_to_surviving_resource'
        else:
            assert row[1] == 'deleted'
            method = 'historical_view_spans_over_original_fixture'
        counts[method] += 1
        records[rid] = {'id': rid, 'text': text, 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                        'recovery_method': method, 'candidate_id': metadata['id'],
                        'attribution_version': metadata['attribution_version'],
                        'source_id': metadata['source_id'], 'source_spans': metadata['spans'],
                        'episode_id': source['episode_id'], 'source_turn_id': source['turn_id']}
    content.close()
    privacy.close()
    adapted = []
    validation = Counter()
    for pred in preds:
        cp = anns[pred['checkpoint_id']]
        mids = pred['input_manifest']['memory_ids']
        assert len(mids) == len(set(mids))
        receipt = pred['input_manifest']['answer_validation']
        assert receipt.get('generation_calls', 0) == int(bool(mids)), 'Cannot prove packet reached model'
        for rid in mids:
            record = records[rid]
            assert record['episode_id'] == pred['episode_id']
            assert positions[(record['episode_id'], record['source_turn_id'])] <= positions[(pred['episode_id'], pred['as_of_turn_id'])]
        for span in pred['output']['retrieved_memory']:
            a, b = span['span']
            assert records[span['memory_id']]['text'][a:b] == span['text']
            validation['exact_released_support_spans_checked'] += 1
        for claim in receipt.get('claims', []):
            for span in claim['evidence']:
                assert span['evidence_id'] in mids
                assert 0 <= span['start'] < span['end'] <= len(records[span['evidence_id']]['text'])
                validation['claim_reference_bounds_checked'] += 1
        output = dict(pred['output'])
        output['memory_audit'] = {'prompt_context': {
            'text': '\n'.join(records[rid]['text'] for rid in mids),
            'record_ids': mids, 'reconstructed': True,
            'method': 'Recorded model input IDs plus immutable historical source selectors; no new retrieval',
            'scope': 'Complete memory evidence content supplied to answer generation; excludes query and evaluator annotations',
        }}
        assert {k: v for k, v in output.items() if k != 'memory_audit'} == pred['output']
        adapted.append({'checkpoint_id': pred['checkpoint_id'], 'episode_id': pred['episode_id'],
                        'as_of_turn_id': pred['as_of_turn_id'], 'asker': cp['asker'],
                        'query_text': cp['query_text'], 'output': output})
    assert counts == {'exact_match_to_surviving_resource': 4111, 'historical_view_spans_over_original_fixture': 141}
    write_jsonl(inputs / 'context_records.jsonl', records.values())
    write_jsonl(inputs / 'predictions.jsonl', adapted)
    proof = {'all_contexts_recovered': True, 'unknown_contexts': 0, 'checkpoints': len(adapted),
             'unique_evidence_records': len(records), 'record_recovery': dict(counts),
             'validation': dict(validation), 'no_context_queries_or_gold_labels': True,
             'evidence_order_preserved': True, 'source_prefix_checked': True,
             'note': 'Deleted historical text is reconstructed only for evaluation of the already recorded benchmark inputs. Runtime stores are read-only.'}
    write_json(HERE / 'context_recovery.json', proof)
    import subprocess
    files = [p for p in [*inputs.rglob('*'), *frozen.rglob('*')] if p.is_file()]
    manifest = {'created_utc': now(), 'source_results': str(SOURCE), 'original_runtime': str(ORIGINAL / 'results'),
                'gov_repository': str(GOV), 'gov_revision': subprocess.check_output(['git', '-C', str(GOV), 'rev-parse', 'HEAD'], text=True).strip(),
                'judge': {'model': MODEL, 'base_url': URL, 'thinking': False, 'max_tokens': 2048,
                          'temperature': 0.0, 'workers': 6, 'gpus': [0, 3]},
                'scoring': 'Unmodified frozen govmem.eval.judge: rule + LLM merge; exact pattern context exposure; strict OR; unchanged U between normal and strict',
                'context_recovery': proof,
                'input_sha256': {str(p.relative_to(HERE)): sha(p) for p in files},
                'python': sys.version, 'original_files_sha256': {str(SOURCE / name): sha(SOURCE / name) for name in ['summary.json', 'scores.jsonl', 'official_predictions.jsonl']}}
    write_json(HERE / 'manifest.json', manifest)
    status('prepared', expected=579, context_recovery=proof)
    print(json.dumps(proof, indent=2), flush=True)


def verify_inputs():
    manifest = json.loads((HERE / 'manifest.json').read_text())
    for name, expected in manifest['input_sha256'].items():
        assert sha(HERE / name) == expected, ('Changed input', name)
    for name, expected in manifest['original_files_sha256'].items():
        assert sha(name) == expected, ('Original export changed', name)


def wait_for_model():
    status('waiting_for_model', endpoint=URL)
    for attempt in range(180):
        try:
            request = urllib.request.Request(URL + '/models', headers={'Authorization': 'Bearer dummy'})
            with urllib.request.urlopen(request, timeout=5) as response:
                models = json.load(response)
            if MODEL in {m['id'] for m in models['data']}:
                print('Model ready:', MODEL, flush=True)
                return
        except Exception:
            pass
        if attempt % 6 == 0:
            print('Waiting for local model service', attempt * 5, 'seconds', flush=True)
        time.sleep(5)
    raise RuntimeError('Local model service did not become ready')


def judge():
    verify_inputs()
    sys.path.insert(0, str(HERE / 'frozen-govmem/src'))
    from govmem.eval.judge import aggregate, exposure, judge_all
    from govmem.llm import ChatClient, ReplyCache
    from govmem.config import ModelEndpoint
    from govmem.adapters.gatemem.data import annotation
    preds = read_jsonl(HERE / 'inputs/predictions.jsonl')
    anns = {cp['checkpoint_id']: annotation(cp) for cp in read_jsonl(HERE / 'inputs/checkpoints.jsonl')}
    # Strict-on-original is a separate exact extension of the previously published
    # response verdicts. It is never substituted for the fresh govmem rejudge.
    old_scores = {s['checkpoint_id']: s for s in read_jsonl(HERE / 'inputs/original_scores.jsonl')}
    extension = []
    for pred in preds:
        old = old_scores[pred['checkpoint_id']]
        extension.append({'checkpoint_id': pred['checkpoint_id'], 'query_type': old['query_type'],
                          'utility_correct': old.get('utility_correct') is True,
                          'privacy_leak': old.get('privacy_leak') is True,
                          'deletion_leak': old.get('deletion_leak') is True,
                          'over_refusal': old.get('over_refusal') is True,
                          'context_exposed': exposure(pred['output']['memory_audit']['prompt_context']['text'], anns[pred['checkpoint_id']])})
    write_json(HERE / 'strict_on_original_scores.json', {'mode': 'original_response_verdicts_plus_govmem_context_exposure',
               'metrics': aggregate(extension), 'note': 'Original U/A/F retained; no new LLM verdicts in this separate result.'})
    write_jsonl(HERE / 'strict_on_original_verdicts.jsonl', extension)
    wait_for_model()
    chat = ChatClient(ModelEndpoint(base_url=URL, api_key='dummy', model=MODEL,
                                    thinking=False, max_tokens=2048, timeout_s=300., max_concurrency=6))
    cache = ReplyCache(HERE / 'judge-cache.sqlite')
    journal = sqlite3.connect(HERE / 'rejudge.sqlite')
    journal.execute('CREATE TABLE IF NOT EXISTS verdicts(checkpoint_id TEXT PRIMARY KEY, position INTEGER NOT NULL, verdict TEXT NOT NULL)')
    completed = {row[0] for row in journal.execute('SELECT checkpoint_id FROM verdicts')}
    pending = [(i, pred) for i, pred in enumerate(preds) if pred['checkpoint_id'] not in completed]
    print(f'Rejudging {len(pending)} pending checkpoints; {len(completed)} already durable', flush=True)

    def one(item):
        position, pred = item
        row = judge_all([pred], anns, chat=chat, cache=cache, repo=HERE / 'frozen-govmem', workers=1)[0]
        if row.get('judge_fallback') or not row.get('llm_judged'):
            raise RuntimeError('LLM judge unavailable for ' + pred['checkpoint_id'] + '; refusing to publish a rule-only fallback')
        return position, row

    def export(complete=False):
        rows = [json.loads(row[0]) for row in journal.execute('SELECT verdict FROM verdicts ORDER BY position')]
        write_jsonl(HERE / 'verdicts.jsonl', rows)
        metrics = aggregate(rows)
        if not any(r['query_type'] == 'utility' for r in rows):
            for key in ['U', 'MGS', 'MGS_strict', 'OR']:
                metrics[key] = None
        for category, fields in [('privacy', ['A', 'A_strict']), ('safety', ['F', 'F_strict'])]:
            if not any(r['query_type'] == category for r in rows):
                for field in fields:
                    metrics[field] = None
        report = {'mode': 'govmem-full-rejudge', 'complete': complete, 'judge_model': MODEL,
                  'coverage': {'expected': len(preds), 'rejudged': len(rows),
                               'context_unknown': 0, 'exact_checkpoint_coverage': {r['checkpoint_id'] for r in rows} == set(anns)},
                  'metrics': metrics, 'context_recovery': 'context_recovery.json',
                  'strict_semantics': 'privacy/deletion answer leak OR protected pattern in complete model memory context',
                  'original_exports_unchanged': True, 'updated_utc': now()}
        write_json(HERE / 'summary.json', report)
        write_json(HERE / 'scores.json', metrics)
        status('complete' if complete else 'rejudging', completed=len(rows), expected=len(preds), metrics=metrics)
        return report

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(one, item): item for item in pending}
        for future in as_completed(futures):
            position, row = future.result()
            with journal:
                journal.execute('INSERT INTO verdicts VALUES (?,?,?)', (row['checkpoint_id'], position, encoded(row)))
            completed.add(row['checkpoint_id'])
            export()
            print(f'[{now()}] rejudged {len(completed)}/{len(preds)}: {row["checkpoint_id"]}', flush=True)
    report = export(complete=True)
    assert report['coverage']['exact_checkpoint_coverage']
    verify_inputs()
    print(json.dumps(report, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    prepare()
    if not args.prepare_only:
        judge()


if __name__ == '__main__':
    try:
        main()
    except BaseException as exc:
        status('failed', error_type=type(exc).__name__, error=str(exc))
        traceback.print_exc()
        raise
