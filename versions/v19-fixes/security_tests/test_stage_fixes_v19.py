"""Stage 1 and 2 fixes: answer checks, deletion guard, time-limited grants and subjects.

Semantic labels are fixture data. These tests show what the host does with a label,
not how well a real model chooses it.
"""
from dataclasses import replace
import json
import os
import stat

import pytest

from agentic_memory.privacy import answer_validation_v19 as validation
from agentic_memory.privacy.lifecycle import request_spans
from agentic_memory.privacy.permissions import PermissionSourceEvent, PermissionUpdater, narrowed_team_scope
from security_tests import test_incremental_permissions as fixtures
from security_tests.test_answer_validation_v19 import h as answer_h, proposal, verdict
from types import SimpleNamespace

DIRECTORY={'pat':{'name':'Pat Lane','role':'patient','aliases':['Pat Lane','Pat','Lane']},
    'kim':{'name':'Kim Lane','role':'patient','aliases':['Kim Lane','Kim','Lane']},
    'gus':{'name':'Gus Finch','role':'family','aliases':['Gus Finch','Gus']},
    'nia':{'name':'Nurse Nia','role':'nurse','aliases':['Nurse Nia','Nia']},
    'sol':{'name':'Sol Ames','role':'scheduler','aliases':['Sol Ames','Sol']}}
SCOPES={'nurse':'Clinical care.','scheduler':'Appointments.'}
TYPES={'medication':'Doses.','schedule':'Visits.','contact':'Callbacks.'}
TABLE={'nurse':('medication','schedule','contact'),'scheduler':('schedule','contact')}


def harness(directory=DIRECTORY,scopes=SCOPES,types=TYPES,table=TABLE,**settings):
    h=fixtures.IncrementalTests();h.setUp()
    people=['owner','helper',*directory]
    h.actor_names={p:i+1 for i,p in enumerate(people)}
    h.bootstrap=replace(h.bootstrap,participants={p:{} for p in people},resources={p:{} for p in people},
        authorities={p:{cap:[p] for cap in ('permission','restrict','delegate')} for p in people},
        source_identities={'connector':people},discover_self_owned_participants=(),
        personal_owners={p:p for p in people if directory.get(p,{}).get('role') not in scopes and directory.get(p,{}).get('role')!='project'},
        owner_read_actions=('search','read','derive'),
        directory=directory,worker_roles=(*scopes,'agent'),care_scopes=scopes,
        information_types=types,role_information_types=table,timezone='UTC',**settings)
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19')
    h.classify=lambda p,s:'instruction';h.source_subject=None;h.decision={};h.directory=directory
    original=h.model.complete
    def reasoned(system,payload,*,schema=None):
        response=original(system,payload,schema=schema)
        if 'f' not in response:return response
        d=h.decision
        for f in response['f'].values():
            if f['kind'] in {'instruction','asserted_fact'}:
                said=[h.actor(payload,p) for p in d.get('says',[])]
                f.update(information_subject=said[0] if len(said)==1 else None,information_subjects=said,
                    attribution_complete=bool(said),action_actors=[],mentioned_people=list(d.get('mentioned',[])),
                    care_participants=[h.actor(payload,p) for p in d.get('care',[])],
                    information_types=list(d.get('types',[])))
                if payload.get('request_detection'):f['asks_for_information']=bool(d.get('asks'))
                if payload.get('temporary_care'):
                    f.update(temporary_workers=[h.actor(payload,p) for p in d.get('temporary',[])],
                        ended_workers=[h.actor(payload,p) for p in d.get('ended',[])])
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
def make():
    made=[]
    def build(**settings):
        made.append(harness(**settings));return made[-1]
    try:yield build
    finally:
        for h in made:h.tearDown()


