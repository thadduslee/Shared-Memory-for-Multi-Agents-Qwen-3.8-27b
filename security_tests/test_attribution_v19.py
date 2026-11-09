"""Batch 5: domain-independent disclosure, correction and projection regressions."""
from copy import deepcopy
from dataclasses import replace
import json
import re
import pytest

from agentic_memory.privacy import AccessDenied, PrivacyMemorySystem
from agentic_memory.privacy.permissions import PermissionUpdater, PermissionSourceEvent
from agentic_memory.privacy.processors import HashEmbedder
from agentic_memory.privacy.answering import PermissionQueryService
from agentic_memory.privacy import attribution_v19 as attribution
from agentic_memory.privacy.system import encoded
from security_tests import test_incremental_permissions as fixtures
from security_tests.test_acceptance_repair_v19 import h as task_h, offer, edit_acceptance


DIRECTORY={'owner':{'name':'Ada Vale','role':'client','aliases':['Ada Vale','Ada','Vale']},
    'peer':{'name':'Bea Vale','role':'client','aliases':['Bea Vale','Bea','Vale']},
    'worker':{'name':'Quinn Reed','role':'staff','aliases':['Quinn Reed','Quinn','Reed']},
    'outsider':{'name':'Morgan Lake','role':'client','aliases':['Morgan Lake','Morgan','Lake']}}
FIRST_NAMES={'Ada':'owner','Bea':'peer','Quinn':'worker','Morgan':'outsider'}


@pytest.fixture
def h():
    h=fixtures.IncrementalTests();h.setUp()
    people=['owner','peer','worker','outsider']
    h.actor_names={p:i+1 for i,p in enumerate(people)}
    h.bootstrap=replace(h.bootstrap,participants={p:{} for p in people},resources={p:{} for p in people},
        authorities={p:{cap:[p] for cap in ('permission','restrict','delegate')} for p in people},
        source_identities={'connector':people},discover_self_owned_participants=(),
        personal_owners={p:p for p in people},owner_read_actions=('search','read','derive'),
        directory=DIRECTORY,worker_roles=('staff',))
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19')
    for actor,name in [('owner','Ada Vale'),('peer','Bea Vale'),('worker','Quinn Reed'),('outsider','Morgan Lake')]:
        h.turn('My name is '+name+'.',actor=actor)
    h.classify=lambda p,s:'instruction';h.source_subject=None;h.says=None;h.mentioned=[]
    original=h.model.complete
    def reasoned(system,payload,*,schema=None):
        # A scripted stand-in for the model's own reading of the fact. The code
        # under test only verifies this decision; it does not read the wording.
        response=original(system,payload,schema=schema)
        if 'f' not in response:return response
        current=next(s for s in payload['sources'] if s['id']==payload['new_source_id'])
        speaker=next(p for p in h.actor_names if h.actor(payload,p)==current['source']['participant'])
        for f in response.get('f',{}).values():
            if f['kind'] in {'instruction','asserted_fact'}:
                said=h.says if h.says is not None else [
                    person for first,person in FIRST_NAMES.items() if first in current['text']] or [speaker]
                f.update(information_subject=h.actor(payload,said[0]) if len(said)==1 else None,
                    information_subjects=[h.actor(payload,p) for p in said],attribution_complete=bool(said),
                    action_actors=[h.actor(payload,speaker)] if re.search(r'\bI\b',current['text']) else [],
                    mentioned_people=list(h.mentioned))
                if not said:f['attribution_source']=None
        return response
    h.model.complete=reasoned
    try:yield h
    finally:h.tearDown()


def candidates(h):
    return [json.loads(r[0]) for r in h.core.db.execute("SELECT body FROM fact_candidates WHERE state='active' ORDER BY rowid")]


def raw(h,text,actor='worker',subject=None,says=None,mentioned=()):
    h.says=says;h.mentioned=list(mentioned)
    h.seq+=1
    job=h.updater.ingest(PermissionSourceEvent(str(h.seq),actor,'connector',text,subject=subject))
    h.updater.update(job['job_id'],max_attempts=2)
    return next((c for c in reversed(candidates(h)) if c['source_id']==job['source_id']),None)


def review(h,c,subjects,**extra):
    return {'subject':subjects[0] if len(subjects)==1 else None,'information_subjects':subjects,
        'attribution_complete':True,'action_actors':['worker'],'subject_source':c['source_id'],
        'subject_quote':c['quote'],'reason':'Review every disclosed identity and association.',**extra}


