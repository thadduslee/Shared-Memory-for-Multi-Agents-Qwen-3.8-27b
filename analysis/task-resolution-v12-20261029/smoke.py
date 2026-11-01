"""Bounded local-model smoke on raw dialogue selected by the saved diagnosis.

Selection is retrospective. No checkpoint, entity/role annotation, expected
action or judge specification reaches ingestion. Original runs stay read-only.
"""
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import time
import hashlib
import shutil
import urllib.request

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.processors import CompletionProcessor,HashEmbedder
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter
from evaluation.gatemem_medical.input_boundary import source_turn
from evaluation.gatemem_medical.runner import load_environment

CASES={
    'outreach':('nurse_casey_liu',['t074','t076','t097','t098','t099','t100','t101','t110','t111','t112']),
    'counseling':('pharmacist_owens',['t069','t070','t071','t091','t092','t116','t117','t144']),
}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--max-calls',type=int,default=28)
    parser.add_argument('--case',choices=['all',*CASES],default='all')
    parser.add_argument('--profile',choices=['trace','pairs'],default='trace')
    args=parser.parse_args()
    if args.output_dir.exists():raise SystemExit('Use a fresh output directory')
    args.output_dir.mkdir(parents=True)
    snapshot=args.output_dir/'code-snapshot';snapshot.mkdir()
    code_hashes={}
    for path in [*ROOT.glob('agentic_memory/privacy/*.py'),ROOT/'evaluation/gatemem_medical/inference_adapter.py',Path(__file__).resolve()]:
        relative=path.relative_to(ROOT);target=snapshot/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,target);code_hashes[str(relative)]=hashlib.sha256(path.read_bytes()).hexdigest()
    (snapshot/'hashes.json').write_text(json.dumps(code_hashes,indent=2)+'\n')
    load_environment(ROOT/'.env')
    base_url=os.environ.get('AMEM_LLM_BASE_URL','http://127.0.0.1:8000/v1')
    if not base_url.startswith(('http://127.0.0.1:8000/','http://localhost:8000/')):
        raise SystemExit('This bounded smoke only permits the existing local model')
    episode=next(json.loads(line) for line in Path('/home/thaddus/GateMem/bench/data/medical/episodes.jsonl').read_text().splitlines()
                 if json.loads(line)['episode_id'].startswith('med_episode_rewrite_en_001_'))
    raw={t['turn_id']:source_turn(t) for t in episode['turns']}
    events=[];cases={};current_case='';started=time.monotonic()
    actual_http_attempts=0
    original_urlopen=urllib.request.urlopen
    def counted_urlopen(request,*args,**kwargs):
        nonlocal actual_http_attempts
        if hasattr(request,'full_url') and request.full_url.endswith('/chat/completions'):
            if actual_http_attempts>=args_budget:raise RuntimeError('Bounded smoke HTTP-request budget exhausted')
            actual_http_attempts+=1
            events[-1]['http_attempts']=events[-1].get('http_attempts',0)+1
        return original_urlopen(request,*args,**kwargs)
    args_budget=args.max_calls
    urllib.request.urlopen=counted_urlopen

    class BoundedProcessor(CompletionProcessor):
        def complete(self,system,payload,*,schema=None):
            if len(events)>=args.max_calls:raise RuntimeError('Bounded smoke model-call budget exhausted')
            event={'case':current_case,'call':len(events)+1,'sources':len(payload['sources']),
                   'candidates':len(payload.get('task_candidates',[])),
                   'request_bytes':len(json.dumps({'instruction':system,'payload':payload,'schema':schema},separators=(',',':')).encode())}
            events.append(event);t=time.monotonic()
            try:
                result=super().complete(system,payload,schema=schema)
                event['task_reviews']=result.get('task_reviews',[])
                event['operations']=[{'op':o.get('op'),'participant':o.get('participant'),
                    'assignment_id':o.get('assignment_id'),'id':o.get('id')} for o in result.get('operations',[])]
                event['context_requests']=result.get('context_requests',[])
                return result
            except Exception as exc:
                event['error']=type(exc).__name__+': '+str(exc)[:300];raise
            finally:
                event['seconds']=round(time.monotonic()-t,3);event['usage']=self.usage
                with (args.output_dir/'model-calls.jsonl').open('a') as f:f.write(json.dumps(event)+'\n')
                print(json.dumps({'case':current_case,'call':event['call'],'seconds':event['seconds'],
                    'usage':self.usage,'error':event.get('error')}),flush=True)

    for name,(requester,turn_ids) in CASES.items():
        if args.case not in {'all',name}:continue
        if args.profile=='pairs':
            turn_ids=(['t097','t099','t100','t101','t110','t111','t112'] if name=='outreach' else ['t116','t117','t144'])
        current_case=name;directory=args.output_dir/name;directory.mkdir()
        core=PrivacyMemorySystem(directory/'memory.sqlite',HashEmbedder())
        model=BoundedProcessor('smoke:permission',base_url,os.environ.get('AMEM_LLM_MODEL','Qwen/Qwen3.8-27B'),
            api_key=os.environ.get('AMEM_LLM_API_KEY','dummy'),timeout=240,max_tokens=4500)
        adapter=InferenceFixtureAdapter(core,{'episode_id':episode['episode_id']+':task-smoke:'+name},
            ['test:hash','smoke:permission'],model,permission_version='12',progress=print)
        completed=[];case_started=time.monotonic()
        try:
            for index,tid in enumerate(turn_ids):
                print(f'{name}: ingest {tid}',flush=True)
                adapter.ingest_batch({'boundary':index+1},[raw[tid]])
                completed.append(tid)
        except Exception as exc:
            cases[name]={'error':type(exc).__name__+': '+str(exc),'completed_turns':completed}
        finally:
            ctx=adapter.query_context({'asker':{'principal_id':requester}})
            records=core.search(ctx,'instructions medication callback',k=100)
            state=adapter.updater.inspect()
            assignments={aid:a for aid,a in state['assignment'].items() if a['participant']==requester}
            source_events={r['id']:json.loads(r['payload'])['source']['event_id'] for r in core.db.execute(
                "SELECT id,payload FROM resources WHERE kind='observation'")}
            cases.setdefault(name,{}).update(completed_turns=completed,source_turns=len(turn_ids),
                processing_seconds=round(time.monotonic()-case_started,3),
                active_assignments=[{'id':aid,'subject':a['subject'],'scope':a['information_scope'],'bindings':len(a['bindings'])}
                    for aid,a in assignments.items() if a['status']=='active'],
                worker_bound_turns=sorted({source_events[b['source_id']] for a in assignments.values() for b in a['bindings']}),
                retrieved_text=[r['text'] for r in records],
                pending_jobs=adapter.updater.pending_jobs(),
                forbidden_family_retrieval_count=len(core.search(adapter.query_context({'asker':{'principal_id':'family_linda_park'}}),'instructions',k=100)))
            validations=[json.loads(r[0] or '{}') for r in core.db.execute('SELECT validation FROM permission_attempts')]
            cases[name].update(rejected_proposals=sum(v.get('accepted') is False for v in validations),
                focused_reassessments=sum(bool(v.get('task_reassessment_required')) for v in validations),
                history_rounds=sum(bool(v.get('context_loaded')) for v in validations))
            core.close()
    result={'method':'Fresh isolated raw-dialogue subset; real local model, HashEmbedder; no official benchmark or score rewrite.',
        'model':os.environ.get('AMEM_LLM_MODEL'), 'profile':args.profile,
        'model_calls':len(events),'max_calls':args.max_calls,
        'http_attempts':actual_http_attempts,
        'prompt_tokens':sum((e.get('usage') or {}).get('prompt_tokens',0) for e in events),
        'completion_tokens':sum((e.get('usage') or {}).get('completion_tokens',0) for e in events),
        'elapsed_seconds':round(time.monotonic()-started,3),'cases':cases}
    (args.output_dir/'report.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2),flush=True)


if __name__=='__main__':main()