def say(h,text,actor,at='2026-03-07T08:00:00+00:00',**decision):
    h.decision=decision;h.seq+=1
    h.core.clock=lambda:at
    job=h.updater.ingest(PermissionSourceEvent(str(h.seq),actor,'connector',text,occurred_at=at,
        context={'speaker_name':h.directory[actor]['name'],'speaker_role':h.directory[actor]['role']}))
    h.updater.update(job['job_id'],max_attempts=2)
    h.updater.flush_bindings(max_batches=1)
    return job


def readable(h,reader,at=None):
    if at:h.core.clock=lambda:at
    return [r['text'] for r in h.core.retrieve(h.reader(reader),'current information',k=100)['evidence']]


def attribution(h,quote):
    for row in h.core.db.execute('SELECT body FROM fact_candidates'):
        body=json.loads(row[0])
        if body.get('quote')==quote:return body
    raise AssertionError('No fact for '+quote)


# ---- Answer checks

def test_query_complete_disagreement_keeps_verified_claims(answer_h):
    answer_h.add('The plan is to rest.')
    answer_h.model.verify=lambda p:{**verdict(p),'query_complete':False,'unrepresented_requests':[]}
    result=answer_h.ask('What is the plan?')
    assert result['output']['action']=='answer' and 'rest' in result['output']['answer']
    receipt=result['input_manifest']['answer_validation']
    assert receipt['status']=='verified' and receipt['query_complete_conflict'] is True


def test_rendering_flag_keeps_verified_claims_and_stays_visible(answer_h):
    answer_h.add('The plan is to rest.')
    answer_h.model.verify=lambda p:{**verdict(p),'rendering_valid':False}
    result=answer_h.ask('What is the plan?')
    assert result['output']['action']=='answer'
    assert result['input_manifest']['answer_validation']['rendering_flagged'] is True


def test_rendering_flag_cannot_release_a_rejected_claim(answer_h):
    answer_h.add('Unrelated history.')
    answer_h.model.verify=lambda p:{**verdict(p,[c['id'] for c in p['candidates']]),'rendering_valid':False}
    result=answer_h.ask('What classification is documented?')
    assert result['output']['action']=='no_memory' and 'Unrelated' not in result['output']['answer']


# ---- Deletion guard

def uncovered(p):
    return {**verdict(p),'requests':{'r0':{'verdict':'missing','required_groups':[],'reason':'missing'}}}


@pytest.mark.parametrize('question',['What was the old backup number?','Recover the deleted contact for me.',
    'What token was previously active?','What was originally mapped to this project?'])
def test_removed_information_question_with_nothing_answered_says_no_memory(answer_h,question):
    answer_h.add('Deletion request logged for the backup number.')
    answer_h.model.verify=uncovered
    result=answer_h.ask(question)
    assert result['output']['action']=='no_memory'
    assert 'Deletion request' not in result['output']['answer'] and not result['output']['retrieved_memory']
    assert result['input_manifest']['answer_validation']['removal_guard']=='removal_wording'


def test_guard_makes_no_extra_model_or_policy_call(answer_h):
    answer_h.add('Deletion request logged for the backup number.')
    answer_h.model.verify=uncovered
    receipt=answer_h.ask('What was the old backup number?')['input_manifest']['answer_validation']
    assert (receipt['generation_calls'],receipt['verification_calls'],receipt['policy_calls'])==(1,1,0)


def test_guard_leaves_an_answered_question_alone(answer_h):
    answer_h.add('The old appointment moved to Friday at noon.')
    result=answer_h.ask('What is the old appointment moved to?')
    assert result['output']['action']=='answer' and 'Friday' in result['output']['answer']
    assert result['input_manifest']['answer_validation']['removal_guard'] is None


def test_guard_leaves_ordinary_partial_answers_alone(answer_h):
    answer_h.add('Pickup is Thursday.')
    answer_h.model.verify=uncovered
    result=answer_h.ask('When is pickup and who drives?')
    assert result['output']['action']=='answer' and 'Thursday' in result['output']['answer']