def grant(h,subject,reader='worker'):
    h.classify=lambda p,s:'context'
    h.policy=lambda p:[{**h.grant(p,recipient=reader,scope='Scheduling and personal information'),
        'subject':h.actor(p,subject)}]
    job=h.turn(f'{reader} may read my scheduling and personal information.',actor=subject)
    h.policy=lambda p:[];h.classify=lambda p,s:'instruction'
    return job


def packet(h,reader='worker'):
    return h.core.retrieve(h.reader(reader),'current information',k=100)


def test_author_and_action_actor_do_not_own_mixed_routing_note(h):
    c=raw(h,"Ada's appointment is Tuesday; Bea's appointment is Friday.")
    assert c['author']=='worker' and c['action_actors']==[]
    assert c['information_subjects']==['owner','peer']
    assert c['attribution']['state']=='RESOLVED'
    assert not packet(h)['evidence']
    assert not packet(h,'owner')['evidence'] and not packet(h,'peer')['evidence']


def test_all_subjects_require_current_recipient_specific_permissions(h):
    c=raw(h,"Ada's appointment is Tuesday; Bea's appointment is Friday.")
    grant(h,'owner')
    assert not packet(h)['evidence']
    grant(h,'peer')
    assert [r['text'] for r in packet(h)['evidence']]==[c['quote']]
    assert not packet(h,'outsider')['evidence']


def test_subject_can_read_own_worker_authored_fact(h):
    c=raw(h,"Ada's appointment is Tuesday.")
    assert c['information_subjects']==['owner']
    assert [r['text'] for r in packet(h,'owner')['evidence']]==[c['quote']]
    assert not packet(h)['evidence']


def test_first_person_action_actor_is_separate_from_its_beneficiary(h):
    c=raw(h,"I booked Ada's appointment for Tuesday.")
    assert c['author']=='worker' and c['action_actors']==['worker']
    assert c['information_subjects']==['owner']
    assert not packet(h)['evidence'] and packet(h,'owner')['evidence']


@pytest.mark.parametrize('text',["My appointment is Tuesday; Bea's appointment is Friday.","I live alone; Bea's appointment is Friday."])
def test_real_author_information_is_not_dropped_from_a_mixed_personal_note(h,text):
    c=raw(h,text,actor='outsider',says=['outsider','peer'])
    assert c['attribution']['state']=='RESOLVED'
    assert c['information_subjects']==['outsider','peer']
    assert not packet(h,'outsider')['evidence'] and not packet(h,'peer')['evidence']
    grant(h,'peer','outsider')
    assert [r['text'] for r in packet(h,'outsider')['evidence']]==[c['quote']]


def test_worker_listed_beside_the_person_they_serve_is_not_a_subject(h):
    c=raw(h,"I booked Ada's appointment for Tuesday.",says=['owner','worker'])
    assert c['information_subjects']==['owner'] and c['attribution']['state']=='RESOLVED'


@pytest.mark.parametrize('text',[
    'I booked the appointment for tomorrow.',
    'I have another person checking in later today.',
    'Calendar recap: the appointment is Tuesday.',
    'I filed the document and will keep the records separate.',
    'I am entering the list exactly as reviewed.',
    'I have the appointment details.',
    'I have Mira checking in later today.',
])
def test_worker_named_as_their_own_subject_is_not_author_ownership(h,text):
    c=raw(h,text,says=['worker'])
    assert c['attribution']['state']=='UNRESOLVED'
    assert 'Authorship/action performance is not evidence of personal information' in c['attribution']['unresolved']
    assert not packet(h)['evidence']
    assert not h.core.db.execute("SELECT 1 FROM attribution_views_v19 WHERE candidate_id=?",(c['id'],)).fetchone()


# The same decision must hold however the statement is phrased. None of these
# match a first-person pattern, and the old wording checks rejected all of them.
@pytest.mark.parametrize('text',[
    'Yes, please.',
    'I can come Tuesday morning.',
    'Please keep the clinical details private.',
    'Apixaban 5 milligrams twice a day.',
    'Okay, no pseudoephedrine.',
    'That works for you and me.',
    "My appointment is Tuesday.",
    "I've had spotting and cramping since last night.",
    'The message reads "I have a headache."',
    'My regular number is 555-0122, but please use my other number.',
])
def test_speaker_statements_resolve_from_the_model_decision_not_their_wording(h,text):
    c=raw(h,text,actor='outsider',says=['outsider'])
    assert c['information_subjects']==['outsider'] and c['attribution']['state']=='RESOLVED'
    assert any(r['text']==text for r in packet(h,'outsider')['evidence'])
    assert not packet(h)['evidence'] and not packet(h,'owner')['evidence']


