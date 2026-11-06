#!/usr/bin/env python3
"""V19 information types on the first 3 episodes through SOCLAAS; no GPUs, tmux or terminal."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import _thread

JOB = Path(__file__).resolve().parent
ROOT = JOB / 'frozen-source'
PYTHON = '/home/thaddus/gatemem/.venv-nb/bin/python'
PREFLIGHT_ATTEMPTS = 6
CHILD = None
SIGNALS = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}


def log(message):
    print(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), message, flush=True)


def status(state, **details):
    temp = JOB / 'status.tmp'
    temp.write_text(json.dumps(dict(state=state, updated_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **details), indent=2) + '\n')
    temp.replace(JOB / 'status.json')


def verify_frozen():
    expected = json.loads((JOB / 'source-hashes.json').read_text())
    if {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in expected} != expected:
        raise RuntimeError('Frozen source differs from the validated snapshot')


def run_stage(name, command, env):
    global CHILD
    log(f'STAGE {name}')
    CHILD = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True,
        preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS))
    last = time.monotonic()
    while CHILD.poll() is None:
        time.sleep(10)
        if time.monotonic() - last >= 60:
            summary = JOB / 'results/summary.json'
            coverage = json.loads(summary.read_text()).get('coverage', {}) if summary.is_file() else {}
            log(f'{name} heartbeat; coverage={coverage}')
            status(name, coverage=coverage)
            last = time.monotonic()
    code = CHILD.returncode
    CHILD = None
    log(f'{name} exited with code {code}')
    return code


def main():
    os.umask(0o077)
    log('V19 information types, show all relevant facts, no owner review; first 3 episodes by ID; all chat roles qwen3.8:27b through SOCLAAS; policy v19-cited-subjects:3; input contract identity-metadata-stream-v1')
    log('Judge uses separate calls to the SAME model; embeddings stay local MiniLM; no GPU services are started')
    verify_frozen()
    if (JOB / 'results').exists():
        raise RuntimeError('Results already exist; this supervisor starts fresh only')
    env = {k: v for k, v in os.environ.items() if not k.startswith('AMEM_')}
    env.update(PYTHONPATH=str(ROOT), CUDA_VISIBLE_DEVICES='', HF_HUB_OFFLINE='1', PYTHONUNBUFFERED='1', OMP_NUM_THREADS='4', TOKENIZERS_PARALLELISM='false')
    command = [PYTHON, '-u', '-m', 'evaluation.gatemem_medical', '--env-file', str(JOB / 'soclaas.env'), '--gatemem-repo', str(JOB / 'frozen-dataset'), '--permission-mode', 'inference', '--permission-version', '19', '--answer-validation-mode', 'enforce', '--episode-limit', '3']
    for attempt in range(1, PREFLIGHT_ATTEMPTS + 1):
        status('preflight', attempt=attempt)
        if not run_stage('preflight', [*command, '--preflight'], env):
            break
        if attempt == PREFLIGHT_ATTEMPTS:
            raise RuntimeError('Preflight failed on every attempt')
        log(f'Preflight attempt {attempt} failed; retrying in 60s (the gateway has returned intermittent 502s)')
        time.sleep(60)
    verify_frozen()
    code = run_stage('evaluating-and-scoring', [*command, '--run-dir', str(JOB / 'results')], env)
    if code:
        raise RuntimeError(f'evaluating-and-scoring exited {code}')
    report = json.loads((JOB / 'results/summary.json').read_text())
    coverage = report['coverage']
    if not report.get('complete') or not coverage['exact_checkpoint_coverage'] or not coverage['exact_source_coverage'] or not coverage['all_boundaries_complete']:
        raise RuntimeError('Runner returned without complete subset coverage; inspect summary.json')
    log('FINAL SCORED SUMMARY\n' + json.dumps(report, indent=2))
    status('complete', coverage=coverage, metrics=report['metrics'])


def sender(pid):
    try:
        command = (Path('/proc') / str(pid) / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')[:300]
        parent = next(line.split()[1] for line in (Path('/proc') / str(pid) / 'status').read_text().splitlines() if line.startswith('PPid:'))
        return f'pid={pid} ppid={parent} cmd={command!r}'
    except OSError:
        return f'pid={pid} (already gone)'


def watch_signals():
    # Records who sent each signal; two earlier runs stopped with no known cause.
    while True:
        info = signal.sigwaitinfo(SIGNALS)
        log(f'Received signal {signal.Signals(info.si_signo).name} from uid={info.si_uid} {sender(info.si_pid)}')
        if info.si_signo != signal.SIGHUP:
            _thread.interrupt_main()
            return


if __name__ == '__main__':
    signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    threading.Thread(target=watch_signals, daemon=True).start()
    code = 0
    try:
        main()
    except KeyboardInterrupt:
        code = 130
        log(f'Job interrupted by a signal')
        status('interrupted')
    except Exception as error:
        code = 1
        log(f'FAILED: {type(error).__name__}: {error}')
        status('failed', error=f'{type(error).__name__}: {error}')
    finally:
        if CHILD is not None and CHILD.poll() is None:
            os.killpg(CHILD.pid, signal.SIGTERM)
        log(f'Supervisor exit={code}')
    sys.exit(code)
