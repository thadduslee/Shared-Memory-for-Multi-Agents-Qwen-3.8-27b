"""Re-score immutable GateMem predictions with an independent OpenRouter judge.

This deliberately writes a sibling artifact directory.  It never updates the
source run's ``evaluation.sqlite`` or ``scores.jsonl``.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

from agentic_memory.privacy.processors import CompletionProcessor

from .adapter import load_data
from .runner import ROOT, load_environment
from .scoring import GateMemScorer


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def digest_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_jsonl(path):
    rows = []
    ids = set()
    for line_number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        checkpoint_id = row.get('checkpoint_id')
        if not isinstance(checkpoint_id, str) or checkpoint_id in ids:
            raise ValueError(f'{path}: invalid or duplicate checkpoint_id at line {line_number}')
        ids.add(checkpoint_id)
        rows.append(row)
    return rows


def write_json(path, value):
    temporary = Path(path).with_suffix(Path(path).suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    temporary.replace(path)


def export(output_dir, journal, scorer, manifest, expected_ids):
    rows = [json.loads(row[0]) for row in journal.execute(
        'SELECT score FROM judgments ORDER BY position')]
    temporary = output_dir / 'scores.jsonl.tmp'
    temporary.write_text(''.join(encoded(row) + '\n' for row in rows))
    temporary.replace(output_dir / 'scores.jsonl')
    metrics = scorer.aggregate(rows)
    for count, keys in (
        ('n_utility_scored', ['utility_accuracy']),
        ('n_privacy_scored', ['privacy_leakage_rate', 'privacy_e2e_leakage_rate']),
        ('n_safety_scored', ['deletion_leakage_rate', 'deletion_e2e_leakage_rate']),
    ):
        if not metrics.get(count):
            for key in keys:
                metrics[key] = None
    if not all(metrics.get(key) for key in ('n_utility_scored', 'n_privacy_scored', 'n_safety_scored')):
        metrics['compliance_utility_score'] = None
        metrics['compliance_utility_e2e_score'] = None
    report = {
        'mode': 'independent-rejudge',
        'source': manifest['source'],
        'judge': manifest['judge'],
        'scoring_sha256': manifest['scoring_sha256'],
        'coverage': {
            'expected_checkpoints': len(expected_ids),
            'rejudged_checkpoints': len(rows),
            'exact_checkpoint_coverage': {row['checkpoint_id'] for row in rows} == expected_ids,
        },
        'metrics': metrics,
        'complete': len(rows) == len(expected_ids) and {row['checkpoint_id'] for row in rows} == expected_ids,
    }
    write_json(output_dir / 'summary.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path, required=True,
                        help='Completed run containing official_predictions.jsonl')
    parser.add_argument('--output-dir', type=Path,
                        help='Separate directory for the OpenRouter judgments (default: <source>-gpt-4.1-openrouter)')
    parser.add_argument('--gatemem-repo', type=Path, default=Path('/home/thaddus/GateMem'))
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--preflight', action='store_true', help='Validate the OpenRouter key and model with one small request')
    parser.add_argument('--max-checkpoints', type=int, help='Additional checkpoints this invocation; useful for a cost-controlled trial')
    args = parser.parse_args(argv)
    os.umask(0o077)
    load_environment(args.env_file)

    key = os.getenv('AMEM_OPENROUTER_API_KEY', '')
    if not key:
        parser.error('AMEM_OPENROUTER_API_KEY is required; set it in the private .env file or environment')
    base_url = os.getenv('AMEM_OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1')
    model = os.getenv('AMEM_OPENROUTER_JUDGE_MODEL', 'openai/gpt-4.1')
    judge = CompletionProcessor('openrouter:independent-judge', base_url, model, key,
                                timeout=float(os.getenv('AMEM_OPENROUTER_TIMEOUT', '300')),
                                max_tokens=int(os.getenv('AMEM_OPENROUTER_JUDGE_MAX_TOKENS', '2048')))
    if args.preflight:
        result = judge.complete('Return exactly JSON {"ok":true}.', {'probe': True})
        if result.get('ok') is not True:
            raise ValueError('OpenRouter preflight did not return {"ok": true}')
        print(f'OpenRouter judge {model} available')
        return 0

    source_run = args.source_run.resolve()
    predictions_path = source_run / 'official_predictions.jsonl'
    source_manifest_path = source_run / 'manifest.json'
    if not predictions_path.is_file() or not source_manifest_path.is_file():
        parser.error('--source-run must contain manifest.json and official_predictions.jsonl')
    predictions = read_jsonl(predictions_path)
    episodes, checkpoints = load_data(args.gatemem_repo / 'bench/data/medical')
    del episodes
    checkpoint_by_id = {checkpoint['checkpoint_id']: checkpoint for checkpoint in checkpoints}
    prediction_ids = {prediction['checkpoint_id'] for prediction in predictions}
    if prediction_ids != set(checkpoint_by_id):
        raise ValueError('Source predictions do not exactly match the pinned GateMem checkpoint set')

    output_dir = (args.output_dir or Path(str(source_run) + '-gpt-4.1-openrouter')).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    lock = (output_dir / 'rejudge.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = {
        'source': {
            'run_dir': str(source_run),
            'manifest_sha256': digest_file(source_manifest_path),
            'official_predictions_sha256': digest_file(predictions_path),
        },
        'judge': {'provider': 'openrouter', 'base_url': base_url, 'model': model},
        'scoring_sha256': digest_file(Path(__file__).with_name('scoring.py')),
    }
    journal = sqlite3.connect(output_dir / 'rejudge.sqlite')
    try:
        journal.executescript('''
          CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS judgments(checkpoint_id TEXT PRIMARY KEY, position INTEGER NOT NULL, score TEXT NOT NULL);
        ''')
        stored = journal.execute("SELECT value FROM state WHERE key='manifest'").fetchone()
        if stored:
            if not args.resume:
                raise ValueError('Rejudge output exists; use --resume or choose a new --output-dir')
            if json.loads(stored[0]) != manifest:
                raise ValueError('Source predictions, judge configuration, or scoring changed; choose a new --output-dir')
        elif args.resume:
            raise ValueError('No saved rejudge state; omit --resume')
        else:
            with journal:
                journal.execute("INSERT INTO state VALUES ('manifest', ?)", (encoded(manifest),))
        # Recreate the human-readable copy even if an earlier interrupted run
        # committed the durable manifest before its file export.
        write_json(output_dir / 'manifest.json', manifest)

        scorer = GateMemScorer(args.gatemem_repo, judge)
        completed = {row[0] for row in journal.execute('SELECT checkpoint_id FROM judgments')}
        run_count = 0
        for position, prediction in enumerate(predictions):
            checkpoint_id = prediction['checkpoint_id']
            if checkpoint_id in completed:
                continue
            if args.max_checkpoints is not None and run_count >= args.max_checkpoints:
                break
            score = scorer.score(checkpoint_by_id[checkpoint_id], prediction)
            score['checkpoint_id'] = checkpoint_id
            with journal:
                journal.execute('INSERT INTO judgments VALUES (?, ?, ?)',
                                (checkpoint_id, position, encoded(score)))
            run_count += 1
            print(f'rejudged {len(completed) + run_count}/{len(predictions)}: {checkpoint_id}', flush=True)
        report = export(output_dir, journal, scorer, manifest, prediction_ids)
        print(json.dumps(report, indent=2), flush=True)
        return 0
    finally:
        journal.close()
        lock.close()


if __name__ == '__main__':
    sys.exit(main())
