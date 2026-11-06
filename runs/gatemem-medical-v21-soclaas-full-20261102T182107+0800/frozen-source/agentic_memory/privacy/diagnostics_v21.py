"""Protected stage diagnosis. Never returns hidden facts to a requester.

No gold answer or future event is an input. A queue/hold is reported as evidence
of possible blockers, not an unsupported claim that it caused this exact query.
"""
import json


def diagnose(updater, result=None):
    c = updater.core
    c._session(updater.ctx, 'audit', updater.compartment)
    from .control_holds import rows
    from .workflow_features import subjects
    state = updater.inspect()
    candidates = updater.fact_candidates()
    holds = rows(c, updater.compartment)
    rejected = []
    intents_lost = []
    for job in c.db.execute('SELECT id,status,source_id,validation FROM permission_jobs WHERE compartment=? ORDER BY rowid DESC LIMIT 32',
                            (updater.compartment,)):
        v = json.loads(job['validation'] or '{}')
        if job['status'] in {'quarantined', 'rejected', 'processor_error'}:
            rejected.append({'job_id': job['id'], 'source_id': job['source_id'],
                             'status': job['status'], 'reason': v.get('reason')})
        attempts = list(c.db.execute('SELECT proposal FROM permission_attempts WHERE job_id=? ORDER BY attempt DESC LIMIT 2', (job['id'],)))[::-1]
        plans = [json.loads(a[0]) for a in attempts if a[0]]
        if any('service' in r.get('intents', []) for p in plans
               for r in p.get('control_review', [])) and plans and not plans[-1].get('permission_changes'):
            intents_lost.append(job['id'])
    processing = updater.processing_metrics() if hasattr(updater, 'processing_metrics') else None
    if processing is None:
        from .fact_candidates import metrics
        processing = metrics(updater)
    evidence = (result or {}).get('input_manifest', {}).get('memory_ids', [])
    statuses = (result or {}).get('input_manifest', {}).get('answer_decision', [])
    return {
        'source_observed': processing.get('source_events', 0),
        'attribution_unknown': sum(not subjects(p) for p in candidates),
        'control_holds': [{'id': h['id'], 'subject': h['subject'], 'job_id': h['job_id']} for h in holds],
        'rejected_updates': rejected, 'recognized_service_without_final_operation': intents_lost,
        'active_grants': sum(a['status'] == 'active' for a in state['assignment'].values()),
        'pending_binding_pairs': processing.get('pending_binding_reviews', 0),
        'scope_exclusions': processing.get('excluded_binding_decisions', 0),
        'retrieved_input_count': len(evidence), 'answer_parts': statuses,
        'assessment': 'observed-stage-state; query causation requires matching exact subject and fact',
        'update_audit_sample_limit': 32,
        'strict_score_modified': False,
    }


def summarize_predictions(predictions):
    """Offline diagnostic tally, separate from all official scoring."""
    from collections import Counter
    counts = Counter()
    for prediction in predictions:
        manifest = prediction.get('input_manifest', {})
        d = manifest.get('diagnosis_v21', {})
        for name in ('attribution_unknown', 'pending_binding_pairs', 'scope_exclusions'):
            counts[name + '_observations'] += d.get(name, 0)
        counts['checkpoints_with_holds'] += bool(d.get('control_holds'))
        counts['checkpoints_without_evidence'] += not manifest.get('memory_ids')
    return dict(counts)
