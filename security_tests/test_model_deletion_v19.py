"""A patient's deletion request: the model chooses the facts, code verifies the owner."""
import json
import pytest

from agentic_memory.privacy.incremental import erasure_selectors
from security_tests.test_care_v19 import h, say, readable  # noqa: F401

FIRST='Use callback 415-555-0186 for urgent calls.';SECOND='Starting Sunday use 415-555-0174 instead.'
KEPT='Pat is booked Friday at noon.'


def seed(h):
    say(h,FIRST,'pat',says=['pat'],types=['contact'])
    say(h,SECOND,'pat',says=['pat'],types=['contact'])
    say(h,KEPT,'sol',says=['pat'],care=['sol'],types=['schedule'])
    assert {FIRST,SECOND,KEPT} <= set(readable(h,'pat'))


def facts_by_quote(h,p):
    return {c['quote']:c['id'] for c in p['candidates']}


def request(h,text,quotes,*,values=None,timing='now',condition=None,effective_at=None,speaker='pat',subject='pat'):
    def erase(p):
        ids=facts_by_quote(h,p)
        return [{'op':'erase_candidates','candidate_ids':[ids[q] for q in quotes],'subject':h.actor(p,subject),
            'values':values if values is not None else list(quotes),'timing':timing,'condition':condition,
            'effective_at':effective_at,'authority_source':p['new_source_id'],'evidence':[p['new_source_id']],
            'reason':'The patient asked to delete this.'}]
    h.classify=lambda p,s:'context';h.policy=erase
    try:return h.turn(text,actor=speaker)
    finally:h.policy=lambda p:[];h.classify=lambda p,s:'instruction'


def stored(h):
    return [json.loads(r[0])['quote'] for r in h.core.db.execute("SELECT body FROM fact_candidates WHERE state='active'")]


def scheduled(h):
    return h.core.db.execute("SELECT id,state,body FROM scheduled_erasures").fetchall()


NUMBERS=['415-555-0186','415-555-0174']


@pytest.mark.parametrize('text',['Please delete my temporary safe numbers.','Get rid of those phone numbers.',
    'I do not want you holding on to the callback numbers anymore.'])
def test_request_without_digits_deletes_the_numbers_however_it_is_phrased(h,text):
    seed(h);request(h,text,[FIRST,SECOND],values=NUMBERS)
    assert not any(n in q for n in NUMBERS for q in stored(h))
    assert KEPT in readable(h,'pat') and not any(n in ' '.join(readable(h,'pat')) for n in NUMBERS)


def test_values_that_are_not_exact_still_delete_the_chosen_facts(h):
    seed(h);request(h,'Please delete my temporary safe numbers.',[FIRST,SECOND],values=['temporary safe numbers'])
    assert FIRST not in stored(h) and SECOND not in stored(h) and KEPT in stored(h)


def test_later_copy_of_a_deleted_number_is_not_stored_or_readable(h):
    seed(h);request(h,'Please delete my temporary safe numbers.',[FIRST,SECOND],values=NUMBERS)
    say(h,'Entries 415-555-0186 and 415-555-0174 were cleared.','nia',says=['pat'],types=['contact'])
    assert not any(n in ' '.join(readable(h,'pat')+stored(h)) for n in NUMBERS)


def test_earlier_copy_outside_the_chosen_facts_is_also_removed(h):
    seed(h)
    say(h,'Note: urgent line is 415-555-0186.','nia',says=['pat'],types=['contact'])
    request(h,'Delete that number.',[FIRST],values=['415-555-0186'])
    assert not any('415-555-0186' in q for q in stored(h))
    assert SECOND in stored(h)


@pytest.mark.parametrize('word',['Tuesday','callback','use'])
def test_one_ordinary_word_cannot_become_a_deletion_value(h,word):
    fact={'id':'f','quote':'Use callback on Tuesday.','source_id':'s'}
    assert erasure_selectors(['f'],{'f':fact},[word])==[{'source_id':'s','quote':'Use callback on Tuesday.'}]


def test_number_alone_is_a_valid_value_and_replaces_the_whole_sentence(h):
    fact={'id':'f','quote':'Call 415-555-0186 now.','source_id':'s'}
    assert erasure_selectors(['f'],{'f':fact},['415-555-0186'])==[{'source_id':'s','quote':'415-555-0186'}]


