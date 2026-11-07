"""Batch 4: offline scoped-control regression tests, on the V19 ACL path."""
from copy import deepcopy
import json

import pytest

from agentic_memory.privacy import PrivacyMemorySystem, AccessDenied
from agentic_memory.privacy.permissions import PermissionSourceEvent, PermissionUpdater, PermissionRepairExhausted
from agentic_memory.privacy.processors import HashEmbedder, ModelContractError
from agentic_memory.privacy import scoped_holds_v19 as holds
from agentic_memory.privacy.answering import PermissionQueryService
from security_tests.test_recipient_revocation_v19 import h, texts, grants, revoke_policy


def pending(h, text):
    h.seq += 1
    return h.updater.ingest(PermissionSourceEvent(str(h.seq), 'owner', 'connector', text))['job_id']


def exhaust(h, text):
    job = pending(h, text)
    original = h.model.complete
    def broken(*args, **kwargs):
        raise ModelContractError('Invalid identifier enum')
    h.model.complete = broken
    try:
        with pytest.raises(PermissionRepairExhausted):
            h.updater.update(job, max_attempts=2)
        h.updater.skip_exhausted(job, max_attempts=2)
    finally:
        h.model.complete = original
    return job


def reopen(h):
    h.core.close()
    h.core = PrivacyMemorySystem(h.path, HashEmbedder(), clock=lambda: '2026-10-31T00:00:00+00:00')
    h.model.core = h.core
    h.host = h.core.issue_session('host', 'ingest', actions=['manage_policy','ingest','read','search','derive','audit','write','delete','link'],
        compartments=['work'], processing_routes=['test:hash','test:semantic','test:answer'])
    h.core.register_processor(h.host, h.model)
    h.updater = PermissionUpdater(h.core, h.host, 'work', h.bootstrap, h.model, version='19')


def assessment(h, jid):
    return next(r for r in h.updater.control_assessments() if r['job_id'] == jid)


def test_failed_ordinary_question_preserves_existing_approved_access_and_audit(h):
    before = deepcopy(grants(h))
    jid = exhaust(h, 'And I should keep the prenatal vitamin and iron?')
    row = assessment(h, jid)
    assert row['state'] == 'VALIDATED_NON_CONTROL'
    assert row['original']['boundary']['unresolved']
    assert len(texts(h, 'linda')) == len(texts(h, 'jules')) == 2
    assert grants(h) == before
    job = h.core.db.execute('SELECT status,attempts FROM permission_jobs WHERE id=?', (jid,)).fetchone()
    assert tuple(job) == ('quarantined', 2)
    assert h.core.db.execute('SELECT COUNT(*) FROM permission_attempts WHERE job_id=?', (jid,)).fetchone()[0] == 2
    assert not h.core.db.execute('SELECT 1 FROM permission_control_holds WHERE job_id=?', (jid,)).fetchone()


def test_factual_interpretation_failure_does_not_invalidate_other_binding(h):
    before = texts(h, 'linda')
    h.classify = lambda p, s: 'instruction'
    h.binding = lambda p, r: {**r, 'in_scope': False, 'subject_supported': False,
        'blocked_by': [], 'reason': 'This new fact has unresolved subject and scope.'}
    job = h.turn('A new uninterpreted statement.')
    assert texts(h, 'linda') == before
    assert assessment(h, job['job_id'])['state'] == 'CONTROL_COMMITTED'


def test_validated_no_change_survives_publication_failure_and_resumes_without_new_call(h):
    before = texts(h,'linda')
    jid = pending(h,'A new ordinary informational statement.')
    h.classify = lambda p,s:'instruction'
    publish = h.updater._publish
    def interrupted(*args,**kwargs):
        publish(*args,**kwargs)
        raise RuntimeError('Interrupted publication')
    h.updater._publish=interrupted
    try:
        with pytest.raises(RuntimeError,match='Interrupted publication'):
            h.updater.update(jid,max_attempts=2)
    finally:
        h.updater._publish=publish
    assert assessment(h,jid)['state']=='CONTROL_VALIDATED_NO_CHANGE'
    assert texts(h,'linda')==before
    calls=len(h.model.calls)
    reopen(h)
    assert assessment(h,jid)['state']=='CONTROL_VALIDATED_NO_CHANGE'
    h.updater.update(jid,max_attempts=2)
    assert len(h.model.calls)==calls
    assert assessment(h,jid)['state']=='CONTROL_COMMITTED'


