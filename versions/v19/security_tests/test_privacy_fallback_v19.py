"""Bounded independent privacy assessment after exhausted V19 processing.

The scripted assessor below is a TEST DOUBLE for the two extra model calls; it
measures host validation, publication and release behavior, never real-model
quality. People and texts are test sources, not runtime fixtures.
"""
from copy import deepcopy
import json

import pytest

from agentic_memory.privacy import AccessDenied
from agentic_memory.privacy.permissions import (PermissionSourceEvent, PermissionRepairExhausted,
    PermissionUpdater, PermissionProposalRejected)
from agentic_memory.privacy.processors import ModelContractError, ProcessorFailure
from agentic_memory.privacy import privacy_fallback_v19 as fallback
from agentic_memory.privacy import scoped_holds_v19 as holds
from agentic_memory.privacy.answering import PermissionQueryService
from security_tests.test_recipient_revocation_v19 import h, texts, grants, revoke_policy  # noqa: F401
from security_tests.test_scoped_holds_v19 import reopen


IDENTIFIER_FAILURE = 'Structured output failed local schema validation: i: enum'


class Crash(BaseException):
    """A hard process death: no handler runs after dispatch."""


def assessor(h, responder):
    """Route only the extra assessment stage to the scripted assessor."""
    handler = h.model.handler
    h.model.handler = lambda p: responder(p) if p.get('stage') == fallback.STAGE else handler(p)


def failing_source(h):
    """Force identifier failures for ordinary processing; extra calls still run."""
    original = h.model.complete
    def complete(system, payload, *, schema=None):
        if payload.get('stage') != fallback.STAGE:
            raise ModelContractError(IDENTIFIER_FAILURE)
        return original(system, payload, schema=schema)
    h.model.complete = complete
    return original


def exhausted(h, text, responder, actor='owner'):
    if responder is not None:
        assessor(h, responder)
    original = failing_source(h)
    try:
        h.seq += 1
        job = h.updater.ingest(PermissionSourceEvent(str(h.seq), actor, 'connector', text))['job_id']
        with pytest.raises(PermissionRepairExhausted):
            h.updater.update(job, max_attempts=2)
        h.skipped = h.updater.skip_exhausted(job, max_attempts=2)
    finally:
        h.model.complete = original
    return job


def row(h, job):
    return next(r for r in h.updater.control_assessments() if r['job_id'] == job)


def calls(h):
    return [p for p in h.model.calls if p.get('stage') == fallback.STAGE]


def attempt_rows(h, job):
    return fallback.attempts(h.updater, job)


def response(outcome='supported_changes', clauses=(), validated=(), reason='Complete original event assessed.'):
    return {'outcome': outcome, 'reason': reason, 'clauses': list(clauses), 'validated_control_applicability': list(validated)}


def clause(kind, quote, *, recipients=(), scope='', values=(), timing='now', dims=(), applicability=(),
           reason='Explicit authenticated instruction in the original event.'):
    return {'kind': kind, 'quote': quote, 'recipients': list(recipients), 'scope': scope, 'values': list(values),
        'timing': timing, 'unresolved_dimensions': list(dims), 'reason': reason, 'applicability': list(applicability)}


def judged(p, rule):
    return [{'fact': f['fact'], 'quote': f['quote'], 'verdict': rule(f['quote']), 'reason': 'Complete fact reviewed.'}
        for f in p['facts']]


def mentions(word):
    return lambda quote: 'applies' if word in quote else 'outside'


def no_instruction(p):
    return response('no_privacy_instruction', reason='Ordinary request; no privacy instruction.')


def timeout(p):
    raise ProcessorFailure('Model request timed out after 300 seconds')


def restriction(h, job):
    return [r for r in h.updater.inspect()['restriction'].values()
        if r.get('independent_privacy_v19', {}).get('job_id') == job]


def honoring(h, rule):
    """Scripted binding reviewer that, like a real model, sees restrictions."""
    def binding(p, r):
        quote = next((c['quote'] for c in [*p.get('candidates', []), *({'id': s['candidate_id'], 'quote': s['quote']}
            for s in p.get('segments', []) if 'candidate_id' in s)] if c['id'] == r['candidate_id']), '')
        assert quote, 'The scripted reviewer needs the complete candidate fact'
        a = p['permission_state']['assignment'].get(r['assignment_id'], {})
        blocked = [rid for rid, x in p['permission_state']['restriction'].items()
            if a.get('participant') in x['participants'] and x['subject'] == a.get('subject') and rule(quote)]
        return {**r, 'in_scope': True, 'subject_supported': True, 'blocked_by': blocked,
            'reason': 'Restriction applies to this complete fact.' if blocked else 'Within this recipient scope.'}
    h.binding = binding


