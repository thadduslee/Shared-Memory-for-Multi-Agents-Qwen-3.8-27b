"""Offline recipient-resolution and publication regressions on frozen V19.

People and relationships below are test sources, never runtime fixtures or
benchmark annotations. The scripted model deliberately proposes hostile IDs.
"""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from agentic_memory.privacy import AccessDenied
from agentic_memory.privacy.permissions import (
    PermissionUpdater, PermissionSourceEvent, PermissionRepairExhausted,
    PermissionProposalRejected,
)
from agentic_memory.privacy import revocation_v19 as revocation
from security_tests import test_incremental_permissions as fixtures


@pytest.fixture
def h():
    harness = fixtures.IncrementalTests()
    harness.setUp()
    people = ['owner', 'linda', 'jules', 'outsider']
    harness.actor_names = {p: i + 1 for i, p in enumerate(people)}
    harness.bootstrap = replace(harness.bootstrap,
        participants={p: {} for p in people}, resources={p: {} for p in people},
        authorities={p: {cap: [p] for cap in ('permission', 'restrict', 'delegate')} for p in people},
        source_identities={'connector': people}, discover_self_owned_participants=())
    harness.updater = PermissionUpdater(harness.core, harness.host, 'work',
        harness.bootstrap, harness.model, version='19')
    harness.turn('My name is Linda.', actor='linda')
    harness.turn('My name is Jules.', actor='jules')
    harness.turn('My mother is Linda.')
    harness.policy = lambda p: [harness.grant(p, recipient='linda', slot=0, scope='Scheduling and billing'),
        harness.grant(p, recipient='jules', slot=1, scope='Scheduling and billing')]
    harness.turn('Linda and Jules may read my scheduling and billing information.')
    harness.policy = lambda p: []
    harness.classify = lambda p, s: 'instruction'
    harness.turn('The ultrasound appointment is Tuesday at 9:20 AM. The billing invoice is 40 dollars.')
    harness.classify = lambda p, s: 'context'
    try:
        yield harness
    finally:
        harness.tearDown()


def grants(h):
    return h.updater.inspect()['assignment']


def source_prefix(h):
    return {row['id']: json.loads(row['payload']) for row in h.core.db.execute(
        "SELECT id,payload FROM resources WHERE compartment='work' AND kind='observation' ORDER BY rowid")}


def revoke_policy(h, recipients):
    def policy(p):
        return [{'op': 'revoke_assignment', 'assignment_id': aid,
            'authority_source': p['new_source_id'], 'evidence': [p['new_source_id']],
            'reason': 'Withdraw only the explicitly requested recipient and scope.'}
            for aid, a in p['permission_state']['assignment'].items()
            if a['participant'] in {h.actor(p, name) for name in recipients}]
    return policy


def texts(h, reader):
    return [r['text'] for r in h.selected(reader)]


def test_revoke_mother_preserves_jules_and_linda_billing_with_provenance(h):
    before = deepcopy(grants(h))
    jules = next(aid for aid, a in before.items() if a['participant'] == 'jules')
    h.policy = revoke_policy(h, ['linda'])
    h.turn("Please revoke my mother's scheduling access too.")
    after = grants(h)
    assert after[jules] == before[jules]
    assert len(texts(h, 'jules')) == 2
    assert texts(h, 'linda') == ['The billing invoice is 40 dollars.']
    linda = next(a for a in after.values() if a['participant'] == 'linda')
    assert linda['status'] == 'active'  # residual billing access
    proof = linda['recipient_revocations'][0]
    assert (proof['requesting_actor'], proof['subject'], proof['recipients']) == ('owner', 'owner', ['linda'])
    assert proof['scope'] == {'kind': 'field', 'value': 'scheduling'}
    evidence = proof['identity_relationship_evidence']['linda']
    assert any(p.get('relationship') == 'mother' and p['subject'] == 'owner' for p in evidence)
    prefix = source_prefix(h)
    for item in [proof['reference'], *evidence]:
        assert item['quote'] == ''.join(prefix[item['source_id']]['text'][a:b] for a, b in item['spans'])
    assert not h.core._read_cache


def test_explicit_staff_recipient_is_not_immune(h):
    before = deepcopy(grants(h))
    linda = next(aid for aid, a in before.items() if a['participant'] == 'linda')
    h.policy = revoke_policy(h, ['jules'])
    h.turn("Revoke Jules's scheduling access.")
    assert grants(h)[linda] == before[linda]
    assert len(texts(h, 'linda')) == 2
    assert texts(h, 'jules') == ['The billing invoice is 40 dollars.']


