"""Replay the failed source on private database copies, without rescoring queries."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agentic_memory.privacy import PrivacyMemorySystem
from evaluation.gatemem_medical.runner import processors,load_environment
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True,exist_ok=False)
    for name in ('memory.sqlite','memory.privacy.sqlite'):
        with sqlite3.connect('file:'+str(args.source/name)+'?mode=ro',uri=True) as source:
            with sqlite3.connect(args.output/name) as target:source.backup(target)
    os.environ['NO_PROXY']='127.0.0.1,localhost'
    load_environment(ROOT/'.env')
    embedder,model,_=processors()
    core=PrivacyMemorySystem(args.output/'memory.sqlite',embedder)
    episode=json.loads((args.source/'schedule.json').read_text())[0]['episode_id']
    report={'mode':'diagnostic-copy-of-failed-prefix','official_queries_replayed':0}
    try:
        clock=json.loads(core.db.execute('SELECT payload FROM resources WHERE id=(SELECT source_id FROM permission_jobs ORDER BY rowid DESC LIMIT 1)').fetchone()[0])['source']['observed_at']
        core.clock=lambda:clock
        adapter=InferenceFixtureAdapter(core,{'episode_id':episode},
            [embedder.processor_id,model.processor_id],model,permission_version='8')
        original=model.complete
        calls=[]
        def complete(system,payload,*,schema=None):
            # Reconstruct v7's redundant binding prose for a same-state size
            # comparison. Full canonical evidence remains in the copied store.
            legacy=deepcopy(payload)
            for source in legacy['sources']:source.pop('bindable_spans',None)
            canonical=adapter.updater.inspect()
            aliases={s['id']:f's{i}' for i,s in enumerate(
                [{'id':r[0]} for r in core.db.execute("SELECT id FROM resources WHERE kind='observation' ORDER BY created,id")],1)}
            def refs(value):
                if isinstance(value,str):return aliases.get(value,value)
                if isinstance(value,list):return [refs(v) for v in value]
                if isinstance(value,dict):return {k:refs(v) for k,v in value.items()}
                return value
            for aid,a in legacy['permission_state']['assignment'].items():
                for b,old in zip(a.get('bindings',[]),canonical['assignment'][aid].get('bindings',[])):
                    b['evidence']=refs(old['evidence'])
                    for d,previous in zip(b.get('restriction_decisions',[]),old.get('restriction_decisions',[])):
                        d['evidence']=refs(previous['evidence'])
            size=lambda value:len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode())
            result=original(system,payload,schema=schema)
            calls.append({'payload_bytes':size(payload),'v7_equivalent_payload_bytes':size(legacy),
                'usage':model.usage,'model_elapsed_s':model.last_elapsed_seconds,
                'question_source_selectable_for_binding':any(
                    payload['new_source_id'] in v['properties']['source_id'].get('enum',[])
                    for v in schema['properties']['operations']['items']['anyOf']
                    if v['properties']['op']['enum']==['bind_information'])})
            print(json.dumps(calls[-1]),flush=True)
            return result
        model.complete=complete
        pending=adapter.updater.pending_jobs()
        assert len(pending)==1,pending
        result=adapter.updater.update(pending[0])
        report.update(status=result['status'],changes=len(result['changes']),calls=calls)
        assert result['status']=='complete'
        assert not any(call['question_source_selectable_for_binding'] for call in calls)
    finally:
        (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        core.close()
    print(json.dumps(report,indent=2),flush=True)


if __name__=='__main__':main()