def test_unrelated_facts_survive_a_deletion(h):
    seed(h);request(h,'Delete my numbers.',[FIRST,SECOND],values=NUMBERS)
    assert KEPT in stored(h) and KEPT in readable(h,'sol')


def test_conditional_request_waits_then_runs_when_a_worker_reports_it_done(h):
    seed(h)
    request(h,'After Tuesday is done, please delete my numbers.',[FIRST,SECOND],values=NUMBERS,
        timing='conditional',condition='After Tuesday is done')
    assert FIRST in readable(h,'pat') and len(scheduled(h))==1
    assert not h.updater._state()['restriction']
    erasure=scheduled(h)[0][0]
    h.classify=lambda p,s:'context'
    h.policy=lambda p:[{'op':'execute_erasure','erasure_id':erasure,'authority_source':p['new_source_id'],
        'reason':'The worker reports Tuesday is finished.'}]
    h.turn('Tuesday coordination is complete; entries cleared.',actor='nia')
    h.policy=lambda p:[];h.classify=lambda p,s:'instruction'
    assert not any(n in q for n in NUMBERS for q in stored(h))
    assert scheduled(h)[0][1]=='complete' and KEPT in stored(h)


def test_reworded_condition_keeps_the_request_with_the_full_statement(h):
    seed(h)
    text='Once my Tuesday visit is over, please delete my numbers.'
    request(h,text,[FIRST,SECOND],values=NUMBERS,timing='conditional',condition='after the Tuesday visit ends')
    body=json.loads(scheduled(h)[0][2])
    assert body['condition']==text and FIRST in readable(h,'pat')


def test_future_time_does_not_run_early(h):
    seed(h)
    request(h,'Delete my numbers next year.',[FIRST,SECOND],values=NUMBERS,timing='at',effective_at='2099-01-01T00:00:00+00:00')
    assert FIRST in stored(h) and scheduled(h)[0][1]=='pending'


def test_someone_else_cannot_confirm_a_condition(h):
    from agentic_memory.privacy.permissions import PermissionRepairExhausted
    seed(h)
    request(h,'After Tuesday is done, please delete my numbers.',[FIRST,SECOND],values=NUMBERS,
        timing='conditional',condition='After Tuesday is done')
    erasure=scheduled(h)[0][0]
    h.classify=lambda p,s:'context'
    h.policy=lambda p:[{'op':'execute_erasure','erasure_id':erasure,'authority_source':p['new_source_id'],'reason':'Claimed done.'}]
    with pytest.raises(PermissionRepairExhausted):h.turn('It is all done, delete them.',actor='kim')
    assert scheduled(h)[0][1]=='pending'


@pytest.mark.parametrize('speaker',['kim','sol'])
def test_only_the_owner_can_request_a_deletion(h,speaker):
    from agentic_memory.privacy.permissions import PermissionRepairExhausted
    seed(h)
    with pytest.raises(PermissionRepairExhausted):
        request(h,'Delete those numbers of hers.',[FIRST,SECOND],values=NUMBERS,speaker=speaker)
    assert FIRST in stored(h) and SECOND in stored(h)


def test_person_cannot_delete_another_persons_facts_by_naming_them_as_their_own(h):
    from agentic_memory.privacy.permissions import PermissionRepairExhausted
    seed(h)
    with pytest.raises(PermissionRepairExhausted):
        request(h,'Delete those numbers.',[FIRST,SECOND],values=NUMBERS,speaker='kim',subject='kim')
    assert FIRST in stored(h) and SECOND in stored(h)


def test_deleted_values_cannot_be_read_by_staff_who_could_read_them_before(h):
    note='Pat prefers callback 415-555-0186 for urgent calls.'
    say(h,note,'nia',says=['pat'],care=['nia'],types=['contact'])
    say(h,'Pat is resting.','nia',says=['pat'],care=['nia'],types=['contact'])
    assert note in readable(h,'nia')
    request(h,'Delete my number.',[note],values=['415-555-0186'])
    assert not any('415-555-0186' in t for t in readable(h,'nia'))
    assert 'Pat is resting.' in readable(h,'nia')