@pytest.mark.parametrize('reference', ["Linda and Jules's", "Linda's and Jules's"])
def test_explicit_recipient_set_is_resolved_before_grant_selection(h, reference):
    h.policy = revoke_policy(h, ['linda', 'jules'])
    h.turn('Revoke ' + reference + ' scheduling access.')
    assert texts(h, 'linda') == texts(h, 'jules') == ['The billing invoice is 40 dollars.']
    assert all(a['recipient_revocations'][0]['recipients'] == ['jules', 'linda'] for a in grants(h).values())


def test_mixed_correct_and_incorrect_grants_cannot_publish(h):
    before = deepcopy(grants(h))
    h.policy = revoke_policy(h, ['linda', 'jules'])
    with pytest.raises(PermissionRepairExhausted):
        h.turn("Please revoke my mother's scheduling access too.")
    assert grants(h) == before
    assert not any(a.get('recipient_revocations') for a in grants(h).values())
    job = h.core.db.execute('SELECT id FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0]
    result = h.updater.skip_exhausted(job, max_attempts=2)
    assert result['status'] == 'quarantined'
    from agentic_memory.privacy.control_holds import rows
    assert not rows(h.core, 'work')  # no lasting uncertainty hold
    # Invalid model-selected grants are never salvaged. The host's independent
    # compiler of the complete source publishes only the Linda-only withdrawal
    # through the existing Batch 1 validator; Jules access is untouched.
    final = next(r for r in h.updater.control_assessments() if r['job_id'] == job)['finalization']
    [revocation] = [c for c in final['verified_controls'] if c['kind'] == 'revocation']
    assert [grants(h)[aid]['participant'] for aid in revocation['assignment_ids']] == ['linda']
    assert len(texts(h, 'jules')) == 2
    assert texts(h, 'linda') == ['The billing invoice is 40 dollars.']


def test_wrong_model_selected_recipient_is_rejected_even_with_correct_operations(h):
    h.policy = revoke_policy(h, ['linda'])
    before = deepcopy(grants(h))
    original = h.model.complete
    def hostile(system, payload, *, schema=None):
        response = original(system, payload, schema=schema)
        for control in response.get('c', {}).values():
            if 'revoke' in control['intents']:
                control['recipient'] = h.actor(payload, 'jules')
        return response
    h.model.complete = hostile
    with pytest.raises(PermissionRepairExhausted):
        h.turn("Please revoke my mother's scheduling access too. Do not share future appointment details with family.")
    assert grants(h) == before


@pytest.mark.parametrize('instruction', [
    "Revoke my father's scheduling access.",
    "Revoke Unknown's scheduling access.",
    "Revoke Linda's scheduling access after Tuesday.",
    "If I call, revoke Linda's scheduling access.",
    "Do not revoke Linda's scheduling access.",
    "Revoke Linda's scheduling access?",
    "Revoke Linda's future appointment access.",
])
def test_unresolved_reference_or_effective_condition_never_guesses_or_clears_hold(h, instruction):
    before = deepcopy(grants(h))
    h.policy = revoke_policy(h, ['jules'])
    with pytest.raises(PermissionRepairExhausted):
        h.turn(instruction)
    assert grants(h) == before


def test_same_name_ambiguity_does_not_use_current_worker_or_matching_grants(h):
    h.turn('My name is Linda.', actor='outsider')
    h.policy = revoke_policy(h, ['linda'])
    before = deepcopy(grants(h))
    with pytest.raises(PermissionRepairExhausted):
        h.turn("Revoke Linda's scheduling access.")
    assert grants(h) == before


def test_unauthorized_actor_cannot_revoke_another_subject_even_with_explicit_subject(h):
    before = deepcopy(grants(h))
    h.policy = revoke_policy(h, ['linda'])
    h.seq += 1
    job = h.updater.ingest(PermissionSourceEvent(str(h.seq), 'outsider', 'connector',
        "Revoke Linda's scheduling access.", subject='owner'))
    with pytest.raises(PermissionRepairExhausted):
        h.updater.update(job['job_id'])
    assert grants(h) == before


@pytest.mark.parametrize('reference', ['my', 'me', 'myself'])
def test_legitimate_self_target_is_accepted(h, reference):
    h.policy = lambda p: [h.grant(p, recipient='owner', scope='Scheduling')]
    h.turn('I may read my own scheduling information.')
    h.policy = revoke_policy(h, ['owner'])
    text = 'Revoke my scheduling access.' if reference == 'my' else f'Revoke {reference} access to scheduling.'
    h.turn(text)
    owner = next(a for a in grants(h).values() if a['participant'] == 'owner')
    assert owner['recipient_revocations'][0]['recipients'] == ['owner']
    assert all(not a.get('recipient_revocations') for a in grants(h).values() if a['participant'] != 'owner')


def test_partial_revocation_blocks_stale_and_future_bindings_without_losing_residual_fields(h):
    reader = h.reader('linda')
    packet = h.core.retrieve(reader, 'appointment', k=100)
    assert any('appointment' in r['text'] for r in packet['evidence'])
    # The model has already generated this buffered answer before the update.
    answer = {'answer': 'Your appointment is Tuesday at 9:20 AM.'}
    h.policy = revoke_policy(h, ['linda'])
    h.turn("Revoke Linda's scheduling access.")
    with pytest.raises(AccessDenied):
        h.core.release(reader, packet['input_ids'], answer)
    billing = h.core.retrieve(reader, 'billing', k=100)
    assert h.core.release(reader, billing['input_ids'], {'answer': 'Invoice is 40 dollars.'})
    h.policy = lambda p: []
    h.classify = lambda p, s: 'instruction'
    h.turn('The appointment is Wednesday at 10 AM. The billing invoice is now 50 dollars.')
    assert all('appointment' not in text for text in texts(h, 'linda'))
    assert any('50 dollars' in text for text in texts(h, 'linda'))
    assert any('Wednesday' in text for text in texts(h, 'jules'))


def test_full_revocation_only_removes_target_recipient(h):
    h.policy = revoke_policy(h, ['linda'])
    h.turn("Revoke Linda's access.")
    assert not texts(h, 'linda')
    assert len(texts(h, 'jules')) == 2
    assert next(a for a in grants(h).values() if a['participant'] == 'linda')['status'] == 'revoked'


def test_partial_actions_preserve_unaffected_actions_and_original_validity(h):
    a = deepcopy(next(iter(grants(h).values())))
    a['actions'] = ['read', 'export']
    old = deepcopy(a['validity'])
    revocation.apply(a, {'requesting_actor': 'owner', 'subject': 'owner',
        'recipients': ['linda'], 'actions': ['read'], 'scope': {'kind': 'all', 'value': '*'},
        'conditions': {'effective': 'now'}, 'reference': {'source_id': 'request'},
        'identity_relationship_evidence': {}, 'assignment_id': a['id']})
    assert a['status'] == 'active' and a['actions'] == ['read', 'export'] and a['validity'] == old
    assert revocation.blocks(a, 'read', 'Appointment Tuesday.')
    assert not revocation.blocks(a, 'export', 'Appointment Tuesday.')


@pytest.mark.parametrize('target', ['missing-grant', 'foreign:grant'])
def test_cross_namespace_and_missing_grant_ids_rejected_on_canonical_path(h, target):
    h.seq += 1
    job = h.updater.ingest(PermissionSourceEvent(str(h.seq), 'owner', 'connector',
        "Revoke Linda's scheduling access."))
    state = h.updater._state()
    before = deepcopy(state)
    revision = h.core.db.execute("SELECT revision FROM permission_bootstrap WHERE compartment='work'").fetchone()[0]
    op = {'op': 'revoke_assignment', 'assignment_id': target, 'issuer': 'owner', 'reason': 'Withdraw scheduling',
        'explanation': 'Withdraw scheduling',
        'evidence': {key: [job['source_id']] for key in ['issuer', 'assignment_id', 'reason']}}
    with pytest.raises(PermissionProposalRejected):
        h.updater._validate({'revision': revision, 'considered_restrictions': [], 'operations': [op]},
            state, source_prefix(h), revision, new_source_id=job['source_id'])
    assert state == before and h.updater._state() == before


def test_wrong_subject_grant_cannot_be_substituted(h):
    h.policy = lambda p: [{**h.grant(p, recipient='linda'), 'subject': h.actor(p, 'outsider'),
        'information_scope': 'Scheduling'}]
    h.turn('Linda may read my scheduling.', actor='outsider')
    h.seq += 1
    job = h.updater.ingest(PermissionSourceEvent(str(h.seq), 'owner', 'connector',
        "Revoke Linda's scheduling access."))
    before = deepcopy(h.updater._state())
    revision = h.core.db.execute("SELECT revision FROM permission_bootstrap WHERE compartment='work'").fetchone()[0]
    # Bypass the model's projected subject filter to test the canonical guard.
    ops = [{'op': 'revoke_assignment', 'assignment_id': aid, 'issuer': 'owner',
        'reason': 'Withdraw scheduling', 'explanation': 'Withdraw scheduling',
        'evidence': {key: [job['source_id']] for key in ['issuer', 'assignment_id', 'reason']}}
        for aid, a in before['assignment'].items() if a['participant'] == 'linda']
    assert len(ops) == 2
    with pytest.raises(PermissionProposalRejected):
        h.updater._validate({'revision': revision, 'considered_restrictions': [], 'operations': ops},
            before, source_prefix(h), revision, new_source_id=job['source_id'])
    assert h.updater._state() == before


def test_publication_failure_rolls_back_all_recipient_changes(h, monkeypatch):
    from agentic_memory.privacy.governance import PolicyGovernor
    before = deepcopy(grants(h))
    runtime = [tuple(r) for r in h.core.db.execute('SELECT * FROM assignments ORDER BY id')]
    original = PolicyGovernor.activate
    calls = []
    def fail_second(self, *args, **kwargs):
        calls.append(1)
        result = original(self, *args, **kwargs)
        if len(calls) == 2:
            raise RuntimeError('Injected second-recipient publication failure')
        return result
    monkeypatch.setattr(PolicyGovernor, 'activate', fail_second)
    h.policy = revoke_policy(h, ['linda', 'jules'])
    with pytest.raises(RuntimeError, match='Injected'):
        h.turn("Revoke Linda and Jules's scheduling access.")
    assert len(calls) == 2 and grants(h) == before
    assert [tuple(r) for r in h.core.db.execute('SELECT * FROM assignments ORDER BY id')] == runtime


def test_rules_are_v19_only(h):
    assert revocation.enabled(h.updater)
    for version in ['18', '20', '21']:
        assert not revocation.enabled(type('Updater', (), {'version': version})())


def test_omitting_one_explicitly_requested_recipient_does_not_publish(h):
    h.policy = revoke_policy(h, ['linda'])
    before = deepcopy(grants(h))
    with pytest.raises(PermissionRepairExhausted):
        h.turn("Revoke Linda and Jules's scheduling access.")
    assert grants(h) == before


def test_multiple_staff_grants_only_matching_scope_is_affected(h):
    h.policy = lambda p: [h.grant(p, recipient='jules', scope='Billing')]
    h.turn('Jules may read my billing only.')
    h.policy = lambda p: [h.grant(p, recipient='jules', scope='Scheduling logistics')]
    h.turn('Jules may read my scheduling logistics.')
    before = deepcopy(grants(h))
    all_jules = revoke_policy(h, ['jules'])
    h.policy = lambda p: [op for op in all_jules(p)
        if 'scheduling' in revocation.field_set(
            p['permission_state']['assignment'][op['assignment_id']]['information_scope'])]
    h.turn("Revoke Jules's scheduling access.")
    changed = {aid for aid, a in grants(h).items() if a != before[aid]}
    assert len(changed) == 2
    assert all(before[aid]['participant'] == 'jules' and
        'scheduling' in revocation.field_set(before[aid]['information_scope']) for aid in changed)
    assert all(a == before[aid] for aid, a in grants(h).items() if aid not in changed)


def test_an_unresolved_recipient_reference_is_released_never_guessed(h):
    # Superseded Batch 4 policy retained a lasting hold here. The agreed policy
    # releases the unresolved reference after the bounded assessment.
    before = deepcopy(grants(h))
    h.policy = revoke_policy(h, ['jules'])
    with pytest.raises(PermissionRepairExhausted):
        h.turn("Revoke my father's scheduling access.")
    job = h.core.db.execute('SELECT id FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0]
    h.updater.skip_exhausted(job, max_attempts=2)
    from agentic_memory.privacy.control_holds import rows
    final = next(r for r in h.updater.control_assessments() if r['job_id'] == job)['finalization']
    assert final['uncertainty_released'] and not final['verified_controls'] and not rows(h.core, 'work')
    assert grants(h) == before  # the hostile Jules revoke is never published
    h.policy = lambda p: []
    h.turn('Continue processing.')
    assert grants(h) == before
    assert len(texts(h, 'linda')) == len(texts(h, 'jules')) == 2


@pytest.mark.parametrize('extra', ['suspend_assignment', 'propose_assignment'])
def test_alternative_effects_cannot_bypass_partial_revocation(h, extra):
    base = revoke_policy(h, ['linda'])
    def hostile(p):
        if extra == 'suspend_assignment':
            jules = next(aid for aid, a in p['permission_state']['assignment'].items()
                if a['participant'] == h.actor(p, 'jules'))
            additional = {'op': extra, 'assignment_id': jules,
                'evidence': [p['new_source_id']], 'reason': 'Suspend the active worker too.'}
        else:
            additional = h.grant(p, recipient='linda', scope='Scheduling')
        return base(p) + [additional]
    h.policy = hostile
    before = deepcopy(grants(h))
    with pytest.raises(PermissionRepairExhausted):
        h.turn("Revoke Linda's scheduling access.")
    assert grants(h) == before


def test_scope_exclusions_are_not_treated_as_permissions():
    assert not revocation.overlaps({'information_scope': 'Billing only; no scheduling'},
        {'kind': 'field', 'value': 'scheduling'})
    with pytest.raises(PermissionProposalRejected):
        revocation.overlaps({'information_scope': 'Unspecified information'},
            {'kind': 'field', 'value': 'scheduling'})


def test_self_asserted_relationship_does_not_resolve_another_subject(h):
    prefix = source_prefix(h)
    prefix = {sid: body for sid, body in prefix.items() if body['text'] != 'My mother is Linda.'}
    prefix['claim'] = {'text': "I'm her mom, Linda.",
        'source': {'participant': 'linda', 'source_identity': 'connector'}}
    prefix['request'] = {'text': "Revoke my mother's scheduling access.",
        'source': {'participant': 'owner', 'source_identity': 'connector'}}
    with pytest.raises(PermissionProposalRejected, match='relationship'):
        revocation.requests(h.updater, prefix, 'request')


def test_contradictory_relationship_evidence_remains_unresolved(h):
    h.turn('Linda is not my mother.')
    before = deepcopy(grants(h))
    h.policy = revoke_policy(h, ['linda'])
    with pytest.raises(PermissionRepairExhausted):
        h.turn("Revoke my mother's scheduling access.")
    assert grants(h) == before


def test_compound_billing_and_scheduling_fact_cannot_leak_a_revoked_time(h):
    h.classify = lambda p, s: 'instruction'
    h.turn('The insurance consultation is Tuesday at 9:20 AM.')
    assert any('insurance consultation' in t for t in texts(h, 'linda'))
    h.classify = lambda p, s: 'context'
    h.policy = revoke_policy(h, ['linda'])
    h.turn("Revoke Linda's scheduling access.")
    assert texts(h, 'linda') == ['The billing invoice is 40 dollars.']
    outgoing = h.model.calls[-1]
    if 'revocation_resolution' in outgoing:
        request = outgoing['revocation_resolution']['requests'][0]
        assert request['requesting_actor'] == request['subject'] == 'p1'
        assert request['recipients'] == ['p2']
        assert set(request['identity_relationship_evidence']) == {'p2'}


def test_first_event_self_revocation_cannot_publish_a_new_ownership_grant():
    h = fixtures.IncrementalTests()
    h.setUp()
    try:
        h.bootstrap = replace(h.bootstrap, owner_read_actions=('search', 'read', 'derive'))
        h.updater = PermissionUpdater(h.core, h.host, 'work', h.bootstrap, h.model, version='19')
        with pytest.raises(PermissionRepairExhausted):
            h.turn('Revoke my access.')
        assert not grants(h)
    finally:
        h.tearDown()


@pytest.mark.parametrize('subject_before,subject_answer,question_subject,expected', [
    ('owner', 'owner', None, 'linda'),
    ('outsider', 'owner', None, None),
    ('owner', 'outsider', None, None),
    ('owner', 'owner', 'outsider', None),
])
def test_bounded_conversational_relationship_needs_subject_corroboration(
        h, subject_before, subject_answer, question_subject, expected):
    def event(actor, text, subject=None):
        return {'text': text, 'source': {'participant': actor, 'source_identity': 'connector', 'subject': subject}}
    prefix = {'one': event(subject_before, 'I am here for help.'),
        'two': event('linda', "I'm her mom, Linda. I can help today."),
        'three': event('jules', 'Do you want your mother to receive information, or only logistical details?', question_subject),
        'four': event(subject_answer, 'Only logistics. She can hear appointment times, but not my results.'),
        'five': event('owner', "Revoke my mother's scheduling access.")}
    if expected:
        request = revocation.requests(h.updater, prefix, 'five')[0]
        assert request['recipients'] == [expected]
        assert {p['source_id'] for p in request['identity_relationship_evidence'][expected]} == {'one','two','three','four'}
    else:
        with pytest.raises(PermissionProposalRejected, match='relationship'):
            revocation.requests(h.updater, prefix, 'five')
