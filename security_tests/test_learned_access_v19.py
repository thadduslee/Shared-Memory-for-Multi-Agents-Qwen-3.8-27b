"""Learned access: table cells that open from the source stream (care_v19.learned_access)."""
from dataclasses import replace
import json
import pytest

from agentic_memory.privacy.permissions import PermissionUpdater, PermissionSourceEvent
from agentic_memory.privacy.care_v19 import LEARNED, stored_learning
from security_tests import test_incremental_permissions as fixtures
from security_tests.test_care_v19 import DIRECTORY, SCOPES, TYPES, TABLE


def make(learned_access_sources=2, reply_access=True):
    h=fixtures.IncrementalTests();h.setUp()
    people=['owner','helper',*DIRECTORY]
    h.actor_names={p:i+1 for i,p in enumerate(people)}
    h.bootstrap=replace(h.bootstrap,participants={p:{} for p in people},resources={p:{} for p in people},
        authorities={p:{cap:[p] for cap in ('permission','restrict','delegate')} for p in people},
        source_identities={'connector':people},discover_self_owned_participants=(),
        personal_owners={p:p for p in people},owner_read_actions=('search','read','derive'),
        directory=DIRECTORY,worker_roles=(*SCOPES,'agent'),care_scopes=SCOPES,
        information_types=TYPES,role_information_types=TABLE,
        learned_access_sources=learned_access_sources,reply_access=reply_access)
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
                    information_types=list(decision.get('types',[])))
        return response
    h.model.complete=reasoned
    handler=h.model.handler
    def without_unrequested_reviews(p):
        result=handler(p)
        if p.get('stage')=='combined_incremental' and not p.get('required_pairs'):result['binding_reviews']=[]
        return result
    h.model.handler=without_unrequested_reviews
    return h


@pytest.fixture
def h():
    h=make()
    try:yield h
    finally:h.tearDown()


@pytest.fixture
def off():
    h=make(0,False)
    try:yield h
    finally:h.tearDown()


def say(h,text,actor,reply_to=None,**decision):
    h.decision=decision;h.seq+=1
    context={'speaker_name':DIRECTORY[actor]['name'],'speaker_role':DIRECTORY[actor]['role'],
             **({'reply_to':reply_to} if reply_to else {})}
    job=h.updater.ingest(PermissionSourceEvent(str(h.seq),actor,'connector',text,context=context))
    h.updater.update(job['job_id'],max_attempts=2)
    h.updater.flush_bindings(max_batches=1)
    return str(h.seq)


def readable(h,reader):
    return [r['text'] for r in h.core.retrieve(h.reader(reader),'current information',k=100)['evidence']]


# --- production: a role reads what its working members keep writing

def test_role_reads_a_type_after_its_working_members_wrote_it_in_two_turns(h):
    # The scheduler's row covers schedule only.
    say(h,'Pat takes 5 mg with lunch.','sol',says=['pat'],care=['sol'],types=['medication'])
    assert 'Pat takes 5 mg with lunch.' not in readable(h,'sol')
    say(h,'Pat stops the evening dose.','sol',says=['pat'],care=['sol'],types=['medication'])
    assert {'Pat takes 5 mg with lunch.','Pat stops the evening dose.'} <= set(readable(h,'sol'))
    cell=stored_learning(h.updater)['role:scheduler']
    assert list(cell)==['medication'] and len(cell['medication'])==2


def test_opened_cell_reaches_facts_written_by_others_and_earlier(h):
    say(h,'Pat starts 10 mg.','nia',says=['pat'],care=['nia'],types=['medication'])
    say(h,'Pat is booked Friday.','sol',says=['pat'],care=['sol'],types=['schedule'])
    assert 'Pat starts 10 mg.' not in readable(h,'sol')
    say(h,'Bring the 10 mg tablets Friday.','sol',says=['pat'],care=['sol'],types=['medication'])
    say(h,'Pat has the tablets ready.','sol',says=['pat'],care=['sol'],types=['medication'])
    assert 'Pat starts 10 mg.' in readable(h,'sol')


def test_one_turn_is_not_enough(h):
    say(h,'Pat takes 5 mg with lunch.','sol',says=['pat'],care=['sol'],types=['medication'])
    say(h,'Pat is booked Friday.','sol',says=['pat'],care=['sol'],types=['schedule'])
    assert 'role:scheduler' not in stored_learning(h.updater)
    assert 'Pat takes 5 mg with lunch.' not in readable(h,'sol')