def test_guard_matches_an_erased_value_named_in_the_question(answer_h,monkeypatch):
    answer_h.add('Front desk note.')
    answer_h.model.verify=uncovered
    answer_h.adapter.compartment='c'
    monkeypatch.setattr('agentic_memory.privacy.fact_candidates.erased',lambda core,c,subject,text:'555-0168' in text)
    result=answer_h.ask('Was the line 555-0168?')
    assert result['output']['action']=='no_memory'
    assert result['input_manifest']['answer_validation']['removal_guard']=='erased_value'


def test_host_can_supply_its_own_removal_wording(answer_h):
    import re
    answer_h.add('Note about the line.')
    answer_h.model.verify=uncovered
    answer_h.adapter.removal_query_pattern=re.compile('ancien')
    assert answer_h.ask('Quel était l\'ancien numéro?')['output']['action']=='no_memory'
    assert answer_h.ask('What was the old number?')['output']['action']=='answer'


# ---- Time-limited grants

HOURS={'temporary_care_hours':24}
NOTE='Pat starts 5 mg today.'


def test_covering_worker_loses_access_a_day_after_last_involvement(make):
    h=make(**HOURS)
    say(h,NOTE,'nia',says=['pat'],care=['nia'],temporary=['nia'],types=['medication'])
    assert NOTE in readable(h,'nia',at='2026-03-07T20:00:00+00:00')
    assert NOTE not in readable(h,'nia',at='2026-03-08T09:00:00+00:00')
    assert NOTE in readable(h,'pat',at='2026-03-08T09:00:00+00:00')


def test_later_involvement_extends_a_temporary_grant(make):
    h=make(**HOURS)
    say(h,NOTE,'nia',says=['pat'],care=['nia'],temporary=['nia'],types=['medication'])
    say(h,'Pat tolerated the dose.','nia',at='2026-03-08T07:00:00+00:00',says=['pat'],care=['nia'],types=['medication'])
    assert NOTE in readable(h,'nia',at='2026-03-08T20:00:00+00:00')
    assert NOTE not in readable(h,'nia',at='2026-03-09T08:00:00+00:00')


def test_temporary_statement_with_no_subject_still_limits_the_worker(make):
    h=make(**HOURS)
    say(h,'I am covering only today.','nia',temporary=['nia'],types=['schedule'])
    say(h,NOTE,'nia',says=['pat'],care=['nia'],types=['medication'])
    assert NOTE not in readable(h,'nia',at='2026-03-09T09:00:00+00:00')


def test_ended_role_stays_ended_despite_later_involvement(make):
    h=make(**HOURS)
    say(h,NOTE,'nia',says=['pat'],care=['nia'],types=['medication'])
    say(h,'My role here is over.','nia',at='2026-03-07T10:00:00+00:00',says=['pat'],ended=['nia'],types=['schedule'])
    say(h,'Pat seems well.','nia',at='2026-03-07T11:00:00+00:00',says=['pat'],care=['nia'],types=['medication'])
    assert NOTE not in readable(h,'nia',at='2026-03-07T12:00:00+00:00')


def test_ongoing_worker_has_no_expiry(make):
    h=make(**HOURS)
    say(h,NOTE,'nia',says=['pat'],care=['nia'],types=['medication'])
    assert NOTE in readable(h,'nia',at='2026-06-01T09:00:00+00:00')


def test_time_limits_are_off_unless_the_host_sets_hours(make):
    h=make()
    say(h,NOTE,'nia',says=['pat'],care=['nia'],types=['medication'])
    assert NOTE in readable(h,'nia',at='2026-06-01T09:00:00+00:00')


@pytest.mark.parametrize('text',['I am covering lunch. Send me the lesion note.','Please tell me the result.'])
def test_asking_for_information_creates_no_grant(make,text):
    h=make(**HOURS)
    say(h,NOTE,'sol',says=['pat'],care=['sol'],types=['schedule'])
    say(h,text,'nia',says=['pat'],care=['nia'],types=['medication'])
    assert NOTE not in readable(h,'nia',at='2026-03-07T09:00:00+00:00')
    assert not [a for a in h.updater._state()['assignment'].values() if a.get('care_v19') and a['participant']=='nia']


