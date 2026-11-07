"""Requirement-by-requirement verification; absent evidence is a failure."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

REQUIREMENTS={
 'recipient_permissions':[
    'test_responsive_question_can_grant_worker_without_becoming_a_fact',
    'test_selected_service_intent_cannot_silently_omit_worker_grant',
    'test_service_context_does_not_confer_control_authority',
    'test_fact_rows_require_complete_attribution_instead_of_a_short_branch',
    'test_deny_overrides_allow_and_no_implicit_owner'],
 'separate_controls':['test_contact_preference_does_not_invalidate_read_permissions',
    'test_retained_reviews_never_bypass_subsequent_revocation'],
 'incremental_processing':['test_fingerprint_unchanged_and_does_not_copy_binding_history',
    'test_erasure_preserves_unrelated_readiness_and_blocks_restated_value',
    'test_v17_maintenance_drains_pending_pairs_with_original_authority',
    'test_v17_maintenance_is_source_driven_bounded_and_in_total_time'],
 'active_forgetting':['test_future_forgetting_waits_and_is_enforced_before_sweep',
    'test_conditional_forgetting_needs_fresh_authority_completion',
    'test_unrelated_actor_cannot_execute_pending_forgetting',
    'test_generated_artifact_inherits_every_input_including_uncited'],
 'complete_permitted_answers':['test_answer_keeps_selected_portal_instruction_and_removes_guessed_prose',
    'test_revocation_during_model_call_is_rechecked'],
 'observability_and_transport':['test_scalar_measurements_survive_forgetting_without_source_text',
    'test_v17_json_transport_uses_the_same_local_grammar',
    'test_provider_preference_preserves_required_parameters',
    'test_process_crash_recovers_uncommitted_pair'],
}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--junit',type=Path,required=True)
    parser.add_argument('--live',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--run',type=Path);args=parser.parse_args()
    cases=list(ET.parse(args.junit).getroot().iter('testcase'))
    grouped={}
    for case in cases:grouped.setdefault(case.get('name').split('[',1)[0],[]).append(case)
    passing={name for name,variants in grouped.items() if all(
        not any(c.find(key) is not None for key in ('failure','error','skipped')) for c in variants)}
    failed=[c.get('name') for c in cases if c.find('failure') is not None or c.find('error') is not None]
    requirements={key:{'passed':all(name in passing for name in names),
        'missing_or_failed':[name for name in names if name not in passing]} for key,names in REQUIREMENTS.items()}
    live=json.loads((args.live/'audit.json').read_text())
    baseline=json.loads((Path(__file__).parent/'host-baseline-60/measurements.json').read_text())
    candidate_path=Path(__file__).parent/'host-v17-60-final/measurements.json'
    candidate=json.loads(candidate_path.read_text())
    timing={'same_turn_count':baseline['source_turns']==candidate['source_turns']==60,
        'same_reader_count':baseline['readers']==candidate['readers']==5,
        'same_readable_count':baseline['readable_last_reader']==candidate['readable_last_reader']==60,
        'no_pending_reviews':baseline['pending_reviews']==candidate['pending_reviews']==0,
        'baseline_late_mean':baseline['last_25_mean'],'candidate_late_mean':candidate['last_25_mean'],
        'speedup':baseline['last_25_mean']/candidate['last_25_mean']}
    timing['passed']=all(timing[k] for k in ('same_turn_count','same_reader_count','same_readable_count','no_pending_reviews')) and timing['speedup']>2
    result={'requirements':requirements,'regressions':{'passing_cases':len(passing),'failures':failed},
        'live_probe':live,'host_only_performance':timing,'full_run':None,'matched_live_outcomes':None}
    if args.run:
        report=json.loads((args.run/'summary.json').read_text());coverage=report['coverage']
        result['full_run']={'complete':report['complete'],'coverage':coverage,'metrics':report['metrics'],
            'passed':report['complete'] and coverage['scored_checkpoints']==579 and
            coverage['ingested_source_turns']==4294 and coverage['completed_boundaries']==567}
        comparison=args.run/'comparison-prefix.json'
        if comparison.exists():
            compare=json.loads(comparison.read_text());base=compare['baseline'];candidate=compare['candidate']
            bm=base['metrics'];cm=candidate['metrics']
            checks={key:compare.get(key) is True for key in ('same_checkpoint_order','same_models','same_dataset')}
            checks['utility_improved']=cm['utility_accuracy']>bm['utility_accuracy']
            checks['over_refusal_reduced']=cm['over_refusal_rate']<bm['over_refusal_rate']
            for key in ('privacy_e2e_leakage_rate','deletion_e2e_leakage_rate'):
                checks[key+'_not_worse']=cm.get(key) is not None and bm.get(key) is not None and cm[key]<=bm[key]
            checks['source_latency_reduced']=candidate['source_latency_seconds']['mean']<base['source_latency_seconds']['mean']
            result['matched_live_outcomes']={'checks':checks,'passed':all(checks.values())}
    result['implementation_verified']=not failed and all(v['passed'] for v in requirements.values()) and live.get('passed') is True and timing['passed']
    # Completing a schedule alone cannot prove useful, fast permitted answers.
    result['full_objective_verified']=result['implementation_verified'] and bool(
        result['full_run'] and result['full_run']['passed'] and
        result['matched_live_outcomes'] and result['matched_live_outcomes']['passed'])
    args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
    if not result['implementation_verified']:raise SystemExit(1)
    if args.run and not result['full_objective_verified']:raise SystemExit(1)


if __name__=='__main__':main()
