"""Explicitly replay the exact paused proposal, retaining all original audit rows."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.system import digest
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter
from evaluation.gatemem_medical.runner import load_environment,processors,write_json
from evaluation.gatemem_medical.recovery import reconcile_manifest,RecoveryController

JOB='permission-job:344d0a25072333fd426a6a178e540229158c2605dfe9b5418e188b5240902ddf'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--proposal-digest',required=True)
    parser.add_argument('--previous-code-digest')
    args=parser.parse_args();run=args.run_dir.resolve()
    lock=(run/'run.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    code_digest=digest({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in (ROOT/'agentic_memory/privacy',ROOT/'evaluation/gatemem_medical')
        for p in sorted(folder.iterdir()) if p.is_file() and (p.suffix=='.py' or p.name=='fixture_policy.json')})
    if args.previous_code_digest:
        journal=sqlite3.connect(run/'evaluation.sqlite')
        current=json.loads((run/'effective_manifest.json').read_text());current['code_digest']=code_digest
        original,revisions=reconcile_manifest(journal,current,resume=True,previous_code_digest=args.previous_code_digest)
        write_json(run/'effective_manifest.json',current)
        write_json(run/'implementation_revisions.json',revisions)
        journal.close()
    load_environment(ROOT/'.env')
    embedder,model,_=processors()
    model_calls=0
    def forbidden(*a,**k):
        nonlocal model_calls
        model_calls+=1
        raise AssertionError('Reviewed proposal replay must never invoke a model')
    model.complete=forbidden
    core=PrivacyMemorySystem(run/'memory.sqlite',embedder)
    try:
        job=core.db.execute('SELECT * FROM permission_jobs WHERE id=?',(JOB,)).fetchone()
        if not job:raise ValueError('Expected paused source job was not found')
        issuer=core.db.execute('SELECT issuer FROM permission_bootstrap WHERE compartment=?',(job['compartment'],)).fetchone()[0]
        if not issuer.startswith('fixture-ingestor:'):raise ValueError('Unexpected source adapter')
        episode_id=issuer[len('fixture-ingestor:'):]
        adapter=InferenceFixtureAdapter(core,{'episode_id':episode_id},[embedder.processor_id,model.processor_id],model,
                                        permission_version='13')
        source=json.loads(core.db.execute('SELECT payload FROM resources WHERE id=?',(job['source_id'],)).fetchone()[0])
        adapter.trusted_time({'timestamp':source['source']['observed_at']})
        result=adapter.updater.replay_rejected(JOB,proposal_digest=args.proposal_digest)
        after=core.db.execute('SELECT * FROM permission_jobs WHERE id=?',(JOB,)).fetchone()
        validation=json.loads(after['validation'])
        state=adapter.updater.access_state()
        assert result['status']=='complete' and model_calls==0
        assert state['activity']['nurse_med_review']['status']=='tentative'
        assert 'nurse_med_review_assign' not in state['assignment']
        assert state['assignment']['nurse_rooming_assign']['status']=='active'
        pending=state['restriction']['elena_linda_med_scope']
        assert pending['status']=='unresolved' and pending['issuer'] is None
        assert 'superseded_by' not in state['restriction']['elena_linda_scope']
        assert validation['authorization_deferrals'] and validation['model_called'] is False
        report={'source_event':source['source']['event_id'],'job_id':JOB,'status':after['status'],
            'model_calls':model_calls,'code_digest':code_digest,'reviewed_replay':validation['reviewed_replay'],
            'authorization_deferrals':validation['authorization_deferrals'],
            'pending_task':state['activity']['nurse_med_review'],
            'medication_grant_installed':False,'existing_rooming_grant_active':True,
            'pending_restriction':pending,'existing_family_restriction_retained':True}
        write_json(run/'reviewed-restriction-replay.json',report)
        RecoveryController(run)._record({'state':'running','stage':'permission '+JOB,
            'reason':'Explicitly reviewed saved proposal replayed with unsupported task and restriction authority claims deferred; no model call.',
            'proposal_digest':args.proposal_digest})
        print(json.dumps(report,indent=2),flush=True)
    finally:
        core.close();lock.close()


if __name__=='__main__':main()