def test_successful_no_change_finalizes_and_releases_partial_adverse_signal(h):
    # Superseded Batch 4 policy: a partial local signal used to hold forever.
    # A complete successful control review finalizes the event instead.
    job = h.turn('Stop sharing with Someone.')
    row = assessment(h, job['job_id'])
    assert row['state'] == 'CONTROL_COMMITTED' and row['finalization']['uncertainty_released']
    assert [p['kind'] for p in row['finalization']['unresolved']] == ['local_boundary']
    assert not row['finalization']['verified_controls']
    assert len(texts(h, 'linda')) == 2
    h.turn('Continue processing.')
    assert len(texts(h, 'linda')) == 2


@pytest.mark.parametrize('operation', ['process','evolve'])
def test_processing_and_evolution_recheck_hold_after_model_returns(h, operation):
    reader = h.reader('linda')
    packet = h.core.retrieve(reader, 'appointment', k=100)
    rid = next(r['id'] for r in packet['evidence'] if 'appointment' in r['text'])
    original = h.model.complete
    calls = []
    def slow(system, payload, *, schema=None):
        calls.append(payload)
        h.updater.reassess_control_event(pending(h, 'Stop sharing with Linda.'))
        return {'updates': [], 'edges': []}
    h.model.complete = slow
    try:
        with pytest.raises(AccessDenied):
            if operation == 'process':
                h.core.process(reader, h.model, [rid], 'Derive a recap.', {})
            else:
                h.core.evolve(reader, h.model, [rid], job_id='batch4-evolution', k=0)
        assert len(calls) == 1
        assert not h.core._tickets
        assert not h.core.db.execute("SELECT 1 FROM provenance WHERE id='batch4-evolution'").fetchone()
    finally:
        h.model.complete = original


def test_pending_question_needs_explicit_original_event_reassessment(h):
    jid = pending(h, 'And I should keep the prenatal vitamin and iron?')
    assert not texts(h, 'linda')
    h.core.clock = lambda: '2036-01-01T00:00:00+00:00'
    assert not texts(h, 'linda')
    calls = len(h.model.calls)
    h.updater.reassess_control_event(jid)
    assert len(texts(h, 'linda')) == 2 and len(h.model.calls) == calls
    assert h.core.db.execute('SELECT status FROM permission_jobs WHERE id=?', (jid,)).fetchone()[0] == 'pending'


def test_explicit_reassessment_resolves_legacy_hold_without_changing_failure_history(h):
    jid = exhaust(h, 'And I should keep the prenatal vitamin and iron?')
    sid = assessment(h,jid)['source_id']
    h.core.db.execute('DELETE FROM control_assessments_v19 WHERE job_id=?',(jid,))
    legacy = {'source_id':sid,'actor':'owner','subject':'owner','reason':'Original identifier enum failure'}
    h.core.db.execute('INSERT INTO permission_control_holds VALUES (?,?,?,?,?,?)',
        ('legacy-hold','work',jid,'owner',json.dumps(legacy),'active'))
    attempts = list(h.core.db.execute('SELECT * FROM permission_attempts WHERE job_id=?',(jid,)))
    assert not texts(h,'linda')
    h.updater.reassess_control_event(jid)
    row = h.core.db.execute("SELECT body,state FROM permission_control_holds WHERE id='legacy-hold'").fetchone()
    assert row['state']=='resolved'
    body = json.loads(row['body'])
    assert body['reason']==legacy['reason']
    assert body['resolution']['kind']=='original_event_non_control_reassessment'
    assert list(h.core.db.execute('SELECT * FROM permission_attempts WHERE job_id=?',(jid,)))==attempts
    assert len(texts(h,'linda'))==2


@pytest.mark.parametrize('question', [
    'Stop sharing with Linda.', 'Could you stop sharing with Linda?',
    'Can you stop sharing with Linda?', 'Would you stop sharing with Linda?',
])
def test_real_control_including_questions_blocks_approved_linda_only(h, question):
    jid = exhaust(h, question)
    row = assessment(h, jid)
    # The complete local boundary is a validated control, not uncertainty.
    assert row['state'] == 'SUPPORTED_CHANGES'
    assert row['boundary']['recipients'] == ['linda']
    [control] = row['finalization']['verified_controls']
    assert (control['kind'], control['participants'], control['information_scope']) == ('restriction', ['linda'], '*')
    assert not texts(h, 'linda')
    assert len(texts(h, 'jules')) == 2


