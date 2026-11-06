#!/usr/bin/env python3
"""Fresh local V19 evaluation; never takes over another user's GPU processes."""
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request

JOB = Path(__file__).resolve().parent
ROOT = JOB / 'frozen-source'
PYTHON = '/home/thaddus/gatemem/.venv-nb/bin/python'
VLLM = '/home/thaddus/gatemem/.venv-vllm/bin/vllm'
MODEL = '/home/thaddus/.cache/huggingface/hub/models--Qwen--Qwen3.8-27B/snapshots/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0'
JUDGE = '/home/thaddus/.cache/huggingface/hub/models--Qwen--Qwen3-14B/snapshots/40c069824f4251a91eefaf281ebe4c544efd3e18'
REVISION = '02a7c8f643f0cdf5697ec3cbc5cf5bbe64705b5b'
SERVERS = []
STAGE = None


def log(message):
    print(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), message, flush=True)


def write_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def status(state, **details):
    write_json(JOB / 'status.json', dict(state=state, updated_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **details))


def verify_frozen():
    expected = json.loads((JOB / 'source-hashes.json').read_text())
    actual = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in expected}
    if actual != expected:
        raise RuntimeError('Frozen source differs from the validated snapshot')
    extra = json.loads((JOB / 'input-and-serving-hashes.json').read_text())
    if any(hashlib.sha256((JOB / name).read_bytes()).hexdigest() != value for name, value in extra.items()):
        raise RuntimeError('Frozen input dataset or serving compatibility files changed')


def gpu_snapshot():
    output = subprocess.check_output(['nvidia-smi', '--id=4,5', '--query-gpu=index,uuid,memory.used,utilization.gpu', '--format=csv,noheader,nounits'], text=True)
    rows = list(csv.reader(output.splitlines(), skipinitialspace=True))
    return {int(index): dict(uuid=uuid, used_mib=int(used), utilization=int(util)) for index, uuid, used, util in rows}


def assert_ports_free():
    for port in (18150,):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))