# --- Release of event uncertainty -------------------------------------------------

def test_no_privacy_instruction_releases_hold_and_preserves_failure_history(h):
    before = deepcopy(grants(h))
    job = exhausted(h, 'Can I still use ibuprofen for cramping?', no_instruction)
    final = row(h, job)
    assert final['state'] == 'NO_PRIVACY_INSTRUCTION'
    assert final['finalization']['outcome'] == 'NO_PRIVACY_INSTRUCTION'
    assert final['finalization']['uncertainty_released'] and final['finalization']['extra_model_calls'] == 1
    assert len(calls(h)) == 1 and calls(h)[0]['original_event']['text'] == 'Can I still use ibuprofen for cramping?'
    # Processing failure stays failed; access uncertainty is finalized separately.
    status, attempts = h.core.db.execute('SELECT status,attempts FROM permission_jobs WHERE id=?', (job,)).fetchone()
    assert (status, attempts) == ('quarantined', 2)
    failures = [json.loads(r[0]) for r in h.core.db.execute('SELECT validation FROM permission_attempts WHERE job_id=?', (job,))]
    assert len(failures) == 2 and all(IDENTIFIER_FAILURE in f['reason'] for f in failures)
    validation = json.loads(h.core.db.execute('SELECT validation FROM permission_jobs WHERE id=?', (job,)).fetchone()[0])
    assert validation['unresolved_access_hold'] is False
    assert validation['privacy_assessment']['outcome'] == 'NO_PRIVACY_INSTRUCTION'
    assert h.skipped['privacy_assessment'] == {'outcome': 'NO_PRIVACY_INSTRUCTION', 'uncertainty_released': True,
        'verified_controls': 0, 'unresolved': 0, 'extra_model_calls': 1}
    # A repeated skip is idempotent; only the first skip carries the audit summary.
    assert h.updater.skip_exhausted(job, max_attempts=2) == {'status': 'quarantined', 'changes': []}
    assert not h.core.db.execute('SELECT 1 FROM permission_control_holds WHERE job_id=?', (job,)).fetchone()
    assert len(texts(h, 'linda')) == len(texts(h, 'jules')) == 2
    assert grants(h) == before and not restriction(h, job)


def test_local_complete_message_grammar_skips_extra_calls(h):
    job = exhausted(h, 'And I should keep the prenatal vitamin and iron?', None)
    assert row(h, job)['state'] == 'VALIDATED_NON_CONTROL'
    assert row(h, job)['finalization']['assessed_by'] == 'local_complete_message_grammar'
    assert not calls(h) and not attempt_rows(h, job)
    assert len(texts(h, 'linda')) == 2


def test_temporary_protection_only_while_the_assessment_is_active(h):
    seen = {}
    def responder(p):
        seen['during'] = texts(h, 'linda')
        return no_instruction(p)
    assessor(h, responder)
    original = failing_source(h)
    h.seq += 1
    job = h.updater.ingest(PermissionSourceEvent(str(h.seq), 'owner', 'connector', 'Can I still use ibuprofen for cramping?'))['job_id']
    assert not texts(h, 'linda')  # pending: processing is active
    with pytest.raises(PermissionRepairExhausted):
        h.updater.update(job, max_attempts=2)
    assert not texts(h, 'linda')  # exhausted, not yet assessed
    h.updater.skip_exhausted(job, max_attempts=2)
    h.model.complete = original
    assert seen['during'] == []
    assert len(texts(h, 'linda')) == 2


@pytest.mark.parametrize('responder,outcome,statuses', [
    (timeout, 'TECHNICAL_EXHAUSTION', ['failed', 'failed']),
    (lambda p: {'outcome': 'maybe'}, 'TECHNICAL_EXHAUSTION', ['unusable', 'unusable']),
    (lambda p: response('no_privacy_instruction', [clause('deny', p['original_event']['text'], recipients=['Linda'],
        scope='*')]), 'TECHNICAL_EXHAUSTION', ['unusable', 'unusable']),
    (lambda p: response('unresolved', reason='Recipient and meaning are unclear.'), 'UNRESOLVED_RELEASED', ['validated']),
])
def test_unusable_or_uncertain_assessment_releases_after_bounded_budget(h, responder, outcome, statuses):
    before = deepcopy(grants(h))
    job = exhausted(h, 'Stop sharing with Someone.', responder)
    final = row(h, job)
    assert final['finalization']['outcome'] == outcome and final['state'] == outcome
    assert [a['status'] for a in attempt_rows(h, job)] == statuses
    assert len(calls(h)) == len(statuses) <= fallback.MAX_CALLS
    # Deliberate policy: established permissions remain; no hold, no guess.
    assert len(texts(h, 'linda')) == len(texts(h, 'jules')) == 2
    assert grants(h) == before and not restriction(h, job)
    assert final['finalization']['unresolved']
    assert all(a.get('elapsed_s') is not None for a in attempt_rows(h, job))


