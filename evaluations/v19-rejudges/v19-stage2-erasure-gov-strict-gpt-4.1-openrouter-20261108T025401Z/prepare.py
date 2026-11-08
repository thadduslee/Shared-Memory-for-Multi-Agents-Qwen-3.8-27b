#!/usr/bin/env python3
"""Rebuild each answer's recorded memory packet for the 21-episode Stage 2 + erasure run, adapt the
predictions to govmem's memory_audit.prompt_context field, and pin everything in manifest.json.

Same reconstruction as v19-gov-strict-gpu03-20261106T170432Z/run_rejudge.py prepare(), applied per
episode because each episode has its own memory.sqlite / memory.privacy.sqlite. Runtime stores are opened
read-only. No new retrieval: the packet is exactly the recorded input_manifest.memory_ids, in order, with
text from immutable attribution-view source selectors over the original fixture.

Difference from the earlier run: 20 predictions retrieved memory but made no answer-model call
(19 deterministic subject-comparison refusals, 1 echo-filtered no_evidence). The earlier prepare() asserted
this never happened. Here the full recorded packet is still used as the context (strict, same method as the
earlier strict jobs), and prompt_context.answer_model_called=false marks these so a sensitivity metric can
treat their context as empty.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

HERE = Path(__file__).resolve().parent
RUN = Path('/home/thaddus/v19-evaluations/v19-stage2-erasure-full-20261107T155701Z')
FIXTURE = Path('/home/thaddus/a-mem-variant/evaluations/v19/v19-model-controls-full-soclaas-20261104T103839Z/frozen-dataset/bench/data/medical')
GOV = Path('/home/thaddus/a-mem-gov')
ORIGINAL_GPT41 = Path('/home/thaddus/a-mem-variant/evaluations/v19-rejudges/v19-stage2-erasure-original-gpt-4.1-openrouter-20261108T024741Z')


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def write_json(path, value):
    tmp = Path(path).with_suffix(Path(path).suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def write_jsonl(path, rows):
    tmp = Path(path).with_suffix(Path(path).suffix + '.tmp')
    tmp.write_text(''.join(encoded(row) + '\n' for row in rows))
    tmp.replace(path)


def ro(path):
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)


def main():
    assert not (HERE / 'manifest.json').exists(), 'Already prepared; inputs are pinned'
    inputs = HERE / 'inputs'
    episodes_dir = sorted(d for d in (RUN / 'results').iterdir() if d.is_dir())
    assert len(episodes_dir) == 21
    for name in ('episodes.jsonl', 'checkpoints.jsonl'):
        shutil.copy2(FIXTURE / name, inputs / name)
    anns = {c['checkpoint_id']: c for c in read_jsonl(inputs / 'checkpoints.jsonl')}
    assert anns == {c['checkpoint_id']: c for c in read_jsonl(GOV / 'bench/data/medical/checkpoints.jsonl')}, 'Fixture mismatch'
    for d in episodes_dir:
        (inputs / 'episodes' / d.name).mkdir(parents=True, exist_ok=True)
        for name in ('manifest.json', 'summary.json'):
            shutil.copy2(d / name, inputs / 'episodes' / d.name / name)
    (inputs / 'original_official_predictions.jsonl').write_text(
        ''.join((d / 'official_predictions.jsonl').read_text() for d in episodes_dir))
    (inputs / 'original_scores.jsonl').write_text(''.join((d / 'scores.jsonl').read_text() for d in episodes_dir))
    shutil.copy2(RUN / 'summary.json', inputs / 'original_summary.json')
    shutil.copy2(ORIGINAL_GPT41 / 'scores.jsonl', inputs / 'original_gpt41_scores.jsonl')
    shutil.copy2(ORIGINAL_GPT41 / 'summary.json', inputs / 'original_gpt41_summary.json')

    sources, positions = {}, {}
    for episode in read_jsonl(inputs / 'episodes.jsonl'):
        eid = episode['episode_id']
        compartment = 'encounter:' + digest(eid)[:24]
        for position, turn in enumerate(episode['turns']):
            sid = 'permission-source:' + digest([compartment, turn['turn_id']])
            sources[sid] = {'text': turn['text'], 'episode_id': eid, 'turn_id': turn['turn_id']}
            positions[(eid, turn['turn_id'])] = position

    records, adapted, counts, validation, no_call = {}, [], Counter(), Counter(), Counter()
    for d in episodes_dir:
        preds = read_jsonl(d / 'official_predictions.jsonl')
        content, privacy = ro(d / 'memory.sqlite'), ro(d / 'memory.privacy.sqlite')
        history = {(cid, v): json.loads(body)['candidate'] for cid, v, body in
                   privacy.execute('SELECT candidate_id,version,body FROM attribution_history_v19')}
        views = defaultdict(list)
        for cid, v, rid in privacy.execute('SELECT candidate_id,version,resource_id FROM attribution_views_v19'):
            views['note:' + rid].append((cid, v))
        for rid in sorted({rid for p in preds for rid in p['input_manifest']['memory_ids']}):
            choices = []
            for key in views[rid]:
                metadata = history.get(key)
                if metadata and metadata.get('source_id') in sources:
                    source = sources[metadata['source_id']]
                    assert all(0 <= a < b <= len(source['text']) for a, b in metadata['spans'])
                    choices.append(('; '.join(source['text'][a:b] for a, b in metadata['spans']), metadata, source))
            assert choices and len({c[0] for c in choices}) == 1, ('Ambiguous context', d.name, rid)
            text, metadata, source = choices[0]
            row = content.execute('SELECT payload,state,kind FROM resources WHERE id=?', (rid,)).fetchone()
            assert row and row[2] == 'memory', (d.name, rid)
            saved = json.loads(row[0]).get('text')
            if saved is not None:
                assert saved == text, ('Projection does not match stored packet bytes', d.name, rid)
                method = 'exact_match_to_surviving_resource'
            else:
                assert row[1] == 'deleted', (d.name, rid)
                method = 'historical_view_spans_over_original_fixture'
            assert rid not in records, ('Record id shared across episodes', rid)
            counts[method] += 1
            records[rid] = {'id': rid, 'run_episode': d.name, 'text': text,
                            'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'recovery_method': method,
                            'candidate_id': metadata['id'], 'attribution_version': metadata['attribution_version'],
                            'source_id': metadata['source_id'], 'source_spans': metadata['spans'],
                            'episode_id': source['episode_id'], 'source_turn_id': source['turn_id']}
        content.close()
        privacy.close()
        for pred in preds:
            cp = anns[pred['checkpoint_id']]
            mids = pred['input_manifest']['memory_ids']
            assert len(mids) == len(set(mids))
            receipt = pred['input_manifest']['answer_validation']
            called = receipt.get('generation_calls', 0) == 1
            if mids and not called:
                # subject_comparison_refused: deterministic refusal before any model call.
                # no_evidence: every packet record was echo-filtered; at most one host-only scope
                # classification call (answering.review_absence) sees the query, never the evidence.
                assert receipt.get('generation_calls') == 0 and receipt.get('verification_calls') == 0, \
                    ('Unexplained packet', pred['checkpoint_id'])
                assert (receipt['status'] == 'subject_comparison_refused' and receipt.get('policy_calls') == 0) or \
                    (receipt['status'] == 'no_evidence' and receipt.get('policy_calls') in (0, 1)), pred['checkpoint_id']
                no_call[receipt['status']] += 1
            else:
                assert receipt.get('generation_calls', 0) == int(bool(mids)), ('Cannot prove packet reached model', pred['checkpoint_id'])
            for rid in mids:
                record = records[rid]
                assert record['episode_id'] == pred['episode_id'] and record['run_episode'] == d.name
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
                'record_ids': mids, 'reconstructed': True, 'answer_model_called': called or not mids,
                'method': 'Recorded model input IDs plus immutable historical source selectors; no new retrieval',
                'scope': 'Complete recorded memory evidence packet for answer generation; excludes query and evaluator annotations',
            }}
            assert {k: v for k, v in output.items() if k != 'memory_audit'} == pred['output']
            adapted.append({'checkpoint_id': pred['checkpoint_id'], 'episode_id': pred['episode_id'],
                            'as_of_turn_id': pred['as_of_turn_id'], 'asker': cp['asker'],
                            'query_text': cp['query_text'], 'output': output})
    assert len(adapted) == len({p['checkpoint_id'] for p in adapted}) == 579 and {p['checkpoint_id'] for p in adapted} == set(anns)
    write_jsonl(inputs / 'context_records.jsonl', records.values())
    write_jsonl(inputs / 'predictions.jsonl', adapted)
    proof = {'all_contexts_recovered': True, 'unknown_contexts': 0, 'checkpoints': len(adapted),
             'unique_evidence_records': len(records), 'record_recovery': dict(counts), 'validation': dict(validation),
             'packets_without_answer_model_call': dict(no_call),
             'packets_without_answer_model_call_policy': 'Full recorded packet counted as context (strict); answer_model_called=false marks them for the sensitivity metric',
             'no_context_queries_or_gold_labels': True, 'evidence_order_preserved': True, 'source_prefix_checked': True,
             'note': 'Deleted historical text is reconstructed only for evaluation of the already recorded benchmark inputs. Runtime stores are read-only.'}
    write_json(HERE / 'context_recovery.json', proof)
    original = {}
    for d in episodes_dir:
        for name in ('official_predictions.jsonl', 'scores.jsonl', 'summary.json', 'manifest.json',
                     'memory.sqlite', 'memory.privacy.sqlite'):
            original[str(d / name)] = sha(d / name)
    for p in (RUN / 'summary.json', ORIGINAL_GPT41 / 'scores.jsonl', ORIGINAL_GPT41 / 'summary.json'):
        original[str(p)] = sha(p)
    files = sorted(p for top in ('inputs', 'frozen-govmem') for p in (HERE / top).rglob('*') if p.is_file())
    manifest = {
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'source_run': str(RUN), 'source_code_commit': (RUN / 'base-commit.txt').read_text().strip(),
        'gov_repository': str(GOV),
        'gov_revision': subprocess.check_output(['git', '-C', str(GOV), 'rev-parse', 'HEAD'], text=True).strip(),
        'gov_worktree_clean': subprocess.check_output(['git', '-C', str(GOV), 'status', '--porcelain'], text=True) == '',
        'judge': {'provider': 'openrouter', 'base_url': 'https://openrouter.ai/api/v1', 'model': 'openai/gpt-4.1',
                  'thinking': False, 'max_tokens': 2048, 'temperature': 0.0, 'workers': 6,
                  'transport': 'Unmodified frozen govmem.llm.ChatClient; response hook records model, provider, usage and rejects model mismatch'},
        'scoring': 'Unmodified frozen govmem.eval.judge: rule + LLM merge; exact pattern context exposure; strict OR; unchanged U between normal and strict',
        'context_recovery': proof,
        'input_sha256': {str(p.relative_to(HERE)): sha(p) for p in files},
        'python': sys.version, 'original_files_sha256': original,
        'strict_benchmark_template': '/home/thaddus/a-mem-variant/evaluations/v19-rejudges/v19-gov-strict-gpt-4.1-openrouter-20261107T023151Z',
        'runner_sha256': sha(HERE / 'run_rejudge.py'),
        'prepare_sha256': sha(HERE / 'prepare.py'),
    }
    write_json(HERE / 'manifest.json', manifest)
    print(json.dumps(proof, indent=2))


if __name__ == '__main__':
    main()
