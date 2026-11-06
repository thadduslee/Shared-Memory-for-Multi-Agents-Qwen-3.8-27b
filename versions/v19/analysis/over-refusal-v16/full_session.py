"""Persistent full-run supervisor and read-only live progress display.

Run in two tmux windows: `full_session.py run RUN` and `full_session.py watch RUN`.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def command(script, *args):
    return [sys.executable, '-u', str(HERE / script), *map(str, args)]


def launch(run, *, resume=False):
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUNBUFFERED='1',
               HF_HUB_OFFLINE='1', CUDA_VISIBLE_DEVICES='',
               AMEM_OPENROUTER_PROVIDER_SORT='latency',
               AMEM_OPENROUTER_PROVIDER_IGNORE='deepinfra,coreweave,reka')
    argv = command('run.py', '--run-dir', run, '--full', '--version', '16')
    if resume:
        argv.append('--resume')
        previous = run / 'invocations' / ('before-resume-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        previous.mkdir(parents=True)
        import shutil
        for name in ('launch.json', 'process.json', 'exit-status.json', 'early-stop.json',
                     'audit-status.json', 'implementation-audit.json', 'implementation-audit.log', 'progress.json'):
            path = run / name
            if path.exists(): shutil.copy2(path, previous / name)
        for name in ('exit-status.json', 'audit-status.json'):
            (run / name).unlink(missing_ok=True)
    write(run / 'launch.json', {'started_at': stamp(), 'command': argv,
          'requested_checkpoints': 579, 'complete_source_schedule': True,
          'resume': resume,
          'stop_on_utility_failure': False, 'stop_on_scored_content_leak': False,
          'provider_sort': 'latency', 'provider_ignore': ['deepinfra', 'coreweave', 'reka']})
    with (run / 'launch-history.jsonl').open('a') as out:
        out.write(json.dumps(json.loads((run / 'launch.json').read_text())) + '\n')
    with (run / 'run.log').open('a', buffering=1) as log:
        print('Starting all 579 checkpoints and source tails: ' + str(run), flush=True)
        child = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        write(run / 'process.json', {'supervisor_pid': os.getpid(), 'runner_pid': child.pid,
                                    'started_at': stamp()})
        for line in child.stdout:
            log.write(line)
            print(line, end='', flush=True)
        status = child.wait()
    write(run / 'exit-status.json', {'finished_at': stamp(), 'exit_code': status})
    print('Runner exited with status ' + str(status), flush=True)
    if (run / 'model-trace.jsonl').exists():
        with (run / 'implementation-audit.log').open('w') as out:
            audit = subprocess.run(command('audit.py', '--run', run), cwd=ROOT, env=env,
                                   stdout=out, stderr=subprocess.STDOUT)
        write(run / 'audit-status.json', {'exit_code': audit.returncode, 'finished_at': stamp()})
    return status


def snapshot(run):
    summary = json.loads((run / 'summary.json').read_text()) if (run / 'summary.json').exists() else {}
    metrics = dict(summary.get('metrics', {}))
    if not metrics.get('n_utility_scored'):
        metrics['over_refusal_rate'] = None
    result = {'updated_at': stamp(), 'requested_checkpoints': 579,
              'status': 'running' if (run / 'process.json').exists() else 'starting',
              'coverage': summary.get('coverage', {}), 'metrics': metrics,
              'summary_complete': summary.get('complete', False)}
    for database, key, query in [
        ('memory.privacy.sqlite', 'source_jobs', 'SELECT status,COUNT(*) FROM permission_jobs GROUP BY status'),
        ('memory.sqlite', 'source_timings', "SELECT COUNT(*),AVG(seconds) FROM pipeline_timings WHERE stage='source_total'"),
        ('evaluation.sqlite', 'checkpoint_counts', 'SELECT COUNT(*),COUNT(score) FROM official')]:
        path = run / database
        if not path.exists():
            continue
        try:
            with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=1) as db:
                rows = db.execute(query).fetchall()
            if key == 'source_jobs': result[key] = dict(rows)
            elif key == 'source_timings': result[key] = {'count': rows[0][0], 'mean_seconds': rows[0][1]}
            else: result[key] = {'predictions': rows[0][0], 'scored': rows[0][1]}
        except sqlite3.Error as exc:
            result[key + '_waiting'] = str(exc)
    if (run / 'exit-status.json').exists():
        result['exit'] = json.loads((run / 'exit-status.json').read_text())
        result['status'] = 'finished' if result['exit']['exit_code'] == 0 else 'stopped'
    return result


def watch(run, once=False):
    attempted_comparison = False
    while True:
        value = snapshot(run)
        write(run / 'progress.json', value)
        with (run / 'progress-history.jsonl').open('a') as out:
            out.write(json.dumps(value) + '\n')
        counts = value.get('checkpoint_counts', {})
        metrics = value['metrics']
        latency = value.get('source_timings', {}).get('mean_seconds')
        print(f"{value['updated_at']} | {value['status']} | scored {counts.get('scored', 0)}/579"
              f" | sources {value.get('source_jobs', {})} | mean/source {latency} s"
              f" | utility {metrics.get('utility_accuracy')} | over-refusal {metrics.get('over_refusal_rate')}"
              f" | privacy {metrics.get('privacy_e2e_leakage_rate')} | deletion {metrics.get('deletion_e2e_leakage_rate')}",
              flush=True)
        if value['coverage'].get('scored_checkpoints', 0) >= 25 and not attempted_comparison:
            attempted_comparison = True
            with (run / 'comparison-first25.log').open('w') as out:
                comparison = subprocess.run(command('compare.py', '--candidate', run, '--count', 25,
                    '--output', run / 'comparison-first25.json'), cwd=ROOT,
                    env=dict(os.environ, PYTHONPATH=str(ROOT)), stdout=out, stderr=subprocess.STDOUT)
            print('First-25 comparison exit status: ' + str(comparison.returncode), flush=True)
        if once or value['status'] in {'finished', 'stopped'}:
            return
        time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['run', 'watch', 'once'])
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    run = args.run_dir.resolve()
    run.mkdir(parents=True, exist_ok=True)
    if args.mode == 'run': return launch(run, resume=args.resume)
    watch(run, once=args.mode == 'once')


if __name__ == '__main__':
    raise SystemExit(main())
