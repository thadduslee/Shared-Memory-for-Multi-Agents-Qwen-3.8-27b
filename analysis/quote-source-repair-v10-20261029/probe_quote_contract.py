"""Live-model copied-prefix check. Never modifies the original run databases."""
from pathlib import Path
import json,os,sqlite3,sys,time
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from evaluation.gatemem_medical.runner import load_environment,processors
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter
from agentic_memory.privacy import PrivacyMemorySystem

os.umask(0o077)
folder=Path(__file__).parent;target=folder/('live-probe-'+sys.argv[1] if len(sys.argv)>1 else 'live-probe');target.mkdir(exist_ok=False)
for name in ('memory.sqlite','memory.privacy.sqlite'):
 with sqlite3.connect('file:'+str(folder/name)+'?mode=ro',uri=True) as source:
  with sqlite3.connect(target/name) as output:source.backup(output)
load_environment(ROOT/'.env');embedder,model,_=processors()
core=PrivacyMemorySystem(target/'memory.sqlite',embedder)
try:
 job=core.db.execute("SELECT * FROM permission_jobs WHERE status='rejected'").fetchone()
 source=json.loads(core.db.execute('SELECT payload FROM resources WHERE id=?',(job['source_id'],)).fetchone()[0])
 assert source['source']['event_id']=='t030'
 original=Path(json.loads((folder/'before.json').read_text())['run'])
 episode=json.loads((original/'schedule.json').read_text())[0]['episode_id']
 adapter=InferenceFixtureAdapter(core,{'episode_id':episode},[embedder.processor_id,model.processor_id],model,permission_version='10')
 adapter.trusted_time({'timestamp':source['source']['observed_at']})
 complete=model.complete;calls=[]
 def capture(system,payload,*,schema=None):
  n=len(calls)+1;calls.append(n)
  (target/f'request-{n}.json').write_text(json.dumps({'system':system,'payload':payload,'schema':schema},ensure_ascii=False))
  value=complete(system,payload,schema=schema)
  (target/f'response-{n}.json').write_text(json.dumps(value,ensure_ascii=False))
  print('Live copied-prefix model call',n,'completed',flush=True)
  return value
 model.complete=capture
 start=time.monotonic()
 try:
  result=adapter.updater.update(job['id'],max_attempts=5)
  report={'status':result['status'],'new_model_calls':len(calls),'elapsed_seconds':round(time.monotonic()-start,3),
    'changes':len(result['changes']),'original_run_modified':False,'all_checks_applied':True,'usage':model.usage}
 except Exception as exc:
  report={'status':'failed','error':type(exc).__name__,'reason':str(exc),'new_model_calls':len(calls),
          'original_run_modified':False}
  (target/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2),flush=True);raise
 (target/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2),flush=True)
finally:core.close()
