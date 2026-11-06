"""Recover the failed source at its original clock before normal boundary replay."""
import fcntl
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.system import digest
from evaluation.gatemem_medical.runner import load_environment,processors
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter

os.umask(0o077)
os.environ['NO_PROXY']='127.0.0.1,localhost'
os.environ['no_proxy']='127.0.0.1,localhost'
run=ROOT/'runs/gatemem-medical-raw-stream-v8-full-20261028T174425+0800'
with (run/'run.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    load_environment(ROOT/'.env')
    embedder,model,_=processors()
    core=PrivacyMemorySystem(run/'memory.sqlite',embedder)
    try:
        job=core.db.execute("SELECT * FROM permission_jobs WHERE status NOT IN ('complete','superseded') ORDER BY rowid LIMIT 1").fetchone()
        assert job and job['status']=='processor_error'
        source=json.loads(core.db.execute('SELECT payload FROM resources WHERE id=?',(job['source_id'],)).fetchone()[0])
        assert source['source']['event_id']=='t061'
        episode=json.loads((run/'schedule.json').read_text())[0]['episode_id']
        adapter=InferenceFixtureAdapter(core,{'episode_id':episode},[embedder.processor_id,model.processor_id],model,
            permission_version='8')
        adapter.trusted_time({'timestamp':source['source']['occurred_at']})
        failed_hash=core.db.execute('SELECT prompt_hash FROM permission_attempts WHERE job_id=? ORDER BY attempt DESC LIMIT 1',
            (job['id'],)).fetchone()[0]
        complete=model.complete
        checked_hashes=[]
        def checked(system,payload,*,schema=None):
            prompt_hash=digest({'instruction':system,'payload':payload,'schema':schema})
            if not checked_hashes:assert prompt_hash==failed_hash
            checked_hashes.append(prompt_hash)
            return complete(system,payload,schema=schema)
        model.complete=checked
        print('Recovering t061 at its source timestamp with the unchanged request.',flush=True)
        result=adapter.updater.update(job['id'])
        report={'source':'t061','status':result['status'],'backend':'guidance','prompt_hash_matches_failed_request':True,
            'model_elapsed_s':model.last_elapsed_seconds,'usage':model.usage,
            'job_attempts':core.db.execute('SELECT attempts FROM permission_jobs WHERE id=?',(job['id'],)).fetchone()[0],
            'published_changes':len(result['changes']),'original_checkpoint_predictions_replayed':0,
            'evolution':'Normal runner resumes source replay and performs t061 evolution with its existing deterministic job ID.'}
        (Path(__file__).parent/'pending-recovery.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2),flush=True)
    finally:core.close()
