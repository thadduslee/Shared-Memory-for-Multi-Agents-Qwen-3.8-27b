"""Audited, bounded t030 retry after a successful copied-prefix live test."""
from pathlib import Path
from datetime import datetime,timezone
import fcntl,hashlib,json,sys
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from evaluation.gatemem_medical.runner import load_environment,processors
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter
from evaluation.gatemem_medical.recovery import RecoveryController
from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.system import digest

folder=Path(__file__).parent;before=json.loads((folder/'before.json').read_text());review=json.loads((folder/'review.json').read_text())
run=Path(before['run']);expected=review['code_digest'];job_id=review['job_id']
if json.loads((run/'effective_manifest.json').read_text())['code_digest']!=expected:
 raise SystemExit('Record the reviewed code revision before repairing this job')
current=digest({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
 for directory in (ROOT/'agentic_memory/privacy',ROOT/'evaluation/gatemem_medical') for p in sorted(directory.iterdir())
 if p.is_file() and (p.suffix=='.py' or p.name=='fixture_policy.json')})
if current!=expected:raise SystemExit('Reviewed implementation changed')
lock=(run/'run.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
load_environment(ROOT/'.env');embedder,model,_=processors()
core=PrivacyMemorySystem(run/'memory.sqlite',embedder)
try:
 job=core.db.execute('SELECT * FROM permission_jobs WHERE id=?',(job_id,)).fetchone()
 if job['status']=='complete':print('t030 already complete; no model request repeated',flush=True)
 else:
  if job['attempts']!=3 or job['status']!='rejected':raise SystemExit('Unexpected job state; further review required')
  authorization={**review,'at':datetime.now(timezone.utc).isoformat(),'prior_attempts':3,
      'reason':'Exact source/text schema pairing and precise source feedback; live copied-prefix probe passed.',
      'old_attempts_preserved':True,'counter_reset':False,'permission_checks_retained':True}
  (run/'reviewed-quote-repair-authorization.json').write_text(json.dumps(authorization,indent=2)+'\n')
  episode=json.loads((run/'schedule.json').read_text())[0]['episode_id']
  adapter=InferenceFixtureAdapter(core,{'episode_id':episode},[embedder.processor_id,model.processor_id],model,permission_version='10')
  source=json.loads(core.db.execute('SELECT payload FROM resources WHERE id=?',(job['source_id'],)).fetchone()[0])
  assert source['source']['event_id']=='t030'
  adapter.trusted_time({'timestamp':source['source']['observed_at']})
  print('Reviewed quote repair: at most two additional proposals for t030; normal checks remain active.',flush=True)
  result=adapter.updater.update(job_id,max_attempts=review['maximum_total_rejected_proposals'])
  attempts=core.db.execute('SELECT attempts FROM permission_jobs WHERE id=?',(job_id,)).fetchone()[0]
  report={'source':'t030','job_id':job_id,'status':result['status'],'new_model_attempts':attempts-3,
      'total_attempts':attempts,'changes':len(result['changes']),'code_digest':expected}
  (run/'reviewed-quote-repair-result.json').write_text(json.dumps(report,indent=2)+'\n')
  RecoveryController(run)._record({'state':'running','stage':'permission '+job_id,
      'reviewed_quote_repair':True,'recovered_after_attempts':attempts,'code_digest':expected})
  print(json.dumps(report,indent=2),flush=True)
finally:core.close();lock.close()