def test_budget_and_finalization_survive_restart_queries_and_reassessment(h):
    job = exhausted(h, 'Stop sharing with Someone.', timeout)
    history = deepcopy(row(h, job))
    reopen(h)
    assessor(h, lambda p: pytest.fail('A finalized event must never be reassessed'))
    assert h.updater.skip_exhausted(job, max_attempts=2)['status'] == 'quarantined'
    assert h.updater.reassess_control_event(job) == history
    assert h.updater.update(job, max_attempts=2)['status'] == 'quarantined'
    service = PermissionQueryService(h.updater)
    service.answer_authorization({'asker': {'principal_id': 'linda'}, 'query_text': 'Current information'})
    assert row(h, job) == history and len(attempt_rows(h, job)) == 2
    assert len(texts(h, 'linda')) == 2


def test_interrupted_call_never_holds_and_restart_cannot_reset_budget(h):
    assessor(h, lambda p: (_ for _ in ()).throw(Crash()))
    original = failing_source(h)
    h.seq += 1
    job = h.updater.ingest(PermissionSourceEvent(str(h.seq), 'owner', 'connector', 'Stop sharing with Someone.'))['job_id']
    with pytest.raises(PermissionRepairExhausted):
        h.updater.update(job, max_attempts=2)
    with pytest.raises(Crash):
        h.updater.skip_exhausted(job, max_attempts=2)
    h.model.complete = original
    assert [a['status'] for a in attempt_rows(h, job)] == ['dispatched']
    assert 'finalization' not in row(h, job)
    assert len(texts(h, 'linda')) == 2  # an interrupted assessment is not an indefinite hold
    reopen(h)
    assessor(h, lambda p: (_ for _ in ()).throw(SystemExit('operator interrupt')))
    with pytest.raises(SystemExit):
        h.updater.skip_exhausted(job, max_attempts=2)
    assert [a['status'] for a in attempt_rows(h, job)] == ['interrupted', 'interrupted']
    reopen(h)
    assessor(h, lambda p: pytest.fail('Both extra calls were consumed'))
    assert h.updater.skip_exhausted(job, max_attempts=2)['status'] == 'quarantined'
    assert row(h, job)['finalization']['outcome'] == 'TECHNICAL_EXHAUSTION'
    assert len(texts(h, 'linda')) == 2


# --- Supported controls ------------------------------------------------------------

def test_supported_restriction_from_mixed_message_is_recipient_specific(h):
    text = 'I took the iron this morning. Please do not share my billing with Linda.'
    quote = 'Please do not share my billing with Linda.'
    job = exhausted(h, text, lambda p: response(clauses=[clause('deny', quote, recipients=['Linda'],
        scope='my billing', applicability=judged(p, mentions('invoice')))]))
    final = row(h, job)
    assert final['state'] == 'SUPPORTED_CHANGES' and len(calls(h)) == 1
    [r] = restriction(h, job)
    assert (r['participants'], r['information_scope'], r['subject'], r['issuer']) == (['linda'], 'my billing', 'owner', 'owner')
    assert r['independent_privacy_v19']['citation']['quote'] == quote
    assert r['independent_privacy_v19']['field'] == 'billing'
    assert texts(h, 'linda') == ['The ultrasound appointment is Tuesday at 9:20 AM.']
    assert len(texts(h, 'jules')) == 2 and len(texts(h, 'owner')) == 0  # owner has no self grant here
    receipts = {json.loads(b[0])['quote']: json.loads(b[0])['verdict'] for b in h.core.db.execute(
        'SELECT body FROM privacy_scope_reviews_v19 WHERE restriction_id=?', (r['id'],))}
    assert receipts == {'The billing invoice is 40 dollars.': 'applies', 'The ultrasound appointment is Tuesday at 9:20 AM.': 'outside'}


