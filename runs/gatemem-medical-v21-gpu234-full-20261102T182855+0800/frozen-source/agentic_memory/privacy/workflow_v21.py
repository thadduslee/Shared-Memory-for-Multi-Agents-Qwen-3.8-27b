"""V19 call scheduling with independent semantic stages and AIM interfaces.

No aggregate source/answer deadline is installed. Provider failures propagate to
the durable retry controller; elapsed time alone never advances a source event.
"""
import json
import time
from .workflow_v20 import INSTRUCTION as SEMANTIC_INSTRUCTION

PROMPT_VERSION = 'permission-inference-v21-aim-v19-scheduling-v1'
INSTRUCTION = SEMANTIC_INSTRUCTION + '''
V21 retains the original authenticated event as authority. A repair must retain
recognized service acceptance and correct its recipient/reference; it must not
discard that intent merely because a proposed grant ID was invalid. Assess
controls, attribution and exact recipient scope independently in the same call.
An ordinary factual update never revokes another valid permission. A possible
adverse control remains protected until validated; no clock clears a hold.
'''


def enabled(value):
    return str(getattr(value, 'version', value)).split('-', 1)[0] == '21'


def validate_repair_continuity(previous, replacement, error):
    """An invalid recipient reference must not silently erase recognized work.

    This creates no permission. An unresolved interpretation remains pending for
    an independent host reassessment of the original event, never an allow.
    """
    effect = getattr(error, 'details', {}).get('required_effect')
    if not effect:
        return
    from .permissions import PermissionProposalRejected
    revised = {row['index']: row for row in replacement['control_review']}
    for row in previous['control_review']:
        intents = set(row.get('intents', [])) & {'service', 'grant', 'deny', 'revoke', 'erase'}
        if intents and not intents & set(revised.get(row['index'], {}).get('intents', [])):
            raise PermissionProposalRejected('Repair discarded a recognized control intent; original event needs reassessment',
                {'stage': 'controls', 'required_effect': effect, 'recipient': row.get('recipient')})


def service_pending(updater, *, max_batches=1, controls=True, source_slot=False):
    """Explicit host work bounded by jobs/calls, never elapsed seconds.

    Recovery uses original receipts and durable attempt counts. It cannot apply
    a historical delta over a later revocation. Never called by answer_request.
    """
    if type(max_batches) is not int or not 0 <= max_batches <= 4:
        raise ValueError('Use zero to four explicit host batches')
    from .workflow_v20 import pending, record
    c = updater.core
    c._session(updater.ctx, 'manage_policy', updater.compartment)
    results = []
    for _ in range(max_batches):
        jobs = [j for j in pending(updater) if j['status'] == 'pending'
                and (controls or j['kind'] != 'control')]
        started = time.monotonic()
        previous = getattr(updater.processor, '_workflow_phase', None)
        updater.processor._workflow_phase = 'host'
        try:
            result = recover_one(updater, jobs[0]) if jobs else updater.flush_bindings(max_batches=1)
            results.append(result)
        finally:
            record(updater, 'host_recovery' if jobs else 'host_maintenance',
                   time.monotonic() - started, hard_deadline=False, charged_to_source=source_slot)
            if previous is None:
                del updater.processor._workflow_phase
            else:
                updater.processor._workflow_phase = previous
        if result.get('status') in {'complete', 'control_hold', 'repair_exhausted'} and not jobs:
            break
    return {'status': results[-1]['status'] if results else 'idle',
            'calls': sum(r.get('calls', r.get('batches', 0)) for r in results),
            'changes': [change for r in results for change in r.get('changes', [])]}


def recover_one(updater, item):
    """Preserve the semantic recovery allowance across provider failures."""
    from .workflow_v20 import recover_one as reassess
    from .processors import ProcessorFailure, ModelContractError
    from .system import encoded
    try:
        return reassess(updater, item)
    except ProcessorFailure as exc:
        if not isinstance(exc, ModelContractError):
            # Admission is durable, but a missing provider response is not a
            # rejected interpretation. Its immutable child attempt remains in
            # the ledger and the same original event remains pending.
            with updater.core.transaction():
                updater.core.db.execute('UPDATE host_recovery SET attempts=?,status=\'pending\',body=? WHERE id=?',
                    (item['attempts'], encoded({'error_type': type(exc).__name__,
                                              'semantic_response': False}), item['id']))
        raise


def answer_request(core, adapter, visible, processor, *, top_k=None):
    from .answering import _answer_request
    from .diagnostics_v21 import diagnose
    from .workflow_v20 import record
    started = time.monotonic()
    # Deliberately use V19's answer path, without V20's deadline wrapper or
    # automatic query maintenance. Every input is still checked at release.
    previous_phase = getattr(processor, '_workflow_phase', None)
    processor._workflow_phase = 'answer'
    try:
        result = _answer_request(core, adapter, visible, processor, top_k=top_k)
    finally:
        if previous_phase is None:
            del processor._workflow_phase
        else:
            processor._workflow_phase = previous_phase
    result['input_manifest']['diagnosis_v21'] = diagnose(adapter.updater, result)
    elapsed = time.monotonic() - started
    result['input_manifest']['answer_seconds'] = elapsed
    record(adapter.updater, 'answer_request', elapsed, hard_deadline=False)
    return result


def summary(core):
    from .workflow_v20 import timing_summary
    result = timing_summary(core)
    values = sorted(r[0] for r in core.db.execute(
        "SELECT seconds FROM pipeline_timings WHERE stage='source_total'"))
    result['source_seconds'] = {
        'count': len(values), 'mean': sum(values) / len(values) if values else 0.,
        'p50': values[len(values)//2] if values else 0.,
        'p95': values[int(.95*(len(values)-1))] if values else 0.,
        'max': max(values, default=0.), 'over_30_seconds': sum(v > 30 for v in values)}
    result['hard_turn_deadline_seconds'] = None
    # Model durations are nested in these wall stages. Host recovery performed
    # inside a source slot is already included in source_total, so count it once.
    extra = sum(row['seconds'] for row in core.db.execute('SELECT stage,seconds,body FROM workflow_timings')
                if row['stage'] != 'model_call' and not json.loads(row['body']).get('charged_to_source'))
    enrichment = sum(r[0] for r in core.db.execute("SELECT seconds FROM pipeline_timings WHERE stage='evolution_total'"))
    result['total_work_seconds'] = sum(values) + enrichment + extra
    result['metric_semantics'] = {'total_work_seconds':
        'source including its maintenance + external host work + answer including diagnostics + judge + enrichment; nested model time not added twice'}
    result['scheduling'] = 'v19-two-semantic-responses-including-maintenance'
    result['deduplication'] = 'bounded-same-authority-provenance-only'
    return result
