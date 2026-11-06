"""Read-only replay of a persisted permission input; never changes an official run."""
import argparse,importlib.util,json,sqlite3,sys,time
from dataclasses import asdict
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from evaluation.gatemem_medical.runner import load_environment
from agentic_memory.privacy.processors import CompletionProcessor
import os
from types import SimpleNamespace

parser=argparse.ArgumentParser()
parser.add_argument('--before',action='store_true')
parser.add_argument('--turn',default='t005')
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
if args.before:
 name='agentic_memory.privacy.permissions_before'
 spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name('permissions_before.py'))
 mod=importlib.util.module_from_spec(spec);sys.modules[name]=mod;spec.loader.exec_module(mod)
else:
 from agentic_memory.privacy import permissions as mod
base=ROOT/'runs/gatemem-medical-raw-stream-v2'
p=sqlite3.connect(f'file:{base}/memory.privacy.sqlite?mode=ro',uri=True);p.row_factory=sqlite3.Row
m=sqlite3.connect(f'file:{base}/memory.sqlite?mode=ro',uri=True);m.row_factory=sqlite3.Row
all_sources={r['id']:json.loads(r['payload']) for r in m.execute('select id,payload from resources')}
sid=next(k for k,v in all_sources.items() if v['source']['event_id']==args.turn)
job=p.execute('select rowid,* from permission_jobs where source_id=? order by rowid limit 1',(sid,)).fetchone()
sources={rid:all_sources[rid] for rid in json.loads(job['inputs'])}
boot=mod.PermissionBootstrap(**json.loads(p.execute('select body from permission_bootstrap').fetchone()[0]))
u=object.__new__(mod.PermissionUpdater);u.bootstrap=boot;u.version='3';u.core=SimpleNamespace(db=m)
bootstrap=u._source_bootstrap(sources)
state={'activity':{},'assignment':{},'restriction':{}}
for r in p.execute('select r.kind,r.id,r.body from permission_records r join permission_jobs j on j.id=r.job_id where j.rowid<? order by j.rowid,r.version',(job['rowid'],)):
 state[r['kind']][r['id']]=json.loads(r['body'])
payload={'revision':job['revision'],'bootstrap':asdict(bootstrap),'new_source_id':sid,
 'sources':[{'id':rid,**body} for rid,body in sources.items()],'permission_state':state,
 'operation_contract':{op:{'allowed':sorted(fields|{'op','evidence','explanation'}),'required':sorted(mod._REQUIRED[op]|{'op','evidence','explanation'})} for op,fields in mod._FIELDS.items()},'contract_notes':mod.CONTRACT_NOTES}
schema=mod.proposal_schema(revision=job['revision'],bootstrap=bootstrap,source_ids=sources)
expand=lambda v:v
if not args.before:
 payload,schema,expand=mod.compact_input(payload,schema)
load_environment(ROOT/'.env')
processor=CompletionProcessor('vllm:qwen',os.environ['AMEM_LLM_BASE_URL'],os.getenv('AMEM_LLM_MODEL','Qwen/Qwen3.8-27B'),os.getenv('AMEM_LLM_API_KEY',''),max_tokens=8192)
start=time.monotonic()
response=processor.complete(mod.INSTRUCTION,payload,schema=schema)
result={'before':args.before,'turn':args.turn,'seconds':time.monotonic()-start,'usage':processor.usage,'payload_chars':len(json.dumps(payload)),'proposal':expand(response)}
try:
 u._validate(result['proposal'],state,sources,job['revision'])
 result['host_validation']='accepted'
except Exception as exc:
 result['host_validation']=type(exc).__name__+': '+str(exc)
args.output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='proposal'}),flush=True)
print('operations:',[o['op'] for o in result['proposal']['operations']],flush=True)