def test_field_limited_control_preserves_proven_unaffected_fields_and_recipients(h):
    jid = exhaust(h, 'Could you stop sharing my scheduling information with Linda?')
    row = assessment(h, jid)
    assert row['boundary']['fields'] == ['scheduling']
    assert row['finalization']['verified_controls'][0]['field'] == 'scheduling'
    # Both extra calls failed, so the ultrasound fact's applicability was never
    # established; the agreed policy does not block it speculatively (see
    # test_privacy_fallback_v19 for the decided case). Billing stays proven.
    assert row['finalization']['outcome'] == 'SUPPORTED_CHANGES'
    assert texts(h, 'linda') == ['The ultrasound appointment is Tuesday at 9:20 AM.', 'The billing invoice is 40 dollars.']
    assert len(texts(h, 'jules')) == 2
    reader = h.reader('linda')
    packet = h.core.retrieve(reader, 'billing', k=100)
    assert h.core.release(reader, packet['input_ids'], {'answer': 'Invoice is 40 dollars.'})


@pytest.mark.parametrize('instruction,reader,answerable', [
    ('Stop sharing with Linda.','jules',True),
    ('Stop sharing with Linda.','linda',False),
    ('Stop sharing my scheduling information with Linda.','linda',True),
    ('Stop sharing with Someone.','jules',True),
    ('And I should keep the prenatal vitamin and iron?','linda',True),
])
def test_answer_service_availability_uses_current_recipient_and_readable_bindings(h,instruction,reader,answerable):
    exhaust(h,instruction)
    service=PermissionQueryService(h.updater)
    visible={'asker':{'principal_id':reader},'query_text':'Current information','as_of_turn_id':'latest'}
    status=service.answer_authorization(visible)
    # Finalized events never report uncertainty; validated controls deny instead.
    assert not status.get('permission_unresolved',False)
    if not answerable:
        assert not any(h.core.allowed(h.reader(reader),r['id'],'read') for r in h.core.retrieve(h.host,'information',k=100)['evidence'])
    else:
        assert status['has_authorized_assignment']
        # Availability test: the source-inference double does not implement an
        # answer/verifier contract. Use an explicit literal-evidence answer
        # double for this broad "Current information" request.
        from security_tests.test_answer_validation_v19 import EvidenceModel
        model=EvidenceModel();h.core.register_processor(h.host,model)
        result=service.answer(reader,visible['query_text'],model,as_of='latest')['output']
        assert result['action']=='answer' and result['used_record_ids']


def test_absence_review_reports_validated_restriction_not_uncertainty(h):
    exhaust(h,'Stop sharing with Linda.')
    service=PermissionQueryService(h.updater)
    for reader,restricted in [('linda',True),('jules',False)]:
        def classify(payload):
            assert not any(s['permission_unresolved'] for s in payload['approved_scopes'])
            assert [(r['information_scope'],r['pending_hold']) for r in payload['restrictions']]==([('*',False)] if restricted else [])
            return {'decisions':{'0':False}}
        h.model.handler=classify
        assert service.review_absence({'asker':{'principal_id':reader}},['An absent field'],h.model)==[False]


def test_apostrophe_normalization_does_not_widen_validated_revoke_scope(h):
    jid=exhaust(h,'Revoke Linda’s scheduling access.')
    assert assessment(h,jid)['boundary']['recipients']==['linda']
    assert texts(h,'linda')==['The billing invoice is 40 dollars.']
    assert len(texts(h,'jules'))==2


@pytest.mark.parametrize('text', [
    'Stop sharing with Someone.',
    'And I should keep the prenatal vitamin and iron? Stop sharing with Linda.',
    'Can you revoke her access?',
    'Stop sharing with Linda if she calls.',
    'Stop sharing my scheduling information with Linda and delete my billing.',
])
def test_unresolved_boundary_is_released_after_bounded_assessment(h, text):
    # Superseded Batch 4 policy: unknown boundaries used to block indefinitely.
    # Both extra calls fail technically here; the agreed policy releases.
    jid = exhaust(h, text)
    row = assessment(h, jid)
    assert row['state'] == 'TECHNICAL_EXHAUSTION' and row['finalization']['uncertainty_released']
    assert not row['finalization']['verified_controls'] and row['finalization']['unresolved']
    assert len(texts(h, 'linda')) == len(texts(h, 'jules')) == 2


