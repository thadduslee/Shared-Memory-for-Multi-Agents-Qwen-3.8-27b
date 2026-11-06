"""Bounded real-model measurements; never edits or scores an existing run."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.processors import CompletionProcessor, HashEmbedder
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter
from evaluation.gatemem_medical.input_boundary import source_turn
from evaluation.gatemem_medical.runner import load_environment


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--case',choices=['prefix','delayed'],required=True)
    args=parser.parse_args()
    os.umask(0o077)
    args.output_dir.mkdir(parents=True,exist_ok=False)
    load_environment(ROOT/'.env')
    if os.environ['AMEM_LLM_BASE_URL']!='http://127.0.0.1:8000/v1':
        raise SystemExit('This smoke requires the existing local model')
    calls=[]

    class Measured(CompletionProcessor):
        def complete(self,system,payload,*,schema=None):
            if len(calls)>=20:raise RuntimeError('Smoke call budget exhausted')
            event={'turn':payload['sources'][-1]['source']['event_id'],
                   'source_count':len(payload['sources']),'call':len(calls)+1}
            calls.append(event);start=time.monotonic()
            try:
                result=super().complete(system,payload,schema=schema)
                event['response']=result
                return result
            except Exception as exc:
                event['error']=type(exc).__name__+': '+str(exc)
                raise
            finally:
                event.update(seconds=round(time.monotonic()-start,3),usage=self.usage)
                with (args.output_dir/'model-calls.jsonl').open('a') as f:f.write(json.dumps(event)+'\n')
                print(json.dumps({k:v for k,v in event.items() if k!='response'}),flush=True)

    if args.case=='prefix':
        schedule=json.loads((ROOT/'runs/gatemem-medical-raw-stream-v12-full-20261029T161317+0800/schedule.json').read_text())
        episode_id=schedule[0]['episode_id']
        episode=next(e for line in Path('/home/thaddus/GateMem/bench/data/medical/episodes.jsonl').read_text().splitlines()
                     if (e:=json.loads(line))['episode_id']==episode_id)
        turns=[source_turn(t) for t in episode['turns'][:8]]
    else:
        episode_id='synthetic-delayed-acceptance'
        pairs=[('helper','Can I pick you up tomorrow?'),
               ('owner','Let me think about whether I want you to pick me up. Do not arrange it yet.')]
        pairs += [('owner',f'The unrelated sample number is {i}.') for i in range(7)]
        pairs += [('owner','Yes, you may pick me up tomorrow. Pickup is at 4 PM outside the library. My diagnosis is private.'),
                  ('owner','You may no longer access my pickup arrangements. I revoke that permission.')]
        turns=[{'turn_id':f't{i:03d}','speaker':{'principal_id':actor},'text':text}
               for i,(actor,text) in enumerate(pairs,1)]
    core=PrivacyMemorySystem(args.output_dir/'memory.sqlite',HashEmbedder())
    model=Measured('smoke:v13',os.environ['AMEM_LLM_BASE_URL'],os.environ['AMEM_LLM_MODEL'],
                   os.environ.get('AMEM_LLM_API_KEY','dummy'),timeout=180,max_tokens=3072)
    adapter=InferenceFixtureAdapter(core,{'episode_id':episode_id},['test:hash','smoke:v13'],model,progress=print)
    completed=[];report={'case':args.case,'method':'Isolated real local model with HashEmbedder; no official scores.'}
    early_access=False
    try:
        for i,turn in enumerate(turns,1):
            adapter.ingest_batch({'boundary':i},[turn]);completed.append(turn['turn_id'])
            (args.output_dir/'current-policy.json').write_text(json.dumps(adapter.updater.access_state(),indent=2)+'\n')
            if args.case=='delayed' and i<10:
                reader=adapter.query_context({'asker':{'principal_id':'helper'}})
                early_access=early_access or bool(core.search(reader,'pickup',k=100))
            if args.case=='delayed' and i==10:
                reader=adapter.query_context({'asker':{'principal_id':'helper'}})
                report['after_acceptance']={'policy':adapter.updater.access_state(),
                                           'retrieved':[r['text'] for r in core.search(reader,'pickup',k=100)]}
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)
    finally:
        report.update(completed_turns=completed,model_calls=len(calls),
            prompt_tokens=sum((c.get('usage') or {}).get('prompt_tokens',0) for c in calls),
            completion_tokens=sum((c.get('usage') or {}).get('completion_tokens',0) for c in calls),
            model_seconds=round(sum(c['seconds'] for c in calls),3),pending_jobs=adapter.updater.pending_jobs())
        if args.case=='delayed':
            reader=adapter.query_context({'asker':{'principal_id':'helper'}})
            report['after_revocation']=[r['text'] for r in core.search(reader,'pickup',k=100)]
            accepted=report.get('after_acceptance',{}).get('retrieved',[])
            report['checks']={'no_early_access':not early_access,
                'pickup_available_after_acceptance':any('4 PM' in text for text in accepted),
                'diagnosis_not_exposed':all('diagnosis' not in text.lower() for text in accepted),
                'revocation_removes_access':not report['after_revocation']}
            if not all(report['checks'].values()):report.setdefault('error','Delayed task semantic checks failed')
        core.close()
        (args.output_dir/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2),flush=True)
    return int('error' in report)


if __name__=='__main__':raise SystemExit(main())
