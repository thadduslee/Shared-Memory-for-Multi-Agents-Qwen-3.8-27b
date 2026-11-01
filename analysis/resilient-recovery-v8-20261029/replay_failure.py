"""Validate the exact rejected t114 proposal on copied databases."""
from pathlib import Path
from copy import deepcopy
import json,os,sqlite3,sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agentic_memory.privacy import PrivacyMemorySystem
from evaluation.gatemem_medical.runner import load_environment,processors
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter
os.umask(0o077)
p=Path(__file__).parent
copy=p/'replay-copy';copy.mkdir(exist_ok=False)
for name in ('memory.sqlite','memory.privacy.sqlite'):
    with sqlite3.connect('file:'+str(p/name)+'?mode=ro',uri=True) as c:
        with sqlite3.connect(copy/name) as target:c.backup(target)
load_environment(ROOT/'.env')
embedder,model,_=processors()
core=PrivacyMemorySystem(copy/'memory.sqlite',embedder)
try:
    job=core.db.execute("SELECT * FROM permission_jobs WHERE status='rejected' ORDER BY rowid DESC LIMIT 1").fetchone()
    assert job
    proposal=json.loads(job['proposal'])
    source=json.loads(core.db.execute('SELECT payload FROM resources WHERE id=?',(job['source_id'],)).fetchone()[0])
    assert source['source']['event_id']=='t114'
    run=ROOT/'runs/gatemem-medical-raw-stream-v8-full-20261028T174425+0800'
    episode=json.loads((run/'schedule.json').read_text())[0]['episode_id']
    adapter=InferenceFixtureAdapter(core,{'episode_id':episode},[embedder.processor_id,model.processor_id],model,permission_version='8')
    adapter.trusted_time({'timestamp':source['source']['observed_at']})
    model.complete=lambda system,payload,schema=None:deepcopy(proposal)
    result=adapter.updater.update(job['id'])
    row=core.db.execute('SELECT proposal,validation FROM permission_jobs WHERE id=?',(job['id'],)).fetchone()
    accepted=json.loads(row['proposal']);audit=json.loads(row['validation'])
    assert accepted['operations']==proposal['operations']
    assert audit['accepted'] and audit['coverage_adjustments']==[{'principal':'patient_elena_park','reported':'changed','actual':'unchanged'}]
    assert not adapter.updater.pending_jobs()
    report={'exact_rejected_proposal_accepted':True,'operations_unchanged':True,'all_authorization_and_binding_checks_applied':True,'original_run_modified':False,'source':'t114','changes':len(result['changes']),'coverage_adjustments':audit['coverage_adjustments']}
    (p/'replay-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
finally:core.close()