def test_restriction_applies_prospectively_to_later_facts_and_grants(h):
    job = exhausted(h, 'Could you stop sharing my scheduling information with Linda?',
        lambda p: response(clauses=[clause('deny', p['original_event']['text'], recipients=['Linda'],
            scope='my scheduling information', applicability=judged(p, mentions('appointment')))],
            validated=[{'control': c['control'], 'fact': f['fact'], 'quote': f['quote'],
                'verdict': mentions('appointment')(f['quote']), 'reason': 'Complete fact reviewed.'}
                for c in p['validated_controls'] for f in p['facts']]))
    assert texts(h, 'linda') == ['The billing invoice is 40 dollars.']
    # Later grant and facts: complete-fact binding reviews see the restriction.
    honoring(h, lambda quote: 'appointment' in quote)
    h.policy = lambda p: [h.grant(p, recipient='linda', scope='Scheduling and billing')]
    h.turn('Linda may read my scheduling and billing information.')
    h.policy = lambda p: []
    h.classify = lambda p, s: 'instruction'
    h.turn('The follow-up appointment is Friday at 3 PM for the ultrasound. The billing invoice is now 50 dollars.')
    assert not any('appointment' in t for t in texts(h, 'linda'))
    assert any('50 dollars' in t for t in texts(h, 'linda'))
    assert any('Friday' in t for t in texts(h, 'jules'))
    assert row(h, job)['state'] == 'SUPPORTED_CHANGES'


def test_closed_complete_fact_grammar_applies_without_a_model_decision(h):
    job = exhausted(h, 'Stop sharing my scheduling information with Linda.', timeout)
    assert row(h, job)['finalization']['outcome'] == 'SUPPORTED_CHANGES'
    assert [c['kind'] for c in row(h, job)['finalization']['verified_controls']] == ['restriction']
    h.classify = lambda p, s: 'instruction'
    h.turn('The appointment is Wednesday at 10 AM.')
    assert not any('Wednesday' in t for t in texts(h, 'linda'))
    assert any('Wednesday' in t for t in texts(h, 'jules'))
    # Applicability of a fact outside the closed grammar was never established:
    # under the agreed policy it is not speculatively blocked.
    assert 'The ultrasound appointment is Tuesday at 9:20 AM.' in texts(h, 'linda')


def test_multiple_independent_clauses_publish_atomically(h):
    text = 'Stop sharing my billing with Linda. Stop sharing my scheduling information with Jules.'
    job = exhausted(h, text, lambda p: response(clauses=[
        clause('deny', 'Stop sharing my billing with Linda.', recipients=['Linda'], scope='my billing',
            applicability=judged(p, mentions('invoice'))),
        clause('deny', 'Stop sharing my scheduling information with Jules.', recipients=['Jules'],
            scope='my scheduling information', applicability=judged(p, mentions('appointment')))]))
    controls = restriction(h, job)
    assert len(controls) == 2
    published = {r[0] for r in h.core.db.execute("SELECT job_id FROM permission_records WHERE kind='restriction' AND id IN (?,?)",
        tuple(r['id'] for r in controls))}
    assert published == {job}
    assert texts(h, 'linda') == ['The ultrasound appointment is Tuesday at 9:20 AM.']
    assert texts(h, 'jules') == ['The billing invoice is 40 dollars.']


def test_partial_instruction_applies_only_the_independent_adverse_clause(h):
    before = deepcopy(grants(h))
    text = 'Stop sharing my billing with Linda, and let Jules read my clinical notes.'
    job = exhausted(h, text, lambda p: response(clauses=[
        clause('deny', 'Stop sharing my billing with Linda', recipients=['Linda'], scope='my billing',
            applicability=judged(p, mentions('invoice'))),
        clause('unresolved', 'let Jules read my clinical notes', dims=['meaning'],
            reason='A grant is outside an independent adverse assessment.')]))
    final = row(h, job)['finalization']
    assert final['outcome'] == 'SUPPORTED_CHANGES'
    assert [p['quote'] for p in final['unresolved'] if p['kind'] == 'unresolved'] == ['let Jules read my clinical notes']
    assert grants(h) == before  # no grant, approval or scope change
    assert texts(h, 'linda') == ['The ultrasound appointment is Tuesday at 9:20 AM.']


def test_invalid_clause_is_corrected_without_salvaging_or_blocking_valid_clause(h):
    replies = []
    def responder(p):
        replies.append(p)
        if p['attempt'] == 1:
            return response(clauses=[
                clause('deny', 'Stop sharing my billing with Linda.', recipients=['Linda'], scope='my billing',
                    applicability=judged(p, mentions('invoice'))),
                clause('deny', 'Stop sharing my scheduling information with Jules.', recipients=['person3'],
                    scope='my scheduling information')])
        return response(clauses=[clause('deny', 'Stop sharing my scheduling information with Jules.', recipients=['Jules'],
            scope='my scheduling information', applicability=judged(p, mentions('appointment')))])
    job = exhausted(h, 'Stop sharing my billing with Linda. Stop sharing my scheduling information with Jules.', responder)
    statuses = [[c['status'] for c in a['clauses']] for a in attempt_rows(h, job)]
    assert statuses == [['validated', 'invalid'], ['validated']]
    assert 'literally' in replies[1]['correction']['problems'][0]
    assert len(restriction(h, job)) == 2 and len(calls(h)) == 2
    assert texts(h, 'linda') == ['The ultrasound appointment is Tuesday at 9:20 AM.']
    assert texts(h, 'jules') == ['The billing invoice is 40 dollars.']


