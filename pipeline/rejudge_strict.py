#!/usr/bin/env python3
"""Strict independent rejudge of a finished run with the frozen govmem judge and GPT-4.1 on OpenRouter.

    python pipeline/rejudge_strict.py runs/<domain> [--env-file pipeline/config/openrouter.env]
    python pipeline/rejudge_strict.py runs/<domain> --replay-only      # no API calls; cache must cover everything

Scorer: data/govmem-strict-judge (a-mem-gov a297102, src/govmem/eval/judge.py and
bench/prompts/judge_prompt.txt, unchanged). judge_all() merges the benchmark's answer
rules with a fresh LLM verdict and runs exposure() on the answer model's memory context.
The judge prompt's asker and question come from the checkpoint's agent-visible
fields. The context is output.memory_audit.prompt_context.text when a prediction has it,
otherwise the joined output.retrieved_memory[].text spans the harness saved. This is how
runs/medical/gpt-4.1-strict-rejudge was made, and --replay-only against its saved cache
reproduces its verdicts.jsonl byte for byte (tests/test_pipeline.py).

Outputs in <run>/gpt-4.1-strict-rejudge/: manifest.json, verdicts.jsonl (dataset order),
scores.json and summary.json (the same report), model_calls.jsonl, and the reply cache
(prompt-hash -> reply): cache/judge.sqlite while running, plus cache/judge-cache.jsonl, a
sorted export that git tracks. A rerun or resume, on any machine, seeds the SQLite cache
from that file and makes no repeated calls. A rule-only fallback for any checkpoint fails
the run rather than being scored.
"""
import argparse
import fcntl
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

sys.path.insert(0, str(common.STRICT_JUDGE / 'src'))
from govmem.adapters.gatemem.data import annotation, visible_checkpoint  # noqa: E402
from govmem.eval.judge import aggregate, judge_all  # noqa: E402
from govmem.llm import ReplyCache  # noqa: E402

GOV_COMMIT = 'a297102d7e707c616c99a8e274f912d6c8e81b3d'
CONTEXT_SOURCE = 'memory_audit.prompt_context.text when present; otherwise joined saved output.retrieved_memory[].text'


def now():
    return datetime.now(timezone.utc).isoformat()


def with_context(pred, checkpoint):
    """Adapt a harness prediction to the govmem judge, as the medical strict rejudge did.

    The judge prompt shows the asker and the question, which harness predictions do not
    repeat, so they come from the checkpoint's agent-visible fields (visible_checkpoint:
    the asker's ID and role and the query text; no annotations). The memory context is
    the answer model's audited prompt context, or else the saved retrieved spans.
    """
    visible = visible_checkpoint(checkpoint)
    pred = dict(pred, asker=visible['asker'], query_text=visible.get('query_text', ''))
    audit = (pred['output'].get('memory_audit') or {}).get('prompt_context') or {}
    if audit.get('text'):
        return pred, 'saved_prompt_context'
    text = '\n'.join(span.get('text', '') for span in pred['output'].get('retrieved_memory') or [])
    out = dict(pred, output=dict(pred['output'], memory_audit={'prompt_context': {'text': text}}))
    return out, 'retrieved_memory_text'


CACHE_EXPORT = 'judge-cache.jsonl'


def seed_cache(cache, path):
    """Load a tracked judge-cache.jsonl into the SQLite reply cache (git ignores the SQLite file)."""
    if path.is_file():
        for row in common.read_jsonl(path):
            if cache.get(row['key']) is None:
                cache.put(row['key'], row['reply'])


def export_cache(db_path, path):
    """Every cached judge reply as sorted JSONL, small enough to keep in git for replay."""
    import sqlite3
    if not db_path.is_file():
        return
    with sqlite3.connect(db_path) as db:
        rows = sorted(db.execute('SELECT k, v FROM cache'))
    path.write_text(''.join(json.dumps({'key': k, 'reply': v}, ensure_ascii=False) + '\n' for k, v in rows))


class NoCalls:
    """A chat client for --replay-only: any cache miss is an error, never a model call."""

    def chat(self, *args, **kwargs):
        raise RuntimeError('replay-only: judge cache has no reply for this checkpoint')