def test_code_does_not_rescue_a_subject_the_model_did_not_name(h):
    c=raw(h,"I've had discomfort since last night.",actor='outsider',says=[])
    assert c['information_subjects']==[] and c['attribution']['state']=='UNRESOLVED'
    assert not packet(h,'outsider')['evidence']


def clear_window(h):
    # The fixture's introductions are recent turns by every client.
    h.classify=lambda p,s:'context'
    for _ in range(6):h.turn('Noted.',actor='worker')
    h.classify=lambda p,s:'instruction'


def contextual_note(h, receipt, *, cite_current=False):
    previous=h.model.complete
    def attributed(system,payload,*,schema=None):
        result=previous(system,payload,schema=schema)
        for item in result.get('f',{}).values():
            if item['kind'] in {'instruction','asserted_fact'}:
                source=next(s for s in payload['sources'] if
                    (s['id']==payload['new_source_id'] if cite_current else s['text']==receipt))
                item['attribution_source']=source['id']
        return result
    h.model.complete=attributed
    return raw(h,'The follow-up appointment is Tuesday.',says=['owner'])


@pytest.mark.parametrize('receipt',["I've had discomfort since last night.",'Yes, please.'])
@pytest.mark.parametrize('cite_current',[False,True])
def test_follow_up_note_uses_subject_turn_in_the_window_however_it_is_worded(h,receipt,cite_current):
    clear_window(h)
    h.classify=lambda p,s:'context';h.turn(receipt,actor='owner')
    h.classify=lambda p,s:'instruction'
    c=contextual_note(h,receipt,cite_current=cite_current)
    assert c['attribution']['state']=='RESOLVED' and c['information_subjects']==['owner']
    assert any(r['text']==c['quote'] for r in packet(h,'owner')['evidence'])
    assert not packet(h)['evidence'] and not packet(h,'outsider')['evidence']


def test_second_person_of_the_same_role_in_the_window_keeps_an_unnamed_fact_protected(h):
    clear_window(h)
    h.classify=lambda p,s:'context';h.turn('Yes, please.',actor='owner');h.turn('I can wait.',actor='peer')
    h.classify=lambda p,s:'instruction'
    c=contextual_note(h,'Yes, please.')
    assert c['attribution']['state']=='UNRESOLVED'
    assert any(reason.startswith('Ambiguous subject owner') for reason in c['attribution']['unresolved'])
    assert not packet(h,'owner')['evidence'] and not packet(h,'peer')['evidence']


def test_a_person_of_another_role_in_the_window_is_not_a_rival_subject(h):
    clear_window(h)
    h.classify=lambda p,s:'context';h.turn('Yes, please.',actor='owner');h.turn('One moment.',actor='worker')
    h.classify=lambda p,s:'instruction'
    assert contextual_note(h,'Yes, please.')['attribution']['state']=='RESOLVED'


def test_fact_that_names_its_subject_is_settled_despite_a_second_person_nearby(h):
    h.classify=lambda p,s:'context';h.turn('I can wait.',actor='peer');h.classify=lambda p,s:'instruction'
    c=raw(h,"Ada's appointment is Tuesday.")
    assert c['attribution']['state']=='RESOLVED' and c['information_subjects']==['owner']


def test_listing_both_people_settles_a_window_with_two_of_them(h):
    clear_window(h)
    h.classify=lambda p,s:'context';h.turn('Yes, please.',actor='owner');h.turn('I can wait.',actor='peer')
    h.classify=lambda p,s:'instruction'
    c=raw(h,'Neither appointment has moved.',says=['owner','peer'])
    assert c['attribution']['state']=='RESOLVED' and c['subject'] is None


def test_same_role_person_named_in_a_fact_must_be_listed(h):
    c=raw(h,"Ada's appointment is Tuesday; Bea's appointment is Friday.",says=['owner'])
    assert c['attribution']['state']=='UNRESOLVED'
    assert 'Unlisted person named in fact peer' in c['attribution']['unresolved']


