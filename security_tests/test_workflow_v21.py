"""V21 causal, privacy, interoperability and no-turn-deadline regressions."""
from dataclasses import replace
from io import BytesIO
import json
import pytest

from agentic_memory.privacy import (
    AccessDenied, PermissionUpdater, MemoryService, JSONClient,
    in_process_transport, ScopedDeduplicator,
)
from agentic_memory.privacy import control_holds
from agentic_memory.privacy.retrieval_v21 import RetrievalConfig, configure, pack
from security_tests import test_incremental_permissions as fixtures
from security_tests import test_workflow_v20 as shared


@pytest.fixture
def owned():
    h = fixtures.IncrementalTests()
    h.setUp()
    h.bootstrap = replace(h.bootstrap, owner_read_actions=('search', 'read', 'derive'))
    h.updater = PermissionUpdater(h.core, h.host, 'work', h.bootstrap, h.model, version='21')
    try:
        yield h
    finally:
        h.tearDown()


# Run the same semantic regressions under V21's distinct scheduling policy.
test_personal_ownership_and_reference_contract = shared.test_own_facts_have_stable_subject_ids_and_no_role_shortcut
test_host_reassesses_original_question = shared.test_host_recovers_original_question_without_new_authority_statement
test_old_recovery_cannot_override_revocation = shared.test_historical_recovery_does_not_reactivate_later_revoked_grant
test_unknown_attribution_stays_protected = shared.test_unknown_subject_does_not_create_cartesian_binding_work
test_mixed_subject_fact_requires_both = shared.test_mixed_person_fact_requires_both_subjects_not_speaker_ownership
test_valid_facts_survive_invalid_controls = shared.test_valid_facts_survive_invalid_control_reference
test_bad_bindings_do_not_create_control_hold = shared.test_bad_binding_section_does_not_create_control_hold
test_subject_mixup_cannot_use_ownership = shared.test_independent_attribution_review_rejects_speaker_subject_mixup
test_accepted_service_keeps_original_receipts = shared.test_accepted_worker_service_retains_original_receipts_and_followthrough
test_long_episode_reuses_semantic_receipts = shared.test_long_episode_reuses_review_and_keeps_model_context_bounded
test_control_recovery_cannot_reextract_old_facts = shared.test_control_reassessment_does_not_reextract_historical_facts
test_stable_people_not_display_names = shared.test_identical_display_names_do_not_merge_authenticated_people
test_redundant_grants_do_not_repeat_reviews = shared.test_existing_materialized_grant_avoids_redundant_same_person_reviews


def test_turn_longer_than_30_seconds_completes_without_deadline(owned, monkeypatch):
    h = owned
    clock = [0.]
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    original = h.model.handler
    def slow(payload):
        assert getattr(h.model, '_deadline', None) is None
        clock[0] += 41.
        return original(payload)
    h.model.handler = slow
    h.classify = lambda p, s: 'instruction'
    job = h.turn('The meeting is Tuesday at four.')
    assert h.core.db.execute('SELECT status FROM permission_jobs WHERE id=?', (job['job_id'],)).fetchone()[0] == 'complete'
    assert len(h.model.calls) <= 2
    assert h.selected('owner')
    assert not control_holds.blocks(h.core, 'work', 'owner')
    assert clock[0] > 30


def test_semantic_repair_budget_survives_restart(owned):
    h = owned
    shared.fail_and_quarantine(h)
    h.model.handler = lambda p: (_ for _ in ()).throw(ValueError('still malformed'))
    h.updater.service_pending(max_batches=1)
    from agentic_memory.privacy import PrivacyMemorySystem
    from agentic_memory.privacy.processors import HashEmbedder
    h.core.close()
    h.core = PrivacyMemorySystem(h.path, HashEmbedder(), clock=lambda: '2026-10-31T00:00:00+00:00')
    h.model.core = h.core
    h.host = h.core.issue_session('host', 'ingest', actions=['manage_policy','ingest','read','search','derive','audit','write','delete','link'],
        compartments=['work'], processing_routes=['test:hash','test:semantic','test:answer'])
    h.core.register_processor(h.host, h.model)
    h.updater = PermissionUpdater(h.core, h.host, 'work', h.bootstrap, h.model, version='21')
    h.updater.service_pending(max_batches=1)
    before = len(h.model.calls)
    h.updater.service_pending(max_batches=1)
    assert len(h.model.calls) == before
    assert control_holds.blocks(h.core, 'work', 'owner')


