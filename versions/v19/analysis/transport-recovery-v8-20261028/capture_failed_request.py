"""Reconstruct the failed v8 request on copied databases, without generation."""
from pathlib import Path
import json
import os
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.system import digest
from evaluation.gatemem_medical.runner import processors,load_environment
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter

os.umask(0o077)
run=ROOT/'runs/gatemem-medical-raw-stream-v8-full-20261028T174425+0800'
output=Path(__file__).parent
copy=output/'capture-copy'
copy.mkdir(exist_ok=False)
for name in ('memory.sqlite','memory.privacy.sqlite'):
    with sqlite3.connect('file:'+str(run/name)+'?mode=ro',uri=True) as source:
        with sqlite3.connect(copy/name) as target:source.backup(target)
load_environment(ROOT/'.env')
embedder,model,_=processors()
core=PrivacyMemorySystem(copy/'memory.sqlite',embedder)
episode=json.loads((run/'schedule.json').read_text())[0]['episode_id']
class Captured(Exception):pass
try:
    job=core.db.execute('SELECT * FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()
    previous_hash=core.db.execute('SELECT prompt_hash FROM permission_attempts WHERE job_id=? ORDER BY attempt DESC LIMIT 1',
        (job['id'],)).fetchone()[0]
    source=json.loads(core.db.execute('SELECT payload FROM resources WHERE id=?',(job['source_id'],)).fetchone()[0])
    core.clock=lambda:source['source']['observed_at']
    adapter=InferenceFixtureAdapter(core,{'episode_id':episode},[embedder.processor_id,model.processor_id],model,
        permission_version='8')
    def capture(system,payload,*,schema=None):
        actual=digest({'instruction':system,'payload':payload,'schema':schema})
        assert actual==previous_hash,(actual,previous_hash)
        request={'system':system,'payload':payload,'schema':schema}
        (output/'failed-request.json').write_text(json.dumps(request,ensure_ascii=False)+'\n')
        report={'prompt_hash':actual,'matches_original_failed_request':True,
            'source_event':source['source']['event_id'],'source_count':len(payload['sources']),
            'schema_bytes':len(json.dumps(schema).encode()),'original_run_modified':False}
        (output/'capture-report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2),flush=True)
        raise Captured()
    model.complete=capture
    try:adapter.updater.run(job['id'])
    except Captured:pass
finally:core.close()