def test_fact_about_a_worker_cannot_leave_out_the_client_it_names(h):
    c=raw(h,'Ada is a different client under Quinn.',actor='peer',says=['worker'])
    assert 'Unlisted person named in fact owner' in c['attribution']['unresolved']


def test_person_of_another_role_named_in_a_fact_is_not_made_a_subject(h):
    c=raw(h,"Quinn moved Ada's appointment to Tuesday.",says=['owner'])
    assert c['information_subjects']==['owner'] and c['attribution']['state']=='RESOLVED'
    assert [r['text'] for r in packet(h,'owner')['evidence']]==[c['quote']]


def test_subject_absent_from_the_window_stays_protected(h):
    clear_window(h)
    c=raw(h,'The follow-up appointment is Tuesday.',says=['owner'])
    assert 'Unanchored subject owner' in c['attribution']['unresolved'] and not packet(h,'owner')['evidence']


def test_citation_to_an_unsupporting_turn_falls_back_to_the_window_without_a_model_call(h):
    clear_window(h)
    h.classify=lambda p,s:'context';h.turn('Yes, please.',actor='owner');h.classify=lambda p,s:'instruction'
    attempts=h.core.db.execute('SELECT COUNT(*) FROM permission_attempts').fetchone()[0]
    c=contextual_note(h,'Yes, please.',cite_current=True)
    assert c['attribution']['state']=='RESOLVED'
    assert any(body=='Yes, please.' for body in c['subject_evidence'].values())
    assert h.core.db.execute('SELECT COUNT(*) FROM permission_attempts').fetchone()[0]==attempts+1


def test_fabricated_citation_quote_is_not_complete(h):
    c=raw(h,"Ada's appointment is Tuesday.")
    decision=review(h,c,['owner'],subject_quote='Words that never appeared.')
    assert h.updater.review_attribution(c['id'],decision,reason='Bad quote')['attribution']['state']=='UNRESOLVED'


def test_unknown_person_blocks_but_drugs_and_places_do_not(h):
    c=raw(h,'There is also a Mira Stone checking in later today.',says=['owner'],mentioned=['Mira Stone'])
    assert 'Unknown person Mira Stone' in c['attribution']['unresolved'] and not packet(h,'owner')['evidence']
    c=raw(h,"Ada takes Mavyret and stays at Rose Lodge in Women's Imaging Suite 2. Continue if needed.",says=['owner'])
    assert c['attribution']['state']=='RESOLVED'
    assert [r['text'] for r in packet(h,'owner')['evidence']]==[c['quote']]


def test_mentioned_name_absent_from_the_fact_is_ignored(h):
    c=raw(h,"Ada's appointment is Tuesday.",mentioned=['Mira Stone'])
    assert c['attribution']['state']=='RESOLVED'


def test_shared_surname_alone_is_ambiguous_and_full_names_are_not(h):
    c=raw(h,"Ada Vale's appointment is Tuesday; Bea Vale's appointment is Friday.")
    assert c['information_subjects']==['owner','peer'] and c['attribution']['state']=='RESOLVED'
    c=raw(h,"Vale's appointment is Friday.",says=['owner'])
    assert 'Ambiguous person Vale' in c['attribution']['unresolved']
    assert not packet(h,'owner')['evidence']


def test_directory_names_match_case_sensitively(h):
    c=raw(h,"Ada will drive by the lake and the vale.",says=['owner'])
    assert c['attribution']['state']=='RESOLVED' and c['information_subjects']==['owner']


def test_full_names_with_same_surname_remain_independently_authorizable(h):
    c=raw(h,"Ada Vale's appointment is Tuesday; Bea Vale's appointment is Friday.")
    assert c['attribution']['state']=='RESOLVED' and c['information_subjects']==['owner','peer']
    grant(h,'owner');assert not packet(h)['evidence']
    grant(h,'peer');assert [r['text'] for r in packet(h)['evidence']]==[c['quote']]


