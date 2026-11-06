#!/usr/bin/env python3
"""SOCLAAS-only fresh V19 evaluation with no local GPU allocation."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

JOB = Path(__file__).resolve().parent
ROOT = Path('/home/thaddus/a-mem-v19-fixes')
PYTHON = '/home/thaddus/gatemem/.venv-nb/bin/python'
BASE = 'http://localhost:19000/v1'
MODEL = 'qwen3.8:27b'
REVISION = '02a7c8f'
CHILD = None


def log(message):
    print(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), message, flush=True)


def write_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def status(state, **details):
    write_json(JOB / 'status.json', dict(state=state, updated_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **details))


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True).strip()


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in git('ls-files', 'agentic_memory', 'evaluation', 'scripts').splitlines() if (ROOT / name).is_file()}


def verify_frozen():
    if not git('rev-parse', 'HEAD').startswith(REVISION) or git('status', '--porcelain', '--', 'agentic_memory', 'evaluation', 'scripts'):
        raise RuntimeError('Runtime revision/files changed; refusing a mixed-code run')
    if hashes() != json.loads((JOB / 'source-hashes.json').read_text()):
        raise RuntimeError('Runtime file hash changed while waiting')


def run_stage(name, command, env):
    global CHILD
    status(name, target_checkpoints=579, target_boundaries=567)
    log(f'STAGE {name}: ' + ' '.join(command))
    CHILD = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
    last = time.monotonic()
    while CHILD.poll() is None:
        time.sleep(10)
        if time.monotonic() - last >= 60:
            summary = JOB / 'results/summary.json'
            coverage = json.loads(summary.read_text()).get('coverage', {}) if summary.is_file() else {}
            log(f'{name} heartbeat; coverage={coverage}')
            status(name, coverage=coverage, target_checkpoints=579, target_boundaries=567)
            last = time.monotonic()
    code = CHILD.returncode
    CHILD = None
    if code:
        raise RuntimeError(f'{name} exited {code}; no automatic store resets or code changes')
    log(f'STAGE {name} completed')


def main():
    os.umask(0o077)
    log('SOCLAAS fresh full V19: 579 checkpoints / 567 boundaries / 4294 source turns; verification ENFORCED, scoring enabled')
    log('All chat roles use qwen3.8:27b; judge uses a separate prompt/call but the SAME MODEL; embeddings remain pinned local MiniLM')
    log('Fresh attempt after failed standalone preflight; no local GPU services are launched')
    log('Raw-stream cohort: no host assignments manufactured; optional registry and authenticated host gateway disabled')
    verify_frozen()
    if (JOB / 'results').exists():
        raise RuntimeError('Results already exist; this supervisor starts fresh only')
    status('initializing-evaluation', target_checkpoints=579, target_boundaries=567)
    log('Using the prior successful strict-schema probe (101.288s); actual source/query/judge transport recovery is enabled')
    if __import__('shutil').disk_usage(JOB).free < 10 * 1024**3:
        raise RuntimeError('Less than 10 GiB disk space available')
    env = {k: v for k, v in os.environ.items() if not k.startswith('AMEM_')}
    env.update(CUDA_VISIBLE_DEVICES='', HF_HUB_OFFLINE='1', PYTHONUNBUFFERED='1', OMP_NUM_THREADS='4', TOKENIZERS_PARALLELISM='false')
    command = [PYTHON, '-u', '-m', 'evaluation.gatemem_medical', '--env-file', str(JOB / 'soclaas.env'), '--permission-mode', 'inference', '--permission-version', '19', '--answer-validation-mode', 'enforce']
    log('Request timeout=900 seconds; transient HTTP/transport failures retry the same causal work item; semantic budgets unchanged')
    run_stage('evaluating-and-scoring', [*command, '--run-dir', str(JOB / 'results')], env)
    report = json.loads((JOB / 'results/summary.json').read_text())
    coverage = report['coverage']
    if not report.get('complete') or coverage['official_predictions'] != 579 or coverage['scored_checkpoints'] != 579 or coverage['completed_boundaries'] != 567 or coverage['ingested_source_turns'] != 4294:
        raise RuntimeError('Full coverage has not been achieved; inspect summary.json')
    log('FINAL SCORED SUMMARY\n' + json.dumps(report, indent=2))
    status('complete', coverage=coverage, metrics=report['metrics'])


def interrupt(signum, frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)
    code = 0
    try:
        main()
    except KeyboardInterrupt:
        code = 130
        log('Interrupted')
        status('interrupted')
    except Exception as error:
        code = 1
        log(f'FAILED: {type(error).__name__}: {error}')
        status('failed', error=f'{type(error).__name__}: {error}')
        __import__('traceback').print_exc()
    finally:
        if CHILD is not None:
            try:
                os.killpg(CHILD.pid, signal.SIGTERM)
                CHILD.wait(timeout=30)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(CHILD.pid, signal.SIGKILL)
                CHILD.wait(timeout=30)
        log(f'Supervisor exit={code}')
    sys.exit(code)
