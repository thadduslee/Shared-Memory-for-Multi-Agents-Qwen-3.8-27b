"""Freeze and supervise a v20 evaluation using only the existing loopback API.

No model servers are started. The legacy --allow-paid-api runner flag is its
live-call opt-in; both actual provider URLs are restricted to localhost here.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def environment(run):
    # Do not inherit OpenRouter URLs, credentials, routing or token overrides.
    env = {k: v for k, v in os.environ.items() if not k.startswith('AMEM_')}
    env.update(PYTHONPATH=str(run / 'frozen-source'), PYTHONUNBUFFERED='1',
               HF_HUB_OFFLINE='1', CUDA_VISIBLE_DEVICES='',
               TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='2',
               MKL_NUM_THREADS='2')
    for line in (run / 'local.env').read_text().splitlines():
        if line and not line.startswith('#'):
            key, value = line.split('=', 1)
            env[key] = value
    for role in ('LLM', 'JUDGE'):
        url = urlsplit(env['AMEM_' + role + '_BASE_URL'])
        if url.hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise ValueError('This launcher only permits the existing loopback API')
    return env


def prepare(run):
    if run.exists():
        raise ValueError('Use a fresh run directory; existing runs are preserved')
    run.mkdir(parents=True, mode=0o700)
    frozen = run / 'frozen-source'
    frozen.mkdir()
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache')
    shutil.copytree(ROOT / 'agentic_memory', frozen / 'agentic_memory', ignore=ignore)
    (frozen / 'evaluation').mkdir()
    shutil.copy2(ROOT / 'evaluation/__init__.py', frozen / 'evaluation/__init__.py')
    shutil.copytree(ROOT / 'evaluation/gatemem_medical',
                    frozen / 'evaluation/gatemem_medical', ignore=ignore)
    scripts = frozen / 'analysis/workflow-v20'
    scripts.mkdir(parents=True)
    for name in ('local_session.py', 'compare_runs.py'):
        shutil.copy2(ROOT / 'analysis/workflow-v20' / name, scripts / name)
    hashes = {str(p.relative_to(frozen)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(frozen.rglob('*')) if p.is_file()}
    write(run / 'frozen-source-hashes.json', hashes)
    with tarfile.open(run / 'evaluated-source.tar.gz', 'w:gz') as archive:
        archive.add(frozen, arcname='frozen-source')
    # Pin the same cached embedding snapshot as the diagnostic v19 run.
    embedding = '/home/thaddus/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/snapshots/1110a243fdf4706b3f48f1d95db1a4f5529b4d41'
    if not Path(embedding).is_dir():
        raise ValueError('Pinned CPU embedding snapshot is unavailable')
    (run / 'local.env').write_text(
        'AMEM_LLM_BASE_URL=http://localhost:19000/v1\n'
        'AMEM_LLM_MODEL=qwen3.8:27b\nAMEM_LLM_API_KEY=\n'
        'AMEM_JUDGE_BASE_URL=http://localhost:19000/v1\n'
        'AMEM_JUDGE_MODEL=gemma4:26b\nAMEM_JUDGE_API_KEY=\n'
        'AMEM_SOURCE_SCHEMA_MODE=strict\nAMEM_TIMEOUT=60\n'
        'AMEM_EMBEDDING_PATH=' + embedding + '\n')
    args = ['--permission-version', '20', '--allow-paid-api',
            '--env-file', str(run / 'local.env'), '--run-dir', str(run)]
    script = scripts / 'local_session.py'
    write(run / 'launch.json', {
        'prepared_at': stamp(), 'runner_arguments': args,
        'command': [sys.executable, '-u', str(script), 'worker', str(run)],
        'frozen_source': str(frozen), 'endpoint': 'http://localhost:19000/v1',
        'models': {'memory': 'qwen3.8:27b', 'judge': 'gemma4:26b'},
        'judge_is_separate_model': True, 'openrouter_calls': False,
        'embedding_device': 'cpu', 'permission_version': '20',
        'expected': {'episodes': 21, 'checkpoints': 579,
                     'source_turns': 4294, 'boundaries': 567},
        'comparison_limitations': ['Provider and judge differ from v19; not a controlled model-matched comparison.',
                                   'Local endpoint billing, if any, is not OpenRouter billing.'],
        'live_opt_in_flag': '--allow-paid-api is the existing runner live-call guard; URLs select localhost only.'})
    (run / 'FOLLOW.md').write_text(
        'V20 full evaluation using the existing localhost:19000 API.\n\n'
        'Memory/answers: qwen3.8:27b; separate judge: gemma4:26b.\n'
        'CPU embeddings; no GPU server starts and no OpenRouter calls.\n'
        'Read progress.json, summary.json and run.log; model-trace.jsonl records calls.\n'
        'Source latency includes scheduled host work. Full coverage is 579 scored checkpoints.\n'
        'The provider/judge change prevents a controlled v19 comparison.\n\n'
        'Resume stopped work with the SAME frozen launcher and run directory:\n\n'
        + sys.executable + ' -u ' + str(script) + ' run ' + str(run) + ' --resume\n')
    print(json.dumps({'run_dir': str(run), 'frozen_launcher': str(script)}))


def worker(run, resume):
    from evaluation.gatemem_medical import runner
    factory = runner.processors

    def recorded(processor):
        complete = processor.complete

        def traced(system, payload, *, schema=None):
            start = time.monotonic()
            row = {'at': stamp(), 'model': processor.model,
                   'processor_id': processor.processor_id,
                   'system': system, 'payload': payload, 'schema': schema}
            try:
                result = complete(system, payload, schema=schema)
                row['response'] = result
                return result
            except BaseException as error:
                row['error_type'] = type(error).__name__
                row['error'] = str(error)
                raise
            finally:
                row.update(seconds=time.monotonic() - start,
                           usage=processor.usage,
                           effective_limits=processor.last_effective_limits,
                           response_model=processor.last_response_model,
                           transport_attempts=processor.transport_attempts)
                with (run / 'model-trace.jsonl').open('a') as out:
                    out.write(json.dumps(row, ensure_ascii=False) + '\n')

        processor.complete = traced
        return processor

    def processors(offline=False):
        embedder, llm, judge = factory(offline)
        return embedder, recorded(llm), recorded(judge)

    runner.processors = processors
    args = json.loads((run / 'launch.json').read_text())['runner_arguments']
    return runner.main(args + (['--resume'] if resume else []))


def supervise(run, resume):
    launch = json.loads((run / 'launch.json').read_text())
    command = launch['command'] + (['--resume'] if resume else [])
    # A resumed supervisor must not retain a previous terminal status.
    if (run / 'exit-status.json').exists():
        shutil.move(run / 'exit-status.json', run / ('previous-exit-' + str(time.time_ns()) + '.json'))
    with (run / 'run.log').open('a', buffering=1) as log:
        child = subprocess.Popen(command, cwd=run / 'frozen-source',
                                 env=environment(run), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        write(run / 'process.json', {'supervisor_pid': os.getpid(),
                                    'runner_pid': child.pid, 'started_at': stamp()})
        for line in child.stdout:
            log.write(line)
            print(line, end='', flush=True)
        code = child.wait()
    write(run / 'exit-status.json', {'exit_code': code, 'finished_at': stamp()})
    return code


def snapshot(run):
    value = {'updated_at': stamp(), 'status': 'starting'}
    if (run / 'process.json').exists():
        process = json.loads((run / 'process.json').read_text())
        value['runner_pid'] = process['runner_pid']
        try:
            os.kill(process['runner_pid'], 0)
            value['status'] = 'running'
        except ProcessLookupError:
            value['status'] = 'runner_not_live'
    if (run / 'summary.json').exists():
        report = json.loads((run / 'summary.json').read_text())
        for key in ('coverage', 'metrics', 'complete', 'workflow_v20'):
            value[key] = report.get(key)
    queries = [
        ('memory.sqlite', 'source_latency', "SELECT count(*) AS count,avg(seconds) AS mean,max(seconds) AS max FROM pipeline_timings WHERE stage='source_total'"),
        ('memory.privacy.sqlite', 'permission_jobs', 'SELECT status,count(*) FROM permission_jobs GROUP BY status'),
        ('memory.privacy.sqlite', 'recovery_queue', 'SELECT status,count(*) FROM host_recovery GROUP BY status'),
        ('evaluation.sqlite', 'checkpoints', 'SELECT count(*) AS predicted,count(score) AS scored FROM official')]
    for filename, key, query in queries:
        if query is None or not (run / filename).exists():
            continue
        try:
            with sqlite3.connect('file:' + str(run / filename) + '?mode=ro', uri=True, timeout=1) as db:
                cursor = db.execute(query)
                rows = cursor.fetchall()
                value[key] = (dict(rows) if key in {'permission_jobs', 'recovery_queue'}
                              else dict(zip([d[0] for d in cursor.description], rows[0])))
        except sqlite3.Error as error:
            value[key] = {'unavailable': str(error)}
    if (run / 'exit-status.json').exists():
        value['exit'] = json.loads((run / 'exit-status.json').read_text())
        value['status'] = 'finished' if value['exit']['exit_code'] == 0 else 'stopped'
    return value


def watch(run, once):
    while True:
        value = snapshot(run)
        write(run / 'progress.json', value)
        print(json.dumps(value), flush=True)
        if once or value['status'] in {'finished', 'stopped', 'runner_not_live'}:
            return
        time.sleep(10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'run', 'worker', 'watch', 'once'])
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    run = args.run_dir.resolve()
    if args.mode == 'prepare':
        prepare(run)
    elif args.mode == 'run':
        raise SystemExit(supervise(run, args.resume))
    elif args.mode == 'worker':
        raise SystemExit(worker(run, args.resume))
    else:
        watch(run, args.mode == 'once')
