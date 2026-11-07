"""Intent preservation and grounded task acceptance; scripted, offline V19."""
from copy import deepcopy
from dataclasses import replace
import json

import jsonschema
import pytest

from agentic_memory.privacy import PrivacyMemorySystem, AccessDenied
from agentic_memory.privacy.permissions import PermissionUpdater, PermissionRepairExhausted, PermissionSourceEvent, PermissionProposalRejected
from agentic_memory.privacy.processors import HashEmbedder
from agentic_memory.privacy.processors import ModelContractError
from agentic_memory.privacy import acceptance_v19 as acceptance
from security_tests import test_incremental_permissions as fixtures


class Model(fixtures.SemanticModel):
    def complete(self, system, payload, *, schema=None):
        base = deepcopy(schema)
        base.get('properties',{}).pop('intent_repair',None)
        base['required'] = [k for k in base.get('required',[]) if k != 'intent_repair']
        result = super().complete(system,payload,schema=base)
        if 'c' in result:
            result = self.edit(payload,result)
        try:jsonschema.validate(result,schema)
        except jsonschema.ValidationError as exc:raise ModelContractError('Invalid scripted service repair contract') from exc
        return result


@pytest.fixture
def h():
    h = fixtures.IncrementalTests();h.setUp()
    h.model = Model();h.model.core=h.core
    h.model.handler=lambda p:h.respond({**p,'stage':'combined_incremental'}
        if p['stage']=='incremental_repair' else p)
    h.model.edit=lambda p,r:r
    h.core.register_processor(h.host,h.model)
    h.actor_names={'owner':1,'tess':2,'pat':3,'other':4}
    people=list(h.actor_names)
    h.bootstrap=replace(h.bootstrap,participants={p:{} for p in people},resources={p:{} for p in people},
        source_identities={'connector':people},discover_self_owned_participants=(),
        authorities={p:{cap:[p] for cap in ('permission','restrict','delegate')} for p in people})
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19')
    h.turn('My name is Tess.',actor='tess')
    h.turn('My name is Pat.',actor='pat')
    h.turn('My name is Jonah.',actor='owner')
    try:yield h
    finally:h.tearDown()


def grants(h, worker=None):
    return {aid:a for aid,a in h.updater.inspect()['assignment'].items()
        if worker is None or a['participant']==worker}