def test_exact_projection_excludes_other_subject_associations(h):
    c=raw(h,"Ada's appointment is Tuesday; Bea's appointment is Friday.")
    end=c['quote'].index(';')
    projected=h.updater.project_information(c['id'],[0,end],review(h,c,['owner']),reason='Independent complete Ada clause')
    assert projected['quote']=="Ada's appointment is Tuesday"
    assert projected['information_subjects']==['owner'] and projected['topics']==[] and projected['relationships']==[]
    h.updater.flush_bindings(max_batches=2)
    visible=packet(h,'owner')['evidence']
    assert [r['text'] for r in visible]==[projected['quote']]
    assert 'Bea' not in encoded(visible)
    assert not packet(h)['evidence']


def test_safe_projection_can_be_independent_of_an_unknown_counterpart(h):
    c=raw(h,"Ada's appointment is Tuesday; Zoe Stone's appointment is Friday.",mentioned=['Zoe Stone'])
    assert c['attribution']['state']=='UNRESOLVED'
    child=h.updater.project_information(c['id'],[0,c['quote'].index(';')],review(h,c,['owner']),reason='Independent known-subject assertion')
    h.updater.flush_bindings(max_batches=2)
    assert [r['text'] for r in packet(h,'owner')['evidence']]==[child['quote']]


def test_qualifiers_in_preceding_sentence_cannot_be_detached(h):
    text="Only if confirmed. Ada's appointment is Tuesday; Bea's appointment is Friday."
    c=raw(h,text)
    assert c['quote']==text
    start=text.index('Ada')
    with pytest.raises(ValueError):
        h.updater.project_information(c['id'],[start,text.index(';')],review(h,c,['owner']),reason='Would omit global condition')


def test_model_can_express_complete_multi_subjects_without_a_primary_subject(h):
    c=raw(h,"Ada's appointment is Tuesday; Bea's appointment is Friday.",says=['owner','peer'])
    assert c['subject'] is None and c['attribution']['state']=='RESOLVED'


def test_unknown_attribution_is_durable_without_fabricating_a_subject(h):
    c=raw(h,'A booking exists without an identified beneficiary.',says=[])
    assert c['information_subjects']==[] and c['attribution']['state']=='UNRESOLVED'
    assert not packet(h)['evidence']
    assert h.updater.attribution_state()


@pytest.mark.parametrize('text',[
    "Only if confirmed: Ada's appointment is Tuesday; Bea's appointment is Friday.",
    "Ada's appointment is not Tuesday; Bea's appointment is Friday.",
    "Ada's appointment is the same as Bea's appointment.",
    "Corrected: Ada's routing is Tuesday, Bea's routing is Friday.",
])
def test_projection_cannot_drop_qualifiers_or_associations(h,text):
    c=raw(h,text)
    with pytest.raises(ValueError):
        h.updater.project_information(c['id'],[0,text.find(';') if ';' in text else len(text)-1],review(h,c,['owner']),reason='Unsafe truncation')


def test_verified_task_authority_allows_its_subject_information(task_h):
    h=task_h
    offer(h);edit_acceptance(h);h.turn('I can make that.')
    h.model.edit=lambda p,r:r;h.classify=lambda p,s:'instruction'
    h.turn("Jonah's appointment is Tuesday.",actor='tess')
    assert any('Tuesday' in r['text'] for r in h.selected('tess'))
    assert not h.selected('pat')
    h.turn("Jonah's appointment is Tuesday; Pat's appointment is Friday.",actor='tess')
    assert all('Pat' not in r['text'] for r in h.selected('tess'))


def seed_wrong_author_binding(h):
    c=raw(h,"Ada's appointment is Tuesday; Bea's appointment is Friday.")
    wrong=deepcopy(c);wrong.update(subject='worker',information_subjects=['worker'])
    h.core.db.execute('UPDATE fact_candidates SET body=? WHERE id=?',(encoded(wrong),c['id']))
    h.updater.flush_bindings(max_batches=2)
    old=packet(h)
    assert old['evidence']
    return wrong,old


def test_correction_invalidates_confident_wrong_ownership_and_cached_release(h):
    wrong,old=seed_wrong_author_binding(h)
    corrected=h.updater.review_attribution(wrong['id'],review(h,wrong,['owner','peer']),reason='Author confused with both disclosed subjects')
    assert corrected['attribution']['version']==2 and corrected['information_subjects']==['owner','peer']
    assert not packet(h)['evidence']
    for rid in old['input_ids']:
        assert not h.core.allowed(h.reader('worker'),rid)
    with pytest.raises(AccessDenied):h.core.release(h.reader('worker'),old['input_ids'],{'answer':'stale'})
    history=[json.loads(r[0]) for r in h.core.db.execute('SELECT body FROM attribution_history_v19 WHERE candidate_id=? ORDER BY version',(wrong['id'],))]
    assert history[-1]['previous_candidate']['information_subjects']==['worker']