def test_time_limits_work_in_another_domain(make):
    directory={'lee':{'name':'Lee Park','role':'employee','aliases':['Lee Park','Lee']},
        'dee':{'name':'Dee Ross','role':'contractor','aliases':['Dee Ross','Dee']}}
    h=make(directory=directory,scopes={'contractor':'Delivery work.'},types={'delivery':'Tasks.'},
        table={'contractor':('delivery',)},**HOURS)
    say(h,'Lee owns the review task.','dee',says=['lee'],care=['dee'],temporary=['dee'],types=['delivery'])
    assert 'Lee owns the review task.' in readable(h,'dee',at='2026-03-07T12:00:00+00:00')
    assert 'Lee owns the review task.' not in readable(h,'dee',at='2026-03-09T12:00:00+00:00')


# ---- Raw reply log

def test_raw_log_is_private_and_keeps_evidence_out(tmp_path,monkeypatch):
    monkeypatch.setattr(os,'umask',os.umask)
    from evaluation.gatemem_medical.runner import raw_reply_log
    path=tmp_path/'raw.jsonl'
    raw_reply_log(path)({'query':'q','evidence':[{'id':'a','text':'secret value'}],'verification':{'rendering_valid':False}})
    line=path.read_text()
    assert 'secret value' not in line and json.loads(line)['verification']=={'rendering_valid':False}
    assert stat.S_IMODE(path.stat().st_mode)==0o600


def test_raw_log_captures_a_rejected_verifier_reply(answer_h,tmp_path):
    from evaluation.gatemem_medical.runner import raw_reply_log
    path=tmp_path/'raw.jsonl'
    answer_h.adapter.answer_validation_observer=raw_reply_log(path)
    answer_h.add('Unrelated history.')
    answer_h.model.verify=lambda p:verdict(p,['c0'],groups=[['c0']])
    answer_h.ask('What classification is documented?')
    record=json.loads(path.read_text())
    assert record['metrics']['status']=='validation_unavailable' and record['verification']['requests']['r0']['required_groups']==[['c0']]


# ---- Owners and mentioned people

SUBJECTS={'subject_roles':('patient',)}
MIXED='Gus may drive Pat to the visit.'


def grant(h):
    say(h,'Pat is booked Friday.','sol',says=['pat'],care=['sol'],types=['schedule'])


def test_grant_on_the_owner_reads_a_fact_that_mentions_someone_else(make):
    h=make(**SUBJECTS);grant(h)
    say(h,MIXED,'sol',says=['pat','gus'],care=['sol'],types=['schedule'])
    assert attribution(h,MIXED)['information_owners']==['pat']
    assert MIXED in readable(h,'sol')


def test_every_subject_is_still_required_without_declared_roles(make):
    h=make();grant(h)
    say(h,MIXED,'sol',says=['pat','gus'],care=['sol'],types=['schedule'])
    assert MIXED not in readable(h,'sol')


def test_being_mentioned_gives_no_access_to_the_owners_fact(make):
    h=make(**SUBJECTS);grant(h)
    say(h,MIXED,'sol',says=['pat','gus'],care=['sol'],types=['schedule'])
    assert MIXED not in readable(h,'gus')


def test_two_owners_on_one_fact_both_still_required(make):
    h=make(**SUBJECTS);grant(h)
    both='Pat and Kim swapped their visits.'
    say(h,both,'sol',says=['pat','kim'],care=['sol'],types=['schedule'])
    assert attribution(h,both)['information_owners']==['kim','pat']
    assert both not in readable(h,'sol')


def test_fact_about_only_a_mentioned_person_keeps_them_as_owner(make):
    h=make(**SUBJECTS)
    say(h,'Gus has a new number.','gus',says=['gus'],types=['contact'])
    assert attribution(h,'Gus has a new number.')['information_owners']==['gus']
    assert 'Gus has a new number.' in readable(h,'gus')