def spawn_server(role, args, gpu_ids):
    env = os.environ.copy()
    env.update(CUDA_VISIBLE_DEVICES=gpu_ids, HF_HOME='/home/thaddus/.cache/huggingface', HF_HUB_OFFLINE='1', OMP_NUM_THREADS='4')
    env.pop('HF_HUB_CACHE', None)
    env.pop('TRANSFORMERS_CACHE', None)
    if role == 'memory':
        env['PYTHONPATH'] = str(JOB / 'vllm_schema_support')
    handle = (JOB / (role + '-server.log')).open('a')
    process = subprocess.Popen([VLLM, 'serve', *args], cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    handle.close()
    SERVERS.append(process)
    log(f'{role} server pid={process.pid}; GPU devices={gpu_ids}')


def stop_process(process):
    # Only process groups created by this supervisor; never signal foreign jobs.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=30)


def wait_servers():
    deadline = time.monotonic() + 1800
    healthy = set()
    while len(healthy) != 1:
        for process in SERVERS:
            if process.poll() is not None:
                raise RuntimeError(f'Model server {process.pid} exited {process.returncode}; inspect server logs')
        for role, port, name in [('memory', 18150, 'Qwen/Qwen3.8-27B')]:
            if role in healthy:
                continue
            try:
                req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/models', headers={'Authorization': 'Bearer dummy'})
                with urllib.request.urlopen(req, timeout=3) as response:
                    models = json.load(response)
                if name in {item['id'] for item in models['data']}:
                    healthy.add(role)
                    log(f'{role} endpoint ready on localhost:{port}')
            except Exception:
                pass
        if time.monotonic() > deadline:
            raise TimeoutError('Model startup exceeded 30 minutes')
        if len(healthy) != 1:
            time.sleep(10)


def run_stage(name, command, env):
    global STAGE
    log(f'STAGE {name}: ' + ' '.join(command))
    status(name, target_checkpoints=579, target_boundaries=567)
    STAGE = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
    last = time.monotonic()
    while STAGE.poll() is None:
        time.sleep(10)
        if any(server.poll() is not None for server in SERVERS):
            raise RuntimeError('A model server exited during ' + name)
        if time.monotonic() - last >= 60:
            details = {}
            summary = JOB / 'results/summary.json'
            if summary.is_file():
                details = json.loads(summary.read_text()).get('coverage', {})
            status(name, coverage=details, target_checkpoints=579, target_boundaries=567)
            log(f'{name} heartbeat; coverage={details}')
            last = time.monotonic()
    code = STAGE.returncode
    STAGE = None
    if code:
        raise RuntimeError(f'{name} exited {code}; no automatic code changes or fresh-store retry')
    log(f'STAGE {name} finished successfully')


def main():
    os.umask(0o077)
    log('Fresh V19 full evaluation: 579 checkpoints, 567 boundaries, 4294 source turns; scoring enabled; answer validation ENFORCED')
    log('Judge uses separate calls to the SAME Qwen3.8-27B model; no independent-model scoring claim')
    log('Raw-stream inference only; task registry/host assignment integration disabled; no evaluator-derived assignment events')
    lock = (JOB.parent / 'gpu-45.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    verify_frozen()
    if (JOB / 'results').exists():
        raise RuntimeError('Results directory already exists; this launcher is fresh-run only')
    assert_ports_free()
    stable = 0
    last_log = 0
    while stable < 1:
        snapshot = gpu_snapshot()
        free = set(snapshot) == {4, 5} and all(row['used_mib'] < 512 and row['utilization'] < 10 for row in snapshot.values())
        stable = stable + 1 if free else 0
        status('waiting-for-gpus', gpus=snapshot, stable_free_checks=stable, target_checkpoints=579, target_boundaries=567)
        if time.monotonic() - last_log >= 60 or stable:
            log(f'GPU availability: {snapshot}; stable-free checks={stable}/1. Foreign jobs are left running.')
            last_log = time.monotonic()
        if stable < 1:
            time.sleep(15)
    verify_frozen()
    assert_ports_free()
    if __import__('shutil').disk_usage(JOB).free < 10 * 1024**3:
        raise RuntimeError('Less than 10 GiB free; refusing to start')
    write_json(JOB / 'allocated-gpus.json', snapshot)
    status('starting-models', gpus=snapshot)
    common = ['--reasoning-parser', 'qwen3', '--dtype', 'bfloat16', '--max-num-seqs', '4', '--gpu-memory-utilization', '0.90', '--host', '127.0.0.1', '--api-key', 'dummy']
    spawn_server('memory', [MODEL, '--served-model-name', 'Qwen/Qwen3.8-27B', '--tensor-parallel-size', '2', '--structured-outputs-config', '{"backend":"guidance"}', '--max-model-len', '262144', '--enable-prefix-caching', '--limit-mm-per-prompt', '{"image":0,"video":0}', '--port', '18150', *common], snapshot[4]['uuid'] + ',' + snapshot[5]['uuid'])
    wait_servers()
    env = os.environ.copy()
    # Ignore inherited AMEM settings: pinned local endpoints, caps, no remote keys.
    env = {k: v for k, v in env.items() if not k.startswith('AMEM_')}
    env.update(PYTHONPATH=str(ROOT), CUDA_VISIBLE_DEVICES='', HF_HUB_OFFLINE='1', PYTHONUNBUFFERED='1', OMP_NUM_THREADS='4', TOKENIZERS_PARALLELISM='false')
    command = [PYTHON, '-u', '-m', 'evaluation.gatemem_medical', '--env-file', str(JOB / 'local.env'), '--gatemem-repo', str(JOB / 'frozen-dataset'), '--permission-mode', 'inference', '--permission-version', '19', '--answer-validation-mode', 'enforce']
    run_stage('preflight', [*command, '--preflight'], env)
    verify_frozen()
    run_stage('evaluating-and-scoring', [*command, '--run-dir', str(JOB / 'results')], env)
    report = json.loads((JOB / 'results/summary.json').read_text())
    coverage = report['coverage']
    if not report.get('complete') or coverage['official_predictions'] != 579 or coverage['scored_checkpoints'] != 579 or coverage['completed_boundaries'] != 567 or coverage['ingested_source_turns'] != 4294:
        raise RuntimeError('Runner returned without complete full coverage; inspect summary.json')
    log('FINAL SCORED SUMMARY\n' + json.dumps(report, indent=2))
    status('complete', coverage=coverage, metrics=report['metrics'])


def interrupted(signum, frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupted)
    code = 0
    try:
        main()
    except KeyboardInterrupt:
        code = 130
        log('Job interrupted')
        status('interrupted')
    except Exception as error:
        code = 1
        log(f'FAILED: {type(error).__name__}: {error}')
        status('failed', error=f'{type(error).__name__}: {error}')
        __import__('traceback').print_exc()
    finally:
        if STAGE is not None:
            stop_process(STAGE)
        for process in reversed(SERVERS):
            stop_process(process)
        log(f'Supervisor exit={code}; owned model processes cleaned up')
    sys.exit(code)
