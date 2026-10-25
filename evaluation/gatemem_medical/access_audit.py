"""Local causal replay: access, retrieval and context checks, never model quality.

Annotation-derived cohorts and scorer inputs are consulted only AFTER retrieval.
No paid calls, saved prediction reuse, or future source ingestion.
"""
import argparse
import collections
import csv
import hashlib
import json
import re
from pathlib import Path
import time

from agentic_memory.privacy import PrivacyMemorySystem, AccessDenied
from agentic_memory.privacy.processors import HashEmbedder
from .adapter import MedicalFixtureAdapter, load_data, schedule, visible_query
from .scoring import GateMemScorer


def run(data, output, cohort_path):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()): raise ValueError('Use a new, empty output directory')
    output.chmod(0o700)
    code_hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for folder in [Path('agentic_memory/privacy'),Path('evaluation/gatemem_medical')] for p in sorted(folder.iterdir()) if p.suffix=='.py' or p.name=='fixture_policy.json'}
    episodes, queries = load_data(data)
    by_episode = {e['episode_id']:e for e in episodes}
    by_query = {q['checkpoint_id']:q for q in queries}
    # Selection is retrospective reporting metadata only; never passed to adapter.
    cohort = {q['checkpoint_id']:q for q in map(json.loads,Path(cohort_path).read_text().splitlines())}
    core = PrivacyMemorySystem(output/'memory.sqlite',HashEmbedder())
    scorer = GateMemScorer(Path(data).parents[2])
    adapters = {}; rows=[]; started=time.monotonic()
    try:
        for event in schedule(episodes,queries):
            eid = event['episode_id']; episode = by_episode[eid]
            if eid not in adapters: adapters[eid] = MedicalFixtureAdapter(core,episode,[core.embedder.processor_id])
            adapter = adapters[eid]
            adapter.ingest_batch(event,episode['turns'][event['start']:event['end']])
            for cid in event['checkpoint_ids']:
                query = by_query[cid]; visible = visible_query(query)
                try:
                    ctx = adapter.query_context(visible)
                    packet = core.retrieve(ctx,visible['query_text'],k=12)
                    evidence = packet['evidence']
                    core.release(ctx,packet['input_ids'],{})
                    assignments=[]
                    for a in core.db.execute("SELECT id,body,state FROM assignments WHERE issuer=?",(adapter.issuer,)):
                        body=json.loads(a['body'])
                        if ctx.principal_id in body['rule'].get('principals',[]):
                            assignments.append({'id':a['id'],'state':a['state'],'scope':body['information_scope'],'provenance':body['provenance']})
                    status = 'access_blocker_fixed' if evidence else 'still_unresolved'
                    reason = 'Authorized task evidence retrieved; completeness and model correctness unproven.' if evidence else 'No authorized indexed evidence under implemented authority/field rules.'
                except AccessDenied:
                    evidence=[]; assignments=[]; status='blocked_other';reason='Unknown identity or inactive scoped session.'
                # The scoring boundary begins here. Empty answer avoids inventing
                # model predictions; only context checks and rule include coverage
                # on the actually retrieved evidence are reported.
                scope_grants=[];scope_evidence=[]
                if cid in cohort:
                    label=cohort[cid]['requested_information_scope_analyst_label']
                    required=[]
                    if 'sharing/revocation' in label: required.append({'sharing'})
                    elif 'contact routing' in label: required.append({'contact','restricted_contact'})
                    if 'medication/' in label: required.append({'medications'})
                    if 'appointments' in label or 'arrival' in label: required.append({'appointments'})
                    if 'transportation assistance' in label: required.append({'transport'})
                    if 'return/call precautions' in label: required.append({'care'})
                    if 'clinical/laboratory summary' in label: required.append({'clinical'})
                    if 'monitoring tests' in label or 'repeat-lab scheduling' in label: required.append({'lab_schedule','appointments'})
                    if 'procedure preparation' in label or 'care/follow-up' in label: required.append({'care','medications','appointments'})
                    permitted={a['scope'] for a in assignments if a['state']=='active' and
                        core._assignment_live(a['id'],core._session(ctx,'read'),'read')}
                    retrieved={r.get('source',{}).get('information_scope') for r in evidence}
                    # A deduplicated routing-status view can carry both contact
                    # and sharing status. Reporting this fact does not issue grants.
                    if any(r.get('source',{}).get('information_scope')=='contact' and
                           re.search(r'\b(access|revok\w*|sharing)\b',r['text'],re.I) for r in evidence):
                        retrieved.add('sharing')
                    scope_grants=[bool(group & permitted) for group in required]
                    scope_evidence=[bool(group & retrieved) for group in required]
                    if not required:
                        status='still_unresolved';reason='Requested scope not mapped for retrospective disposition; manual review required.'
                    elif not all(scope_grants):
                        status='still_unresolved';reason='Requested task scope lacks active authority; unrelated retrieved evidence is not a fix.'
                    elif not all(scope_evidence):
                        status='blocked_other';reason='Assignment covers the task but retrieval has no selected field evidence for at least one requested scope.'
                    else:
                        status='access_blocker_fixed';reason='Requested task scope authorized and field evidence retrieved; factual completeness and model correctness unproven.'
                memory=[{'memory_id':r['id'],'text':r['text'],'information_scope':r.get('source',{}).get('information_scope')} for r in evidence]
                pred={'action':'no_memory','answer':'','answer_structured':{},'retrieved_memory':memory,'used_record_ids':[]}
                score=scorer.score_one(checkpoint=query,pred_output=pred)
                probe=dict(pred,action='answer',answer=' '.join(r['text'] for r in evidence))
                completeness=scorer.score_one(checkpoint=query,pred_output=probe)
                rows.append({'checkpoint_id':cid,'episode_id':eid,'as_of_turn_id':query['as_of_turn_id'],
                    'principal_id':visible['asker']['principal_id'],'category':query['query_type'],'cohort_118':cid in cohort,
                    'disposition':status,'reason':reason,'assignments':assignments,'evidence':memory,
                    'requested_scope_grant_coverage':scope_grants,'requested_scope_retrieval_coverage':scope_evidence,
                    'context_privacy_leak_auxiliary':score.get('privacy_context_leak'),
                    'context_deletion_leak_auxiliary':score.get('deletion_context_leak'),
                    'retrieved_include_hits_auxiliary':completeness.get('aux',{}),
                    'answer_quality':'not_measured'})
            if event['boundary'] % 50 == 0: print(f"boundary {event['boundary']}/567; {len(rows)}/579 retrievals",flush=True)
        cohort_rows=[r for r in rows if r['cohort_118']]
        assert len(cohort_rows)==118 and len(rows)==579
        summary={'mode':'local-policy-and-retrieval-audit','embedding':'deterministic HashEmbedder test double',
            'evolution':False,'model_calls':0,'queries':len(rows),'boundaries':567,'source_turns':sum(len(e['turns']) for e in episodes),
            'cohort_118_dispositions':dict(collections.Counter(r['disposition'] for r in cohort_rows)),
            'cohort_118_nonempty_retrieval':sum(bool(r['evidence']) for r in cohort_rows),
            'categories':{},'answer_quality':'not_measured','real_model_utility_accuracy':None,
            'seconds':time.monotonic()-started,
            'limitations':'Access coverage is not answer correctness. Auxiliary literal context checks are neither an independent privacy judgment nor semantic retrieval validation.',
            'input_hashes':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(data)/'episodes.jsonl',Path(data)/'checkpoints.jsonl',Path(cohort_path)]},
            'code_hashes':code_hashes}
        for category in ('utility','privacy','safety'):
            subset=[r for r in rows if r['category']==category]
            summary['categories'][category]={'queries':len(subset),'nonempty_retrieval':sum(bool(r['evidence']) for r in subset),
                'context_privacy_flags_auxiliary':sum(r['context_privacy_leak_auxiliary'] is True for r in subset),
                'context_deletion_flags_auxiliary':sum(r['context_deletion_leak_auxiliary'] is True for r in subset)}
        (output/'retrievals.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        (output/'cohort_118.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in cohort_rows))
        with (output/'cohort_118.csv').open('w') as handle:
            writer=csv.DictWriter(handle,fieldnames=['checkpoint_id','principal_id','disposition','reason','evidence_count','scopes'])
            writer.writeheader()
            for r in cohort_rows: writer.writerow({**{k:r[k] for k in ['checkpoint_id','principal_id','disposition','reason']},'evidence_count':len(r['evidence']),'scopes':','.join(sorted({a['scope'] for a in r['assignments'] if a['state']=='active'}))})
        (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
        print(json.dumps(summary,indent=2),flush=True)
    finally: core.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,default=Path('/home/thaddus/GateMem/bench/data/medical'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--cohort',type=Path,default=Path('analysis/staff-permission-audit/queries.jsonl'))
    a=p.parse_args();run(a.data,a.output,a.cohort)

if __name__=='__main__':main()
