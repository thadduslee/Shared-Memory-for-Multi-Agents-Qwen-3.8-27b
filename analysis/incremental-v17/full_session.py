"""Freeze v17 source, run the full native evaluation, and expose live progress."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time

ROOT=Path(__file__).resolve().parents[2]
BASELINE=ROOT/'runs/gatemem-medical-v16-openrouter-full-20261101T171100+0800'


def write(path,value):
    temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)


def stamp():return datetime.now(timezone.utc).isoformat()


def freeze(run):
    frozen=run/'frozen-source'
    frozen.mkdir()
    ignore=shutil.ignore_patterns('__pycache__','*.pyc','.pytest_cache')
    shutil.copytree(ROOT/'agentic_memory',frozen/'agentic_memory',ignore=ignore)
    (frozen/'evaluation').mkdir()
    shutil.copy2(ROOT/'evaluation/__init__.py',frozen/'evaluation/__init__.py')
    shutil.copytree(ROOT/'evaluation/gatemem_medical',frozen/'evaluation/gatemem_medical',ignore=ignore)
    wrapper=frozen/'analysis/over-refusal-v16';wrapper.mkdir(parents=True)
    for name in ('run.py','audit.py','compare.py'):
        shutil.copy2(ROOT/'analysis/over-refusal-v16'/name,wrapper/name)
    with tarfile.open(run/'evaluated-source.tar.gz','w:gz') as out:out.add(frozen,arcname='frozen-source')
    return frozen


def launch(run,version='17',gate_checkpoints=None):
    from evaluation.gatemem_medical import openrouter
    openrouter.configure(ROOT/'.env')
    if run.exists():raise ValueError('A fresh run directory is required; existing artifacts are preserved')
    run.mkdir(parents=True,mode=0o700)
    frozen=freeze(run)
    env=dict(os.environ,PYTHONPATH=str(frozen),PYTHONUNBUFFERED='1',HF_HUB_OFFLINE='1',CUDA_VISIBLE_DEVICES='',
        AMEM_OPENROUTER_PROVIDER_SORT='latency',AMEM_OPENROUTER_PROVIDER_IGNORE='deepinfra,coreweave,reka')
    argv=[sys.executable,'-u',str(frozen/'analysis/over-refusal-v16/run.py'),'--run-dir',str(run),'--full','--version',version]
    gate_argv=[arg for arg in argv if arg!='--full']+['--checkpoints',str(gate_checkpoints)] if gate_checkpoints else None
    write(run/'launch.json',{'started_at':stamp(),'command':argv,'frozen_source':str(frozen),
        'expected':{'checkpoints':579,'source_turns':4294,'boundaries':567,'episodes':21},
        'permission_version':version,'stop_on_scored_failure':False,'gate_checkpoints':gate_checkpoints,
        'models':{'memory':openrouter.MEMORY_MODEL,'judge':openrouter.JUDGE_MODEL}})
    (run/'FOLLOW.md').write_text('Fresh v'+version+' full evaluation.\n\n'
        'The runner uses frozen-source, not the changing worktree.\n'
        'Inspect progress.json, summary.json and run.log. Full completion requires all 579 scores, '
        '4,294 source turns and 567 completed boundaries. Utility/privacy failures are retained; '
        'the run does not stop on a score.\n')
    with (run/'run.log').open('a',buffering=1) as log:
        commands=[('diagnostic_gate',gate_argv),('full',argv+['--resume'])] if gate_argv else [('full',argv)]
        status=0
        for phase,command in commands:
            child=subprocess.Popen(command,cwd=frozen,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
            write(run/'process.json',{'supervisor_pid':os.getpid(),'runner_pid':child.pid,'started_at':stamp(),'phase':phase})
            for line in child.stdout:log.write(line);print(line,end='',flush=True)
            status=child.wait()
            if status:break
            if phase=='diagnostic_gate':
                scores=[json.loads(line) for line in (run/'scores.jsonl').read_text().splitlines() if line.strip()]
                utility=[s for s in scores if s.get('query_type')=='utility']
                leaks=[s for s in scores if any(s.get(k) is True for k in
                    ('privacy_leak','privacy_context_leak','deletion_leak','deletion_context_leak'))]
                passed=(len(scores)>=gate_checkpoints and bool(utility) and
                    all(s.get('utility_correct') is True and s.get('over_refusal') is False for s in utility)
                    and not leaks and all(s.get('action_correct') is True for s in scores))
                write(run/'gate-status.json',{'passed':passed,'scores':len(scores),'utility':len(utility),
                    'leaks':len(leaks),'checked_at':stamp(),'continuation':'full_same_frozen_source' if passed else 'stopped_for_diagnosis'})
                if not passed:
                    print('Diagnostic gate failed; preserving all results and stopping before full continuation.',flush=True)
                    status=2;break
    write(run/'exit-status.json',{'exit_code':status,'finished_at':stamp()})
    if (run/'manifest.json').exists() and (run/'model-trace.jsonl').exists():
        with (run/'implementation-audit.log').open('w') as out:
            result=subprocess.run([sys.executable,str(frozen/'analysis/over-refusal-v16/audit.py'),'--run',str(run)],
                cwd=frozen,env=env,stdout=out,stderr=subprocess.STDOUT)
        write(run/'audit-status.json',{'exit_code':result.returncode,'finished_at':stamp()})
    return status


def snapshot(run):
    value={'updated_at':stamp(),'status':'starting'}
    if (run/'summary.json').exists():
        summary=json.loads((run/'summary.json').read_text())
        value.update(coverage=summary['coverage'],metrics=summary['metrics'],complete=summary['complete'],
            pending_reviews=summary['memory_processing'].get('pending_binding_reviews_at_latest_publication'))
    if (run/'process.json').exists():
        pid=json.loads((run/'process.json').read_text())['runner_pid']
        try:os.kill(pid,0);value.update(status='running',runner_pid=pid)
        except ProcessLookupError:
            supervisor=json.loads((run/'process.json').read_text()).get('supervisor_pid')
            try:
                if not supervisor:raise ProcessLookupError
                os.kill(supervisor,0)
                value.update(status='transitioning',runner_pid=pid)
            except ProcessLookupError:value.update(status='runner_not_live',runner_pid=pid)
    if (run/'exit-status.json').exists():
        value['exit']=json.loads((run/'exit-status.json').read_text())
        value['status']='finished' if value['exit']['exit_code']==0 else 'stopped'
    for filename,key,query in [('memory.privacy.sqlite','source_jobs','SELECT status,count(*) FROM permission_jobs GROUP BY status'),
        ('memory.sqlite','source_latency',"SELECT count(*),avg(seconds) FROM pipeline_timings WHERE stage='source_total'"),
        ('evaluation.sqlite','checkpoints','SELECT count(*),count(score) FROM official')]:
        if not (run/filename).exists():continue
        try:
            with sqlite3.connect(f'file:{run/filename}?mode=ro',uri=True,timeout=1) as db:rows=db.execute(query).fetchall()
            value[key]=dict(rows) if key=='source_jobs' else rows[0]
        except sqlite3.Error:value[key]={'database_busy':True}
    return value


def watch(run,once=False):
    deadline=time.monotonic()+30
    while not run.exists():
        if once or time.monotonic()>=deadline:
            print(json.dumps({'status':'not_started','run_dir':str(run)}),flush=True)
            return
        time.sleep(.5)
    compared=False
    while True:
        value=snapshot(run);write(run/'progress.json',value)
        with (run/'progress-history.jsonl').open('a') as out:out.write(json.dumps(value)+'\n')
        print(json.dumps(value),flush=True)
        if not compared and (run/'official_predictions.jsonl').exists() and (BASELINE/'official_predictions.jsonl').exists():
            count=min(25,len((BASELINE/'official_predictions.jsonl').read_text().splitlines()))
            if count and len((run/'official_predictions.jsonl').read_text().splitlines())>=count:
                frozen=run/'frozen-source'
                with (run/'comparison-prefix.log').open('w') as out:
                    result=subprocess.run([sys.executable,str(frozen/'analysis/over-refusal-v16/compare.py'),
                        '--baseline',str(BASELINE),'--candidate',str(run),'--count',str(count),
                        '--output',str(run/'comparison-prefix.json')],cwd=frozen,
                        env=dict(os.environ,PYTHONPATH=str(frozen)),stdout=out,stderr=subprocess.STDOUT)
                compared=True;write(run/'comparison-status.json',{'exit_code':result.returncode,'checkpoints':count})
        if once or value['status'] in {'finished','stopped','runner_not_live'}:return
        time.sleep(10)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['run','watch','once'])
    parser.add_argument('--version',default='17')
    parser.add_argument('--gate-checkpoints',type=int)
    parser.add_argument('run_dir',type=Path);args=parser.parse_args();os.umask(0o077)
    run=args.run_dir.resolve()
    if args.mode=='run':raise SystemExit(launch(run,args.version,args.gate_checkpoints))
    else:watch(run,once=args.mode=='once')