@pytest.mark.parametrize('text,first', [
    ('Stop sharing with her.', clause('deny', 'Stop sharing with her.', recipients=['her'], scope='*')),
    ('Stop sharing with Someone.', clause('deny', 'Stop sharing with Someone.', recipients=['Someone'], scope='*')),
    ('Stop sharing with Linda next month.', clause('deny', 'Stop sharing with Linda next month.', recipients=['Linda'],
        scope='*', timing='future_or_conditional')),
    ('Stop sharing with Linda if she calls.', clause('deny', 'Stop sharing with Linda if she calls.', recipients=['Linda'],
        scope='*', timing='future_or_conditional')),
])
def test_ambiguous_recipient_or_timing_is_released_not_guessed(h, text, first):
    before = deepcopy(grants(h))
    job = exhausted(h, text, lambda p: response(clauses=[first]) if p['attempt'] == 1 else
        response('unresolved', [clause('unresolved', text, dims=['recipient', 'timing'], reason='Cannot establish.')]))
    final = row(h, job)['finalization']
    assert final['outcome'] == 'UNRESOLVED_RELEASED' and not restriction(h, job)
    assert len(texts(h, 'linda')) == len(texts(h, 'jules')) == 2 and grants(h) == before
    assert {p['status'] for p in final['unresolved'] if p.get('attempt') == 1} == {'invalid'}


def test_unresolved_scope_applicability_does_not_speculatively_block(h):
    job = exhausted(h, 'Please keep that private from Linda.', lambda p: response(clauses=[
        clause('deny', 'Please keep that private from Linda.', recipients=['Linda'], scope='that',
            applicability=judged(p, lambda q: 'unresolved'))]))
    [r] = restriction(h, job)
    assert r['information_scope'] == 'that'
    assert len(texts(h, 'linda')) == 2  # applicability unresolved: established access stays
    verdicts = {json.loads(b[0])['verdict'] for b in h.core.db.execute(
        'SELECT body FROM privacy_scope_reviews_v19 WHERE restriction_id=?', (r['id'],))}
    assert verdicts == {'unresolved'}


def test_compound_fact_applies_when_any_part_discloses_withheld_content(h):
    h.classify = lambda p, s: 'instruction'
    h.turn('The invoice of 40 dollars covers the Tuesday ultrasound appointment.')
    h.classify = lambda p, s: 'context'
    job = exhausted(h, 'Stop sharing my billing with Linda.', lambda p: response(clauses=[
        clause('deny', 'Stop sharing my billing with Linda.', recipients=['Linda'], scope='my billing',
            applicability=judged(p, lambda q: 'applies' if 'invoice' in q or 'dollars' in q else 'outside'))]))
    assert texts(h, 'linda') == ['The ultrasound appointment is Tuesday at 9:20 AM.']
    assert len(texts(h, 'jules')) == 3
    assert row(h, job)['state'] == 'SUPPORTED_CHANGES'


def test_fact_quote_must_be_complete_and_exact_for_applicability(h):
    def responder(p):
        partial = [{**d, 'quote': d['quote'][:12]} for d in judged(p, mentions('invoice'))]
        return response(clauses=[clause('deny', 'Stop sharing my billing with Linda.', recipients=['Linda'],
            scope='my billing', applicability=partial if p['attempt'] == 1 else judged(p, mentions('invoice')))])
    job = exhausted(h, 'Stop sharing my billing with Linda.', responder)
    assert [a['status'] for a in attempt_rows(h, job)] == ['validated', 'validated']
    assert any('complete exact fact quote' in p for p in attempt_rows(h, job)[0]['problems'])
    assert texts(h, 'linda') == ['The ultrasound appointment is Tuesday at 9:20 AM.']