def test_literal_permission_does_not_require_a_worker_offer_even_if_labeled_service(h):
    statement='Tess may read my appointment time and location until 2026-11-01T00:00:00+00:00.'
    def edit(p,r):
        if not any(s['text']==statement and s['id']==p['new_source_id'] for s in p['sources']):
            return r
        r['c']['0'].update(intents=['service'],recipient=h.actor(p,'tess'))
        r['p']=[{'o':'fields','i':'new','p':h.actor(p,'tess'),'s':h.actor(p,'owner'),
            'e':p['new_source_id'],'quote':statement,'v':{'until':'2026-11-01T00:00:00+00:00'},
            'r':'The subject explicitly authorizes only these fields.'}]
        return r
    h.model.edit=edit
    job=h.turn(statement)
    assert not h.updater.acceptance_state()
    assert h.core.db.execute('SELECT status FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0]=='complete'
    assert len(grants(h,'tess'))==1 and not grants(h,'pat')
    h.model.edit=lambda p,r:r;h.classify=lambda p,s:'instruction'
    h.turn('My appointment is Tuesday at 4 PM.')
    assert any('Tuesday' in r['text'] for r in h.selected('tess'))
    assert not h.selected('pat')
    h.core.clock=lambda:'2026-11-02T00:00:00+00:00'
    assert not h.selected('tess')


def test_field_operation_cannot_disguise_acceptance_as_literal_read_permission(h):
    def edit(p,r):
        statement=next(s['text'] for s in p['sources'] if s['id']==p['new_source_id'])
        r['c']['0'].update(intents=['service'],recipient=h.actor(p,'tess'))
        r['p']=[{'o':'fields','i':'new','p':h.actor(p,'tess'),'s':h.actor(p,'owner'),
            'e':p['new_source_id'],'quote':statement,'v':{},'r':'Unsupported literal permission.'}]
        return r
    h.model.edit=edit
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.',max_attempts=1)
    assert not grants(h,'tess')
    assert h.updater.acceptance_state()[0]['state']=='PENDING_RECIPIENT_RESOLUTION'


def test_literal_permission_can_repair_a_mistaken_service_operation(h):
    calls=[]
    statement='Tess may read my appointment time and location only.'
    def edit(p,r):
        calls.append(p)
        r['c']['0'].update(intents=['grant'],recipient=h.actor(p,'tess'))
        if len(calls)==1:
            worker=next(s for s in p['sources'] if s['source']['participant']==h.actor(p,'tess'))
            r['p']=[{'o':'activity','via':'response','i':'new','wi':'new','s':h.actor(p,'owner'),
                'e':p['new_source_id'],'q':worker['id'],'p':h.actor(p,'tess'),
                'work':'appointment scheduling','wf':['appointment times'],'v':{},'wv':{},
                'r':'Incorrectly treating literal permission as service acceptance.'}]
        else:
            r['p']=[{'o':'fields','i':'new','p':h.actor(p,'tess'),'s':h.actor(p,'owner'),
                'e':p['new_source_id'],'quote':statement,'v':{},'r':'Literal subject permission.'}]
        return r
    h.model.edit=edit
    h.turn(statement)
    assert len(calls)==2 and not h.updater.acceptance_state()
    assert len(grants(h,'tess'))==1


def offer(h, actor='tess', text='I can arrange your Tuesday appointment scheduling.'):
    return h.turn(text,actor=actor)


def edit_acceptance(h, *, invalid_first=False, drop=False, proof=None, worker='tess',
                    service='appointment scheduling', new_grant=True):
    calls=[]
    def edit(p,r):
        if not any('make that' in s['quote'] or 'That works' in s['quote'] or 'accept' in s['quote']
                or 'please proceed' in s['quote'].lower() for s in p['control_segments']):
            return r
        calls.append(deepcopy(p))
        repairing=p.get('correction',{}).get('mode')=='reference_repair'
        is_drop=drop and repairing
        actual=h.actor(p,worker)
        r['c']={str(s['index']):{'intents':['none' if is_drop else 'service'],
            'recipient':None if is_drop else actual,'existing':None,'r':'Subject accepts the offered service.'}
            for s in p['control_segments']}
        r['p']=[]
        if invalid_first and not repairing:
            # Structurally valid reference, semantically the wrong recipient.
            r['c']['0']['existing']=next(aid for aid,a in p['permission_state']['assignment'].items()
                if a['participant']!=actual)
        elif new_grant and not is_drop:
            candidates=p['task_acceptance_context']['outstanding_offers']
            typed=[o for o in candidates if o['service'] in acceptance.service_fields(service)]
            if typed:candidates=typed
            days=[d for d in ('Monday','Tuesday','Wednesday','Thursday','Friday')
                if any(d.casefold() in s['quote'].casefold() for s in p['control_segments'])]
            if days:candidates=[o for o in candidates if all(d.casefold() in o['offer']['quote'].casefold() for d in days)]
            found=next(o for o in candidates if o['worker']==actual)
            # Offer context retains canonical opaque history refs; the wire q
            # uses the matching source alias from the authenticated source list.
            source=next(s['id'] for s in p['sources'] if s['text']==found['offer']['quote']
                and s['source']['participant']==actual)
            r['p']=[{'o':'activity','via':'response','i':'new','wi':'new',
                'p':actual,'s':h.actor(p,'owner'),'e':p['new_source_id'],'q':source,
                'work':service,'wf':['pickup instructions'] if 'pickup' in service else ['appointment times','parking instructions'],
                'v':{},'wv':{},
                'r':'Authenticated subject accepts this worker original offer.'}]
        if repairing:
            r['intent_repair']=proof or {'outcome':'REFERENCE_REPAIR','reason':'Correct the recipient grant reference.',
                'contrary_evidence':[]}
        return r
    h.model.edit=edit
    return calls


def test_recognized_acceptance_survives_wrong_recipient_grant_repair(h):
    h.policy=lambda p:[h.grant(p,recipient='pat',scope='Scheduling')]
    h.turn('Pat may read my scheduling.')
    h.policy=lambda p:[]
    offer(h)
    calls=edit_acceptance(h,invalid_first=True)
    h.classify=lambda p,s:'instruction'
    before=len(h.model.calls)
    h.turn('I can make that.')
    assert len(h.model.calls)-before==2 and len(calls)==2
    assert calls[1]['correction']['mode']=='reference_repair'
    assert calls[1]['task_acceptance_context']['interpretations'][0]['original']['intent']=='SERVICE_ACCEPTANCE'
    rows=h.updater.acceptance_state();assert len(rows)==1
    row=rows[0]
    assert row['state']=='ACCEPTED' and row['original']['existing_grant']
    assert row['task']['worker']=='tess' and row['task']['requester']==row['task']['beneficiary']==row['task']['subject']=='owner'
    assert row['task']['service']=='scheduling' and row['task']['limits']
    assert grants(h,'tess') and grants(h,'pat')
    assert all(a.get('accepted_service',{}).get('worker')=='tess' for a in grants(h,'tess').values())


def test_silent_intent_drop_is_not_success_and_facts_survive(h):
    h.policy=lambda p:[h.grant(p,recipient='pat')];h.turn('Pat may read pickup information.');h.policy=lambda p:[]
    offer(h)
    edit_acceptance(h,invalid_first=True,drop=True)
    h.classify=lambda p,s:'instruction'
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    assert not grants(h,'tess')
    row=h.updater.acceptance_state()[0]
    assert row['original']['intent']=='SERVICE_ACCEPTANCE' and row['state']=='REFERENCE_REPAIR_REQUIRED'
    assert any(f['quote']=='I can make that.' for f in h.updater.fact_candidates())


def test_multiple_outstanding_offers_are_pending_not_most_recent_worker(h):
    offer(h);offer(h,'pat')
    edit_acceptance(h)
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    row=h.updater.acceptance_state()[0]
    assert row['state']=='PENDING_RECIPIENT_RESOLUTION' and row['task'] is None
    assert {o['worker'] for o in row['candidate_offers']}=={'tess','pat'}
    assert not grants(h)


def test_explicit_recipient_overrides_two_offers_and_model_default(h):
    offer(h);offer(h,'pat')
    edit_acceptance(h,worker='tess')
    h.turn('Tess, I can make that Tuesday appointment.')
    assert grants(h,'tess') and not grants(h,'pat')


def test_wrong_model_recipient_cannot_override_explicit_source(h):
    offer(h);offer(h,'pat')
    edit_acceptance(h,worker='pat')
    with pytest.raises(PermissionRepairExhausted):h.turn('Tess, I can make that Tuesday appointment.')
    assert not grants(h)
    assert h.updater.acceptance_state()[0]['task']['worker']=='tess'


def test_delayed_acceptance_uses_original_offer_beyond_six_turn_window(h):
    offer(h)
    for i in range(8):h.turn(f'Unrelated note {i}.',actor='other')
    edit_acceptance(h)
    h.turn('Tess, I can make that Tuesday appointment.')
    row=h.updater.acceptance_state()[0]
    assert row['state']=='ACCEPTED' and row['task']['offer']['quote'].startswith('I can arrange')
    assert grants(h,'tess')


def test_interleaved_different_services_do_not_steal_schedule_acceptance(h):
    offer(h)
    offer(h,'pat','I can arrange your parcel pickup.')
    edit_acceptance(h)
    h.turn('I can make that.')
    assert grants(h,'tess') and not grants(h,'pat')


def test_cancellation_and_expiry_prevent_acceptance(h):
    offer(h)
    h.turn('I withdraw the appointment scheduling offer.',actor='tess')
    edit_acceptance(h,new_grant=False)
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    assert not grants(h) and h.updater.acceptance_state()[0]['state']=='PENDING_RECIPIENT_RESOLUTION'


def test_worker_title_or_assertion_without_subject_acceptance_grants_nothing(h):
    h.turn('I am the scheduler and have full access to all appointments.',actor='tess')
    assert not grants(h,'tess') and not h.updater.acceptance_state()


def test_explicit_grounded_reassessment_can_reject_false_initial_acceptance(h):
    offer(h)
    proof={'outcome':'SEMANTIC_REASSESSMENT','reason':'The source explicitly declines the offered time.',
        'contrary_evidence':[]}
    edit_acceptance(h,drop=True,new_grant=False,proof=proof)
    original=h.model.edit
    def edit(p,r):
        r=original(p,r)
        if p.get('correction',{}).get('mode')=='reference_repair':
            r['intent_repair']['contrary_evidence']=[{'source_id':p['new_source_id'],'quote':'I cannot make that'}]
        return r
    h.model.edit=edit
    # Keep the scripted first mistaken service classification intentional.
    h.turn('I cannot make that; I do not accept the appointment.')
    row=h.updater.acceptance_state()[0]
    assert row['state']=='SEMANTICALLY_REASSESSED' and row['reassessment']['original']['intent']=='SERVICE_ACCEPTANCE'
    assert row['reassessment']['contrary_evidence'] and not grants(h,'tess')


def test_same_confirmation_is_not_contrary_evidence(h):
    offer(h)
    edit_acceptance(h,drop=True,new_grant=False,proof={'outcome':'SEMANTIC_REASSESSMENT',
        'reason':'Attendance is merely informational.','contrary_evidence':[{'source_id':'unknown','quote':'I can make that.'}]})
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    row=h.updater.acceptance_state()[0]
    assert row['state']!='SEMANTICALLY_REASSESSED' and not grants(h,'tess')


def reopen(h):
    h.core.close()
    h.core=PrivacyMemorySystem(h.path,HashEmbedder(),clock=lambda:'2026-10-31T00:00:00+00:00')
    h.model.core=h.core
    h.host=h.core.issue_session('host','ingest',actions=['manage_policy','ingest','read','search','derive','audit','write','delete','link'],
        compartments=['work'],processing_routes=['test:hash','test:semantic','test:answer'])
    h.core.register_processor(h.host,h.model)
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19')


def test_restart_preserves_pending_intent_and_does_not_reset_two_call_budget(h):
    offer(h);offer(h,'pat');edit_acceptance(h)
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    before=h.updater.acceptance_state()
    jid=before[0]['job_id'];count=len(h.model.calls)
    reopen(h)
    assert h.updater.acceptance_state()==before
    with pytest.raises(PermissionRepairExhausted):h.updater.update(jid)
    assert len(h.model.calls)==count and not grants(h)


def test_later_cancellation_blocks_inflight_release_and_older_recovery(h):
    offer(h);edit_acceptance(h);h.turn('I can make that.')
    h.model.edit=lambda p,r:r;h.classify=lambda p,s:'instruction'
    h.turn('Appointment Tuesday at 9:20 AM.')
    old=h.core.retrieve(h.reader('tess'),'appointment',k=100)
    assert old['input_ids']
    h.classify=lambda p,s:'context'
    h.turn('Cancel my appointment scheduling task with Tess.')
    assert h.updater.acceptance_state()[0]['state']=='CANCELLED'
    with pytest.raises(AccessDenied):h.core.release(h.reader('tess'),old['input_ids'],{'answer':'stale'})
    # A completed old job is terminal; restarting it makes no processor call or grant.
    count=len(h.model.calls);job=h.updater.acceptance_state()[0]['job_id']
    h.updater.update(job)
    assert len(h.model.calls)==count and h.updater.acceptance_state()[0]['state']=='CANCELLED'


def test_one_worker_two_offers_requires_explicit_task_reference(h):
    offer(h);offer(h,text='I can arrange your Friday appointment scheduling.')
    edit_acceptance(h)
    h.turn('Tess, I can make that Friday appointment.')
    task=h.updater.acceptance_state()[0]['task']
    assert 'Friday' in task['offer']['quote']


def test_expired_offer_is_not_outstanding(h):
    offer(h,text='I can arrange your Tuesday appointment scheduling; expires at 2026-10-30T12:00:00+00:00.')
    edit_acceptance(h,new_grant=False)
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    assert not grants(h)


def test_offer_expiry_is_rechecked_at_final_release_without_maintenance(h):
    offer(h,text='I can arrange your Tuesday appointment scheduling; expires at 2026-10-31T12:00:00+00:00.')
    edit_acceptance(h);h.turn('I can make that.')
    h.model.edit=lambda p,r:r;h.classify=lambda p,s:'instruction';h.turn('Appointment Tuesday at 9:20 AM.')
    packet=h.core.retrieve(h.reader('tess'),'appointment',k=100)
    assert packet['input_ids']
    h.core.clock=lambda:'2026-10-31T12:00:01+00:00'
    with pytest.raises(AccessDenied):h.core.release(h.reader('tess'),packet['input_ids'],{'answer':'stale'})


def test_restart_after_one_reference_failure_spends_only_remaining_call(h):
    h.policy=lambda p:[h.grant(p,recipient='pat')];h.turn('Pat may read scheduling.');h.policy=lambda p:[]
    offer(h);edit_acceptance(h,invalid_first=True)
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.',max_attempts=1)
    jid=h.updater.acceptance_state()[0]['job_id'];original=deepcopy(h.updater.acceptance_state()[0]['original'])
    reopen(h);before=len(h.model.calls)
    h.updater.update(jid)
    assert len(h.model.calls)-before==1 and h.updater.acceptance_state()[0]['original']==original
    assert h.updater.acceptance_state()[0]['state']=='ACCEPTED' and grants(h,'tess')


def test_repair_cannot_replace_the_recognized_service(h):
    h.policy=lambda p:[h.grant(p,recipient='pat')];h.turn('Pat may read scheduling.');h.policy=lambda p:[]
    offer(h);edit_acceptance(h,invalid_first=True,service='parcel pickup')
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    assert not grants(h,'tess') and h.updater.acceptance_state()[0]['original']['service']=='scheduling'


def test_revocation_is_not_overwritten_by_an_old_acceptance(h):
    offer(h);edit_acceptance(h);h.turn('I can make that.')
    task=h.updater.acceptance_state()[0];original=deepcopy(task['original'])
    h.model.edit=lambda p,r:r
    def revoke(p):
        aid=next(aid for aid,a in p['permission_state']['assignment'].items() if a['participant']==h.actor(p,'tess'))
        return [{'op':'revoke_assignment','assignment_id':aid,'authority_source':p['new_source_id'],
            'evidence':[p['new_source_id']],'reason':'Withdraw scheduling access from Tess.'}]
    h.policy=revoke;h.turn("Revoke Tess's scheduling access.")
    assert h.updater.acceptance_state()[0]['state']=='REVOKED'
    h.policy=lambda p:[];reopen(h);h.updater.update(task['job_id'])
    assert h.updater.acceptance_state()[0]['original']==original
    assert h.updater.acceptance_state()[0]['state']=='REVOKED'
    assert all(a.get('recipient_revocations') for a in grants(h,'tess').values())


def test_handoff_needs_the_new_workers_own_receipt_and_subject_acceptance(h):
    offer(h,'pat');h.turn('I am handing over appointment scheduling to Tess.',actor='pat')
    offer(h,'tess')
    edit_acceptance(h)
    h.turn('Tess, I can make that Tuesday appointment.')
    task=h.updater.acceptance_state()[0]['task']
    assert task['worker']=='tess' and task['handoff_from'] and task['original_offer']
    assert grants(h,'tess') and not grants(h,'pat')


def test_handoff_assertion_alone_cannot_supply_a_new_worker_grant(h):
    offer(h,'pat');h.turn('I am handing over appointment scheduling to Tess.',actor='pat')
    edit_acceptance(h,new_grant=False)
    with pytest.raises(PermissionRepairExhausted):h.turn('Tess, I can make that Tuesday appointment.')
    assert not grants(h)


def test_pending_outcome_preserves_intent_without_claiming_success(h):
    offer(h);offer(h,'pat')
    edit_acceptance(h,proof={'outcome':'PENDING_RECIPIENT_RESOLUTION','reason':'Two equally supported offers.',
        'contrary_evidence':[]})
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    row=h.updater.acceptance_state()[0]
    assert row['state']=='PENDING_RECIPIENT_RESOLUTION' and not grants(h)
    assert row['history'][-1]['outcome']=='PENDING_RECIPIENT_RESOLUTION'


def test_unrelated_contrary_statement_cannot_retract_current_acceptance(h):
    past=h.turn('I do not accept a parcel pickup.')
    offer(h)
    edit_acceptance(h,drop=True,new_grant=False,proof={'outcome':'SEMANTIC_REASSESSMENT',
        'reason':'A different event declined a different task.',
        'contrary_evidence':[{'source_id':past['source_id'],'quote':'I do not accept a parcel pickup.'}]})
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    assert h.updater.acceptance_state()[0]['state']!='SEMANTICALLY_REASSESSED'


def test_named_other_subject_offer_cannot_be_claimed_by_current_actor(h):
    h.seq+=1
    job=h.updater.ingest(PermissionSourceEvent(str(h.seq),'tess','connector',
        'I can arrange your Tuesday appointment scheduling.',subject='other'))
    h.updater.update(job['job_id']);edit_acceptance(h,new_grant=False)
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    assert not grants(h) and h.updater.acceptance_state()[0]['state']=='PENDING_RECIPIENT_RESOLUTION'


def test_unauthorized_requester_cannot_accept_for_another_information_subject(h):
    offer(h)
    edit_acceptance(h,new_grant=False)
    h.seq+=1
    job=h.updater.ingest(PermissionSourceEvent(str(h.seq),'other','connector','I can make that.',subject='owner'))
    with pytest.raises(PermissionRepairExhausted):h.updater.update(job['job_id'])
    assert not grants(h) and h.updater.acceptance_state()[0]['state']=='PENDING_AUTHORITY_RESOLUTION'


def test_repair_does_not_drop_independent_adverse_controls(h):
    offer(h)
    edit_acceptance(h,new_grant=False)
    base=h.model.edit
    def edit(p,r):
        r=base(p,r)
        r['c']['1']['intents']=['none' if p.get('correction',{}).get('mode')=='reference_repair' else 'deny']
        return r
    h.model.edit=edit
    with pytest.raises(PermissionRepairExhausted):
        h.turn('I can make that. Do not share my billing details.')
    assert not grants(h)
    row=h.updater.acceptance_state()[0]
    assert row['adverse_intents']['1']==['deny']


def test_reading_task_receipts_requires_host_policy_session(h):
    offer(h);edit_acceptance(h);h.turn('I can make that.')
    reader=h.reader('tess')
    unauthorized=PermissionUpdater.__new__(PermissionUpdater)
    unauthorized.core=h.core;unauthorized.ctx=reader;unauthorized.compartment='work';unauthorized.version='19'
    with pytest.raises(AccessDenied):unauthorized.acceptance_state()


def test_missing_repair_outcome_cannot_publish_an_informational_fallback(h):
    offer(h);edit_acceptance(h,drop=True,new_grant=False)
    base=h.model.edit
    def edit(p,r):
        r=base(p,r);r.pop('intent_repair',None);return r
    h.model.edit=edit
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    assert not grants(h) and h.updater.acceptance_state()[0]['original']['intent']=='SERVICE_ACCEPTANCE'


def test_task_and_grant_publication_roll_back_together_then_resume_without_reinference(h,monkeypatch):
    from agentic_memory.privacy.governance import PolicyGovernor
    offer(h);edit_acceptance(h)
    original=PolicyGovernor.activate
    def broken(*args,**kwargs):raise RuntimeError('injected task publication failure')
    monkeypatch.setattr(PolicyGovernor,'activate',broken)
    with pytest.raises(RuntimeError,match='injected'):h.turn('I can make that.')
    assert not grants(h) and h.updater.acceptance_state()[0]['state']!='ACCEPTED'
    jid=h.updater.acceptance_state()[0]['job_id'];calls=len(h.model.calls)
    monkeypatch.setattr(PolicyGovernor,'activate',original)
    reopen(h);h.updater.update(jid)
    assert len(h.model.calls)==calls and grants(h,'tess')
    assert h.updater.acceptance_state()[0]['state']=='ACCEPTED'


def test_unrelated_billing_withdrawal_does_not_steal_scheduling_acceptance(h):
    h.policy=lambda p:[h.grant(p,recipient='tess',scope='Billing')]
    h.turn('Tess may read billing information.');h.policy=lambda p:[]
    offer(h)
    def revoke(p):
        aid=next(aid for aid,a in p['permission_state']['assignment'].items() if a['participant']==h.actor(p,'tess'))
        return [{'op':'revoke_assignment','assignment_id':aid,'authority_source':p['new_source_id'],
            'evidence':[p['new_source_id']],'reason':'Withdraw billing only.'}]
    h.policy=revoke;h.turn("Revoke Tess's billing access.");h.policy=lambda p:[]
    before=deepcopy(grants(h,'tess'))
    edit_acceptance(h);h.turn('I can make that.')
    assert h.updater.acceptance_state()[0]['state']=='ACCEPTED'
    assert all(grants(h,'tess')[aid]==a for aid,a in before.items())


def test_export_only_mask_does_not_revoke_task_read_authority():
    assignment={'status':'active','information_scope':'Scheduling',
        'recipient_revocations':[{'actions':['export'],'scope':{'kind':'all','value':'*'}}]}
    assert not acceptance.withdrawn_for(assignment,'scheduling',{'read','search','derive'})


def test_other_actor_cannot_cancel_one_offer_to_disambiguate_someone_elses_acceptance(h):
    offer(h);offer(h,'pat')
    h.turn('Cancel my appointment scheduling task with Tess.',actor='other')
    edit_acceptance(h,new_grant=False)
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    assert not grants(h)
    assert {o['worker'] for o in h.updater.acceptance_state()[0]['candidate_offers']}=={'tess','pat'}


def test_conditional_offer_does_not_become_unconditional_authority(h):
    offer(h,text='I can arrange appointment scheduling after you call me.')
    edit_acceptance(h,new_grant=False)
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    assert not grants(h)


@pytest.mark.parametrize('utterance',['She said yes.','Yes?','I never agreed to that appointment.'])
def test_model_acceptance_does_not_override_reported_question_or_negative_source(h,utterance):
    offer(h)
    def edit(p,r):
        if p['control_segments'][0]['quote']!=utterance:return r
        r['c']['0'].update(intents=['service'],recipient=h.actor(p,'tess'),existing=None)
        if p.get('correction',{}).get('mode')=='reference_repair':
            r['intent_repair']={'outcome':'REFERENCE_REPAIR','reason':'Keep the model classification.',
                'contrary_evidence':[]}
        return r
    h.model.edit=edit
    with pytest.raises(PermissionRepairExhausted):h.turn(utterance)
    assert not grants(h) and h.updater.acceptance_state()[0]['state']=='PENDING_SEMANTIC_REASSESSMENT'


def test_accepted_worker_handoff_ends_old_task_without_transferring_its_grant(h):
    offer(h,'pat');edit_acceptance(h,worker='pat');h.turn('I can make that.')
    old=h.updater.acceptance_state()[0]
    h.model.edit=lambda p,r:r
    h.turn('I am handing over appointment scheduling to Tess.',actor='pat')
    assert h.updater.acceptance_state()[0]['state']=='HANDED_OFF'
    offer(h,'tess');edit_acceptance(h);h.turn('Tess, I can make that Tuesday appointment.')
    rows=h.updater.acceptance_state()
    assert next(r for r in rows if r['id']==old['id'])['state']=='HANDED_OFF'
    new=next(r for r in rows if r['id']!=old['id'])
    assert new['state']=='ACCEPTED' and new['task']['worker']=='tess'
    assert set(new['grant_ids']).isdisjoint(old['grant_ids'])


def test_two_tasks_for_one_worker_keep_separate_grants_and_cancel_only_named_day(h):
    offer(h);edit_acceptance(h);h.turn('I can make that.')
    before=deepcopy(grants(h,'tess'))
    h.model.edit=lambda p,r:r
    offer(h,text='I can arrange your Friday appointment scheduling.')
    edit_acceptance(h);h.turn('Tess, I can make that Friday appointment.')
    assert all(grants(h,'tess')[aid]==a for aid,a in before.items())
    h.model.edit=lambda p,r:r;h.turn('Cancel my Friday appointment scheduling task with Tess.')
    rows=h.updater.acceptance_state()
    assert next(r for r in rows if 'Friday' in r['task']['offer']['quote'])['state']=='CANCELLED'
    assert next(r for r in rows if 'Tuesday' in r['task']['offer']['quote'])['state']=='ACCEPTED'


def test_ambiguous_cancellation_does_not_pick_one_of_two_accepted_tasks(h):
    offer(h);edit_acceptance(h);h.turn('I can make that.')
    h.model.edit=lambda p,r:r;offer(h,text='I can arrange your Friday appointment scheduling.')
    edit_acceptance(h);h.turn('Tess, I can make that Friday appointment.')
    h.model.edit=lambda p,r:r;h.turn('Cancel my appointment scheduling task with Tess.')
    assert {r['state'] for r in h.updater.acceptance_state()}=={'PENDING_CANCELLATION_RESOLUTION'}
    reopen(h)
    assert {r['state'] for r in h.updater.acceptance_state()}=={'PENDING_CANCELLATION_RESOLUTION'}


def test_same_worker_grant_for_another_offer_cannot_discharge_new_task(h):
    offer(h);edit_acceptance(h);h.turn('I can make that.')
    before=deepcopy(grants(h,'tess'))
    h.model.edit=lambda p,r:r;offer(h,text='I can arrange your Friday appointment scheduling.')
    edit_acceptance(h,new_grant=False)
    base=h.model.edit
    def edit(p,r):
        r=base(p,r)
        r['c']['0']['existing']=next(aid for aid,a in p['permission_state']['assignment'].items()
            if a['participant']==h.actor(p,'tess'))
        return r
    h.model.edit=edit
    with pytest.raises(PermissionRepairExhausted):h.turn('Tess, I can make that Friday appointment.')
    assert grants(h,'tess')==before


def test_an_unresolved_condition_is_not_a_reason_to_guess_the_other_worker(h):
    offer(h,text='I can arrange appointment scheduling after you call me.')
    offer(h,'pat')
    edit_acceptance(h,worker='pat',new_grant=False)
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.')
    assert not grants(h) and h.updater.acceptance_state()[0]['state']=='PENDING_RECIPIENT_RESOLUTION'


def test_canonical_validation_cannot_bypass_a_pending_recognized_intent(h):
    offer(h);edit_acceptance(h,new_grant=False)
    with pytest.raises(PermissionRepairExhausted):h.turn('I can make that.',max_attempts=1)
    row=h.updater.acceptance_state()[0]
    state=h.updater._state()
    revision=h.core.db.execute("SELECT revision FROM permission_bootstrap WHERE compartment='work'").fetchone()[0]
    sources={r['id']:json.loads(r['payload']) for r in h.core.db.execute(
        "SELECT id,payload FROM resources WHERE kind='observation' AND compartment='work' ORDER BY rowid")}
    with pytest.raises(PermissionProposalRejected,match='pending service'):
        h.updater._validate({'revision':revision,'considered_restrictions':[], 'operations':[]},
            state,sources,revision,new_source_id=row['source_id'])
    assert not grants(h)


def test_reassessment_cannot_manufacture_a_literal_grant_to_compensate(h):
    offer(h)
    edit_acceptance(h,drop=True,new_grant=False,proof={'outcome':'SEMANTIC_REASSESSMENT',
        'reason':'The subject declined the offered appointment.','contrary_evidence':[]})
    base=h.model.edit
    def edit(p,r):
        r=base(p,r)
        if p.get('correction',{}).get('mode')=='reference_repair':
            r['c']['0']['intents']=['grant']
            r['intent_repair']['contrary_evidence']=[{'source_id':p['new_source_id'],'quote':'I cannot make that'}]
            statement=next(s for s in p['sources'] if s['id']==p['new_source_id'])
            r['p']=[{'o':'fields','i':'new','p':h.actor(p,'tess'),'s':h.actor(p,'owner'),
                'e':p['new_source_id'],'quote':statement['text'],'v':{},'r':'Compensating literal access.'}]
        return r
    h.model.edit=edit
    with pytest.raises(PermissionRepairExhausted):h.turn('I cannot make that; I do not accept the appointment.')
    assert not grants(h) and h.updater.acceptance_state()[0]['state']=='SEMANTICALLY_REASSESSED'


def test_independent_factual_expansion_failure_cannot_erase_recognized_intent(h,monkeypatch):
    from agentic_memory.privacy import incremental_wire
    offer(h);calls=edit_acceptance(h)
    original=incremental_wire.contract
    fired=[]
    def contract(*args,**kwargs):
        query,schema,decode=original(*args,**kwargs)
        def reject_first_fact_expansion(value):
            if not fired:
                fired.append(True)
                raise PermissionProposalRejected('Injected independent factual expansion failure')
            return decode(value)
        return query,schema,reject_first_fact_expansion
    monkeypatch.setattr(incremental_wire,'contract',contract)
    h.turn('I can make that.')
    assert len(calls)==2 and calls[1]['correction']['mode']=='reference_repair'
    assert h.updater.acceptance_state()[0]['state']=='ACCEPTED'
    assert h.updater.acceptance_state()[0]['history'][0]['outcome']=='INITIAL_INTERPRETATION'


def test_grounding_worker_acceptance_does_not_change_the_self_request_policy(h):
    def edit(p,r):
        if p['control_segments'][0]['quote']!='Please help me plan my appointment.':return r
        r['c']['0'].update(intents=['service'],recipient=h.actor(p,'owner'),existing=None)
        r['p']=[{'o':'activity','via':'request','i':'new','s':h.actor(p,'owner'),
            'e':p['new_source_id'],'work':'appointment planning','r':'The subject requests own help.','v':{}}]
        return r
    h.model.edit=edit;h.turn('Please help me plan my appointment.')
    assert grants(h,'owner') and not grants(h,'tess')
    assert not h.updater.acceptance_state()


def test_explicit_responsive_service_question_keeps_existing_task_acceptance_policy(h):
    offer(h)
    def edit(p,r):
        if p['control_segments'][0]['quote']!='Tess, could you schedule my Tuesday appointment?':return r
        r['c']['0'].update(intents=['service'],recipient=h.actor(p,'tess'),existing=None)
        receipt=next(s['id'] for s in p['sources'] if s['text'].startswith('I can arrange'))
        r['p']=[{'o':'activity','via':'response','i':'new','wi':'new','p':h.actor(p,'tess'),
            's':h.actor(p,'owner'),'e':p['new_source_id'],'q':receipt,'work':'appointment scheduling',
            'wf':['appointment times'],'r':'The subject asks the actual worker for this service.','v':{},'wv':{}}]
        return r
    h.model.edit=edit;h.turn('Tess, could you schedule my Tuesday appointment?')
    assert grants(h,'tess') and h.updater.acceptance_state()[0]['state']=='ACCEPTED'


def test_explicit_service_reference_overrides_the_short_confirmation_default(h):
    offer(h);offer(h,text='I can arrange your parcel pickup.')
    edit_acceptance(h,service='parcel pickup')
    h.turn('Tess, I can make that parcel pickup.')
    task=h.updater.acceptance_state()[0]['task']
    assert task['service']=='pickup' and 'parcel pickup' in task['offer']['quote']
