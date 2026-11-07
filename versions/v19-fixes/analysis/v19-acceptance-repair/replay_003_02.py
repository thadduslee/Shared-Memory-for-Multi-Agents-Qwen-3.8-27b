"""Offline retrospective of saved t044 interpretation/repair; never publish ACLs.

Run from the repository with PYTHONPATH=. and --run-dir, --card, --output.
Benchmark selection is confined to this analysis program. The resolver receives
only retained authenticated source payloads and the recorded raw-stream bootstrap.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from agentic_memory.privacy import acceptance_v19 as acceptance
from agentic_memory.privacy.permissions import PermissionUpdater, PermissionBootstrap, PermissionProposalRejected


def readonly(path):
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    db.row_factory=sqlite3.Row
    return db


def raw(proposal):
    return {'c':{str(d['index']):{'intents':d.get('intents',['none']),
        'recipient':d.get('recipient'),'existing':d.get('existing'),'r':d['reason']}
        for d in proposal['control_review']},'p':[]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--card',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    card=json.loads(args.card.read_text())
    attempts=[a for a in card['selected_control_attempts'] if a['source_turn']=='t044']
    policy=readonly(args.run_dir/'memory.privacy.sqlite');memory=readonly(args.run_dir/'memory.sqlite')
    scratch=sqlite3.connect(':memory:',isolation_level=None)
    scratch.execute("ATTACH DATABASE ':memory:' AS privacy")
    try:
        job=policy.execute('SELECT * FROM permission_jobs WHERE id=?',(attempts[0]['job_id'],)).fetchone()
        sources={}
        for sid in json.loads(job['inputs']):
            row=memory.execute('SELECT payload FROM resources WHERE id=?',(sid,)).fetchone()
            if row:sources[sid]=json.loads(row[0])
        boot=policy.execute('SELECT body FROM permission_bootstrap WHERE compartment=?',(job['compartment'],)).fetchone()
        cutoff=policy.execute('SELECT MIN(rowid) FROM permission_records WHERE job_id=?',(job['id'],)).fetchone()[0]
        state={k:{} for k in ('assignment','activity','restriction')}
        for row in policy.execute('SELECT kind,id,body FROM permission_records WHERE compartment=? AND rowid<? ORDER BY rowid',
                (job['compartment'],cutoff)):
            state[row['kind']][row['id']]=json.loads(row['body'])
        updater=PermissionUpdater.__new__(PermissionUpdater)
        updater.bootstrap=PermissionBootstrap(**json.loads(boot[0]));updater.version='19'
        updater.compartment=job['compartment'];updater._state=lambda:state
        now=sources[job['source_id']]['source'].get('observed_at')
        updater.core=SimpleNamespace(db=scratch,clock=lambda:now)
        acceptance.install(updater)
        acceptance.observe(updater,job['id'],job['source_id'],raw(attempts[0]['proposal']),sources,{},1)
        first=acceptance.rows(updater,job['id'])[0]
        try:
            acceptance.review(updater,job['id'],raw(attempts[1]['proposal']),sources,{},2,True)
        except PermissionProposalRejected as exc:
            rejected=str(exc)
        else:
            raise AssertionError('The historical silent intent drop unexpectedly validated')
        final=acceptance.rows(updater,job['id'])[0]
        assert final['original']==first['original']
        wrong=first['original']['existing_grant']
        result={'method':'Immutable read-only historical SQLite plus an in-memory interpretation ledger; no processor calls or ACL publication.',
            'historical_run':str(args.run_dir),'evidence_card':str(args.card),
            'evidence_card_sha256':hashlib.sha256(args.card.read_bytes()).hexdigest(),
            'retained_prefix_sources':len(sources),'source_id':job['source_id'],
            'original_intent':first['original'],'task':final['task'],'state':final['state'],
            'candidate_offers':final['candidate_offers'],
            'saved_existing_grant_actual_recipient':state['assignment'].get(wrong,{}).get('participant'),
            'saved_silent_intent_drop_rejected':rejected,
            'limits':['Retained end-of-run audit/source rows can have been scrubbed by later erasure.',
                'This checks interpretation preservation and source resolution, not a paid evaluation or historical utility score.',
                'No historical artifact or score was changed.']}
        args.output.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({'state':result['state'],'resolved_worker':(result['task'] or {}).get('worker'),
            'saved_grant_recipient':result['saved_existing_grant_actual_recipient'],'rejected':rejected}))
    finally:
        policy.close();memory.close();scratch.close()


if __name__=='__main__':main()
