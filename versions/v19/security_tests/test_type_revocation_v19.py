"""Withdrawing one kind of information from one person, however it is phrased."""
import json
import pytest

from agentic_memory.privacy import AccessDenied
from agentic_memory.privacy.permissions import PermissionSourceEvent
from agentic_memory.privacy.revocation_v19 import model_requests
from security_tests.test_care_v19 import h, say, readable, identifier  # noqa: F401

MED='Pat starts 5 mg today.';SCHED='Pat is booked Friday at noon.';COVER='Pat is booked Monday.'
PHRASINGS=["Revoke Nurse Nia's access to my medications.","Revoke Nurse Nia's medication access.",
    'Please stop Nurse Nia from seeing my medications.','Nurse Nia should not see my medication list anymore.',
    'Keep my prescriptions away from Nurse Nia from now on.','Nia, I would rather you did not look at my meds.']


def seed(h):
    say(h,MED,'nia',says=['pat'],care=['nia'],types=['medication'])
    say(h,SCHED,'nia',says=['pat'],care=['nia'],types=['schedule'])
    say(h,COVER,'sol',says=['pat'],care=['sol'],types=['schedule'])
    assert {MED,SCHED} <= set(readable(h,'nia'))


def withdraw(h,text,*,speaker='pat',recipient='nia',types=('medication',),subject='pat',extra=()):
    def revoke(p):
        ops=[]
        for person in (recipient,*extra):
            alias=next(k for k,a in p['permission_state']['assignment'].items()
                if a['participant']==h.actor(p,person) and a['subject']==h.actor(p,subject))
            ops.append({'op':'revoke_assignment','assignment_id':alias,'authority_source':p['new_source_id'],
                'evidence':[p['new_source_id']],'reason':'The patient withdrew this.','withdraw_types':list(types)})
        return ops
    h.classify=lambda p,s:'context';h.policy=revoke
    try:h.turn(text,actor=speaker)
    finally:h.policy=lambda p:[];h.classify=lambda p,s:'instruction'


@pytest.mark.parametrize('text',PHRASINGS)
def test_same_result_however_the_request_is_phrased(h,text):
    seed(h);withdraw(h,text)
    visible=readable(h,'nia')
    assert MED not in visible and SCHED in visible
    grant=h.updater._state()['assignment'][identifier('pat','nia')]
    assert grant['status']=='active'
    assert grant['recipient_revocations'][0]['scope']=={'kind':'types','value':['medication']}


def test_future_facts_of_that_type_stay_hidden_and_others_stay_visible(h):
    seed(h);withdraw(h,PHRASINGS[0])
    say(h,'Pat starts 10 mg tomorrow.','nia',says=['pat'],care=['nia'],types=['medication'])
    say(h,'Pat is booked Tuesday.','nia',says=['pat'],care=['nia'],types=['schedule'])
    visible=readable(h,'nia')
    assert 'Pat starts 10 mg tomorrow.' not in visible and 'Pat is booked Tuesday.' in visible


def test_fact_mixing_a_withdrawn_type_is_hidden_whole(h):
    seed(h);withdraw(h,PHRASINGS[0])
    say(h,'Pat takes 5 mg before the Friday visit.','nia',says=['pat'],care=['nia'],types=['medication','schedule'])
    assert 'Pat takes 5 mg before the Friday visit.' not in readable(h,'nia')


def test_fact_with_no_type_is_not_readable_after_a_withdrawal(h):
    from agentic_memory.privacy.revocation_v19 import blocks
    grant={'recipient_revocations':[{'actions':['*'],'scope':{'kind':'types','value':['medication']}}]}
    assert blocks(grant,'read','text',None) and blocks(grant,'read','text',[])
    assert blocks(grant,'read','text',['medication']) and not blocks(grant,'read','text',['schedule'])


def test_everyone_else_keeps_their_access(h):
    seed(h)
    say(h,'Pat is resting.','bo',says=['pat'],care=['bo'],types=['billing'])
    withdraw(h,PHRASINGS[0])
    assert COVER in readable(h,'sol') and MED in readable(h,'pat')
    assert 'Pat is resting.' in readable(h,'bo')


def test_withdrawing_several_types_at_once(h):
    seed(h);withdraw(h,'Nurse Nia can no longer see my meds or visits.',types=('medication','schedule'))
    assert not {MED,SCHED} & set(readable(h,'nia'))


