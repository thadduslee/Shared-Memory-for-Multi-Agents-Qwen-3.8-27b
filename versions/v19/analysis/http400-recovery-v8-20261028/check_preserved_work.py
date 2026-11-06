"""Read-only verification that recovery retained committed experiment work."""
from pathlib import Path
import hashlib,json,sqlite3
p=Path(__file__).parent
run=p.parents[1]/'runs/gatemem-medical-raw-stream-v8-full-20261028T174425+0800'
before=json.loads((p/'before.json').read_text())
with sqlite3.connect('file:'+str(run/'memory.privacy.sqlite')+'?mode=ro',uri=True) as c:
    jobs={r[0]:list(r) for r in c.execute("SELECT id,request_hash,proposal,changes FROM permission_jobs WHERE status='complete'")}
    assert all(jobs.get(row[0])==row for row in before['completed_jobs'])
    statuses=dict(c.execute('SELECT status,count(*) FROM permission_jobs GROUP BY status'))
with sqlite3.connect('file:'+str(run/'evaluation.sqlite')+'?mode=ro',uri=True) as c:
    official={r[0]:list(r) for r in c.execute('SELECT * FROM official')}
    assert all(official.get(row[0])==row for row in before['official'])
assert hashlib.sha256((run/'manifest.json').read_bytes()).hexdigest()==before['manifest_sha256']
report={'prior_completed_jobs_unchanged':len(before['completed_jobs']),'prior_predictions_and_scores_unchanged':len(before['official']),'manifest_unchanged':True,'source_jobs':statuses,'official_predictions':len(official),'exit_status_file_absent_while_running':not (run/'exit-status.txt').exists()}
(p/'preserved-work.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