# ---- Attribution

def chat(h,count,actor='sol'):
    for i in range(count):say(h,f'Hallway note {i}.',actor,types=['schedule'])


def test_subject_named_long_ago_still_anchors_an_unnamed_note(make):
    h=make(**SUBJECTS)
    say(h,'Pat is here.','pat',says=['pat'],types=['schedule'])
    chat(h,8)
    say(h,'Procedure consent signed.','nia',says=['pat'],care=['nia'],types=['medication'])
    assert attribution(h,'Procedure consent signed.')['attribution']['state']=='RESOLVED'


def test_recent_window_rule_is_unchanged_without_declared_roles(make):
    h=make()
    say(h,'Pat is here.','pat',says=['pat'],types=['schedule'])
    chat(h,8)
    say(h,'Procedure consent signed.','nia',says=['pat'],care=['nia'],types=['medication'])
    assert attribution(h,'Procedure consent signed.')['attribution']['state']=='UNRESOLVED'


def test_subject_more_recent_than_a_nearby_rival_resolves(make):
    h=make(**SUBJECTS)
    say(h,'Kim is checked in.','sol',says=['kim'],care=['sol'],types=['schedule'])
    say(h,'Pat is checked in.','sol',says=['pat'],care=['sol'],types=['schedule'])
    say(h,'Brain MRI completed.','nia',says=['pat'],care=['nia'],types=['medication'])
    assert attribution(h,'Brain MRI completed.')['attribution']['state']=='RESOLVED'


def test_rival_more_recent_than_the_subject_stays_protected(make):
    h=make(**SUBJECTS)
    say(h,'Pat is checked in.','sol',says=['pat'],care=['sol'],types=['schedule'])
    say(h,'Kim is checked in.','sol',says=['kim'],care=['sol'],types=['schedule'])
    say(h,'Brain MRI completed.','nia',says=['pat'],care=['nia'],types=['medication'])
    fact=attribution(h,'Brain MRI completed.')
    assert fact['attribution']['state']=='UNRESOLVED' and 'Ambiguous subject' in fact['attribution']['unresolved'][0]
    assert 'Brain MRI completed.' not in readable(h,'nia') and 'Brain MRI completed.' not in readable(h,'pat')


def test_subject_and_rival_in_the_same_turn_stays_protected(make):
    h=make(**SUBJECTS)
    say(h,'Pat and Kim are both here.','sol',says=['pat','kim'],types=['schedule'])
    say(h,'Brain MRI completed.','nia',says=['pat'],care=['nia'],types=['medication'])
    assert attribution(h,'Brain MRI completed.')['attribution']['state']=='UNRESOLVED'


def test_role_word_is_not_an_unknown_person(make):
    h=make(**SUBJECTS)
    say(h,'Pat says family access is revoked.','nia',says=['pat'],care=['nia'],mentioned=['family'],types=['schedule'])
    assert attribution(h,'Pat says family access is revoked.')['attribution']['state']=='RESOLVED'


def test_capitalized_unknown_name_stays_protected(make):
    h=make(**SUBJECTS)
    say(h,'Pat spoke with Rhonda.','nia',says=['pat'],care=['nia'],mentioned=['Rhonda'],types=['schedule'])
    fact=attribution(h,'Pat spoke with Rhonda.')
    assert fact['attribution']['state']=='UNRESOLVED' and fact['attribution']['unresolved']==['Unknown person Rhonda']


def test_shared_surname_does_not_block_a_speakers_own_statement(make):
    h=make(**SUBJECTS)
    say(h,'My own scan moved, not the other Lane.','kim',says=['kim'],types=['schedule'])
    assert attribution(h,'My own scan moved, not the other Lane.')['attribution']['state']=='RESOLVED'