def test_second_request_adds_to_the_first(h):
    seed(h);withdraw(h,PHRASINGS[0])
    withdraw(h,'Nurse Nia should not see my visits either.',types=('schedule',))
    assert not {MED,SCHED} & set(readable(h,'nia'))


def test_empty_list_withdraws_all_access(h):
    seed(h);withdraw(h,'Nurse Nia should not see anything of mine.',types=())
    assert h.updater._state()['assignment'][identifier('pat','nia')]['status']=='revoked'
    assert not readable(h,'nia')


def test_withdrawn_grant_does_not_come_back_with_new_care_work(h):
    seed(h);withdraw(h,PHRASINGS[0])
    say(h,'Pat starts 20 mg next week.','nia',says=['pat'],care=['nia'],types=['medication'])
    assert 'Pat starts 20 mg next week.' not in readable(h,'nia')
    assert len(h.updater._state()['assignment'][identifier('pat','nia')]['recipient_revocations'])==1


def test_earlier_read_cannot_be_released_after_the_withdrawal(h):
    seed(h)
    packet=h.core.retrieve(h.reader('nia'),'current information',k=100)
    withdraw(h,PHRASINGS[0])
    with pytest.raises(AccessDenied):
        h.core.release(h.reader('nia'),packet['input_ids'],{'answer':'cached'})


def verify(h,text,speaker,grant_id,types=('medication',)):
    """The host check on its own, so each test proves its specific rejection reason."""
    job=h.updater.ingest(PermissionSourceEvent(str(h.seq+100),speaker,'connector',text))
    h.seq+=1
    sources={r['id']:json.loads(r['payload']) for r in h.core.db.execute(
        "SELECT id,payload FROM resources WHERE compartment='work' AND kind='observation' ORDER BY rowid")}
    op={'op':'revoke_assignment','assignment_id':grant_id,'issuer':speaker,'reason':'r','withdraw_types':list(types),
        'evidence':{'issuer':[job['source_id']]}}
    return model_requests(h.updater,[op],h.updater._state(),sources,job['source_id'])


@pytest.mark.parametrize('speaker',['kim','sol','bo'])
def test_only_the_subject_can_withdraw(h,speaker):
    seed(h)
    with pytest.raises(ValueError,match='only the information subject'):
        verify(h,'Nurse Nia should not see the meds.',speaker,identifier('pat','nia'))


def test_subject_passes_the_same_check(h):
    seed(h)
    [request]=verify(h,'Nurse Nia should not see my meds.','pat',identifier('pat','nia'))
    assert request['recipients']==['nia'] and request['scope']=={'kind':'types','value':['medication']}


def test_recipient_absent_from_the_conversation_is_rejected(h):
    say(h,MED,'nia',says=['pat'],care=['nia'],types=['medication'])
    h.classify=lambda p,s:'context'
    for _ in range(6):h.turn('Noted.',actor='sol')
    h.classify=lambda p,s:'instruction'
    with pytest.raises(ValueError,match='does not appear in the conversation'):
        verify(h,'Take it off my list.','pat',identifier('pat','nia'))


def test_person_cannot_withdraw_their_own_owner_access(h):
    from agentic_memory.privacy.ownership import identifier as owner_id
    seed(h)
    with pytest.raises(ValueError,match='their own access'):
        verify(h,'I should not see my meds.','pat',owner_id('pat','pat'))


def test_unknown_information_type_is_rejected(h):
    seed(h)
    with pytest.raises(ValueError,match='unknown information type'):
        verify(h,'Nurse Nia should not see my meds.','pat',identifier('pat','nia'),types=('astrology',))


def test_every_grant_of_that_person_on_that_patient_is_covered(h):
    seed(h)
    h.classify=lambda p,s:'context'
    h.policy=lambda p:[{**h.grant(p,recipient='nia',scope='Pickup instructions'),'subject':h.actor(p,'pat')}]
    h.turn('Nurse Nia may read my pickup instructions.',actor='pat')
    h.policy=lambda p:[];h.classify=lambda p,s:'instruction'
    withdraw(h,PHRASINGS[0])
    grants=[a for a in h.updater._state()['assignment'].values() if a['participant']=='nia' and a['subject']=='pat']
    assert len(grants)==2 and all(g.get('recipient_revocations') for g in grants)


def test_other_patients_grant_is_not_touched(h):
    seed(h)
    say(h,"Kim's biopsy is Monday.",'nia',says=['kim'],care=['nia'],types=['medication'])
    withdraw(h,PHRASINGS[0])
    assert "Kim's biopsy is Monday." in readable(h,'nia')