def test_relative_timing_is_never_applied_early_and_is_released(h):
    jid = exhaust(h, 'Stop sharing with Linda tomorrow.')
    row = assessment(h, jid)
    assert row['boundary']['recipients'] == ['linda']
    assert 'timing' in row['boundary']['unresolved']
    assert row['state'] == 'TECHNICAL_EXHAUSTION' and not row['finalization']['verified_controls']
    assert len(texts(h, 'linda')) == len(texts(h, 'jules')) == 2


def test_finalized_event_is_not_reopened_by_later_identity_evidence(h):
    jid=exhaust(h,'Stop sharing with Someone.')
    finalized=deepcopy(assessment(h,jid))
    assert len(texts(h,'linda'))==2
    identity=h.turn('My name is Someone.',actor='outsider')
    # Neither queue progress nor explicit reassessment reopens a finalized
    # event, resets its budget, or retroactively creates a control.
    assert h.updater.reassess_control_event(jid,identity_source_ids=[identity['source_id']])==finalized
    assert assessment(h,jid)==finalized and len(texts(h,'linda'))==2


def bound_quotes(h, aid):
    a = grants(h)[aid]
    payloads = {b['source_id']: json.loads(h.core.db.execute('SELECT payload FROM resources WHERE id=?', (b['source_id'],)).fetchone()[0])
        for b in a['bindings']}
    return [''.join(payloads[b['source_id']]['text'][i:j] for i, j in b['spans']) for b in a['bindings']]


def test_new_grants_and_facts_do_not_escape_validated_restriction(h):
    from security_tests.test_privacy_fallback_v19 import honoring
    jid = exhaust(h, 'Stop sharing my scheduling information with Linda.')
    before = set(grants(h))
    # Later facts and grants: complete-fact reviews see the durable restriction,
    # and the closed complete-fact grammar enforces it independently.
    honoring(h, lambda quote: 'appointment' in quote)
    h.policy = lambda p: [h.grant(p, recipient='linda', scope='Scheduling and billing')]
    h.turn('Linda may read my scheduling and billing information.')
    [new] = [aid for aid in grants(h) if aid not in before]
    h.policy = lambda p: []
    h.classify = lambda p, s: 'instruction'
    h.turn('The appointment is Wednesday at 10 AM. The billing invoice is now 50 dollars.')
    assert any('Wednesday' in t for t in texts(h, 'jules'))
    assert not any('Wednesday' in t for t in texts(h, 'linda'))
    assert any('50 dollars' in t for t in texts(h, 'linda'))
    quotes = bound_quotes(h, new)
    assert any('50 dollars' in q for q in quotes) and not any('appointment' in q for q in quotes)
    assert assessment(h, jid)['state'] == 'SUPPORTED_CHANGES'

def test_successful_existing_grant_revoke_retains_prospective_restriction(h):
    from security_tests.test_privacy_fallback_v19 import honoring
    h.policy = revoke_policy(h, ['linda'])
    job = h.turn("Revoke Linda's scheduling access.")
    row = assessment(h, job['job_id'])
    # The validated boundary is retained as a durable prospective restriction.
    assert row['state'] == 'CONTROL_COMMITTED'
    [control] = row['finalization']['verified_controls']
    assert (control['kind'], control['participants'], control['field']) == ('restriction', ['linda'], 'scheduling')
    honoring(h, lambda quote: 'appointment' in quote)
    h.policy = lambda p: [h.grant(p, recipient='linda', scope='Scheduling and billing')]
    h.turn('Linda may read my scheduling and billing information.')
    assert texts(h, 'linda') == ['The billing invoice is 40 dollars.']


@pytest.mark.parametrize('action', ['read','search','derive'])
def test_all_runtime_actions_consult_same_hold(h, action):
    reader = h.reader('linda')
    packet = h.core.retrieve(reader, 'appointment', k=100)
    rid = next(r['id'] for r in packet['evidence'] if 'appointment' in r['text'])
    jid = pending(h, 'Stop sharing my scheduling information with Linda.')
    h.updater.reassess_control_event(jid)
    assert not h.core.allowed(reader, rid, action)
    with pytest.raises(AccessDenied): h.core.read(reader, rid)
    with pytest.raises(AccessDenied): h.core.release(reader, packet['input_ids'], {'answer': 'stale'})