def test_writing_without_being_shown_working_teaches_nothing(h):
    say(h,'Pat takes 5 mg with lunch.','sol',says=['pat'],types=['medication'])
    say(h,'Pat stops the evening dose.','sol',says=['pat'],types=['medication'])
    assert stored_learning(h.updater)=={}


def test_a_cell_opens_for_that_role_only(h):
    for text in ('Pat takes 5 mg.','Pat stops the evening dose.'):
        say(h,text,'sol',says=['pat'],care=['sol'],types=['medication'])
    say(h,'Pat paid the invoice.','bo',says=['pat'],care=['bo'],types=['billing'])
    assert 'Pat takes 5 mg.' in readable(h,'sol') and 'Pat takes 5 mg.' not in readable(h,'bo')


def test_learned_cells_never_reach_the_model(h):
    for text in ('Pat takes 5 mg.','Pat stops the evening dose.','Pat is booked Friday.'):
        say(h,text,'sol',says=['pat'],care=['sol'],types=['medication'])
    assert stored_learning(h.updater)
    for payload in h.seen:
        text=json.dumps(payload)
        assert 'role:scheduler' not in text and LEARNED not in text


def test_off_by_default_changes_nothing(off):
    for text in ('Pat takes 5 mg.','Pat stops the evening dose.'):
        say(off,text,'sol',says=['pat'],care=['sol'],types=['medication'])
    assert 'Pat takes 5 mg.' not in readable(off,'sol')
    assert off.core.db.execute('SELECT COUNT(*) FROM fact_processing WHERE job_id LIKE ?',(LEARNED+'%',)).fetchone()[0]==0


# --- replies: the person a reply answers reads its types, on that subject only

def test_reply_lets_the_working_asker_read_the_answered_type(h):
    say(h,'Pat is booked Friday.','sol',says=['pat'],care=['sol'],types=['schedule'])
    question=say(h,'Sol checking which dose Pat should bring Friday.','sol',says=['pat'],care=['sol'],types=['schedule'])
    say(h,'Pat brings the 5 mg tablets.','nia',reply_to=question,says=['pat'],care=['nia'],types=['medication'])
    assert 'Pat brings the 5 mg tablets.' in readable(h,'sol')
    assert set(stored_learning(h.updater))=={'person:sol:pat'}


def test_reply_from_someone_who_may_not_read_it_grants_nothing(h):
    say(h,'Pat is booked Friday.','sol',says=['pat'],care=['sol'],types=['schedule'])
    question=say(h,'Sol checking which dose Pat should bring Friday.','sol',says=['pat'],care=['sol'],types=['schedule'])
    say(h,'Pat paid for the 5 mg tablets.','bo',reply_to=question,says=['pat'],care=['bo'],types=['medication'])
    assert 'Pat paid for the 5 mg tablets.' not in readable(h,'sol')
    assert stored_learning(h.updater)=={}


def test_reply_to_someone_not_shown_working_grants_nothing(h):
    question=say(h,'Bo asking which dose Pat is on.','bo',says=['pat'],types=['medication'])
    say(h,'Pat is on 5 mg.','nia',reply_to=question,says=['pat'],care=['nia'],types=['medication'])
    assert 'Pat is on 5 mg.' not in readable(h,'bo')


def test_reply_grant_covers_only_that_subject(h):
    say(h,'Pat is booked Friday.','sol',says=['pat'],care=['sol'],types=['schedule'])
    say(h,'Kim is booked Monday.','sol',says=['kim'],care=['sol'],types=['schedule'])
    say(h,'Kim takes 20 mg.','nia',says=['kim'],care=['nia'],types=['medication'])
    question=say(h,'Sol checking which dose Pat should bring Friday.','sol',says=['pat'],care=['sol'],types=['schedule'])
    say(h,'Pat brings the 5 mg tablets.','nia',reply_to=question,says=['pat'],care=['nia'],types=['medication'])
    assert 'Pat brings the 5 mg tablets.' in readable(h,'sol') and 'Kim takes 20 mg.' not in readable(h,'sol')


def test_reply_switch_off_ignores_reply_metadata():
    h=make(0,False)
    try:
        say(h,'Pat is booked Friday.','sol',says=['pat'],care=['sol'],types=['schedule'])
        question=say(h,'Sol checking which dose.','sol',says=['pat'],care=['sol'],types=['schedule'])
        say(h,'Pat brings the 5 mg tablets.','nia',reply_to=question,says=['pat'],care=['nia'],types=['medication'])
        assert 'Pat brings the 5 mg tablets.' not in readable(h,'sol')
    finally:h.tearDown()
