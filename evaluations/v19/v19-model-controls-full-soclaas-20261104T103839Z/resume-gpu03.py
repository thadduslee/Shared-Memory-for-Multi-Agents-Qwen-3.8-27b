"""Resume the frozen V19 run using local tensor-parallel serving on GPUs 0,3."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request

JOB = Path(__file__).resolve().parent
ROOT = JOB / 'frozen-source'
CHILD = None


def status(state, **details):
    value = {'state': state, 'updated_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
             'tmux_session': 'v19-gpu03', 'gpus': [0, 3], 'serving': 'local-vllm', **details}
    temp = JOB / 'status.tmp'
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(JOB / 'status.json')


def coverage():
    path = JOB / 'results/summary.json'
    return json.loads(path.read_text()).get('coverage', {}) if path.exists() else {}


def stop(signum, frame):
    raise KeyboardInterrupt


def stage(name, command, env):
    global CHILD
    print('STAGE', name, flush=True)
    status(name, coverage=coverage())
    CHILD = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
    last = time.monotonic()
    while CHILD.poll() is None:
        time.sleep(5)
        if time.monotonic() - last >= 30:
            status(name, coverage=coverage())
            print(name, 'heartbeat;', coverage(), flush=True)
            last = time.monotonic()
    code = CHILD.returncode
    CHILD = None
    if code:
        raise RuntimeError(f'{name} exited with code {code}')


def main():
    os.umask(0o077)
    expected = json.loads((JOB / 'source-hashes.json').read_text())
    if any(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != checksum
           for name, checksum in expected.items()):
        raise RuntimeError('Frozen source differs from validated snapshot')
    env = {k: v for k, v in os.environ.items() if not k.startswith('AMEM_')}
    env.update(PYTHONPATH=str(ROOT), CUDA_VISIBLE_DEVICES='',
               HF_HUB_OFFLINE='1', PYTHONUNBUFFERED='1', OMP_NUM_THREADS='4',
               TOKENIZERS_PARALLELISM='false')
    status('waiting-for-local-model', coverage=coverage())
    deadline = time.monotonic() + 1800
    while True:
        try:
            with urllib.request.urlopen('http://127.0.0.1:18000/health', timeout=5) as response:
                if response.status == 200:
                    break
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError('Local model did not become healthy within 30 minutes')
        time.sleep(5)
    command = ['/home/thaddus/gatemem/.venv-nb/bin/python', '-u', '-m',
               'evaluation.gatemem_medical', '--env-file', str(JOB / 'gpu03.env'),
               '--gatemem-repo', str(JOB / 'frozen-dataset'),
               '--permission-mode', 'inference', '--permission-version', '19',
               '--answer-validation-mode', 'enforce']
    stage('local-preflight', [*command, '--preflight'], env)
    stage('evaluating-and-scoring', [*command, '--run-dir', str(JOB / 'results'), '--resume'], env)
    report = json.loads((JOB / 'results/summary.json').read_text())
    if not report.get('complete'):
        raise RuntimeError('Runner exited without full coverage')
    status('complete', coverage=report['coverage'], metrics=report['metrics'])
    print('Full evaluation complete', flush=True)


if __name__ == '__main__':
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)
    try:
        main()
    except KeyboardInterrupt:
        status('interrupted', coverage=coverage())
        raise SystemExit(130)
    except Exception as error:
        status('failed', error=f'{type(error).__name__}: {error}', coverage=coverage())
        raise
    finally:
        if CHILD is not None and CHILD.poll() is None:
            os.killpg(CHILD.pid, signal.SIGTERM)
            CHILD.wait(timeout=30)
