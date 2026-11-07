"""Read-only checks of saved work and the explicitly revised implementation."""
from pathlib import Path
import hashlib,json,sqlite3
p=Path(__file__).parent
run=p.parents[1]/'runs/gatemem-medical-raw-stream-v8-full-20261028T174425+0800'
before=json.loads((p/'before.json').read_text())
with sqlite3.connect('file:'+str(run/'memory.privacy.sqlite')+'?mode=ro',uri=True) as c:
    jobs={r[0]:list(r) for r in c.execute("SELECT id,request_hash,proposal,changes FROM permission_jobs WHERE status='complete'")}
    assert all(jobs.get(row[0])==row for row in before['completed_jobs'])
    statuses=dict(c.execute('SELECT status,count(*) FROM permission_jobs GROUP BY status'))
    recovered=c.execute("SELECT validation FROM permission_jobs WHERE source_id=?",('permission-source:c7ced2d9b1bd80a25ba5858ca1e0607f2da10f7bd9021dd9239783cac7b62238',)).fetchone()
with sqlite3.connect('file:'+str(run/'evaluation.sqlite')+'?mode=ro',uri=True) as c:
    official={r[0]:list(r) for r in c.execute('SELECT * FROM official')}
    assert all(official.get(row[0])==row for row in before['official'])
assert hashlib.sha256((run/'manifest.json').read_bytes()).hexdigest()==before['manifest_sha256']
effective=json.loads((run/'effective_manifest.json').read_text())
assert effective['code_digest']==json.loads((p/'revised-code.json').read_text())['code_digest']
assert {k:v for k,v in effective.items() if k!='code_digest'}=={k:v for k,v in before['manifest'].items() if k!='code_digest'}
report={'prior_completed_jobs_unchanged':len(before['completed_jobs']),'prior_predictions_and_scores_unchanged':len(before['official']),'original_manifest_unchanged':True,'effective_implementation_matches_tested_revision':True,'source_jobs':statuses,'official_predictions':len(official),'exit_status_file_absent_while_running':not (run/'exit-status.txt').exists(),'recovered_t114_validation':json.loads(recovered[0])}
(p/'preserved-work.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k!='recovered_t114_validation'},indent=2))
