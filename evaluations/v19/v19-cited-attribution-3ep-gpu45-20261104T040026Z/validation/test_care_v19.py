"""Role-limited staff access from cited care involvement, plus directory contracts."""
from dataclasses import replace
import json
import pytest

from agentic_memory.privacy.incremental_routing import related_turns
from agentic_memory.privacy.permissions import PermissionUpdater, PermissionSourceEvent
from agentic_memory.privacy.care_v19 import identifier
from security_tests import test_incremental_permissions as fixtures

DIRECTORY={'pat':{'name':'Pat Lane','role':'patient','aliases':['Pat Lane','Pat','Lane']},
    'kim':{'name':'Kim Dale','role':'patient','aliases':['Kim Dale','Kim','Dale']},
    'nia':{'name':'Nurse Nia','role':'nurse','aliases':['Nurse Nia','Nia']},
    'sol':{'name':'Sol Ames','role':'scheduler','aliases':['Sol Ames','Sol','Ames']},
    'bo':{'name':'Bo Finch','role':'billing','aliases':['Bo Finch','Bo','Finch']}}
SCOPES={'nurse':'Clinical care.','scheduler':'Appointments.','billing':'Billing.'}


@pytest.fixture
def h():
    h=fixtures.IncrementalTests();h.setUp()
    people=['owner','helper',*DIRECTORY]  # The shared scripted model refers to the first two.
    h.actor_names={p:i+1 for i,p in enumerate(people)}
    h.bootstrap=replace(h.bootstrap,participants={p:{} for p in people},resources={p:{} for p in people},
        authorities={p:{cap:[p] for cap in ('permission','restrict','delegate')} for p in people},
        source_identities={'connector':people},discover_self_owned_participants=(),
        personal_owners={p:p for p in people},owner_read_actions=('search','read','derive'),
        directory=DIRECTORY,worker_roles=(*SCOPES,'agent'),care_scopes=SCOPES)
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19')
    h.classify=lambda p,s:'instruction';h.source_subject=None;h.decision={};h.seen=[]
    original=h.model.complete
    def reasoned(system,payload,*,schema=None):
        response=original(system,payload,schema=schema)
        if 'f' not in response:return response
        h.seen.append(payload)
        decision=h.decision
        for f in response['f'].values():
            if f['kind'] in {'instruction','asserted_fact'}:
                said=[h.actor(payload,p) for p in decision.get('says',[])]
                f.update(information_subject=said[0] if len(said)==1 else None,information_subjects=said,
                    attribution_complete=bool(said),action_actors=[],mentioned_people=[],
                    care_participants=[h.actor(payload,p) for p in decision.get('care',[])],
                    care_roles=list(decision.get('roles',[])))
        return response
    h.model.complete=reasoned
    try:yield h
    finally:h.tearDown()


def say(h,text,actor,**decision):
    h.decision=decision;h.seq+=1
    job=h.updater.ingest(PermissionSourceEvent(str(h.seq),actor,'connector',text,
        context={'speaker_name':DIRECTORY[actor]['name'],'speaker_role':DIRECTORY[actor]['role']}))
    h.updater.update(job['job_id'],max_attempts=2)
    h.updater.flush_bindings(max_batches=1)
    return job


def readable(h,reader):
    return [r['text'] for r in h.core.retrieve(h.reader(reader),'current information',k=100)['evidence']]


def test_worker_shown_doing_care_reads_only_what_their_role_covers(h):
    say(h,'Pat starts 5 mg today.','nia',says=['pat'],care=['nia'],roles=['nurse'])
    say(h,'Pat is booked Friday at noon.','sol',says=['pat'],care=['sol'],roles=['scheduler','nurse'])
    assert set(readable(h,'nia'))=={'Pat starts 5 mg today.','Pat is booked Friday at noon.'}
    assert readable(h,'sol')==['Pat is booked Friday at noon.']


def test_role_alone_grants_nothing(h):
    say(h,'Pat starts 5 mg today.','nia',says=['pat'],roles=['nurse','billing'])
    assert not readable(h,'nia') and not readable(h,'bo')
    assert identifier('pat','nia') not in h.updater._state()['assignment']


def test_grant_reaches_earlier_facts_without_a_model_review(h):
    say(h,'Pat starts 5 mg today.','sol',says=['pat'],roles=['nurse'])
    calls=h.core.db.execute("SELECT COUNT(*) FROM fact_attempts WHERE stage='binding_maintenance'").fetchone()[0]
    say(h,'Pat is resting comfortably.','nia',says=['pat'],care=['nia'],roles=['nurse'])
    assert 'Pat starts 5 mg today.' in readable(h,'nia')
    assert h.core.db.execute("SELECT COUNT(*) FROM fact_attempts WHERE stage='binding_maintenance'").fetchone()[0]==calls


def test_care_for_one_patient_does_not_reach_another(h):
    say(h,'Pat starts 5 mg today.','nia',says=['pat'],care=['nia'],roles=['nurse'])
    say(h,"Kim's biopsy is Monday.",'sol',says=['kim'],roles=['nurse'])
    assert readable(h,'nia')==['Pat starts 5 mg today.']