def load_env(path):
    if path and Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                if key.strip().startswith('AMEM_'):
                    os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def openrouter_chat(out, model, workers):
    from govmem.config import ModelEndpoint
    from govmem.llm import ChatClient
    key = os.environ.get('AMEM_OPENROUTER_API_KEY', '')
    if not key:
        raise SystemExit('AMEM_OPENROUTER_API_KEY is required (pipeline/config/openrouter.env)')
    chat = ChatClient(ModelEndpoint(base_url=os.environ.get('AMEM_OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1'),
                                    api_key=key, model=model, thinking=False, max_tokens=2048, timeout_s=300.,
                                    max_concurrency=workers))
    lock = threading.Lock()

    def audit(response):
        if response.status_code != 200:
            return
        response.read()
        data = response.json()
        entry = {'at_utc': now(), 'response_id': data.get('id'), 'model': data.get('model'),
                 'provider': data.get('provider'), 'usage': data.get('usage') or {}}
        with lock, (out / 'model_calls.jsonl').open('a') as fh:
            fh.write(json.dumps(entry) + '\n')
        if data.get('model') != model:
            raise RuntimeError(f'OpenRouter answered with {data.get("model")}, not {model}')

    chat._http.event_hooks['response'].append(audit)
    return chat


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run_dir', type=Path)
    ap.add_argument('--out', type=Path, help='default: <run>/gpt-4.1-strict-rejudge')
    ap.add_argument('--env-file', type=Path, default=common.REPO / 'pipeline/config/openrouter.env')
    ap.add_argument('--model', default='openai/gpt-4.1')
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--replay-only', action='store_true', help='make no API calls; every reply must be cached')
    args = ap.parse_args(argv)
    os.umask(0o077)
    run = args.run_dir.resolve()
    domain = common.run_domain(run)
    out = (args.out or run / 'gpt-4.1-strict-rejudge').resolve()
    out.mkdir(parents=True, exist_ok=True)
    lock = (out / 'rejudge.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    lines, files = common.combined_predictions(run, domain)
    preds = [json.loads(line) for line in lines]
    cps = {cp['checkpoint_id']: cp for cp in common.checkpoints(domain)}
    anns = {cid: annotation(cp) for cid, cp in cps.items()}
    judged, sources = zip(*(with_context(p, cps[p['checkpoint_id']]) for p in preds))
    manifest = {
        'mode': 'strict-independent-rejudge', 'domain': domain, 'source_run': str(run),
        'source_episode_files': files,
        'benchmark_repo': 'data/govmem-strict-judge (a-mem-gov)', 'benchmark_commit': GOV_COMMIT,
        'benchmark_files': {name: common.sha256(common.DATA / 'bench/data' / domain / name)
                            for name in ('episodes.jsonl', 'checkpoints.jsonl')},
        'strict_judge_file_sha256': common.sha256(common.STRICT_JUDGE / 'src/govmem/eval/judge.py'),
        'judge_prompt_sha256': common.sha256(common.STRICT_JUDGE / 'bench/prompts/judge_prompt.txt'),
        'judge': {'provider': 'openrouter', 'base_url': 'https://openrouter.ai/api/v1', 'model': args.model},
        'context_source': CONTEXT_SOURCE,
        'context_source_counts': {k: sources.count(k) for k in ('saved_prompt_context', 'retrieved_memory_text')},
        'expected_checkpoints': len(preds),
    }
    if (out / 'manifest.json').exists():
        old = json.loads((out / 'manifest.json').read_text())
        if {k: v for k, v in old.items() if k != 'source_run'} != {k: v for k, v in manifest.items() if k != 'source_run'}:
            raise SystemExit(f'{out} holds a rejudge of different inputs; choose another --out')
    common.write_json(out / 'manifest.json', manifest)

    if args.replay_only:
        chat = NoCalls()
    else:
        load_env(args.env_file)
        chat = openrouter_chat(out, args.model, args.workers)
    cache = ReplyCache(out / 'cache' / 'judge.sqlite')
    seed_cache(cache, out / 'cache' / CACHE_EXPORT)
    try:
        rows = judge_all(list(judged), anns, chat=chat, cache=cache, repo=common.STRICT_JUDGE, workers=args.workers)
    finally:
        export_cache(out / 'cache' / 'judge.sqlite', out / 'cache' / CACHE_EXPORT)
    fallbacks = [r['checkpoint_id'] for r in rows if r.get('judge_fallback') or not r.get('llm_judged')]
    if fallbacks:
        raise SystemExit(f'{len(fallbacks)} checkpoints have no LLM verdict (first: {fallbacks[0]}); '
                         'rerun to retry them (cached verdicts are kept)')
    (out / 'verdicts.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False, separators=(',', ':')) + '\n'
                                                for r in rows))
    metrics = aggregate(rows)
    # scores.json and summary.json hold the same report, as in the archived medical rejudge.
    summary = {'mode': manifest['mode'], 'domain': domain, 'source_run': str(run), 'judge': manifest['judge'],
               'benchmark': {'repo': 'a-mem-gov', 'commit': GOV_COMMIT,
                             'judge_sha256': manifest['strict_judge_file_sha256'],
                             'prompt_sha256': manifest['judge_prompt_sha256']},
               'coverage': {'expected_checkpoints': len(preds), 'rejudged_checkpoints': len(rows),
                            'exact_checkpoint_coverage': [r['checkpoint_id'] for r in rows] == [p['checkpoint_id'] for p in preds]},
               'context_source': CONTEXT_SOURCE, 'context_source_counts': manifest['context_source_counts'],
               'metrics': metrics, 'complete': True}
    common.write_json(out / 'summary.json', summary)
    common.write_json(out / 'scores.json', summary)
    m = metrics
    print(f"{domain} strict rejudge: U={m['U']:.4f} A={m['A']:.4f} F={m['F']:.4f} MGS={m['MGS']:.4f} "
          f"A_strict={m['A_strict']:.4f} F_strict={m['F_strict']:.4f} MGS_strict={m['MGS_strict']:.4f} -> {out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