def test_confident_wrong_attribution_can_be_quarantined_before_repair(h):
    wrong,old=seed_wrong_author_binding(h)
    before=h.updater._snapshot()
    receipt=h.updater.request_attribution_review(wrong['id'],reason='Confident wrong ownership detected')
    assert receipt['state']=='REVIEW_REQUIRED' and h.updater._snapshot()!=before
    assert not packet(h)['evidence']
    with pytest.raises(AccessDenied):h.core.release(h.reader('worker'),old['input_ids'],{'answer':'cached'})
    result=h.updater.review_attribution(wrong['id'],review(h,wrong,['owner','peer']),reason='Original source reviewed')
    assert result['attribution']['version']==3


def test_attribution_review_is_host_only(h):
    c=raw(h,"Ada's appointment is Tuesday.")
    host=h.updater.ctx;h.updater.ctx=h.reader('owner')
    try:
        with pytest.raises(AccessDenied):h.updater.request_attribution_review(c['id'],reason='Not a host')
        with pytest.raises(AccessDenied):h.updater.attribution_state()
    finally:h.updater.ctx=host


@pytest.mark.parametrize('operation',['process','evolve'])
def test_attribution_correction_during_model_call_is_enforced_before_results(h,operation):
    wrong,old=seed_wrong_author_binding(h)
    rid=old['evidence'][0]['id'];calls=[]
    def slow(system,payload,*,schema=None):
        calls.append(payload)
        h.updater.review_attribution(wrong['id'],review(h,wrong,['owner','peer']),reason='Correct during slow call')
        return {'updates':[],'edges':[]}
    h.model.complete=slow
    with pytest.raises(AccessDenied):
        if operation=='process':h.core.process(h.reader('worker'),h.model,[rid],'Derive',{})
        else:h.core.evolve(h.reader('worker'),h.model,[rid],job_id='attribution-slow',k=0)
    assert len(calls)==1 and not h.core._tickets


def test_later_update_cannot_reuse_corrected_author_ownership(h):
    wrong,old=seed_wrong_author_binding(h)
    h.updater.review_attribution(wrong['id'],review(h,wrong,['owner','peer']),reason='Correct author mistake')
    raw(h,"Ada's appointment is Wednesday; Bea's appointment is Thursday.")
    assert not packet(h)['evidence']


def test_one_revoked_subject_blocks_a_previously_fully_authorized_mixed_record(h):
    raw(h,"Ada's appointment is Tuesday; Bea's appointment is Friday.")
    grant(h,'owner');grant(h,'peer')
    old=packet(h);assert old['evidence']
    h.classify=lambda p,s:'context'
    h.policy=lambda p:[{'op':'revoke_assignment','assignment_id':aid,'authority_source':p['new_source_id'],
        'evidence':[p['new_source_id']],'reason':'Withdraw only this subject grant'}
        for aid,a in p['permission_state']['assignment'].items()
        if a['participant']==h.actor(p,'worker') and a['subject']==h.actor(p,'owner')]
    h.turn('Revoke worker access.',actor='owner')
    assert not packet(h)['evidence']
    with pytest.raises(AccessDenied):h.core.release(h.reader('worker'),old['input_ids'],{'answer':'stale mixed'})


def test_raw_or_unattributed_records_cannot_evade_disclosure_gate(h):
    c=raw(h,"Ada's appointment is Tuesday; Bea's appointment is Friday.")
    h.core.install_policy(h.host,'raw-test',{'allows':[{'principals':['worker','host'],'actions':['read','search','derive']}],
        'processors':['test:hash','test:semantic']})
    h.core.create(h.host,'bare:test','memory',{'text':c['quote']},policy_id='raw-test',compartment='work')
    assert not h.core.allowed(h.reader('worker'),'bare:test')