def test_two_agent_registry_and_wsgi_identity_cannot_be_forged(owned):
    h = owned
    service = MemoryService(h.updater)
    token_a = service.register_authenticated_client('owner', 'agent-a', connector='connector', actions=['ingest','read','search','derive'])
    token_b = service.register_authenticated_client('owner', 'different-platform-b')
    stranger = service.register_authenticated_client('outsider', 'agent-c')
    h.actor_names['owner'] = 1
    h.classify = lambda p, s: 'instruction'
    a = JSONClient(token_a, in_process_transport(service))
    b = JSONClient(token_b, in_process_transport(service))
    assert a.request('ingest', event_id='1', text='My appointment is Tuesday at four.')['status'] == 'complete'
    assert b.request('search', query='appointment')
    assert service.dispatch(stranger, 'search', {'query': 'appointment'}) == []
    with pytest.raises(ValueError):
        service.dispatch(stranger, 'search', {'query': 'appointment', 'principal_id': 'owner'})
    with pytest.raises(AccessDenied):
        b.request('diagnostics')
    with pytest.raises(AccessDenied):
        b.request('delete', resource_id=b.request('search', query='appointment')[0]['id'])
    body = json.dumps({'operation': 'search', 'payload': {'query': 'appointment'}}).encode()
    response = []
    output = service.wsgi_app({'REQUEST_METHOD': 'POST', 'HTTP_AUTHORIZATION': 'Bearer '+token_b,
        'CONTENT_LENGTH': str(len(body)), 'wsgi.input': BytesIO(body)}, lambda status, headers: response.append(status))
    assert response == ['200 OK']
    assert json.loads(output[0])
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(service.dispatch, token_b, 'search', {'query':'appointment'})
        future_b = executor.submit(service.dispatch, stranger, 'search', {'query':'appointment'})
        assert future_a.result()
        assert future_b.result() == []
    service.revoke_client(token_b)
    with pytest.raises(AccessDenied):
        b.request('search', query='appointment')


def test_delegated_agent_revocation_blocks_stale_release(owned):
    h = owned
    h.setup_access()
    h.classify = lambda p, s: 'instruction'
    h.turn('Pickup is Tuesday at four.')
    service = MemoryService(h.updater)
    token = service.register_authenticated_client('helper', 'external-agent')
    rows = service.dispatch(token, 'search', {'query': 'pickup'})
    assert rows
    ctx = service._clients[token][0]
    h.policy = lambda p: [{'op':'revoke_assignment','assignment_id':next(
        aid for aid,a in p['permission_state']['assignment'].items() if a['participant']==h.actor(p,'helper') and a['subject']==h.actor(p,'owner')),
        'authority_source':p['new_source_id'],'reason':'Owner revokes pickup access.'}]
    h.turn('Revoke helper access.')
    assert service.dispatch(token, 'search', {'query': 'pickup'}) == []
    with pytest.raises(AccessDenied):
        h.core.release(ctx, [r['id'] for r in rows], 'cached answer')


def test_exact_dedup_retains_sources_and_differing_doses(owned):
    h = owned
    h.classify = lambda p, s: 'instruction'
    h.turn('Take acetaminophen 650 mg as needed.')
    h.turn('Take acetaminophen 650 mg as needed.')
    h.turn('Take acetaminophen 500 mg as needed.')
    h.turn('Do not take acetaminophen 650 mg.')
    raw = h.core.db.execute("SELECT count(*) FROM fact_candidates WHERE state='active'").fetchone()[0]
    rows = h.selected('owner')
    assert raw == 4
    assert len(rows) == 3
    assert any('500 mg' in r['text'] for r in rows)
    assert any('Do not' in r['text'] for r in rows)
    assert not h.selected('outsider')


