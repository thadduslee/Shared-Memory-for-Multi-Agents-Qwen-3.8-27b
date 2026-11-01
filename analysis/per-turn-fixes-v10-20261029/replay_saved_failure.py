"""Read-only t005 replay and serialized response-size check. No model calls."""
from copy import deepcopy
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agentic_memory.privacy.permissions import (
    PermissionUpdater, PermissionBootstrap, PermissionProposalRejected, compact_input, proposal_schema,
)
from agentic_memory.privacy.permission_context import context_view


def main():
    run=ROOT/'runs/gatemem-medical-raw-stream-v9-full-20261029T011945+0800'
    db=sqlite3.connect('file:'+str(run/'memory.sqlite')+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    db.execute('ATTACH DATABASE ? AS privacy',('file:'+str(run/'memory.privacy.sqlite')+'?mode=ro',))
    db.execute('PRAGMA query_only=ON')
    try:
        job=db.execute("SELECT * FROM permission_jobs WHERE status='rejected'").fetchone()
        boot=db.execute('SELECT body FROM permission_bootstrap WHERE compartment=?',(job['compartment'],)).fetchone()
        updater=object.__new__(PermissionUpdater)
        updater.core=SimpleNamespace(db=db,snapshot_read=lambda key,load:load())
        updater.compartment=job['compartment'];updater.bootstrap=PermissionBootstrap(**json.loads(boot[0]))
        updater.version='9';updater.processor=SimpleNamespace(require_permission_coverage=True)
        sources={sid:json.loads(db.execute('SELECT payload FROM resources WHERE id=?',(sid,)).fetchone()[0])
                 for sid in json.loads(job['inputs'])}
        state=updater._state();selected,records,context=context_view(sources,state,job['source_id'])
        original=json.loads(job['proposal']);restriction=next(o for o in original['operations'] if o['op']=='record_restriction')
        rejected=accepted=0
        def validate(proposal):
            return updater._validate(deepcopy(proposal),state,sources,job['revision'],
                new_source_id=job['source_id'],available_source_ids=set(selected),available_records=records)
        for ops in permutations(original['operations']):
            proposal={**original,'operations':list(ops)}
            try:validate(proposal)
            except PermissionProposalRejected as exc:
                assert exc.details['missing_restriction_ids']==[restriction['id']]
                rejected+=1
            else:raise AssertionError('Unassessed binding accepted')
            corrected=deepcopy(proposal)
            for op in corrected['operations']:
                if op['op']=='bind_information':
                    assignment=state['assignment'][op['assignment_id']]
                    assert assignment['participant'] not in restriction['participants']
                    op['restriction_decisions']=[{'restriction_id':restriction['id'],'applies':False,
                        'explanation':'This restriction names another recipient.',
                        'evidence':restriction['evidence']['participants']}]
            validate(corrected);accepted+=1
        bootstrap=updater._source_bootstrap(sources)
        payload={'revision':job['revision'],'sources':[{'id':sid,**body} for sid,body in selected.items()],
            'bootstrap':json.loads(json.dumps(bootstrap.__dict__)),'new_source_id':job['source_id'],
            'permission_state':records,'contract_notes':{},'coverage_required':updater._coverage_principals(sources,state),
            'context':context,'correction':{'previous_proposal':original,'validation':{}}}
        wire,schema,expand=compact_input(payload,proposal_schema(bootstrap=bootstrap,source_ids=selected,require_coverage=True))
        small=wire['correction']['previous_proposal']
        # Compare equal source aliases so ID compression does not inflate the delta saving.
        canonical=expand(small)
        aliases={s['id']:f's{i}' for i,s in enumerate(payload['sources'],1)}
        def alias(value):
            if isinstance(value,dict):return {k:alias(v) for k,v in value.items()}
            if isinstance(value,list):return [alias(v) for v in value]
            return aliases.get(value,value) if isinstance(value,str) else value
        canonical=alias(canonical)
        def size(value):return len(json.dumps(value,separators=(',',':')).encode())
        old=size(canonical);new=size(small)
        report={'source':'t005','unassessed_orders_rejected':rejected,'explicitly_assessed_orders_accepted':accepted,
            'same_source_alias_response_bytes':{'canonical':old,'compact':new,'reduction_percent':round(100*(1-new/old),1)},
            'live_model_calls':0,'original_databases_modified':False,
            'note':'The supplied applicability correction is justified by disjoint recipients. '
                   'The runtime never invents this semantic decision.'}
        print(json.dumps(report,indent=2))
    finally:db.close()


if __name__=='__main__':main()