@pytest.mark.parametrize('change',['delete','revoke','correct'])
def test_projection_and_derived_lineage_recheck_authorization(h,change):
    c=raw(h,"Ada's appointment is Tuesday; Bea's appointment is Friday.")
    child=h.updater.project_information(c['id'],[0,c['quote'].index(';')],review(h,c,['owner']),reason='Exact clause')
    grant(h,'owner')
    h.updater.flush_bindings(max_batches=2)
    old=packet(h);assert old['evidence']
    parent=old['evidence'][0]['id']
    h.core.install_policy(h.host,'derived-test',{'allows':[{'principals':['worker'],'actions':['read','search','derive']}],
        'processors':['test:hash','test:semantic']})
    h.core.create(h.host,'derived:test','memory',{'text':child['quote']},policy_id='derived-test',compartment='work',dependencies=[parent],generated=True)
    assert h.core.allowed(h.reader('worker'),'derived:test')
    if change=='delete':h.core.delete(h.host,c['source_id'])
    elif change=='correct':h.updater.review_attribution(c['id'],review(h,c,['owner','peer']),reason='Rechecked interpretation')
    else:
        h.classify=lambda p,s:'context'
        def revoke(p):
            return [{'op':'revoke_assignment','assignment_id':aid,'authority_source':p['new_source_id'],
                'evidence':[p['new_source_id']],'reason':'Withdraw worker access'}
                for aid,a in p['permission_state']['assignment'].items() if a['participant']==h.actor(p,'worker') and not a.get('ownership')]
        h.policy=revoke;h.turn('Revoke worker access.',actor='owner')
    assert not h.core.allowed(h.reader('worker'),'derived:test')
    with pytest.raises(AccessDenied):h.core.release(h.reader('worker'),old['input_ids'],{'answer':'cached'})


def test_unauthorized_identity_never_enters_answer_input(h):
    raw(h,"Ada's private routing is Tuesday; Bea's private routing is Friday.")
    service=PermissionQueryService(h.updater);seen=[]
    original=h.core.process
    def capture(ctx,processor,ids,system,payload,**kwargs):
        if ctx.principal_id=='worker':seen.append(deepcopy(payload))
        return original(ctx,processor,ids,system,payload,**kwargs)
    h.core.process=capture
    result=service.answer('worker','Which person is assigned to which route?',h.model,as_of='now')['output']
    assert not result['retrieved_memory'] and not result['used_record_ids'] and not seen


def test_restart_preserves_history_and_protection(h):
    c=raw(h,"Ada's appointment is Tuesday; Bea's appointment is Friday.")
    h.core.close();h.core=PrivacyMemorySystem(h.path,HashEmbedder())
    reader=h.core.issue_session('worker','answer',actions=['read','search','derive'],compartments=['work'],processing_routes=['test:hash','test:semantic'])
    assert not h.core.retrieve(reader,'appointment')['evidence']
    assert h.core.db.execute('SELECT COUNT(*) FROM attribution_history_v19 WHERE candidate_id=?',(c['id'],)).fetchone()[0]==1


def test_explicit_migration_protects_legacy_attribution_without_rewriting_source(h):
    c=raw(h,"Ada's appointment is Tuesday.")
    original=deepcopy(c)
    h.core.db.execute('DELETE FROM privacy_metadata WHERE key=?',('attribution-policy:work',))
    with pytest.raises(ValueError,match='fresh store'):
        PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19')
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19',attribution_migration='quarantine_existing')
    assert not packet(h,'owner')['evidence']
    legacy=deepcopy(c);legacy.pop('attribution');legacy.pop('information_subjects')
    h.core.db.execute('UPDATE fact_candidates SET body=? WHERE id=?',(encoded(legacy),c['id']))
    assert not packet(h,'owner')['evidence']
    repaired=h.updater.review_attribution(c['id'],review(h,c,['owner']),reason='Explicit legacy review')
    assert repaired['information_subjects']==['owner']
    history=json.loads(h.core.db.execute('SELECT body FROM attribution_history_v19 WHERE candidate_id=? AND version=0',(c['id'],)).fetchone()[0])
    assert history['previous_candidate']['digest']==attribution.digest(original)


def test_unknown_policy_version_cannot_silently_downgrade_or_read(h):
    raw(h,"Ada's appointment is Tuesday.")
    assert packet(h,'owner')['evidence']
    before=h.updater._snapshot()
    h.core.db.execute('UPDATE privacy_metadata SET value=? WHERE key=?',('future-policy','attribution-policy:work'))
    assert h.updater._snapshot()!=before
    assert not packet(h,'owner')['evidence']
    with pytest.raises(ValueError,match='policy version mismatch'):
        PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19')