def test_scoped_semantic_dedup_is_bounded_and_invalidates_on_source_deletion(owned):
    h = owned
    h.classify = lambda p, s: 'instruction'
    h.turn('The appointment is Tuesday at four.')
    h.turn('The appointment is on Tuesday at four.')
    from agentic_memory.privacy.processors import ScriptedProcessor
    model = ScriptedProcessor(response={'0': True})
    model.processor_id = 'test:semantic'
    # Use an already policy-approved route; registry approval alone cannot
    # expand the immutable inputs' processor ceilings.
    ctx = h.core.issue_session('host', 'dedup', actions=['read','search','derive','audit','manage_policy'],
        compartments=['work'], processing_routes=['test:hash','test:semantic'])
    h.core.register_processor(h.host, model)
    views = [r[0] for r in h.core.db.execute("SELECT id FROM resources WHERE kind='memory' AND id LIKE 'note:permission-view:%'")]
    result = ScopedDeduplicator(h.core).review(ctx, views[-1], model, limit=1)
    assert result['comparisons'] == 1
    assert len(model.calls) == 1
    assert len(h.selected('owner')) == 1
    source = h.updater.fact_candidates()[-1]['source_id']
    h.core.delete(h.host, source)
    assert h.core.db.execute("SELECT count(*) FROM resources WHERE id IN (?,?) AND state='active'", views).fetchone()[0] == 1
    # Whole-observation deletion also requires the kernel's authority
    # reassessment. Equivalence must never resurrect its erased input.
    assert all(source not in r['id'] for r in h.selected('owner'))
    # A caller without management authority cannot compare the protected store.
    with pytest.raises(AccessDenied):
        ScopedDeduplicator(h.core).review(h.reader('outsider'), views[-1], model)


def test_packer_never_slices_fact_or_exceeds_budget():
    evidence = [{'id':'a','text':'x'*500}, {'id':'b','text':'Do not take it if symptoms worsen.'}]
    selected, used = pack(evidence, 150)
    assert selected == [evidence[1]]
    assert used <= 150


@pytest.mark.parametrize('values', [{'tag_weight': float('nan')}, {'recency_weight': -1},
                                  {'max_context_tokens': 0}, {'rerank_candidates': 25}])
def test_retrieval_config_rejects_invalid_bounds(values):
    with pytest.raises(ValueError):
        RetrievalConfig(**values)


def test_cheap_tag_ablation_adds_no_model_calls(owned):
    h = owned
    h.classify = lambda p, s: 'instruction'
    h.turn('The appointment is Tuesday at four.')
    before = len(h.model.calls)
    configure(h.updater, RetrievalConfig(tag_weight=0))
    assert h.selected('owner')
    configure(h.updater, RetrievalConfig(tag_weight=.12))
    assert h.selected('owner')
    assert len(h.model.calls) == before


def test_optional_reranker_uses_one_batch_and_cannot_add_a_fact(owned):
    from agentic_memory.privacy.retrieval_v21 import rerank
    from agentic_memory.privacy.processors import ScriptedProcessor
    h = owned
    h.classify = lambda p, s: 'instruction'
    h.turn('The appointment is Tuesday at four.')
    h.turn('Bring the printed referral.')
    ctx = h.reader('owner')
    packet = h.core.retrieve(ctx, 'appointment referral', k=24)
    model = ScriptedProcessor(response={'order': [1, 0]})
    model.processor_id = 'test:semantic'
    h.core.register_processor(h.host, model)
    result = rerank(h.core, ctx, packet, 'appointment referral', model, 2)
    assert len(model.calls) == 1
    assert result['input_ids'] == packet['input_ids']
    assert result['evidence'] == packet['evidence'][::-1]
    model.response = {'order': [0, 0]}
    with pytest.raises(Exception):
        rerank(h.core, ctx, packet, 'appointment referral', model, 2)


