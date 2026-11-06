"""V20 local regressions. All completions are scripted; no network access."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from agentic_memory.privacy import AccessDenied, PrivacyMemorySystem
from agentic_memory.privacy.permissions import PermissionUpdater, PermissionSourceEvent, PermissionRepairExhausted
from agentic_memory.privacy.processors import HashEmbedder, ModelContractError
from agentic_memory.privacy import control_holds, incremental
from agentic_memory.privacy.workflow_v20 import pending, semantic_context
from security_tests import test_incremental_permissions as fixtures


@pytest.fixture
def owned():
    h=fixtures.IncrementalTests();h.setUp()
    h.bootstrap=replace(h.bootstrap,owner_read_actions=('search','read','derive'))
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='20')
    try:yield h
    finally:h.tearDown()


def fail_and_quarantine(h,text='And I should keep the current instructions?'):
    handler=h.model.handler
    def invalid(p):raise ModelContractError('Invalid reference: expected an ownership grant, not an activity')
    h.model.handler=invalid
    h.seq+=1
    h.actor_names.setdefault('owner',len(h.actor_names)+1)
    job=h.updater.ingest(PermissionSourceEvent(str(h.seq),'owner','connector',text))
    with pytest.raises(PermissionRepairExhausted):h.updater.update(job['job_id'])
    h.updater.skip_exhausted(job['job_id'],max_attempts=2)
    h.model.handler=handler
    return job


def reopen(h):
    h.core.close()
    h.core=PrivacyMemorySystem(h.path,HashEmbedder(),clock=lambda:'2026-10-31T00:00:00+00:00')
    h.model.core=h.core
    h.host=h.core.issue_session('host','ingest',actions=['manage_policy','ingest','read','search','derive','audit','write','delete','link'],
        compartments=['work'],processing_routes=['test:hash','test:semantic','test:answer'])
    h.core.register_processor(h.host,h.model)
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='20')


def test_own_facts_have_stable_subject_ids_and_no_role_shortcut(owned):
    h=owned;h.classify=lambda p,s:'instruction'
    before=len(h.model.calls)
    h.turn('The project meeting is Tuesday at four.')
    assert len(h.model.calls)-before<=2
    candidates=h.updater.fact_candidates()
    assert candidates[0]['subjects']==['owner']
    assert h.selected('owner')
    assert not h.selected('outsider')
    assert all('legal_operation_references' in p for p in h.model.calls if p['stage']=='combined_incremental')


def test_host_recovers_original_question_without_new_authority_statement(owned):
    h=owned;h.classify=lambda p,s:'instruction'
    h.turn('My project meeting is Tuesday at four.')
    job=fail_and_quarantine(h)
    assert control_holds.blocks(h.core,'work','owner')
    assert not h.selected('owner')
    result=h.updater.service_pending(seconds=5)
    assert result['calls']==1
    assert not control_holds.blocks(h.core,'work','owner')
    assert h.selected('owner')
    row=h.core.db.execute('SELECT attempts,status FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()
    assert tuple(row)==(2,'quarantined')
    assert pending(h.updater)[0]['status']=='complete'


def test_recovery_budget_survives_restart_and_stops(owned):
    h=owned;job=fail_and_quarantine(h)
    def invalid(p):raise ModelContractError('still invalid')
    h.model.handler=invalid
    assert h.updater.service_pending()['status']=='pending'
    reopen(h)
    assert h.updater.service_pending()['status']=='needs_review'
    before=len(h.model.calls)
    h.updater.service_pending()
    assert len(h.model.calls)==before
    assert pending(h.updater)[0]['attempts']==2
    assert control_holds.blocks(h.core,'work','owner')


def test_historical_recovery_does_not_reactivate_later_revoked_grant(owned):
    h=owned;h.setup_access()
    job=fail_and_quarantine(h,'You may read the project schedule.')
    aid=next(aid for aid,a in h.updater.inspect()['assignment'].items() if a['participant']=='helper' and a['subject']=='owner')
    h.policy=lambda p:[{'op':'revoke_assignment','assignment_id':next(
        aid for aid,a in p['permission_state']['assignment'].items() if a['participant']==h.actor(p,'helper') and a['subject']==h.actor(p,'owner')),
        'authority_source':p['new_source_id'],'reason':'The owner withdraws this recipient access.'}]
    h.turn('Revoke helper access.')
    h.policy=lambda p:[h.grant(p)]
    result=h.updater.service_pending()
    assert result['status']=='pending'
    assert h.updater.inspect()['assignment'][aid]['status']=='revoked'
    assert not h.selected('helper')
    assert control_holds.blocks(h.core,'work','owner')


def test_cache_ignores_bookkeeping_but_invalidates_real_scope_change():
    a={'id':'grant','participant':'worker','subject':'person','authority':'person',
       'basis':'explicit','actions':['read'],'information_scope':'schedule','status':'active',
       'inference_version':'20','scope_evidence':[{'source_id':'original','text':'Read the schedule.'}],
       'bindings':[],'explanation':'first','evidence':{'information_scope':['original']}}
    state={'activity':{},'assignment':{'grant':a},'restriction':{}}
    first=incremental.pair_key({'id':'fact'},a,state)
    a['bindings'].append({'source_id':'new'})
    a['explanation']='A longer explanation'
    a['evidence']['information_scope'].append('unrelated-bookkeeping')
    assert incremental.pair_key({'id':'fact'},a,state)==first
    a['information_scope']='schedule and contact'
    assert incremental.pair_key({'id':'fact'},a,state)!=first


def test_unknown_subject_does_not_create_cartesian_binding_work(owned):
    h=owned
    original=h.respond
    def unknown(p):
        d=original(p)
        for s in d.get('segments',[]):
            s.update(subject=None,subjects=[],subject_source=None,subject_quote=None)
        return d
    h.classify=lambda p,s:'instruction';h.model.handler=unknown
    h.turn('A meeting is Tuesday; the person is not identified.')
    sources={r[0]:None for r in h.core.db.execute("SELECT id FROM resources WHERE kind='observation'")}
    assert incremental.pairs(h.updater,incremental.facts.rows(h.updater),h.updater.inspect(),sources)==[]
    assert not h.selected('owner')
    assert pending(h.updater)[0]['kind']=='attribution'


def test_mixed_person_fact_requires_both_subjects_not_speaker_ownership(owned):
    h=owned;h.turn('Hello.',actor='helper')
    h.classify=lambda p,s:'instruction'
    original=h.respond
    def mixed(p):
        d=original(p)
        for s in d.get('segments',[]):
            if s['kind']=='instruction':s['subjects']=[h.actor(p,'owner'),h.actor(p,'helper')]
        return d
    h.model.handler=mixed
    h.turn('Alex has a project meeting and Jordan has a private interview.')
    h.updater.flush_bindings(max_batches=1)
    assert not h.selected('owner')
    assert not h.selected('helper')
    assert not h.selected('outsider')


def test_recovery_tick_zero_budget_makes_no_call(owned):
    h=owned;fail_and_quarantine(h)
    before=len(h.model.calls)
    assert h.updater.service_pending(seconds=0)['status']=='deferred'
    assert len(h.model.calls)==before


def rejected_sections(h,*,bad_controls=False,bad_bindings=False):
    from agentic_memory.privacy.incremental_wire import encode_fixture
    from agentic_memory.privacy.processors import ProcessorContractFailure
    original=h.model.complete
    captured={}
    def wire(p):
        result=encode_fixture(h.respond(p),p)
        if bad_controls:result['p']=[{'o':'activity','i':'not-a-legal-grant'}]
        if bad_bindings:result['b']=[{'f':'not-a-fact','g':'not-a-grant','d':'allow','q':'bad','r':'Invalid reference'}]
        captured['draft']=result
        return result
    h.model.handler=wire
    def complete(system,payload,*,schema=None):
        try:return original(system,payload,schema=schema)
        except ModelContractError as exc:
            failure=ProcessorContractFailure('Structured output has an invalid reference')
            failure.details={'rejected_output':captured['draft']}
            raise failure from exc
    h.model.complete=complete


def test_valid_facts_survive_invalid_control_reference(owned):
    h=owned;h.turn('Hello.')
    h.classify=lambda p,s:'instruction'
    rejected_sections(h,bad_controls=True)
    h.seq+=1
    job=h.updater.ingest(PermissionSourceEvent(str(h.seq),'owner','connector','Keep the project deadline on Friday.'))
    with pytest.raises(PermissionRepairExhausted):h.updater.update(job['job_id'])
    assert any('project deadline' in p['quote'] for p in h.updater.fact_candidates())
    assert h.core.db.execute('SELECT status FROM fact_extractions WHERE job_id=?',(job['job_id'],)).fetchone()[0]=='complete'
    assert not h.selected('outsider')


def test_bad_binding_section_does_not_create_control_hold(owned):
    h=owned;h.turn('Hello.')
    h.classify=lambda p,s:'instruction'
    rejected_sections(h,bad_bindings=True)
    h.turn('Keep the project deadline on Friday.')
    assert not control_holds.rows(h.core,'work')
    assert any('project deadline' in p['quote'] for p in h.updater.fact_candidates())
    assert not h.selected('owner')  # The malformed review never authorizes a fact.


def test_independent_attribution_review_rejects_speaker_subject_mixup(owned):
    h=owned;h.classify=lambda p,s:'instruction'
    def binding(p,r):
        pair=deepcopy(r)
        pair.update(in_scope=True,subject_supported=False,blocked_by=[],
                    reason='This is another person information, not the speaker personal fact.')
        return pair
    h.binding=binding
    h.turn('The other person has a confidential interview tomorrow.')
    assert not h.selected('owner')
    assert any(p['stage']=='incremental_binding_repair' for p in h.model.calls)


def test_accepted_worker_service_retains_original_receipts_and_followthrough(owned):
    from agentic_memory.privacy.incremental_wire import encode_fixture
    h=owned
    h.turn('May I coordinate your project delivery?',actor='helper')
    def accept(p):
        result=encode_fixture(h.respond(p),p)
        worker=h.actor(p,'helper');subject=h.actor(p,'owner')
        earlier=next(s['id'] for s in p['sources'] if s['source']['participant']==worker)
        result['c']['0'].update(intents=['service'],recipient=worker)
        result['p']=[{'o':'service','i':'new','s':subject,'p':worker,
            'e':p['new_source_id'],'q':earlier,'v':{},'work':'Coordinate project delivery',
            'wf':['initial delivery location'],'r':'Subject accepts this worker project coordination.'}]
        return result
    h.model.handler=accept
    h.turn('Yes, coordinate my project delivery.')
    worker=next(a for a in h.updater.inspect()['assignment'].values()
                if a['participant']=='helper' and a['subject']=='owner')
    assert 'not a closed field list' in worker['information_scope']
    assert worker['accepted_service']['worker']=='helper'
    assert worker['accepted_service']['subject_source_id']
    assert len([a for a in h.updater.inspect()['assignment'].values()
                if a['participant']=='owner' and a['subject']=='owner'])==1
    h.model.handler=h.respond;h.classify=lambda p,s:'instruction'
    h.turn('Delivery instructions: arrive Friday; the old Monday slot is canceled.',actor='helper')
    assert any('old Monday slot is canceled' in r['text'] for r in h.selected('helper'))
    assert not h.selected('outsider')


def test_long_episode_reuses_review_and_keeps_model_context_bounded(owned):
    h=owned;h.classify=lambda p,s:'instruction'
    h.turn('The project appointment is Friday at four.')
    before=h.core.db.execute("SELECT snapshot,body FROM fact_processing WHERE job_id LIKE 'incremental-reviews:%'").fetchall()
    h.classify=lambda p,s:'context'
    sizes=[]
    for i in range(102):
        n=len(h.model.calls)
        h.turn('Acknowledged.')
        assert len(h.model.calls)-n==1
        sizes.append(len(json.dumps(h.model.calls[-1])))
    after=h.core.db.execute("SELECT snapshot,body FROM fact_processing WHERE job_id LIKE 'incremental-reviews:%'").fetchall()
    assert [tuple(r) for r in before]==[tuple(r) for r in after]
    assert max(sizes[-10:])<=1.3*max(sizes[:10])
    assert h.selected('owner')


@pytest.mark.parametrize('scheduled',[False,True])
def test_v20_exact_erasure_preserves_other_facts_and_blocks_stale_release(owned,scheduled):
    from security_tests.test_selective_memory_v17 import erase
    h=owned;h.classify=lambda p,s:'instruction'
    h.turn('Pickup Tuesday at four. Use entrance code 4519.')
    reader=h.reader('owner')
    packet=h.core.retrieve(reader,'entrance code')
    if scheduled:
        erase(h,'4519',timing='at',effective_at='2026-11-03T18:00:00+00:00')
        assert any('4519' in r['text'] for r in h.selected('owner'))
        h.core.clock=lambda:'2026-11-03T18:00:00+00:00'
    else:erase(h,'4519')
    assert [r['text'] for r in h.selected('owner')]==['Pickup Tuesday at four.']
    with pytest.raises(AccessDenied):h.core.release(reader,packet['input_ids'],{'answer':'stale code'})
    if scheduled:h.updater.process_scheduled_forgetting()
    h.policy=lambda p:[];h.classify=lambda p,s:'instruction'
    h.turn('Deletion confirmed: the previous code was 4519.')
    assert all('4519' not in r['text'] for r in h.selected('owner'))


def test_recovering_old_fact_cannot_resurrect_later_erased_value(owned):
    from security_tests.test_selective_memory_v17 import erase
    h=owned;h.classify=lambda p,s:'instruction'
    h.turn('Pickup Tuesday at four. Use entrance code 4519.')
    fail_and_quarantine(h,'Keep entrance code 4519 for the delivery.')
    erase(h,'4519')
    h.policy=lambda p:[];h.classify=lambda p,s:'instruction'
    result=h.updater.service_pending()
    assert result['status']=='complete'
    assert all('4519' not in r['text'] for r in h.selected('owner'))
    assert any('Pickup Tuesday' in r['text'] for r in h.selected('owner'))


def test_interrupted_child_is_not_replayed_as_foreground_work(owned,monkeypatch):
    h=owned;h.classify=lambda p,s:'instruction';h.turn('The project appointment is Friday.')
    fail_and_quarantine(h)
    update=h.updater.update
    def crash(job_id,**kwargs):raise KeyboardInterrupt()
    monkeypatch.setattr(h.updater,'update',crash)
    with pytest.raises(KeyboardInterrupt):h.updater.service_pending()
    assert pending(h.updater)[0]['attempts']==1
    assert h.updater.pending_jobs()==[]
    monkeypatch.setattr(h.updater,'update',update)
    h.classify=lambda p,s:'context';h.turn('Acknowledged.')
    assert control_holds.blocks(h.core,'work','owner')
    assert h.updater.service_pending()['status']=='complete'
    assert pending(h.updater)[0]['attempts']==2
    assert not control_holds.blocks(h.core,'work','owner')
    children=list(h.core.db.execute("SELECT status FROM permission_jobs WHERE id LIKE 'host-recovery:%' ORDER BY rowid"))
    assert [r[0] for r in children]==['quarantined','complete']


def test_compartment_file_lock_respects_host_wait_budget(owned,monkeypatch):
    import fcntl
    from pathlib import Path
    from agentic_memory.privacy.system import digest
    from agentic_memory.privacy.processors import ProcessorDeadlineFailure
    h=owned
    path=Path(str(h.core.privacy_path)+'.v20-'+digest('work')[:16]+'.lock')
    clock=[100.]
    monkeypatch.setattr('time.monotonic',lambda:clock[0])
    monkeypatch.setattr('time.sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    with path.open('a+b') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        before=len(h.model.calls)
        with pytest.raises(ProcessorDeadlineFailure):h.updater.service_pending(seconds=.05)
        assert len(h.model.calls)==before
        assert clock[0]<=100.06
        fcntl.flock(handle,fcntl.LOCK_UN)


def wire_fault(h,mutate):
    from agentic_memory.privacy.incremental_wire import encode_fixture
    from agentic_memory.privacy.processors import ProcessorContractFailure
    original=h.model.complete
    captured={}
    def respond(p):
        canonical=h.respond(p)
        if 'pair_context' in p:canonical={'binding_reviews':canonical['binding_reviews']}
        draft=encode_fixture(canonical,p)
        mutate(draft,p)
        captured['draft']=draft
        return draft
    h.model.handler=respond
    def complete(system,payload,*,schema=None):
        try:return original(system,payload,schema=schema)
        except ModelContractError as exc:
            error=ProcessorContractFailure('One independent section is invalid')
            error.details={'rejected_output':captured['draft']}
            raise error from exc
    h.model.complete=complete


def test_independent_fact_slots_survive_and_host_finishes_missing_extraction(owned):
    h=owned;h.turn('Hello.');h.classify=lambda p,s:'instruction'
    def broken(draft,p):
        if isinstance(draft.get('f'),dict) and draft['f']:
            draft['f'][list(draft['f'])[-1]].pop('attribution_reason',None)
    wire_fault(h,broken)
    h.turn('Project appointment is Friday. Arrival is at three.')
    quotes=[p['quote'] for p in h.updater.fact_candidates()]
    assert 'Project appointment is Friday.' in quotes
    assert 'Arrival is at three.' not in quotes
    assert not control_holds.rows(h.core,'work')
    assert any(p['kind']=='extraction' for p in pending(h.updater))
    original=h.model.complete
    def finish(system,payload,*,schema=None):
        if payload.get('stage')=='extraction_recovery':
            person=next(iter(payload['resources']))
            return {key:{'kind':'instruction','subject':person,'subjects':[person],
                'subject_source':list(payload['sources'])[-1],'reason':'The original source identifies the owner.',
                'topics':[],'relationships':[]} for key in payload['segments']}
        return original(system,payload,schema=schema)
    h.model.complete=finish
    result=h.updater.service_pending()
    assert result['status']=='complete',pending(h.updater)
    assert 'Arrival is at three.' in [p['quote'] for p in h.updater.fact_candidates()]


def test_valid_focused_rows_survive_one_malformed_verdict(owned):
    h=owned;h.turn('Hello.');h.classify=lambda p,s:'instruction'
    def broken(draft,p):
        if p['stage']=='incremental_binding_repair':
            assert isinstance(draft['b'],dict)
            draft['b'][list(draft['b'])[-1]].pop('r')
    wire_fault(h,broken)
    h.turn('Project appointment is Friday. Arrival is at three.')
    assert any('Project appointment' in r['text'] for r in h.selected('owner'))
    assert all('Arrival is at three' not in r['text'] for r in h.selected('owner'))
    assert not control_holds.rows(h.core,'work')


def test_budget_decline_does_not_spend_a_semantic_recovery_attempt(owned,monkeypatch):
    from agentic_memory.privacy.processors import ProcessorBudgetFailure
    h=owned;h.turn('Hello.');fail_and_quarantine(h)
    original=h.model.complete
    def budget(*a,**kw):raise ProcessorBudgetFailure('HTTP 402')
    monkeypatch.setattr(h.model,'complete',budget)
    with pytest.raises(ProcessorBudgetFailure):h.updater.service_pending()
    assert pending(h.updater)[0]['attempts']==0
    monkeypatch.setattr(h.model,'complete',original)
    assert h.updater.service_pending()['status']=='complete'
    assert pending(h.updater)[0]['attempts']==1
    assert not control_holds.blocks(h.core,'work','owner')


def test_recovery_and_hold_resolution_commit_atomically(owned,monkeypatch):
    h=owned;h.turn('Hello.');fail_and_quarantine(h)
    original=h.updater.update
    def crash_after_commit(*a,**kw):
        original(*a,**kw)
        raise KeyboardInterrupt()
    monkeypatch.setattr(h.updater,'update',crash_after_commit)
    with pytest.raises(KeyboardInterrupt):h.updater.service_pending()
    assert pending(h.updater)[0]['status']=='complete'
    assert not control_holds.blocks(h.core,'work','owner')


def test_source_recovery_budget_is_shared_by_all_queue_kinds(owned):
    from agentic_memory.privacy.workflow_v20 import enqueue,recovery_available
    h=owned;job=fail_and_quarantine(h)
    control=pending(h.updater)[0]
    with h.core.transaction():h.core.db.execute('UPDATE host_recovery SET attempts=2 WHERE id=?',(control['id'],))
    key=enqueue(h.updater,job['job_id'],kind='attribution')
    item=next(p for p in pending(h.updater) if p['id']==key)
    assert recovery_available(h.updater,item) is False
    assert next(p for p in pending(h.updater) if p['id']==key)['status']=='needs_review'


def test_attribution_recovery_restores_opaque_ids_without_new_grants(owned):
    h=owned;h.classify=lambda p,s:'instruction'
    def unknown(p):
        result=h.respond(p)
        for s in result['segments']:s.update(subject=None,subjects=[],subject_source=None,subject_quote=None)
        result['binding_reviews']=[]
        return result
    h.model.handler=unknown
    h.turn('My project appointment is Friday.')
    grants=set(h.updater.inspect()['assignment'])
    original=h.model.complete
    def attribution(system,payload,*,schema=None):
        if payload.get('stage')=='attribution_recovery':
            person=next(iter(payload['resources']))
            assert person.startswith('p') and person!='owner'
            return {key:{'subjects':[person],'source_id':next(reversed(payload['sources']))}
                    for key in schema['properties']}
        return original(system,payload,schema=schema)
    h.model.complete=attribution
    assert h.updater.service_pending()['status']=='complete'
    assert h.updater.fact_candidates()[0]['subjects']==['owner']
    assert set(h.updater.inspect()['assignment'])==grants
    h.model.handler=h.respond
    outcome=h.updater.service_pending()
    assert h.selected('owner'),outcome
    assert not h.selected('outsider')


def test_late_custom_processor_result_is_discarded_before_recovery_publication(owned,monkeypatch):
    from agentic_memory.privacy.processors import ProcessorDeadlineFailure
    h=owned;h.turn('Hello.');fail_and_quarantine(h)
    clock=[100.]
    monkeypatch.setattr('time.monotonic',lambda:clock[0])
    original=h.model.complete
    def late(*a,**kw):
        result=original(*a,**kw)
        clock[0]+=6.
        return result
    monkeypatch.setattr(h.model,'complete',late)
    with pytest.raises(ProcessorDeadlineFailure):h.updater.service_pending(seconds=5)
    assert control_holds.blocks(h.core,'work','owner')
    assert pending(h.updater)[0]['status']=='pending'


def test_identical_display_names_do_not_merge_authenticated_people(owned):
    h=owned;h.classify=lambda p,s:'instruction'
    def identified(p):
        result=h.respond(p)
        if p.get('segments'):
            current=next(s for s in p['sources'] if s['id']==p['new_source_id'])
            for d in result['segments']:
                d.update(subject=current['source']['participant'],subjects=[current['source']['participant']],
                    subject_source=current['id'],subject_quote=current['text'])
        return result
    h.model.handler=identified
    h.turn('I am Alex Lee. My project meeting is Friday.',actor='owner')
    h.turn('I am Alex Lee. My project meeting is Monday.',actor='helper')
    first=' '.join(r['text'] for r in h.selected('owner'))
    second=' '.join(r['text'] for r in h.selected('helper'))
    assert 'Friday' in first and 'Monday' not in first
    assert 'Monday' in second and 'Friday' not in second


def test_numeric_person_tag_cannot_rewrite_the_protocol_version():
    payload={'permission_version':'20','prompt_version':'20','input_contract':'raw-stream-v1',
             'subject':'20','text':'20','event_id':'20','subjects':['1','20']}
    mapped=incremental.remap(payload,{'1':'p1','20':'p2'})
    assert mapped['permission_version']=='20' and mapped['prompt_version']=='20'
    assert mapped['subject']=='p2' and mapped['subjects']==['p1','p2']
    assert mapped['text']=='20' and mapped['event_id']=='20'


def test_adaptive_review_batches_fit_pending_window(owned,monkeypatch):
    h=owned
    monkeypatch.setattr('time.monotonic',lambda:100.)
    assert incremental.binding_limit(h.updater)==8
    h.model._deadline=105.
    assert incremental.binding_limit(h.updater)==3
    h.model._deadline=102.
    assert incremental.binding_limit(h.updater)==1
    h.model._deadline=101.
    assert h.updater.flush_bindings(max_batches=1)['status']=='deferred_deadline'
    assert not h.model.calls


def test_source_does_not_pay_for_discarded_provisional_reviews(owned):
    h=owned;h.classify=lambda p,s:'instruction'
    h.turn('My project meeting is Friday at four.')
    main=h.model.calls[0]
    assert main['binding_review_scope']=='independent_focused_call_only'
    assert main['required_pairs']==[]
    assert 'own_binding_assignments' not in main
    assert any('pair_context' in p for p in h.model.calls)
    assert h.selected('owner')
    assert len(h.model.calls)<=2


def test_control_reassessment_does_not_reextract_historical_facts(owned):
    h=owned;h.classify=lambda p,s:'instruction'
    h.turn('My project meeting is Friday at four.')
    job=fail_and_quarantine(h,'Should I continue my current plan?')
    before=len(h.updater.fact_candidates());calls=len(h.model.calls)
    h.updater.service_pending(seconds=5)
    recovery=h.model.calls[calls:]
    assert len(recovery)==1 and recovery[0]['control_reassessment_only'] is True
    assert recovery[0]['segments']==[]
    assert len(h.updater.fact_candidates())==before
    assert not control_holds.blocks(h.core,'work','owner')
    assert not hasattr(h.updater,'_control_reassessment_only')


def test_existing_materialized_grant_avoids_redundant_same_person_reviews(owned):
    h=owned;h.setup_access();h.classify=lambda p,s:'instruction'
    h.turn('Pickup is Friday at four.')
    state=h.updater.inspect()
    aid,a=next((aid,a) for aid,a in state['assignment'].items()
        if a['participant']=='helper' and a['subject']=='owner')
    state['assignment']['overlapping-grant']={**deepcopy(a),'id':'overlapping-grant','bindings':[]}
    sources={r[0]:None for r in h.core.db.execute("SELECT id FROM resources WHERE kind='observation'")}
    work=incremental.pairs(h.updater,h.updater.fact_candidates(),state,sources)
    assert not any(p['assignment_id']=='overlapping-grant' for p in work)
    assert h.selected('helper')