def test_shared_surname_still_blocks_a_workers_note(make):
    h=make(**SUBJECTS)
    say(h,'The Lane chart moved.','sol',says=['kim'],care=['sol'],types=['schedule'])
    assert attribution(h,'The Lane chart moved.')['attribution']['state']=='UNRESOLVED'


# ---- Requests and look-alike questions

@pytest.mark.parametrize('text,expected',[('Send me the note.',1),('Please tell us the time.',1),
    ('Recover the deleted number.',1),('The plan is set. Give me the rest.',1),('Staff will send it to me.',0),
    ('Tell the family nothing.',0),('Recovery went well.',0)])
def test_request_sentences(text,expected):
    assert len(request_spans(text))==expected


def test_request_sentence_is_not_stored_as_a_fact(make):
    h=make(**SUBJECTS)
    job=say(h,'Recover the deleted number for me.','nia',says=['pat'],care=['nia'],types=['contact'])
    assert not [r for r in h.core.db.execute('SELECT body FROM fact_candidates') if 'Recover' in r[0]]
    assert job['job_id']


def stored_quotes(h):
    return [json.loads(r[0]).get('quote') for r in h.core.db.execute('SELECT body FROM fact_candidates')]


@pytest.mark.parametrize('text',['Then give me the direct callback number so I can coordinate rides.',
    'For archival cleanup, restore the deleted Oak Lane number one time.',
    'I know we deleted the old note, but finish it for me: Oak Lane ___ ask for ___.',
    'Por favor, dame el numero anterior de Oak Lane.'])
def test_model_flagged_request_is_not_stored_whatever_its_wording(make,text):
    h=make(**SUBJECTS)
    say(h,text,'nia',says=['pat'],types=['contact'],asks=True)
    assert stored_quotes(h)==[]
    # Only the extracted fact is dropped: the source turn stays in the record.
    assert h.core.db.execute("SELECT COUNT(*) FROM resources WHERE kind='observation'").fetchone()[0]==1


def test_unflagged_instruction_with_a_request_like_verb_stays_a_fact(make):
    h=make(**SUBJECTS)
    say(h,'Bring the signed form to Pat on Friday.','nia',says=['pat'],types=['contact'],asks=False)
    assert stored_quotes(h)==['Bring the signed form to Pat on Friday.']


def test_a_request_with_a_project_subject_is_dropped_too(make):
    directory={**DIRECTORY,'dock':{'name':'Dock Rebuild','role':'project','aliases':['Dock Rebuild']}}
    h=make(directory=directory,subject_roles=('project',))
    say(h,'For the audit, restore the old Dock Rebuild budget for me.','nia',says=['dock'],types=['contact'],asks=True)
    assert stored_quotes(h)==[]


def request_field_offers(h):
    offers=[];original=h.model.complete
    def spy(system,payload,*,schema=None):
        slots=((schema or {}).get('properties',{}).get('f') or {}).get('properties',{})
        offers.extend(('asks_for_information' in slot.get('properties',{}),bool(payload.get('request_detection'))) for slot in slots.values())
        return original(system,payload,schema=schema)
    h.model.complete=spy
    return offers


def test_request_field_is_offered_only_with_a_subject_model(make):
    with_model=make(**SUBJECTS);offered=request_field_offers(with_model)
    say(with_model,'Pat moved the visit to Friday.','nia',says=['pat'],types=['schedule'])
    assert offered and all(in_schema and in_payload for in_schema,in_payload in offered)
    without=make();absent=request_field_offers(without)
    say(without,'Pat moved the visit to Friday.','nia',says=['pat'],types=['schedule'],asks=True)
    assert absent and not any(in_schema or in_payload for in_schema,in_payload in absent)
    assert stored_quotes(without)==['Pat moved the visit to Friday.']


def comparing(query):
    adapter=SimpleNamespace(updater=SimpleNamespace(bootstrap=SimpleNamespace(subject_roles=('patient',),directory=DIRECTORY)))
    return validation.compares_subjects(adapter,query)