def test_recognized_service_cannot_silently_disappear_on_reference_repair():
    from agentic_memory.privacy.workflow_v21 import validate_repair_continuity
    from agentic_memory.privacy.permissions import PermissionProposalRejected
    original = {'control_review':[{'index':0,'recipient':'worker','intents':['service']}]}
    replacement = {'control_review':[{'index':0,'recipient':None,'intents':['none']}]}
    error = PermissionProposalRejected('Wrong worker grant', {'required_effect':'grant'})
    with pytest.raises(PermissionProposalRejected):
        validate_repair_continuity(original, replacement, error)
    replacement['control_review'][0]['intents'] = ['service']
    validate_repair_continuity(original, replacement, error)


def test_service_event_replay_is_stable_across_agents_and_time(owned):
    h = owned
    service = MemoryService(h.updater)
    one = service.register_authenticated_client('owner', 'a', connector='connector', actions=['ingest'])
    two = service.register_authenticated_client('owner', 'b', connector='connector', actions=['ingest'])
    h.actor_names['owner'] = 1
    h.classify = lambda p, s: 'instruction'
    request = {'event_id':'caller-1', 'text':'My appointment is Tuesday at four.'}
    assert service.dispatch(one, 'ingest', request)['status'] == 'complete'
    calls = len(h.model.calls)
    h.core.clock = lambda: '2026-11-02T01:00:00+00:00'
    assert service.dispatch(two, 'ingest', request)['status'] == 'complete'
    assert len(h.model.calls) == calls
    with pytest.raises(ValueError):
        service.dispatch(two, 'ingest', {**request, 'text':'Changed immutable event'})


def test_service_answer_never_exposes_host_diagnostics(owned):
    h = owned
    h.classify = lambda p, s: 'instruction'
    h.turn('My appointment is Tuesday at four.')
    service = MemoryService(h.updater)
    token = service.register_authenticated_client('owner', 'answer-client')
    output = service.dispatch(token, 'answer', {'query':'When is my appointment?'})
    assert output['action'] == 'answer'
    assert 'Tuesday' in output['answer']
    assert 'input_manifest' not in output
    assert 'diagnosis_v21' not in output


def test_v21_partial_answer_correction_needs_no_second_prose_call(owned):
    from security_tests.test_answering_v20 import configure
    h = owned
    h.classify = lambda p, s: 'instruction'
    h.turn('Continue the project preparation; the old Monday meeting was canceled.')
    service, model = configure(h)
    model.missing = True
    result = service.answer('owner', 'What is my current plan and other information?', model)
    assert result['output']['action'] == 'answer'
    assert 'project preparation' in result['output']['answer']
    assert "don't have the remaining" in result['output']['answer']
    assert 'Unverified prose' not in result['output']['answer']
    assert [p['stage'] for p in model.calls] == ['answer', 'policy_classification']