def test_unresolved_fact_creates_no_grant(h):
    h.decision={}
    say(h,'Pat and Kim both moved.','nia',says=['pat'],care=['nia'],roles=['nurse'])
    assert identifier('pat','nia') not in h.updater._state()['assignment']


def test_mixed_fact_does_not_establish_care_for_either_person(h):
    say(h,'Pat and Kim both moved to Friday.','nia',says=['pat','kim'],care=['nia'],roles=['nurse'])
    state=h.updater._state()['assignment']
    assert identifier('pat','nia') not in state and identifier('kim','nia') not in state


def test_worker_named_by_a_colleague_gets_involvement_but_not_an_unnamed_one(h):
    say(h,'Nurse Nia covers Pat this weekend.','sol',says=['pat'],care=['nia','bo'],roles=['nurse'])
    state=h.updater._state()['assignment']
    assert identifier('pat','nia') in state and identifier('pat','bo') not in state


def test_non_worker_cannot_vouch_for_someone_elses_care(h):
    say(h,"Kim says Nurse Nia treats Pat.",'kim',says=['pat'],care=['nia'],roles=['nurse'])
    assert identifier('pat','nia') not in h.updater._state()['assignment']


def test_subject_can_name_their_own_worker(h):
    say(h,'Nurse Nia is treating me.','pat',says=['pat'],care=['nia'],roles=['nurse'])
    assert identifier('pat','nia') in h.updater._state()['assignment']


def test_worker_never_gets_a_care_grant_over_themselves(h):
    say(h,'I am off on Friday.','nia',says=['nia'],care=['nia'],roles=['nurse'])
    assert identifier('nia','nia') not in h.updater._state()['assignment']


def test_restricted_worker_pair_needs_a_model_review_not_the_label(h):
    from agentic_memory.privacy.care_v19 import labelled, restricted
    say(h,'Pat starts 5 mg today.','nia',says=['pat'],care=['nia'],roles=['nurse'])
    state=h.updater._state();aid=identifier('pat','nia')
    assert not restricted(state['assignment'][aid],state)
    state['restriction']['r1']={'subject':'pat','participants':['nia'],'actions':['read']}
    assert restricted(state['assignment'][aid],state)
    assert not labelled({'care':{'roles':['nurse'],'participants':[]}},state['assignment'][aid],state)


def test_revoked_care_grant_never_reactivates(h):
    say(h,'Pat starts 5 mg today.','nia',says=['pat'],care=['nia'],roles=['nurse'])
    assert readable(h,'nia')
    aid=identifier('pat','nia')
    def revoke(p):
        alias=next(k for k,a in p['permission_state']['assignment'].items()
            if a['participant']==h.actor(p,'nia') and a['subject']==h.actor(p,'pat'))
        return [{'op':'revoke_assignment','assignment_id':alias,'authority_source':p['new_source_id'],
            'evidence':[p['new_source_id']],'reason':'Withdraw nurse access'}]
    h.classify=lambda p,s:'context';h.policy=revoke
    h.turn('Revoke Nurse Nia access.',actor='pat')
    h.policy=lambda p:[];h.classify=lambda p,s:'instruction'
    assert h.updater._state()['assignment'][aid]['status']=='revoked'
    assert not readable(h,'nia')
    say(h,'Pat is resting comfortably.','nia',says=['pat'],care=['nia'],roles=['nurse'])
    assert h.updater._state()['assignment'][aid]['status']=='revoked' and not readable(h,'nia')


def test_erasing_the_citation_ends_the_grant(h):
    job=say(h,'Pat starts 5 mg today.','nia',says=['pat'],care=['nia'],roles=['nurse'])
    assert readable(h,'nia')
    h.core.delete(h.host,job['source_id'])
    assert not readable(h,'nia')


def test_model_sees_names_and_roles_but_never_raw_ids_or_host_policy_fields(h):
    say(h,'Pat starts 5 mg today.','nia',says=['pat'],roles=['nurse'])
    payload=h.seen[-1]
    assert payload['care_scopes']==SCOPES
    assert not {'directory','worker_roles','care_scopes'} & set(payload['bootstrap'])
    assert {'name':'Nurse Nia','role':'nurse'} in payload['bootstrap']['participants'].values()
    current=next(s for s in payload['sources'] if s['id']==payload['new_source_id'])
    assert current['source']['context']['speaker_role']=='nurse'
    assert current['source']['context']['speaker_name']=='Nurse Nia'
    assert 'aliases' not in json.dumps(payload)


def test_similar_older_turns_extend_context_but_do_not_replace_the_window(h):
    h.classify=lambda p,s:'context'
    h.decision={}
    sids=[say(h,f'Filler note {i}.','sol')['source_id'] for i in range(8)]
    sources={r['id']:json.loads(r['payload']) for r in h.core.db.execute(
        "SELECT id,payload FROM resources WHERE compartment='work' AND kind='observation' ORDER BY rowid")}
    recent=set(sids[-6:])
    extra=related_turns(h.updater,sources,sids[-1],recent)
    assert len(extra)<=2 and not set(extra)&recent and sids[-1] not in extra
    assert related_turns(h.updater,{s:sources[s] for s in sids[-3:]},sids[-1],set(sids[-3:]))==[]