def test_supported_deletion_uses_existing_erasure_and_prevents_resurrection(h):
    h.classify = lambda p, s: 'instruction'
    h.turn('The backup callback line is 617-555-0140.')
    h.turn('The portal is the routine contact route.')
    assert any('617-555-0140' in t for t in texts(h, 'linda'))
    job = exhausted(h, 'Delete the backup callback line 617-555-0140 from memory.', lambda p: response(clauses=[
        clause('erase', 'Delete the backup callback line 617-555-0140 from memory.', values=['617-555-0140'])]))
    final = row(h, job)['finalization']
    assert final['outcome'] == 'SUPPORTED_CHANGES'
    [deletion] = final['deletions']
    assert deletion['status'] == 'completed' and deletion['values'] == ['617-555-0140'] and deletion['affected_resources'] > 0
    assert h.core.db.execute('SELECT COUNT(*) FROM source_erasures WHERE job_id=?', (job,)).fetchone()[0] == 1
    for reader in ('linda', 'jules', 'owner'):
        assert not any('617-555-0140' in t for t in texts(h, reader))
    assert any('portal' in t for t in texts(h, 'linda'))
    # A later copy (for example a recap) cannot resurrect the deleted value.
    h.turn('Recap: backup line 617-555-0140 and portal messaging.')
    assert not any('617-555-0140' in t for t in texts(h, 'linda'))


def test_unresolved_deletion_is_never_labeled_completed(h):
    h.classify = lambda p, s: 'instruction'
    h.turn('The backup callback line is 617-555-0140.')
    h.classify = lambda p, s: 'context'
    job = exhausted(h, 'Please delete the old backup contact information.', lambda p: response(clauses=[
        clause('erase', 'Please delete the old backup contact information.', values=['617-555-0140'])])
        if p['attempt'] == 1 else response('unresolved', [clause('unresolved',
            'Please delete the old backup contact information.', dims=['scope'],
            reason='The exact values to delete are not named in this event.')]))
    final = row(h, job)['finalization']
    assert final['outcome'] == 'UNRESOLVED_RELEASED' and final['deletions'] == []
    assert not h.core.db.execute('SELECT 1 FROM source_erasures WHERE job_id=?', (job,)).fetchone()
    assert any('617-555-0140' in t for t in texts(h, 'linda'))


def test_fallback_cannot_create_grants_bindings_or_approvals(h):
    before = deepcopy(grants(h))
    text = 'Pickup is at 4 PM. Stop sharing my billing with Linda.'
    job = exhausted(h, text, lambda p: response(clauses=[clause('deny', 'Stop sharing my billing with Linda.',
        recipients=['Linda'], scope='my billing', applicability=judged(p, mentions('invoice')))]))
    after = grants(h)
    assert set(after) == set(before)
    assert all(a['status'] == before[aid]['status'] and a['bindings'] == before[aid]['bindings'] for aid, a in after.items())
    assert not any('Pickup' in t for reader in ('owner', 'linda', 'jules') for t in texts(h, reader))
    with pytest.raises(PermissionProposalRejected, match='only adverse'):
        state = h.updater._state()
        sid = row(h, job)['source_id']
        h.updater._validate({'revision': 0, 'considered_restrictions': list(state['restriction']),
            'operations': [{'op': 'propose_assignment'}]}, state, {}, 0, new_source_id=sid, independent_adverse=True)


def test_preserves_independently_validated_controls_from_other_events(h):
    h.policy = revoke_policy(h, ['linda'])
    h.turn("Revoke Linda's scheduling access.")
    h.policy = lambda p: []
    first = exhausted(h, 'Stop sharing my billing with Jules.', lambda p: response(clauses=[
        clause('deny', 'Stop sharing my billing with Jules.', recipients=['Jules'], scope='my billing',
            applicability=judged(p, mentions('invoice')))]))
    controls = deepcopy(h.updater.inspect()['restriction'])
    revocations = deepcopy({aid: a.get('recipient_revocations') for aid, a in grants(h).items()})
    second = exhausted(h, 'Stop sharing with Someone.', timeout)
    assert row(h, second)['finalization']['outcome'] == 'TECHNICAL_EXHAUSTION'
    assert h.updater.inspect()['restriction'] == controls
    assert {aid: a.get('recipient_revocations') for aid, a in grants(h).items()} == revocations
    assert texts(h, 'linda') == ['The billing invoice is 40 dollars.']
    assert texts(h, 'jules') == ['The ultrasound appointment is Tuesday at 9:20 AM.']
    assert row(h, first)['state'] == 'SUPPORTED_CHANGES'


