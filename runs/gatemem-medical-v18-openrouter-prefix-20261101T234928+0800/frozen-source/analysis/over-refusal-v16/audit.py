"""Audit the evaluated implementation and empty-start/input invariants."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from agentic_memory.privacy.system import digest
from evaluation.gatemem_medical.runner import ROOT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    args=parser.parse_args()
    manifest=json.loads((args.run/'manifest.json').read_text())
    current=digest({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in (ROOT/'agentic_memory/privacy',ROOT/'evaluation/gatemem_medical')
        for p in sorted(folder.iterdir()) if p.is_file() and
        (p.suffix=='.py' or p.name=='fixture_policy.json')})
    trace=[json.loads(l) for l in (args.run/'model-trace.jsonl').read_text().splitlines()]
    semantic=[r for r in trace if r['payload'].get('stage') in
              {'combined_incremental','incremental_repair','incremental_binding_repair'}]
    first=semantic[0]['payload']
    hidden={'expected_action','judge_spec','query_type','attack_type','leak_targets',
            'gold_answer_structured','gold_refusal_category','policy_snapshot','entities',
            'role','permissions','access_level','subject_id'}
    def hidden_keys(value):
        if isinstance(value,dict):
            return bool(set(value)&hidden) or any(hidden_keys(v) for v in value.values())
        if isinstance(value,list):return any(hidden_keys(v) for v in value)
        return False
    private=sqlite3.connect(f'file:{args.run}/memory.privacy.sqlite?mode=ro',uri=True)
    budgets=[]
    for job, in private.execute('SELECT id FROM permission_jobs'):
        attempts=[json.loads(v or '{}') for v, in private.execute(
            'SELECT validation FROM permission_attempts WHERE job_id=?',(job,))]
        budgets.append(sum(v.get('semantic_response',True) for v in attempts))
    bootstrap=[json.loads(v) for v, in private.execute('SELECT body FROM permission_bootstrap')]
    audit={'evaluated_code_matches_current':manifest['code_digest']==current,
        'initial_access_empty':all(not v for v in first['permission_state'].values()),
        'bootstrap_registry_empty':all(not b[k] for b in bootstrap for k in
            ('participants','resources','authorities','initial_permissions','self_use_actions')),
        'no_initial_requester_grants':manifest['initial_requester_grants']==0,
        'no_hidden_annotations_in_model_inputs':all(not hidden_keys(r['payload']) for r in trace),
        'opaque_source_participants':all(s['source']['participant'].startswith('p') and
            s['source']['participant'][1:].isdigit() for r in semantic for s in r['payload'].get('sources',[])),
        'at_most_two_semantic_calls_per_source':max(budgets,default=0)<=2,
        'semantic_source_calls':len(semantic),'max_semantic_calls':max(budgets,default=0),
        'historical_revisions_unchanged':json.loads((args.run/'implementation_revisions.json').read_text())==[]}
    private.close()
    checks=[k for k,v in audit.items() if type(v) is bool]
    audit['passed']=all(audit[k] for k in checks)
    (args.run/'implementation-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
    print(json.dumps(audit,indent=2))
    if not audit['passed']:raise SystemExit(1)


if __name__=='__main__':main()