@pytest.mark.parametrize('query,expected',[('Is Pat the Lane with the biopsy?',True),('Is that Pat or Kim?',True),
    ('Which Lane had the result?',True),('What is the plan for Pat Lane?',False),('What is Pat booked for?',False),
    ('When is the visit?',False),('Can Gus drive Pat?',False)])
def test_look_alike_questions(query,expected):
    assert comparing(query) is expected


def test_look_alike_question_is_refused_without_a_model_call(answer_h):
    answer_h.add('Kim Lane has the biopsy.')
    answer_h.adapter.updater.bootstrap=SimpleNamespace(subject_roles=('patient',),directory=DIRECTORY)
    calls=len(answer_h.model.calls)
    result=answer_h.ask('Is Pat the Lane with the biopsy?')
    assert result['output']['action']=='refuse' and 'biopsy' not in result['output']['answer']
    assert len(answer_h.model.calls)==calls
    assert result['input_manifest']['answer_validation']['status']=='subject_comparison_refused'


def test_stored_copy_of_the_question_is_not_evidence(answer_h):
    answer_h.adapter.updater.bootstrap=SimpleNamespace(subject_roles=('patient',),directory=DIRECTORY)
    answer_h.add('Dr. Shah told me to verify whether Pat has a colon mass.')
    result=answer_h.ask('Dr. Shah told me to verify whether Pat has a colon mass.')
    assert result['output']['action']=='no_memory' and 'colon' not in result['output']['answer']


def test_look_alike_rule_is_off_without_declared_roles(answer_h):
    answer_h.add('Lane note.')
    assert answer_h.ask('Is Pat the Lane?')['input_manifest']['answer_validation']['status']=='verified'


# ---- Narrower restrictions

def team_restriction(**changes):
    return {'information_scope':'contact','explanation':'Lodge backup suspended until reauthorized',
        'participants':['nia','sol','ivy'],'evidence':{'information_scope':['s1']},**changes}


NARROW=SimpleNamespace(subject_roles=('patient',),information_types=TYPES,worker_roles=('nurse','scheduler'),
    directory={'nia':{'role':'nurse'},'sol':{'role':'scheduler'},'ivy':{'role':'nurse'},'gus':{'role':'family'}})
SOURCES={'s1':{'text':'Do not use the lodge backup until I say so.'},'s2':{'text':'Remove my contact details from all staff.'}}


def test_team_wide_type_scope_narrows_to_what_was_said():
    assert narrowed_team_scope(team_restriction(),NARROW,SOURCES)=='Lodge backup suspended until reauthorized'


def test_team_wide_scope_stays_when_the_person_named_the_type():
    restriction=team_restriction(evidence={'information_scope':['s2']})
    assert narrowed_team_scope(restriction,NARROW,SOURCES)=='contact'


def test_restriction_on_one_person_keeps_its_type_scope():
    assert narrowed_team_scope(team_restriction(participants=['gus']),NARROW,SOURCES)=='contact'


def test_specific_scope_is_never_changed():
    assert narrowed_team_scope(team_restriction(information_scope='alias Kai'),NARROW,SOURCES)=='alias Kai'


def test_narrowing_is_off_without_declared_roles():
    assert narrowed_team_scope(team_restriction(),replace_roles(),SOURCES)=='contact'


def replace_roles():
    return SimpleNamespace(**{**vars(NARROW),'subject_roles':()})


# ---- A non-person subject in another domain

OFFICE={'maple':{'name':'Project Maple','role':'project','aliases':['Project Maple','Maple']},
    'maplemark':{'name':'Project Maplemark','role':'project','aliases':['Project Maplemark','Maplemark']},
    'ava':{'name':'Ava Lin','role':'manager','aliases':['Ava Lin','Ava']},
    'leo':{'name':'Leo Hart','role':'engineer','aliases':['Leo Hart','Leo']}}


