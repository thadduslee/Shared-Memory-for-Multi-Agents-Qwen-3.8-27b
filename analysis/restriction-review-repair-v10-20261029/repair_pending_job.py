"""Explicit, bounded operator repair after the reviewed host-contract correction."""
from pathlib import Path
from datetime import datetime,timezone
import fcntl,hashlib,json,sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from evaluation.gatemem_medical.runner import load_environment,processors
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter
from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.system import digest

folder=Path(__file__).parent;before=json.loads((folder/'before.json').read_text());run=Path(before['run'])
job_id='permission-job:65c05bcd81bf7dcaced8d3a5d820f6439a2a8ab04a86086d2c55f55145f0a471'
expected='d44e9d4aecc2bfe7d388ea22ca6ce79ab0a88a23085aff0de80fd9c74ffb1065'
active=json.loads((run/'effective_manifest.json').read_text())
if active['code_digest']!=expected:raise SystemExit('Expected reviewed code revision must be recorded before repair')
current=digest({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
    for directory in (ROOT/'agentic_memory/privacy',ROOT/'evaluation/gatemem_medical') for p in sorted(directory.iterdir())
    if p.is_file() and (p.suffix=='.py' or p.name=='fixture_policy.json')})
if current!=expected:raise SystemExit('Repair implementation changed')
load_environment(ROOT/'.env');embedder,model,_=processors()
lock=(run/'run.lock').open('a')
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
core=PrivacyMemorySystem(run/'memory.sqlite',embedder)
try:
 job=core.db.execute('SELECT * FROM permission_jobs WHERE id=?',(job_id,)).fetchone()
 if job['status']=='complete':print('Reviewed job already complete; no extra model call',flush=True)
 else:
  if job['attempts']!=3 or job['status']!='rejected':raise SystemExit('Unexpected pending-job state; review required')
  authorization={'at':datetime.now(timezone.utc).isoformat(),'job_id':job_id,
      'reason':'Reviewed correction of stored-versus-new restriction review IDs; exact rejected responses replayed and 259 tests passed.',
      'prior_attempts':3,'maximum_total_rejected_proposals':5,'maximum_new_attempts':2,
      'code_digest':expected,'existing_attempts_preserved':True,'permission_checks_unchanged':True}
  (run/'reviewed-repair-authorization.json').write_text(json.dumps(authorization,indent=2)+'\n')
  episode=json.loads((run/'schedule.json').read_text())[0]['episode_id']
  adapter=InferenceFixtureAdapter(core,{'episode_id':episode},[embedder.processor_id,model.processor_id],model,permission_version='10')
  source=json.loads(core.db.execute('SELECT payload FROM resources WHERE id=?',(job['source_id'],)).fetchone()[0])
  adapter.trusted_time({'timestamp':source['source']['observed_at']})
  print('Reviewed t008 repair: allowing at most two further model proposals; no history or budget records reset.',flush=True)
  result=adapter.updater.update(job_id,max_attempts=5)
  attempts=core.db.execute('SELECT attempts FROM permission_jobs WHERE id=?',(job_id,)).fetchone()[0]
  report={'job_id':job_id,'source':'t008','status':result['status'],'total_attempts':attempts,
          'new_model_attempts':attempts-3,'changes':len(result['changes']),'code_digest':expected}
  (run/'reviewed-repair-result.json').write_text(json.dumps(report,indent=2)+'\n')
  from evaluation.gatemem_medical.recovery import RecoveryController
  RecoveryController(run)._record({'state':'running','stage':'permission '+job_id,
      'reviewed_repair':True,'recovered_after_attempts':attempts,'code_digest':expected})
  print(json.dumps(report,indent=2),flush=True)
finally:
 core.close();lock.close()
