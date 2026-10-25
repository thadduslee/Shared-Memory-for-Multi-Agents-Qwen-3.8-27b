"""Read-only checks of the completed paired run; emits aggregate diagnostics."""
import argparse
import json
from pathlib import Path
import sqlite3

from agentic_memory.privacy.evolution import validate_proposal


def rows(path):
    return {r['checkpoint_id']:r for r in map(json.loads,path.read_text().splitlines())}


def audit(directory):
    db=sqlite3.connect(f'file:{directory}/memory.sqlite?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    db.execute('ATTACH DATABASE ? AS privacy',(f'file:{directory}/memory.privacy.sqlite?mode=ro',))
    resources={r['id']:dict(r) for r in db.execute('SELECT * FROM resources')}
    jobs={r['id']:dict(r) for r in db.execute('SELECT * FROM evolution_jobs')}
    violations=[]
    enrichments=0
    diagnostics=0
    for rid,resource in resources.items():
        if resource['state']!='active':continue
        payload=json.loads(resource['payload'])
        if resource['kind']=='enrichment':
            enrichments+=1
            base=resources[payload['base_memory_id']]
            if payload['text']!=json.loads(base['payload']).get('text',''):
                violations.append({'id':rid,'error':'generated_answer_text'})
            try:
                validate_proposal({'updates':[{'id':base['id'],**payload['semantic']}],'edges':[]},
                    {base['id']:{'kind':base['kind'],'payload':json.loads(base['payload'])}})
            except Exception:
                violations.append({'id':rid,'error':'ungrounded_metadata'})
        if resource['kind']=='evolution_diagnostic':
            diagnostics+=1
            if json.loads(resource['dependencies'])!=payload['input_ids']:
                violations.append({'id':rid,'error':'diagnostic_dependency_mismatch'})
            if len(json.loads(resource['policy_ceiling']))!=len(payload['input_ids']):
                violations.append({'id':rid,'error':'diagnostic_missing_ceiling'})
    lineage_errors=[]
    for row in db.execute('SELECT * FROM provenance'):
        if row['id'] not in jobs:lineage_errors.append(row['id']);continue
        if row['inputs']!=jobs[row['id']]['inputs']:lineage_errors.append(row['id'])
        for rid in json.loads(row['outputs']):
            # Edges additionally depend on their two endpoints, already in inputs.
            if set(json.loads(resources[rid]['dependencies']))!=set(json.loads(row['inputs'])):
                lineage_errors.append(rid)
    result={
        'memory_integrity':db.execute('PRAGMA main.integrity_check').fetchone()[0],
        'privacy_integrity':db.execute('PRAGMA privacy.integrity_check').fetchone()[0],
        'active_enrichments':enrichments,'active_diagnostics':diagnostics,
        'grounding_or_diagnostic_violations':violations,'lineage_errors':lineage_errors,
        'indexed_diagnostics':db.execute("SELECT COUNT(*) FROM representations v JOIN resources r ON v.id=r.id WHERE r.kind='evolution_diagnostic'").fetchone()[0],
        'deleted_vectors':db.execute("SELECT COUNT(*) FROM representations v JOIN resources r ON v.id=r.id WHERE r.state='deleted'").fetchone()[0],
        'dangling_access_bindings':db.execute('SELECT COUNT(*) FROM privacy.access_edges a LEFT JOIN resources r ON a.memory_id=r.id WHERE r.id IS NULL').fetchone()[0],
        'active_resources_without_primary_binding':db.execute("SELECT COUNT(*) FROM resources r WHERE r.state='active' AND NOT EXISTS (SELECT 1 FROM privacy.access_edges a WHERE a.memory_id=r.id AND a.policy_id=r.policy_id AND a.assignment_id='')").fetchone()[0],
        'nonempty_applied_jobs':db.execute("SELECT COUNT(*) FROM provenance WHERE outputs!='[]'").fetchone()[0],
        'nonempty_repaired_jobs':db.execute("SELECT COUNT(*) FROM provenance p JOIN evolution_jobs j ON p.id=j.id WHERE p.outputs!='[]' AND j.attempts>1 AND j.status='applied'").fetchone()[0],
        'evolution_attempt_seconds':db.execute('SELECT COALESCE(SUM(elapsed_s),0) FROM evolution_attempts').fetchone()[0],
        'total_boundary_seconds':sum(r['seconds'] for r in json.loads((directory/'boundary_results.json').read_text())),
    }
    db.close()
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result={arm:audit((args.run/arm).resolve()) for arm in ('baseline','evolved')}
    pred=[rows(args.run/arm/'official_predictions.jsonl') for arm in ('baseline','evolved')]
    scores=[rows(args.run/arm/'scores.jsonl') for arm in ('baseline','evolved')]
    disagreements=[]
    for cid in pred[0]:
        visible=lambda p:{k:p[cid]['output'].get(k) for k in ('action','answer','answer_structured')}
        if visible(pred[0])==visible(pred[1]):
            for key in ('utility_correct','privacy_leak','deletion_leak'):
                if scores[0][cid][key]!=scores[1][cid][key]:disagreements.append({'id':cid,'field':key})
    result['identical_output_verdict_disagreements']=disagreements
    result['queries_consuming_evolution']={
        'enrichments':sum(any(rid.startswith('enrichment:') for rid in p['input_manifest']['memory_ids']) for p in pred[1].values()),
        'edges':sum(any(rid.startswith('edge:') for rid in p['input_manifest']['memory_ids']) for p in pred[1].values())}
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
