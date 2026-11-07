"""Read-only reproduction of saved omissions and the new rejection boundary."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agentic_memory.privacy import task_tracking as tasks
from agentic_memory.privacy.permissions import PermissionProposalRejected


def main():
    run=ROOT/'runs/gatemem-medical-raw-stream-v10-full-20261029T105316+0800'
    protected=[*list((ROOT/'analysis/over-refusal-trace-v10-20261029').glob('*')),
               *[run/n for n in ('official_predictions.jsonl','scores.jsonl','manifest.json')]]
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected if p.is_file()}
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.execute('ATTACH DATABASE ? AS saved_memory',((run/'memory.sqlite').resolve().as_uri()+'?mode=ro',))
    db.execute('ATTACH DATABASE ? AS privacy',((run/'memory.privacy.sqlite').resolve().as_uri()+'?mode=ro',))
    # The runtime's new task table exists only in this transient in-memory DB.
    db.execute('CREATE TABLE permission_task_candidates(compartment TEXT,id TEXT,body TEXT)')
    db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    trace=json.loads((ROOT/'analysis/over-refusal-trace-v10-20261029/trace.json').read_text())
    results=[]
    for case in trace['cases']:
        checkpoint=case['checkpoint'];requester=checkpoint['asker']['principal_id']
        cutoff=checkpoint['as_of_turn_id']
        row=db.execute("SELECT j.*,j.rowid AS job_position FROM permission_jobs j JOIN resources s ON s.id=j.source_id "
                       "WHERE json_extract(s.payload,'$.source.event_id')=? ORDER BY j.rowid DESC LIMIT 1",(cutoff,)).fetchone()
        jobs={r['id']:r['job_position'] for r in db.execute('SELECT id,rowid AS job_position FROM permission_jobs WHERE compartment=?',(row['compartment'],))}
        state={'activity':{},'assignment':{},'restriction':{}}
        for r in db.execute('SELECT * FROM permission_records WHERE compartment=? ORDER BY rowid',(row['compartment'],)):
            if jobs[r['job_id']]<=row['job_position']:state[r['kind']][r['id']]=json.loads(r['body'])
        assert not any(r['participant']==requester for kind in ('activity','assignment') for r in state[kind].values())
        semantic_turn='t101' if cutoff=='t112' else 't117'
        job=db.execute("SELECT j.* FROM permission_jobs j JOIN resources s ON s.id=j.source_id "
                       "WHERE j.compartment=? AND json_extract(s.payload,'$.source.event_id')=? ORDER BY j.rowid DESC LIMIT 1",
                       (row['compartment'],semantic_turn)).fetchone()
        prefix=json.loads(job['inputs'])
        sources={sid:json.loads(db.execute('SELECT payload FROM resources WHERE id=?',(sid,)).fetchone()[0]) for sid in prefix}
        core=SimpleNamespace(db=db)
        selected,_,_=tasks.prepare(core,row['compartment'],sources,prefix,job['source_id'])
        assert requester in {r['participant'] for r in selected.values()}
        def fail(message,details=None):raise PermissionProposalRejected(message,details)
        try:
            tasks.validate_reviews(deepcopy(json.loads(job['proposal'])),selected,sources,set(prefix[-6:]),state,fail,{})
        except PermissionProposalRejected as exc:
            rejection=str(exc)
        else:raise AssertionError('Saved silent omission unexpectedly accepted')
        results.append({'checkpoint':cutoff,'requester':requester,'original_missing_grant_reproduced':True,
                        'saved_accepted_turn':semantic_turn,'new_contract_rejects_silent_omission':rejection,
                        'actual_model_replay':False})
    db.rollback();db.close()
    after={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in before}
    assert before==after
    output={'method':'Read-only original ledger reconstruction and pure new-contract validation; not a model-quality result.',
            'cases':results,'original_artifacts_unchanged':before==after,'protected_hashes':before,'model_calls':0}
    path=Path(__file__).parent/'retrospective.json';path.write_text(json.dumps(output,indent=2)+'\n')
    print(json.dumps(output,indent=2))


if __name__=='__main__':main()
