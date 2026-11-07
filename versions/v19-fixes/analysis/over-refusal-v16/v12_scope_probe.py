"""Read-only scope regression over an archived, synthetic authorized review packet."""
from copy import deepcopy
import json
import os
from pathlib import Path
from agentic_memory.privacy.incremental import BINDING_REPAIR_INSTRUCTION
from agentic_memory.privacy.incremental_wire import GROUNDED_INSTRUCTION
from agentic_memory.privacy.processors import CompletionProcessor
from evaluation.gatemem_medical import openrouter,runner

base=Path(__file__).resolve().parent
records=[json.loads(l) for l in (base/'candidate-11/model-trace.jsonl').read_text().splitlines()]
record=next(r for r in records if r['stage']=='incremental_binding_repair' and
    any(c['quote'].startswith('Imaging schedule updated:') for c in r['payload']['candidates']))
payload=deepcopy(record['payload'])
family=[aid for aid,a in payload['permission_state']['assignment'].items() if a['participant']=='p3']
template=deepcopy(payload['candidates'][0])
for ident,text in [('probe_contact','Private callback is 555-0199.'),('probe_price','Private price is 42000.')]:
    payload['candidates'].append({**template,'id':ident,'quote':text,'relationships':[]})
    payload['required_pairs'].append({'candidate_id':ident,'assignment_id':family[0]})
props={'q':{'type':'string','enum':[c['quote'][:80] for c in payload['candidates']]},
    'f':{'type':'string','enum':[c['id'] for c in payload['candidates']]},
    'g':{'type':'string','enum':list(payload['permission_state']['assignment'])},
    'r':{'type':'string','minLength':1,'maxLength':120},
    'd':{'type':'string','enum':['allow','outside','unattributed','unattributed_in_scope']},
    'deny':{'type':'array','items':{'type':'string','enum':list(payload['permission_state']['restriction'])}}}
schema={'type':'object','additionalProperties':False,'properties':{
    'review':{'type':'string','minLength':1,'maxLength':320},'b':{'type':'array',
    'maxItems':12,'items':{'type':'object','additionalProperties':False,'properties':props,
        'required':['q','f','g','r','d']}}},'required':['review','b']}
openrouter.configure(runner.ROOT/'.env')
model=CompletionProcessor('probe',openrouter.BASE_URL,openrouter.MEMORY_MODEL,
    os.environ['AMEM_OPENROUTER_API_KEY'],max_tokens=2048)
candidate_index={c['id']:c for c in payload['candidates']}
receipts={r['id']:r for r in payload['authorization_sources']}
paired=[]
for pair in payload['required_pairs']:
    candidate=candidate_index[pair['candidate_id']]
    grant=deepcopy(payload['permission_state']['assignment'][pair['assignment_id']])
    grant['authorization_context']=[receipts[r['source_id']] for r in grant['authorization_context']]
    restrictions=[r for r in payload['permission_state']['restriction'].values()
        if r['subject']==grant['subject'] and grant['participant'] in r['participants']]
    paired.append({'candidate':candidate,'assignment':grant,'applicable_restrictions':restrictions})
paired_payload={'stage':payload['stage'],'permission_version':'16','as_of':payload['as_of'],
    'pair_context':paired,'source_context':payload['source_context']}
schema['properties'].pop('review');schema['required']=['b']
response=model.complete(BINDING_REPAIR_INSTRUCTION,
    paired_payload,schema=schema)
decisions={(r['f'],r['g']):r for r in response['b']}
actual_id=template['id']
audit={'schedule_with_cancellation_allowed_by_general_time_location_grant':
    decisions.get((actual_id,family[0]),{}).get('d')=='allow',
    'contact_excluded':decisions.get(('probe_contact',family[0]),{}).get('d')=='outside',
    'price_excluded':decisions.get(('probe_price',family[0]),{}).get('d')=='outside'}
audit['passed']=all(audit.values())
result={'audit':audit,'response':response,'seconds':model.last_elapsed_seconds,
    'evidence_kind':'read-only archived synthetic packet; not a full evaluation or timing comparison'}
(base/'v12-scope-probe-result.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result),flush=True)
if not audit['passed']:raise SystemExit(1)