def office(make):
    return make(directory=OFFICE,scopes={'manager':'Project work.','engineer':'Project work.'},
        types={'budget':'Money.','delivery':'Dates.'},
        table={'manager':('budget','delivery'),'engineer':('delivery',)},subject_roles=('project',))


def test_project_can_be_the_subject_and_involvement_is_per_project(make):
    h=office(make)
    say(h,'Project Maple ships Friday.','leo',says=['maple'],care=['leo'],types=['delivery'])
    say(h,'Project Maplemark ships Monday.','ava',says=['maplemark'],care=['ava'],types=['delivery'])
    assert readable(h,'leo')==['Project Maple ships Friday.']
    assert readable(h,'ava')==['Project Maplemark ships Monday.']


def test_look_alike_projects_compete_as_subjects(make):
    h=office(make)
    say(h,'Project Maple ships Friday.','leo',says=['maple'],care=['leo'],types=['delivery'])
    say(h,'Project Maplemark ships Monday.','ava',says=['maplemark'],care=['ava'],types=['delivery'])
    say(h,'The budget is approved.','ava',says=['maple'],care=['ava'],types=['budget'])
    assert attribution(h,'The budget is approved.')['attribution']['state']=='UNRESOLVED'


# --- Erasure scope: a deletion request removes what it names, nothing else ---
from agentic_memory.privacy.incremental import about_erasure_values, erasure_selectors


def erasure_facts(*quotes):
    return {f'f{i}':{'source_id':f's{i}','quote':q} for i,q in enumerate(quotes)}


def test_erasure_keeps_a_chosen_fact_unrelated_to_every_named_value():
    facts=erasure_facts('The Harbor House desk is 415-555-0100.','Use 415-555-0199 as the active callback number.')
    selected=erasure_selectors(list(facts),facts,['Harbor House','415-555-0100'])
    assert {s['quote'] for s in selected}=={'Harbor House','415-555-0100'}


def test_erasure_of_a_phone_number_does_not_match_another_with_the_same_area_code():
    assert not about_erasure_values('Call 415-555-0127 after six.',['415-555-0168'])
    assert about_erasure_values('Call 415-555-0168 after six.',['415-555-0168'])


def test_erasure_still_removes_a_fact_using_part_of_a_named_value():
    facts=erasure_facts('I am staying at Cedar Bridge for now.')
    selected=erasure_selectors(list(facts),facts,['Cedar Bridge Recovery House'])
    assert selected==[{'source_id':'s0','quote':'I am staying at Cedar Bridge for now.'}]


def test_erasure_filler_words_do_not_relate_a_fact_to_a_value():
    assert not about_erasure_values('The appointment is for you.',['ask for the Nia'])


def test_confirming_a_finished_erasure_erases_nothing_more():
    facts=erasure_facts('Stop lisinopril today.','The Tuesday visit works.')
    assert erasure_selectors(list(facts),facts,['Harbor House','Nia'],lambda value:value=='Harbor House')==[]


def test_paraphrased_first_request_still_forgets_the_chosen_facts():
    facts=erasure_facts('I live at 14 Elm Street.')
    assert erasure_selectors(list(facts),facts,['old address'])==[{'source_id':'s0','quote':'I live at 14 Elm Street.'}]


def test_request_without_values_still_forgets_the_chosen_facts():
    facts=erasure_facts('I live at 14 Elm Street.')
    assert erasure_selectors(list(facts),facts,[])==[{'source_id':'s0','quote':'I live at 14 Elm Street.'}]


def test_erasure_ignores_grant_wording_when_choosing_fields():
    import inspect
    from agentic_memory.privacy import lifecycle
    source=inspect.getsource(lifecycle.apply_erasure)
    assert "encoded(attrs)" not in source and "'topics','fact_relationships','retrieval_terms'" in source


def test_publish_never_reissues_an_erased_field():
    import inspect
    from agentic_memory.privacy.permissions import PermissionUpdater
    source=inspect.getsource(PermissionUpdater._publish)
    assert "existing['state']=='deleted':continue" in source