def test_provider_timeout_retries_same_source_without_spending_semantic_budget(owned):
    from agentic_memory.privacy.processors import ProcessorDeadlineFailure
    h = owned
    h.classify = lambda p, s: 'instruction'
    original = h.model.handler
    attempts = [0]
    def timeout_once(payload):
        attempts[0] += 1
        assert getattr(h.model, '_deadline', None) is None
        if attempts[0] == 1:
            raise ProcessorDeadlineFailure('Provider request timed out')
        return original(payload)
    h.model.handler = timeout_once
    with pytest.raises(ProcessorDeadlineFailure):
        h.turn('My appointment is Tuesday at four.')
    job = h.core.db.execute('SELECT id,status FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()
    assert job['status'] not in {'complete','quarantined','skipped'}
    assert h.updater.update(job['id'])['status'] == 'complete'
    assert attempts[0] == 3  # One transport failure, two semantic responses.
    assert h.selected('owner')


def test_binding_transport_failure_is_retryable_not_a_semantic_rejection(owned):
    from agentic_memory.privacy.processors import ProcessorFailure
    h = owned
    h.classify = lambda p, s: 'instruction'
    original = h.model.handler
    attempts = [0]
    def timeout_on_review(payload):
        attempts[0] += 1
        if attempts[0] == 2:
            raise ProcessorFailure('Transient provider transport timeout')
        return original(payload)
    h.model.handler = timeout_on_review
    with pytest.raises(ProcessorFailure):
        h.turn('My appointment is Tuesday at four.')
    job = h.core.db.execute('SELECT id,status FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()
    assert job['status'] not in {'complete', 'quarantined', 'skipped'}
    assert h.updater.update(job['id'])['status'] == 'complete'
    assert attempts[0] == 3
    assert h.selected('owner')


def test_host_recovery_transport_timeout_preserves_semantic_allowance(owned):
    from agentic_memory.privacy.processors import ProcessorFailure
    from agentic_memory.privacy.workflow_features import pending
    h = owned
    h.classify = lambda p, s: 'instruction'
    h.turn('My appointment is Tuesday at four.')
    shared.fail_and_quarantine(h)
    original = h.model.handler
    h.model.handler = lambda p: (_ for _ in ()).throw(ProcessorFailure('Provider transport timeout'))
    for _ in range(2):
        with pytest.raises(ProcessorFailure):
            h.updater.service_pending(max_batches=1)
        job = next(j for j in pending(h.updater) if j['kind'] == 'control')
        assert job['status'] == 'pending'
        assert job['attempts'] == 0
        assert control_holds.blocks(h.core, 'work', 'owner')
    h.model.handler = original
    assert h.updater.service_pending(max_batches=1)['calls'] == 1
    assert not control_holds.blocks(h.core, 'work', 'owner')
    assert h.selected('owner')


def test_binding_maintenance_transport_timeouts_do_not_suppress_pending_pairs(owned):
    from agentic_memory.privacy.processors import ProcessorFailure
    h = owned
    h.classify = lambda p, s: 'instruction'
    original = h.model.handler
    def missing_reviews(payload):
        response = original(payload)
        response['binding_reviews'] = []
        return response
    h.model.handler = missing_reviews
    h.turn('My appointment is Tuesday at four.')
    assert not h.selected('owner')
    h.model.handler = lambda p: (_ for _ in ()).throw(ProcessorFailure('Provider transport timeout'))
    for _ in range(2):
        with pytest.raises(ProcessorFailure):
            h.updater.flush_bindings(max_batches=1)
    h.model.handler = original
    assert h.updater.flush_bindings(max_batches=1)['batches'] == 1
    assert h.selected('owner')


def test_v21_erasure_preserves_unrelated_fields_and_blocks_restatement(owned):
    from security_tests.test_selective_memory_v17 import test_erasure_preserves_unrelated_readiness_and_blocks_restated_value
    test_erasure_preserves_unrelated_readiness_and_blocks_restated_value(owned)


def test_v21_future_erasure_enforced_at_release_before_sweep(owned):
    from security_tests.test_selective_memory_v17 import test_future_forgetting_waits_and_is_enforced_before_sweep
    test_future_forgetting_waits_and_is_enforced_before_sweep(owned)


def test_v21_conditional_erasure_requires_fresh_authority(owned):
    from security_tests.test_selective_memory_v17 import test_conditional_forgetting_needs_fresh_authority_completion
    test_conditional_forgetting_needs_fresh_authority_completion(owned)


def test_v21_retained_reviews_do_not_bypass_revocation_after_erasure(owned):
    from security_tests.test_selective_memory_v17 import test_retained_reviews_never_bypass_subsequent_revocation
    test_retained_reviews_never_bypass_subsequent_revocation(owned)