@pytest.mark.parametrize('change', ['hold','revoke','delete'])
def test_release_rechecks_change_during_slow_model_call(h, change):
    reader = h.reader('linda')
    packet = h.core.retrieve(reader, 'appointment', k=100)
    assert packet['input_ids']
    # Model invocation occurs outside the transaction. Mutate authorization
    # after prompt assembly, before that invocation returns its buffered output.
    def slow_model():
        if change == 'hold':
            h.updater.reassess_control_event(pending(h, 'Stop sharing with Linda.'))
        elif change == 'revoke':
            h.policy = revoke_policy(h, ['linda']); h.turn("Revoke Linda's scheduling access.")
        else:
            h.core.delete(h.host, packet['input_ids'][0])
        return {'answer': 'Your appointment is Tuesday.'}
    answer = slow_model()
    with pytest.raises(AccessDenied): h.core.release(reader, packet['input_ids'], answer)


def test_restart_preserves_finalization_and_non_control_audit(h):
    ordinary = exhaust(h, 'And I should keep the prenatal vitamin and iron?')
    history = deepcopy(assessment(h, ordinary))
    reopen(h)
    assert assessment(h, ordinary) == history and len(texts(h, 'linda')) == 2
    unknown = exhaust(h, 'Stop sharing with Someone.')
    before = deepcopy(h.updater.control_assessments())
    reopen(h)
    assert h.updater.control_assessments() == before
    assert assessment(h, ordinary)['state'] == 'VALIDATED_NON_CONTROL'
    # Released after the bounded assessment; restart neither restores a hold
    # nor reopens the exhausted two-call budget.
    assert assessment(h, unknown)['state'] == 'TECHNICAL_EXHAUSTION'
    assert len(texts(h, 'linda')) == 2
    assert holds.installed(h.core, 'work')


def test_loss_of_boundary_evidence_cannot_leave_narrow_exception(h):
    jid = exhaust(h, 'Stop sharing my scheduling information with Linda.')
    assert len(texts(h, 'jules')) == 2
    proof = assessment(h, jid)['boundary_evidence']['identity_relationship_evidence']['linda'][0]
    h.core.delete(h.host, proof['source_id'])
    assert not texts(h, 'jules')


def test_cache_and_snapshot_invalidate_on_reassessment(h):
    reader = h.reader('linda')
    rid = h.core.retrieve(reader, 'appointment', k=100)['input_ids'][0]
    before = h.updater._snapshot()
    with h.core.transaction():
        assert h.core.allowed(reader, rid)
        jid = pending(h, 'Stop sharing with Linda.')
        h.updater.reassess_control_event(jid)
        assert not h.core.allowed(reader, rid)
    assert h.updater._snapshot() != before
    assert not h.core._tickets


def test_maintenance_preserves_unaffected_binding_work(h):
    from security_tests.test_privacy_fallback_v19 import honoring
    exhaust(h, 'Stop sharing my scheduling information with Linda.')
    honoring(h, lambda quote: 'appointment' in quote)
    # Force genuinely outstanding reviews, retaining old binding audit.
    h.core.db.execute("DELETE FROM fact_processing WHERE job_id='incremental-reviews:work'")
    before = len(h.model.calls)
    h.updater.flush_bindings(max_batches=1)
    assert len(h.model.calls) > before
    assert not any('appointment' in t for t in texts(h, 'linda'))


def test_field_uncertainty_is_conservative_even_if_one_keyword_looks_unrelated():
    boundary = {'subjects':['owner'], 'recipients':['linda'], 'fields':['scheduling']}
    assert holds.intersects(boundary, subject='owner', recipient='linda', text='Invoice includes private arrival notes.')
    assert holds.intersects(boundary, subject='owner', recipient='linda', text='The invoice is 40 dollars. The appointment is Tuesday at 9:20 AM.')
    assert not holds.intersects(boundary, subject='owner', recipient='linda', text='The billing invoice is 40 dollars.')
    assert not holds.intersects(boundary, subject='owner', recipient='jules', text='Unknown fields')
    # A clinical appointment modifier is additional content, not proof that the
    # complete fact belongs exclusively to scheduling. No domain policy inferred.
    assert not holds.proven_fields('The ultrasound appointment is Tuesday at 9:20 AM.')
    assert not holds.intersects({'fields':[]},text='Unknown fields')
