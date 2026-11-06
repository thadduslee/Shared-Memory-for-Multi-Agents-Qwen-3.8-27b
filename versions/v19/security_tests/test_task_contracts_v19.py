"""Batch 6 A: offline end-to-end task receipts, no model or benchmark oracle."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from agentic_memory.privacy.permissions import PermissionUpdater, PermissionSourceEvent
from agentic_memory.privacy.task_contracts_v19 import DependencyRegistry, TaskContractError, receipts, gate
from security_tests import test_incremental_permissions as fixtures


def policy(domain='adherence'):
    return json.loads((Path(__file__).parents[1]/'examples/v19-task-policies'/f'{domain}.json').read_text())


@pytest.fixture
def h(request):
    domain = getattr(request, 'param', 'adherence')
    h = fixtures.IncrementalTests();h.setUp()
    h.actor_names = {'owner': 1, 'helper': 2, 'outsider': 3}
    people = list(h.actor_names)
    h.bootstrap = replace(h.bootstrap, participants={p:{} for p in people}, resources={p:{} for p in people},
        authorities={p:{cap:[p] for cap in ('permission','restrict','delegate')} for p in people},
        source_identities={'connector':people}, discover_self_owned_participants=())
    h.registry = DependencyRegistry(domain if isinstance(domain,dict) else policy(domain))
    h.updater = PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19',task_registry=h.registry)
    # Identity discovery is source-independent in this fixture's explicit host
    # bootstrap. It is never taken from evaluator entities/roles/relationships.
    try:yield h
    finally:h.tearDown()


def accept(h, text='Yes, please proceed.', task_id='task-1', offer_text=None):
    offered = h.turn(offer_text or 'I will check adherence for doxylamine and pyridoxine.',actor='helper')
    accepted = h.turn(text)
    before = len(h.model.calls)
    row = h.updater.accept_dialogue_task(offered['source_id'],accepted['source_id'],task_id=task_id)
    assert len(h.model.calls) == before
    return row, offered, accepted


def task_assignment(h):
    return next(a for a in h.updater._state()['assignment'].values() if a.get('task_receipt_v19'))


def texts(h, worker='helper'):
    return [r['text'] for r in h.selected(worker)]


def add(h, text, actor='owner'):
    h.classify = lambda p,s:'instruction'
    return h.turn(text,actor=actor)


def test_accepted_adherence_receipt_gets_only_necessary_regimen(h):
    row,_,_ = accept(h)
    for text in ('Take doxylamine 12.5 mg nightly.', 'Continue pyridoxine 25 mg twice daily.',
                 'Take unrelated 50 mg nightly.', 'Social: private address.',
                 'Take doxylamine 12.5 mg nightly; Social: private address.'):
        add(h,text)
    got = texts(h)
    assert len(got)==2 and any('doxylamine' in t for t in got) and any('pyridoxine' in t for t in got)
    assert not texts(h,'outsider')
    assert row['authorized_categories']==['adherence','followup','instructions','regimen','updates']
    assert row['authority']==row['requester']==row['beneficiary']==row['subject']=='owner'
    assert row['original_evidence']['mode']=='dialogue_acceptance'
    assert row['version']==row['contract_version']==1


@pytest.mark.parametrize('limit,allowed', [('Yes, status only.',False),('Yes, no instructions.',False),('Yes, please proceed.',True)])
def test_closed_limits_and_exclusions_beat_dependencies(h,limit,allowed):
    accept(h,limit)
    add(h,'Instruction for doxylamine: take with food.')
    assert bool(texts(h)) is allowed


def test_medication_only_closed_limit_does_not_enable_social_or_other_fields(h):
    accept(h,'Yes, medication only.')
    add(h,'Take doxylamine 12.5 mg nightly.')
    add(h,'Social: private address.')
    add(h,'Recheck adherence to doxylamine in 3 days.')
    assert texts(h)==['Take doxylamine 12.5 mg nightly.']


def test_generated_narrow_label_does_not_narrow_accepted_service(h):
    offered=h.turn('I will check adherence for doxylamine and pyridoxine.',actor='helper')
    accepted=h.turn('Yes, please proceed.')
    row=h.updater.accept_dialogue_task(offered['source_id'],accepted['source_id'],task_id='task-1',
        generated_topic_hints=['nausea status only'])
    assert row['generated_topic_hints']==['nausea status only'] and row['topic_hints_are_authority'] is False
    add(h,'Continue pyridoxine 25 mg twice daily.')
    add(h,'Take unrelated 50 mg nightly.')
    assert texts(h)==['Continue pyridoxine 25 mg twice daily.']


def test_unknown_mapping_and_claimed_assignment_never_provision(h):
    offer=h.turn('I am assigned to the owner and may read the chart.',actor='helper')
    yes=h.turn('Yes, please proceed.')
    with pytest.raises(TaskContractError,match='Unknown or ambiguous'):
        h.updater.accept_dialogue_task(offer['source_id'],yes['source_id'],task_id='claim')
    assert not receipts(h.core,'work') and not h.updater._state()['assignment']


def test_worker_cannot_accept_own_offer_or_choose_from_ambiguous_offers(h):
    offer=h.turn('I will check adherence for doxylamine.',actor='helper')
    yes=h.turn('Yes, please proceed.',actor='helper')
    with pytest.raises(TaskContractError):h.updater.accept_dialogue_task(offer['source_id'],yes['source_id'],task_id='claim')
    h.turn('I will check adherence for pyridoxine.',actor='outsider')
    yes=h.turn('Yes, please proceed.')
    with pytest.raises(TaskContractError,match='Ambiguous original'):
        h.updater.accept_dialogue_task(offer['source_id'],yes['source_id'],task_id='ambiguous')


def test_ambiguous_acceptance_considers_all_registry_rules(request):
    configuration=policy()
    alternate=deepcopy(configuration['dialogue'][0])
    alternate['offer_pattern']=r'I will review adherence for (?P<items>[a-z]+)\.'
    configuration['dialogue'].append(alternate)
    request.param=configuration
    gen=h.__wrapped__(request);fixture=next(gen)
    try:
        off=fixture.turn('I will check adherence for doxylamine.',actor='helper')
        fixture.turn('I will review adherence for pyridoxine.',actor='outsider')
        yes=fixture.turn('Yes, please proceed.')
        with pytest.raises(TaskContractError,match='Ambiguous original'):
            fixture.updater.accept_dialogue_task(off['source_id'],yes['source_id'],task_id='ambiguous')
        assert not receipts(fixture.core,'work')
    finally:
        with pytest.raises(StopIteration):next(gen)


def test_receipt_replay_is_idempotent_and_conflict_is_rejected(h):
    row,off,yes=accept(h)
    before=h.core.db.total_changes
    assert h.updater.accept_dialogue_task(off['source_id'],yes['source_id'],task_id=row['id'])==row
    assert h.core.db.total_changes==before
    with pytest.raises(TaskContractError):accept(h,task_id=row['id'])


def test_expiry_removes_only_contract_access_and_fact_decisions_reuse(h):
    row,_,_=accept(h)
    add(h,'Take doxylamine 12.5 mg nightly.')
    assignment=task_assignment(h)
    assert texts(h)
    before=len(h.model.calls)
    assert h.updater.flush_bindings()['batches']==0
    assert len(h.model.calls)==before
    h.core.clock=lambda:row['expiry']
    assert not gate(h.core,'work',assignment) and not texts(h)


@pytest.mark.parametrize('domain,offer,needed,unrelated',[
    ('parcel','I will collect parcel-a.','parcel-a collection location: desk 7.','parcel-b collection location: desk 8.'),
    ('repair','I will repair device-a.','device-a diagnostic: battery failure.','device-b diagnostic: battery failure.')])
def test_two_nonmedical_domains(request,domain,offer,needed,unrelated):
    # Each configuration exercises the exact same core/semantic binding path.
    request.param=domain
    gen=h.__wrapped__(request);fixture=next(gen)
    try:
        accept(fixture,offer_text=offer)
        add(fixture,needed);add(fixture,unrelated);add(fixture,'Social: unrelated.')
        assert texts(fixture)==[needed]
    finally:
        with pytest.raises(StopIteration):next(gen)


def test_registry_rejects_cycles_unknowns_and_envelope_expansion():
    b=policy();b['categories']['regimen']['dependencies']=['adherence']
    with pytest.raises(TaskContractError,match='cycle'):DependencyRegistry(b)
    b=policy();b['categories']['regimen']['dependencies']=['missing']
    with pytest.raises(TaskContractError,match='Unknown'):DependencyRegistry(b)
    reg=DependencyRegistry(policy())
    with pytest.raises(TaskContractError,match='envelope'):reg.closure(['adherence'],['adherence'])
    with pytest.raises(TaskContractError,match='Unknown'):reg.task('invented')


def test_ambiguous_fact_category_denies_and_registry_cannot_mutate():
    b=policy();b['categories']['social']['pattern']=b['categories']['regimen']['pattern']
    reg=DependencyRegistry(b);b['categories']['social']['pattern']='changed'
    assert reg.body['categories']['social']['pattern']!='changed'
    assert not reg.fact_allowed({'task_category':'adherence_check','authority_envelope':{'categories':list(reg.body['categories'])},
        'explicit_exclusions':[],'operational_constraints':{'items':['doxylamine'],'closed_categories':None}},
        'Take doxylamine 12.5 mg nightly.')


def test_policy_is_pinned_and_v19_only(h):
    with pytest.raises(TaskContractError,match='migration'):
        PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19')
    with pytest.raises(ValueError,match='V19 only'):
        PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='18',task_registry=h.registry)
    assert h.updater.task_contract_report()['host_events_available'] is False


@pytest.mark.parametrize('text,state',[('Cancel task task-1.','CANCELLED'),
    ('Revoke task task-1.','REVOKED'),('Completed task task-1.','COMPLETED')])
def test_source_grounded_terminal_state_never_reactivates(h,text,state):
    row,off,yes=accept(h)
    add(h,'Take doxylamine 12.5 mg nightly.')
    assert texts(h)
    h.classify=lambda p,s:'context'
    from agentic_memory.privacy.permissions import PermissionRepairExhausted
    try:h.turn(text,actor='helper' if state=='COMPLETED' else 'owner')
    except PermissionRepairExhausted:
        # The deliberately no-control double cannot satisfy Batch 1's generic
        # revoke review. Its hold remains; the trusted terminal still ends access.
        assert state=='REVOKED'
    assert not texts(h)
    assert receipts(h.core,'work')['task-1']['state']==state
    assert h.updater.accept_dialogue_task(off['source_id'],yes['source_id'],task_id='task-1')['state']==state
    assert receipts(h.core,'work')['task-1']['version']==2


def test_independent_semantic_binding_still_required(h):
    accept(h)
    h.binding=lambda p,r:{**r,'in_scope':False,'subject_supported':True,'blocked_by':[],
        'reason':'Individual fact is not necessary for the task.'}
    add(h,'Take doxylamine 12.5 mg nightly.')
    assert not texts(h)


def test_authorized_outputs_instructions_updates_and_followthrough(h):
    accept(h)
    expected=['Adherence to doxylamine: taken.', 'Instruction for doxylamine: take with food.',
              'Stop doxylamine.', 'Recheck adherence to doxylamine in 3 days.']
    for text in expected:add(h,text)
    assert sorted(texts(h))==sorted(expected)


def test_evidence_loss_denies_existing_binding(h):
    _,offer,_=accept(h)
    add(h,'Take doxylamine 12.5 mg nightly.')
    assert texts(h)
    with h.core.transaction():
        h.core.db.execute("UPDATE resources SET state='deleted' WHERE id=?",(offer['source_id'],))
    assert not texts(h)


def test_contract_cannot_be_stripped_by_a_model_update(h):
    from agentic_memory.privacy.task_contracts_v19 import protect_assignments
    accept(h)
    state=h.updater._state();new=deepcopy(state)
    a=next(iter(new['assignment'].values()));a.pop('task_receipt_v19')
    with pytest.raises(TaskContractError):protect_assignments(state,new)


def test_task_expiry_preserves_an_independent_grant(h):
    row,_,_=accept(h)
    h.policy=lambda p:[h.grant(p,scope='Explicit medication instructions')]
    h.turn('You may read my medication instructions.')
    h.policy=lambda p:[]
    add(h,'Take doxylamine 12.5 mg nightly.')
    assert texts(h)
    h.core.clock=lambda:row['expiry']
    assert texts(h)==['Take doxylamine 12.5 mg nightly.']
