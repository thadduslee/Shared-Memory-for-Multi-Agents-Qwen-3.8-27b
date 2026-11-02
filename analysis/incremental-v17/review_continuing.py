"""Read-only comparison of a recorded field-review batch; no score/state edits."""
import argparse
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path

from agentic_memory.privacy.incremental import BINDING_REPAIR_INSTRUCTION,obj,enum
from agentic_memory.privacy.incremental_v18 import BINDING_SCOPE_INSTRUCTION
from agentic_memory.privacy.incremental_wire import self_activity_scope
from evaluation.gatemem_medical import openrouter,runner


def main():
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True)
    p.add_argument('--source-ref',required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();os.umask(0o077);args.output.mkdir(parents=True,exist_ok=False)
    rows=[json.loads(line) for line in (args.run/'model-trace.jsonl').read_text().splitlines()]
    record=next(row for row in rows if row['stage']=='incremental_binding_repair' and any(
        pair['candidate']['source_id']==args.source_ref and pair['assignment']['participant']==pair['assignment']['subject']
        for pair in row['payload'].get('pair_context',[])))
    payload=deepcopy(record['payload']);changed=[]
    prefix="The subject's own responsive facts, instructions, results, current plan and updates for: "
    for index,pair in enumerate(payload['pair_context']):
        a=pair['assignment'];scope=a['information_scope']
        if a['participant']==a['subject'] and scope.startswith(prefix):
            activity=scope[len(prefix):].split('. Includes continuing requirements',1)[0]
            a['information_scope']=self_activity_scope(activity,continuing=True);changed.append(str(index))
            # Recover the exact worker selected by the original accepted call;
            # do not infer service links from roles, medicine names or scores.
            for origin in rows:
                for op in (origin.get('response') or {}).get('p',[]):
                    if op.get('o')!='activity' or op.get('via')!='response' or op.get('work')!=activity:continue
                    sources={s['id']:s for s in origin['payload'].get('sources',[])}
                    subject=sources.get(op['e']);worker=sources.get(op['q'])
                    if not subject or not worker or not any(r['text']==subject['text'] for r in a['authorization_context']):continue
                    current=next(r for r in a['authorization_context'] if r['text']==subject['text'])
                    question=next((r for r in a['authorization_context'] if r['text']==worker['text']),None)
                    if question is None:question={'id':'review-question-'+str(index),'participant':op['p'],'text':worker['text']}
                    a['accepted_service']={'worker':op['p'],'worker_source_id':question['id'],
                        'subject_source_id':current['id']}
                    a['authorization_context']=[{k:v for k,v in r.items() if k!='context_only'} for r in [current,question]]
                    break
                if 'accepted_service' in a:break
    slots={str(i):obj({'r':{'type':'string','minLength':1,'maxLength':80},
        'd':enum(['allow','outside','unattributed','unattributed_in_scope']),
        'deny':{'type':'array','items':{'type':'string'}}}) for i in range(len(payload['pair_context']))}
    for slot in slots.values():slot['required']=['r','d']
    openrouter.configure(runner.ROOT/'.env')
    spec=importlib.util.spec_from_file_location('recorded_run',runner.ROOT/'analysis/over-refusal-v16/run.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    model=module.RecordedCompletion('analysis:continuing',openrouter.BASE_URL,openrouter.MEMORY_MODEL,
        os.environ['AMEM_OPENROUTER_API_KEY']);model.trace=args.output/'model-trace.jsonl'
    result=model.complete(BINDING_REPAIR_INSTRUCTION+BINDING_SCOPE_INSTRUCTION,payload,schema=obj({'b':obj(slots)}))
    before=record['response']['b'];after=result['b']
    report={'read_only':True,'native_predictions_changed':False,'scope_changed_slots':changed,
        'decisions':[{'slot':str(i),'reader':pair['assignment']['participant'],
            'subject':pair['assignment']['subject'],'fact_id':pair['candidate']['id'],
            'before':before[str(i)],'after':after[str(i)]} for i,pair in enumerate(payload['pair_context'])]}
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'read_only':True,'pairs':len(after),'changed_verdicts':[
        {'slot':key,'before':before[key]['d'],'after':after[key]['d']} for key in after if before[key]['d']!=after[key]['d']]},indent=2))


if __name__=='__main__':main()
