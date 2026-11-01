"""Reproduce isolated host measurements; no LLM/benchmark-quality claim.

Run from the repository root with the test environment:
PYTHONPATH=. /home/thaddus/gatemem/.venv-nb/bin/python \
  analysis/durable-candidates-v14-20261030/measure.py
"""
from pathlib import Path
from collections import Counter
import json
import sqlite3

from security_tests.test_fact_candidates import DurableCandidates
from agentic_memory.privacy.fact_candidates import metrics

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent.parent
RUN=ROOT/'runs/gatemem-medical-raw-stream-v13-independent-facts-full-20261029T202536+0800'


def main():
    cases={
        'omitted_medication_plan':('test_original_omitted_plan_preserves_all_doses_units_and_negations',
            ['stop ibuprofen','650 mg q6h PRN','3,000 mg/day max','75 mcg daily','12.5 mg nightly','25 mg BID']),
        'late_grant_after_window':('test_unbound_durable_omitted_dose_reconsidered_after_window_and_restart',
            ['30 mg nightly','Stop labetalol','if dizzy']),
        'appointment_and_contact_updates':('test_replacement_cancellation_contact_context_and_similarity_history',
            ['Cancel Monday','Friday at 11:20','Arrive at 11 AM','628-555-0162; no detailed voicemail']),
        'multi_recipient_scope':('test_multi_recipient_decisions_explicit_unrelated_excluded_later_reuses_grants',
            ['30 mg nightly','unless fasting']),
        'restriction_replacement':('test_restriction_replacement_allocates_new_id_and_preserves_history',
            ['Pickup is at 4 PM']),
        'revocation':('test_revocation_remains_effective_for_old_candidates_and_derived_reads',[]),
        'source_deletion':('test_deletion_erasure_and_revocation_cannot_release_candidates',[]),
        'candidate_erasure':('test_historical_candidate_erasure_does_not_require_source_repetition',[]),
        'same_event_self_use':('test_existing_self_use_compiler_binds_beneficiary_in_same_event',['Pickup is at 4 PM']),
        'live_missed_grant_correction':('test_missed_supported_grant_is_resolved_in_triggering_event',['30 mg nightly']),
        'partial_extraction_correction':('test_partial_extraction_is_durable_and_only_omission_is_corrected',[]),
    }
    probes={}
    for name,(method,expected) in cases.items():
        case=DurableCandidates(method);case.setUp()
        try:
            getattr(case,method)()
            selected=case.selected()
            text=' '.join(r['text'] for r in selected)
            coverage=[term in text for term in expected]
            assert all(coverage),(name,coverage,text)
            ids=[r['id'] for r in selected]
            case.f.core.release(case.f.reader,ids,{'text':text})
            probes[name]={**metrics(case.f.updater),'retrieved_authorized_records':len(selected),
                'expected_evidence_items':len(expected),'retrieved_expected_items':sum(coverage),
                'final_release_validated':True,'final_answer_accuracy':None}
        finally:case.tearDown()
    db=sqlite3.connect('file:'+str(RUN/'memory.privacy.sqlite')+'?mode=ro',uri=True)
    try:
        skipped=[{'job_id':r[0],'compartment':r[1],'reason':json.loads(r[2]).get('reason')}
            for r in db.execute("SELECT id,compartment,validation FROM permission_jobs WHERE status='skipped'")]
        historical={'job_status_counts':dict(db.execute('SELECT status,count(*) FROM permission_jobs GROUP BY status')),
            'skipped_jobs':skipped,'held_compartments':len({r['compartment'] for r in skipped}),
            'skip_reason_counts':dict(Counter(r['reason'] for r in skipped))}
    finally:db.close()
    result={'mode':'isolated causal-prefix scripted semantic decisions',
        'limitations':['Not real-model extraction/grant accuracy.',
            'Retrieval coverage uses explicit test assertions, not benchmark gold labels.',
            'Stage times are local scripted test overhead, not model latency.',
            'Final answer accuracy is unmeasured; only evidence release is exercised.',
            'Deletion intentionally queues a protected reassessment; that hold is retained.'],
        'original_run':historical,'probes':probes,'real_model_services':{'8000':'connection refused','8001':'connection refused'},
        'new_full_benchmark_score':None}
    (HERE/'measurements.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'cases':len(probes),'retrieved_expected_items':sum(p['retrieved_expected_items'] for p in probes.values()),
        'expected_evidence_items':sum(p['expected_evidence_items'] for p in probes.values()),
        'new_full_benchmark_score':None},indent=2))


if __name__=='__main__':main()