def test_exhausted_complete_revocation_publishes_existing_batch1_revocation(h):
    job = exhausted(h, "Revoke Linda's scheduling access.", None)
    final = row(h, job)['finalization']
    assert final['assessed_by'] == 'local_complete_message_grammar' and not calls(h)
    assert {c['kind'] for c in final['verified_controls']} == {'restriction', 'revocation'}
    linda = next(a for a in grants(h).values() if a['participant'] == 'linda')
    assert linda['status'] == 'active' and linda['recipient_revocations'][0]['scope'] == {'kind': 'field', 'value': 'scheduling'}
    assert texts(h, 'linda') == ['The billing invoice is 40 dollars.'] and len(texts(h, 'jules')) == 2


# --- Freshness, rollback and release ------------------------------------------------

def test_stale_policy_snapshot_publishes_nothing_then_rebuilds(h):
    def responder(p):
        if p['attempt'] == 1:
            with h.core.transaction():
                h.core.db.execute("UPDATE permission_bootstrap SET revision=revision+1 WHERE compartment='work'")
        return response(clauses=[clause('deny', 'Please do not share my billing with Linda.', recipients=['Linda'],
            scope='my billing', applicability=judged(p, mentions('invoice')))])
    job = exhausted(h, 'Please do not share my billing with Linda.', responder)
    assert [a['status'] for a in attempt_rows(h, job)] == ['stale', 'validated']
    assert attempt_rows(h, job)[0].get('published') is None
    assert len(restriction(h, job)) == 1 and texts(h, 'linda') == ['The ultrasound appointment is Tuesday at 9:20 AM.']


def test_changed_original_source_evidence_publishes_nothing(h):
    def responder(p):
        source = row(h, job_ref[0])['source_id']
        h.core.delete(h.host, source)
        return response(clauses=[clause('deny', 'Please do not share my billing with Linda.', recipients=['Linda'], scope='my billing')])
    job_ref = []
    assessor(h, responder)
    original = failing_source(h)
    h.seq += 1
    job = h.updater.ingest(PermissionSourceEvent(str(h.seq), 'owner', 'connector', 'Please do not share my billing with Linda.'))['job_id']
    job_ref.append(job)
    with pytest.raises(PermissionRepairExhausted):
        h.updater.update(job, max_attempts=2)
    h.updater.skip_exhausted(job, max_attempts=2)
    h.model.complete = original
    final = row(h, job)['finalization']
    assert final['outcome'] == 'UNRESOLVED_RELEASED' and not restriction(h, job)
    assert [a['status'] for a in attempt_rows(h, job)] == ['stale']
    assert len(texts(h, 'linda')) == 2


def test_receipt_freshness_follows_the_complete_fact_and_source(h):
    job = exhausted(h, 'Please do not share my billing with Linda.', lambda p: response(clauses=[
        clause('deny', 'Please do not share my billing with Linda.', recipients=['Linda'], scope='my billing',
            applicability=judged(p, mentions('invoice')))]))
    [r] = restriction(h, job)
    receipt = next(json.loads(b[0]) for b in h.core.db.execute(
        'SELECT body FROM privacy_scope_reviews_v19 WHERE restriction_id=?', (r['id'],)) if 'invoice' in json.loads(b[0])['quote'])
    cid = receipt['candidate_id']
    attrs = {'fact_candidate_id': cid}
    assert fallback.applies(h.core, 'work', r, attrs, '')
    body = json.loads(h.core.db.execute('SELECT body FROM fact_candidates WHERE id=?', (cid,)).fetchone()[0])
    changed = {**body, 'quote': body['quote'] + ' Updated.'}
    with h.core.transaction():
        h.core.db.execute('UPDATE fact_candidates SET body=? WHERE id=?', (json.dumps(changed), cid))
    assert not fallback.applies(h.core, 'work', r, attrs, '')
    assert receipt['policy_version'] == fallback.POLICY_VERSION


def test_publication_rollback_installs_nothing_and_consumes_one_call(h):
    publish = h.updater._publish
    def broken(validated, job_id):
        publish(validated, job_id)
        raise PermissionProposalRejected('Injected publication failure after partial writes')
    def responder(p):
        h.updater._publish = broken if p['attempt'] == 1 else publish
        return response(clauses=[clause('deny', 'Please do not share my billing with Linda.', recipients=['Linda'],
            scope='my billing', applicability=judged(p, mentions('invoice')))])
    job = exhausted(h, 'Please do not share my billing with Linda.', responder)
    first, second = attempt_rows(h, job)
    assert first['status'] == 'unusable' and 'rolled back' in first['reason'] and second['status'] == 'validated'
    published = h.core.db.execute("SELECT COUNT(*) FROM permission_records WHERE kind='restriction' AND job_id=?", (job,)).fetchone()[0]
    assert published == 1  # only the second, complete publication
    assert texts(h, 'linda') == ['The ultrasound appointment is Tuesday at 9:20 AM.']


