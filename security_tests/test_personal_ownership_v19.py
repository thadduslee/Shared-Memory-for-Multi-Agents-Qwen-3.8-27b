"""Personal OWNS access does not depend on a narrow self-task grant."""
from dataclasses import replace

import pytest

from agentic_memory.privacy import AccessDenied
from agentic_memory.privacy.permissions import PermissionUpdater, PermissionBootstrap
from security_tests import test_incremental_permissions as fixtures
from security_tests.test_selective_memory_v17 import erase


@pytest.fixture
def owned():
    h=fixtures.IncrementalTests();h.setUp()
    h.bootstrap=replace(h.bootstrap,owner_read_actions=('search','read','derive'))
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19')
    try:yield h
    finally:h.tearDown()


def test_own_personal_facts_across_tasks_but_not_other_readers(owned):
    h=owned
    h.turn('Hello, I own my personal resource.')
    assert not h.selected('owner')
    assert len(h.updater.inspect()['assignment'])==1
    h.classify=lambda p,s:'instruction'
    h.turn('Project meeting is Tuesday at 4 PM. My private garden code is 4519.')
    assert len(h.selected('owner'))==2
    assert not h.selected('helper') and not h.selected('outsider')
    h.turn('Keep the cover installed during startup.',actor='helper')
    assert any('cover installed' in r['text'] for r in h.selected('owner'))
    assert not h.selected('helper')
    grants=h.updater.inspect()['assignment']
    assert len(grants)==2 and all(a['ownership'] for a in grants.values())
    assert all(a['participant']==a['subject'] for a in grants.values())


def test_owner_is_not_a_way_around_erasure_and_stale_release(owned):
    h=owned;h.classify=lambda p,s:'instruction'
    h.turn('Pickup Tuesday at 4 PM. Use entrance code 4519.')
    reader=h.reader('owner');old=h.core.retrieve(reader,'entrance code')
    erase(h,'4519')
    assert [r['text'] for r in h.selected('owner')]==['Pickup Tuesday at 4 PM.']
    with pytest.raises(AccessDenied):h.core.release(reader,old['input_ids'],{'answer':'stale code'})
    h.policy=lambda p:[];h.classify=lambda p,s:'instruction'
    h.turn('Deletion confirmed: the previous code was 4519.')
    assert all('4519' not in r['text'] for r in h.selected('owner'))


def test_revoke_other_reader_does_not_remove_ownership(owned):
    h=owned;h.setup_access();h.classify=lambda p,s:'instruction'
    h.turn('Pickup Tuesday at 4 PM.')
    assert h.selected('owner') and h.selected('helper')
    packet=h.core.retrieve(h.reader(),'pickup')
    h.policy=lambda p:[{'op':'revoke_assignment','assignment_id':next(aid for aid,a in
        p['permission_state']['assignment'].items() if a['participant']!=a['subject']),
        'authority_source':p['new_source_id'],'evidence':[p['new_source_id']],'reason':'Revoke this helper.'}]
    h.classify=lambda p,s:'context';h.turn('Revoke the helper access.')
    assert h.selected('owner') and not h.selected('helper')
    with pytest.raises(AccessDenied):h.core.release(h.reader(),packet['input_ids'],{'answer':'stale pickup'})


def test_owner_edge_never_revives_after_explicit_revocation(owned):
    h=owned;h.classify=lambda p,s:'instruction';h.turn('My project time is 4 PM.')
    h.classify=lambda p,s:'context'
    h.policy=lambda p:[{'op':'revoke_assignment','assignment_id':next(aid for aid,a in
        p['permission_state']['assignment'].items() if a.get('ownership')),
        'authority_source':p['new_source_id'],'evidence':[p['new_source_id']],'reason':'Revoke my own read grant.'}]
    h.turn('Revoke my own read access.')
    assert not h.selected('owner')
    h.policy=lambda p:[];h.classify=lambda p,s:'instruction';h.turn('My new project time is 5 PM.')
    assert not h.selected('owner')


def test_incorrect_subject_review_keeps_candidate_protected(owned):
    h=owned;h.classify=lambda p,s:'instruction'
    h.binding=lambda p,r:{**r,'in_scope':True,'subject_supported':False,'blocked_by':[],
        'reason':'The asserted fact is not supported for this subject.'}
    h.turn('A statement whose information subject is uncertain.')
    assert h.updater.fact_candidates() and not h.selected('owner')


def test_administrator_does_not_automatically_own_company_resource():
    b=PermissionBootstrap(participants={'boss':{}},resources={'company':{},'personal':{}},
        authorities={'company':{'permission':['boss']}},source_identities={'connector':['boss']},
        task_convention='Explicit authority.',personal_owners={'personal':'boss'},owner_read_actions=('read',))
    b.validate()
    from agentic_memory.privacy.ownership import relations
    assert relations(b)=={'personal':'boss'}
    with pytest.raises(ValueError):replace(b,personal_owners={'company':'unknown'}).validate()


def test_company_fact_stays_protected_even_from_its_administrator(owned):
    h=owned
    h.bootstrap=replace(h.bootstrap,participants={'owner':{}},resources={'owner':{},'company':{}},
        personal_owners={'owner':'owner'},authorities={
            'owner':{c:['owner'] for c in ('permission','restrict','delegate')},
            'company':{c:['owner'] for c in ('permission','restrict','delegate')}})
    # A different compartment is not needed: the first updater has no sources.
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19-company')
    original=h.model.handler
    def response(p):
        result=original(p)
        for s in result.get('segments',[]):s['subject']='resource2'
        result['binding_reviews']=[]
        return result
    h.model.handler=response;h.classify=lambda p,s:'instruction'
    h.turn('The company reserve is 9000.')
    assert h.updater.fact_candidates()[0]['subject']=='company'
    assert not h.selected('owner')


@pytest.mark.parametrize('attempts,maintenance_calls',[(1,1),(2,0)])
def test_source_maintenance_shares_two_response_budget(owned,monkeypatch,attempts,maintenance_calls):
    h=owned
    from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter,EmptyPermissionProcessor
    adapter=InferenceFixtureAdapter(h.core,{'episode_id':'budget-check'},
        ['test:hash','test:permission'],EmptyPermissionProcessor(),permission_version='19')
    update=adapter._update_permission
    def completed(job_id):
        result=update(job_id)
        h.core.db.execute('UPDATE permission_jobs SET attempts=? WHERE id=?',(attempts,job_id))
        return result
    monkeypatch.setattr(adapter,'_update_permission',completed)
    calls=[]
    def maintenance(**kwargs):
        calls.append(kwargs);return {'changes':[],'batches':0}
    monkeypatch.setattr(adapter.updater,'flush_bindings',maintenance)
    adapter.ingest_batch({'boundary':1},[{'turn_id':'one','speaker':{'principal_id':'owner'},'text':'Hello.'}])
    assert len(calls)==maintenance_calls
    assert adapter.core.db.execute("SELECT status FROM permission_jobs WHERE compartment=?",
        (adapter.compartment,)).fetchone()[0]=='complete'


def test_ownership_cannot_be_enabled_in_a_historical_version(owned):
    h=owned
    with pytest.raises(ValueError,match='version 19'):
        PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='18')


def test_generic_integration_can_explicitly_leave_ownership_disabled():
    h=fixtures.IncrementalTests();h.setUp()
    try:
        h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19')
        assert 'host has explicitly enabled personal OWNS' not in h.updater.instruction
        h.classify=lambda p,s:'instruction';h.turn('My personal project time is 4 PM.')
        assert not h.selected('owner')
    finally:h.tearDown()
