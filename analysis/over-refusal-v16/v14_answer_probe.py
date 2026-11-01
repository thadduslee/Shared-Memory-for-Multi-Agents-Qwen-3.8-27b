"""Answer-only regression using a previously authorized synthetic packet; no gold input."""
import json
import argparse
import os
from pathlib import Path
from agentic_memory.privacy.answering import PLAN_INSTRUCTION,plan_output_schema,EVIDENCE_REVIEW_INSTRUCTION,evidence_roles_schema
from agentic_memory.privacy.processors import CompletionProcessor
from evaluation.gatemem_medical import openrouter,runner

b=Path(__file__).resolve().parent
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--candidate',default='candidate-13');p.add_argument('--prefix',default='v14')
args=p.parse_args()
records=[json.loads(l) for l in (b/args.candidate/'model-trace.jsonl').read_text().splitlines()]
record=next(r for r in records if r['payload'].get('query') and 'through Friday' in r['payload']['query'])
payload=record['payload'];ids=[v['id'] for v in payload['evidence']]
schema={'type':'object','additionalProperties':False,'properties':{
    'answer_structured':plan_output_schema(),
    'request_parts':{'type':'array','minItems':1,'items':{'type':'object','additionalProperties':False,
        'properties':{'request':{'type':'string'},'status':{'type':'string','enum':['supported','missing','out_of_scope','erased']},
            'evidence_ids':{'type':'array','items':{'type':'string','enum':ids}}},'required':['request','status','evidence_ids']}},
    'action':{'type':'string','enum':['answer','answer_redacted','refuse','no_memory']},
    'answer':{'type':'string'},'used_record_ids':{'type':'array','items':{'type':'string','enum':ids}}},
    'required':['answer_structured','request_parts','action','answer','used_record_ids']}
schema['properties']={'evidence_roles':evidence_roles_schema(len(ids)),**schema['properties']}
schema['required']=['evidence_roles',*schema['required']]
prompt=(EVIDENCE_REVIEW_INSTRUCTION+PLAN_INSTRUCTION.replace('FIRST populate','Then populate')
    +record['system'][record['system'].index('After answer_structured,'):])
openrouter.configure(runner.ROOT/'.env')
model=CompletionProcessor('probe',openrouter.BASE_URL,openrouter.MEMORY_MODEL,
    os.environ['AMEM_OPENROUTER_API_KEY'],max_tokens=4096)
output=model.complete(prompt,payload,schema=schema)
result={'query':payload['query'],'output':output,'seconds':model.last_elapsed_seconds,
    'provider':model.last_provider,'response_model':model.last_response_model,
    'evidence_kind':'answer-only regression over a previously authorized packet; no policy update or gold input'}
(b/f'{args.prefix}-answer-probe-result.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result),flush=True)
