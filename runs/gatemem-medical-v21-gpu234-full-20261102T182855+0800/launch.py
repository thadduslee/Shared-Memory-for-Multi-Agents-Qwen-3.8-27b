"""Run the authorized full V21 evaluation from its immutable source snapshot."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

RUN = Path(__file__).resolve().parent
FROZEN = RUN / 'frozen-source'


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write(name, value):
    target = RUN / name
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(target)


def environment():
    env = {k: v for k, v in os.environ.items() if not k.startswith('AMEM_')}
    env.update(PYTHONPATH=str(FROZEN), PYTHONUNBUFFERED='1',
        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', CUDA_VISIBLE_DEVICES='',
        TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='2', MKL_NUM_THREADS='2')
    for line in (RUN / 'local.env').read_text().splitlines():
        if line and not line.startswith('#'):
            key, value = line.split('=', 1)
            env[key] = value
    for role in ('LLM', 'JUDGE'):
        url = urlsplit(env['AMEM_' + role + '_BASE_URL'])
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.port != (38721 if role == 'LLM' else 38722):
            raise ValueError('This run is restricted to the dedicated GPU 2-4 endpoints')
    return env


def worker(launch):
    from evaluation.gatemem_medical import runner
    factory = runner.processors

    def recorded(processor):
        complete = processor.complete

        def traced(system, payload, *, schema=None):
            started = time.monotonic()
            row = {'at': stamp(), 'model': processor.model,
                'processor_id': processor.processor_id, 'stage': payload.get('stage', 'query'),
                'system': system, 'payload': payload, 'schema': schema}
            try:
                result = complete(system, payload, schema=schema)
                row['response'] = result
                return result
            except BaseException as exc:
                row.update(error_type=type(exc).__name__, error=str(exc),
                    error_details=getattr(exc, 'details', {}))
                raise
            finally:
                row.update(seconds=time.monotonic() - started, usage=processor.usage,
                    effective_limits=processor.last_effective_limits,
                    response_model=processor.last_response_model,
                    transport_attempts=processor.transport_attempts)
                with (RUN / 'model-trace.jsonl').open('a') as out:
                    out.write(json.dumps(row, ensure_ascii=False) + '\n')
        processor.complete = traced
        return processor

    def processors(offline=False):
        embedder, llm, judge = factory(offline)
        return embedder, recorded(llm), recorded(judge)
    runner.processors = processors
    return runner.main(launch['runner_arguments'] + (['--resume'] if '--resume' in sys.argv else []))


def supervise(launch):
    from agentic_memory.privacy.processors import CompletionProcessor
    write('process.json', {'status': 'preflight', 'supervisor_pid': os.getpid(), 'started_at': stamp()})
    schema = {'type': 'object', 'additionalProperties': False,
        'properties': {'ok': {'type': 'boolean', 'enum': [True]}}, 'required': ['ok']}
    for role in ('LLM', 'JUDGE'):
        processor = CompletionProcessor('preflight:' + role.lower(),
            os.environ['AMEM_' + role + '_BASE_URL'], os.environ['AMEM_' + role + '_MODEL'],
            api_key=os.environ['AMEM_' + role + '_API_KEY'], timeout=300, max_tokens=96)
        payload = {'probe': True}
        if role == 'LLM':
            payload.update(stage='policy_classification', permission_version='21')
        started = time.monotonic()
        result = processor.complete('Return exactly JSON {"ok":true}.', payload, schema=schema)
        if result != {'ok': True}:
            raise ValueError(role + ' preflight did not return the required JSON')
        receipt = {'at': stamp(), 'role': role, 'model': processor.model,
            'response_model': processor.last_response_model, 'seconds': time.monotonic() - started,
            'usage': processor.usage, 'effective_limits': processor.last_effective_limits}
        with (RUN / 'provider-preflight.jsonl').open('a') as out:
            out.write(json.dumps(receipt) + '\n')
        print(role + ' ready: ' + processor.model, flush=True)
    command = [launch['python'], '-u', str(Path(__file__).resolve()), 'worker']
    if '--resume' in sys.argv:
        command.append('--resume')
    child = subprocess.Popen(command, cwd=FROZEN, env=environment(), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1)
    write('process.json', {'status': 'running', 'supervisor_pid': os.getpid(),
        'runner_pid': child.pid, 'started_at': stamp(), 'tmux_session': launch['tmux_session']})
    with (RUN / 'run.log').open('a', buffering=1) as log:
        for line in child.stdout:
            log.write(line)
            print(line, end='', flush=True)
    code = child.wait()
    write('exit-status.json', {'exit_code': code, 'finished_at': stamp()})
    write('process.json', {'status': 'finished' if code == 0 else 'stopped',
        'supervisor_pid': os.getpid(), 'runner_pid': child.pid, 'exit_code': code, 'finished_at': stamp()})
    return code


if __name__ == '__main__':
    os.umask(0o077)
    launch = json.loads((RUN / 'launch.json').read_text())
    env = environment()
    os.environ.clear()
    os.environ.update(env)
    sys.path.insert(0, str(FROZEN))
    os.chdir(FROZEN)
    try:
        code = worker(launch) if len(sys.argv) > 1 and sys.argv[1] == 'worker' else supervise(launch)
    except Exception as exc:
        if len(sys.argv) < 2 or sys.argv[1] != 'worker':
            write('exit-status.json', {'exit_code': 1, 'finished_at': stamp(),
                'error_type': type(exc).__name__, 'error': str(exc)})
            write('process.json', {'status': 'stopped', 'supervisor_pid': os.getpid(),
                'finished_at': stamp(), 'error_type': type(exc).__name__})
        raise
    raise SystemExit(code)