def test_final_buffered_release_rechecks_a_restriction_published_meanwhile(h):
    reader = h.reader('linda')
    packet = h.core.retrieve(reader, 'billing invoice', k=100)
    assert any('invoice' in r['text'] for r in packet['evidence'])
    exhausted(h, 'Stop sharing my billing with Linda.', lambda p: response(clauses=[clause('deny',
        'Stop sharing my billing with Linda.', recipients=['Linda'], scope='my billing',
        applicability=judged(p, mentions('invoice')))]))
    with pytest.raises(AccessDenied):
        h.core.release(reader, packet['input_ids'], {'answer': 'The invoice is 40 dollars.'})


def test_queries_never_reassess_or_make_assessment_calls(h):
    job = exhausted(h, 'Stop sharing with Someone.', timeout)
    before = (deepcopy(row(h, job)), len(h.model.calls), attempt_rows(h, job))
    service = PermissionQueryService(h.updater)
    for reader in ('linda', 'jules'):
        service.answer_authorization({'asker': {'principal_id': reader}, 'query_text': 'Current information'})
        h.core.retrieve(h.reader(reader), 'appointment', k=100)
    assert (row(h, job), len(h.model.calls), attempt_rows(h, job)) == before


# --- Audit, metrics and compatibility ------------------------------------------------

def test_metrics_and_audit_distinguish_all_outcomes(h):
    jobs = {
        'NO_PRIVACY_INSTRUCTION': exhausted(h, 'Can I still use ibuprofen for cramping?', no_instruction),
        'SUPPORTED_CHANGES': exhausted(h, 'Stop sharing my billing with Linda.', lambda p: response(clauses=[
            clause('deny', 'Stop sharing my billing with Linda.', recipients=['Linda'], scope='my billing',
                applicability=judged(p, mentions('invoice')))])),
        'UNRESOLVED_RELEASED': exhausted(h, 'Stop sharing with her.', lambda p: response('unresolved', reason='Unclear recipient.')),
        'TECHNICAL_EXHAUSTION': exhausted(h, 'Stop sharing with Someone.', timeout),
    }
    from agentic_memory.privacy.fact_candidates import metrics
    m = metrics(h.updater)['independent_privacy_v19']
    for outcome in jobs:
        assert m['outcomes'][outcome] == 1
    assert m['extra_model_calls'] == 1 + 1 + 1 + 2
    assert m['verified_controls']['restriction'] == 1 and m['active_temporary_holds'] == 0
    assert m['applicability_receipts'] == {'applies': 1, 'outside': 1, 'unresolved': 0}
    report = {r['job_id']: r for r in h.updater.privacy_assessments()}
    for outcome, job in jobs.items():
        final = report[job]['finalization']
        assert final['outcome'] == outcome and final['uncertainty_released']
        assert final['release_reason'] == fallback.RELEASE[outcome]
    attempt = report[jobs['SUPPORTED_CHANGES']]['attempts'][0]
    assert {'request_digest', 'snapshot', 'source_digest', 'elapsed_s', 'usage', 'model_elapsed_s', 'clauses',
            'published', 'prompt_version'} <= set(attempt)
    assert attempt['prompt_version'] == fallback.PROMPT_VERSION


def test_historical_store_policy_is_refused_not_migrated(h):
    exhausted(h, 'Can I still use ibuprofen for cramping?', no_instruction)
    with h.core.transaction():
        h.core.db.execute('UPDATE privacy_metadata SET value=? WHERE key=?', ('v19-old', 'independent-privacy-policy:work'))
    with pytest.raises(ValueError, match='fresh store'):
        PermissionUpdater(h.core, h.host, 'work', h.bootstrap, h.model, version='19')
    with h.core.transaction():
        h.core.db.execute('DELETE FROM privacy_metadata WHERE key=?', ('independent-privacy-policy:work',))
    with pytest.raises(ValueError, match='fresh store'):
        PermissionUpdater(h.core, h.host, 'work', h.bootstrap, h.model, version='19')


def test_payload_hides_identifiers_and_uses_only_the_original_prefix(h):
    seen = []
    exhausted(h, 'Can I still use ibuprofen for cramping?', lambda p: seen.append(p) or no_instruction(p))
    payload = json.dumps(seen[0])
    for secret in ('owner', 'linda', 'jules', 'permission-source:', 'fact:'):
        assert secret not in payload
    assert seen[0]['original_event']['text'] == 'Can I still use ibuprofen for cramping?'
    assert [f['quote'] for f in seen[0]['facts']] == ['The ultrasound appointment is Tuesday at 9:20 AM.',
        'The billing invoice is 40 dollars.']
