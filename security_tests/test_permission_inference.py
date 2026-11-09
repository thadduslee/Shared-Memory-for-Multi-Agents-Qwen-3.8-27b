"""General permission inference contracts; no medical role or keyword compiler."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from agentic_memory.privacy import PrivacyMemorySystem, AccessDenied
from agentic_memory.privacy.permissions import (
    PermissionBootstrap, PermissionSourceEvent, PermissionUpdater,
    PermissionProposalRejected, PermissionRevisionConflict,
)
from agentic_memory.privacy.processors import HashEmbedder, ScriptedProcessor, ProcessorFailure


class Model(ScriptedProcessor):
    processor_id='test:permission'
    def __init__(self):super().__init__();self.handler=None;self.core=None
    def complete(self, system, payload, *, schema=None):
        assert not self.core.db.in_transaction, 'LLM called inside transaction'
        self.calls.append(deepcopy(payload))
        if self.handler:return self.handler(payload)
        return {'revision':payload['revision'],
                'considered_restrictions':list(payload['permission_state']['restriction']), 'operations':[]}


def operation(name, evidence, **kwargs):
    return {'op':name,'evidence':evidence,'explanation':'Grounded in the identified source statements.',**kwargs}


def checks():
    return {'task_requires':'The pickup instruction is needed to arrange this ride.',
            'scope_supported':'The original scope authorizes pickup instructions only.',
            'context_preserved':'The complete pickup clause retains time and event association.',
            'metadata_safe':'Only opaque view IDs and selected text are visible.',
            'restrictions_checked':'Every current restriction was evaluated for this text and action.'}


class PermissionInference(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.core=PrivacyMemorySystem(Path(self.tmp.name)/'memory.sqlite',HashEmbedder(),clock=lambda:'2026-10-26T12:00:00+00:00')
        self.model=Model();self.model.core=self.core
        self.routes=['test:hash','test:permission']
        self.host=self.core.issue_session('host','ingest',actions=['manage_policy','ingest','read','search','derive','audit','write','delete','link'],
            compartments=['conversation'],processing_routes=self.routes)
        self.core.register_processor(self.host,self.model)
        self.bootstrap=PermissionBootstrap(
            participants={'owner':{'role':'customer'},'helper':{'role':'pharmacist'},'outsider':{}},
            resources={'owner':{'type':'customer'},'project':{'type':'project'}},
            source_identities={'connector':['owner','helper','outsider']},
            authorities={sid:{'permission':['owner'],'restrict':['owner'],'delegate':['owner']} for sid in ['owner','project']},
            task_convention='Confirmed ride coordination permits only necessary pickup instructions.')
        self.updater=PermissionUpdater(self.core,self.host,'conversation',self.bootstrap,self.model,version='1')
        self.reader=self.core.issue_session('helper','answer',actions=['read','search','derive','export','write','forward','delegate'],
            compartments=['conversation'],processing_routes=self.routes)
        self.index=0

    def tearDown(self):self.core.close();self.tmp.cleanup()

    def ingest(self,text='Please arrange my ride.',actor='owner',subject='owner',timestamp=None):
        self.index+=1
        return self.updater.ingest(PermissionSourceEvent(str(self.index),actor,'connector',text,subject,timestamp))

    def apply(self,job,ops=()):
        self.model.handler=lambda p:{'revision':p['revision'],
            'considered_restrictions':list(p['permission_state']['restriction']),'operations':list(ops)}
        return self.updater.run(job['job_id'])

    def evidence(self,sid,*names):return {name:[sid] for name in names}

    def task(self,first,second,activity='ride',status='established',subject='owner'):
        return operation('establish_activity' if status=='established' else 'observe_participation',
            self.evidence(second,'participant','subject','activity','status','basis'),id=activity,participant='helper',subject=subject,
            activity='ride coordination',status=status,basis='corroborated' if status=='established' else 'performance',
            performance=[first],corroboration=[second],
            self_check={'involvement_supported':'The participant performed ride coordination.',
                        'corroboration_supports':'The owner accepted this specific ride coordination, rather than rejecting contact.',
                        'scope_resolved':'The authenticated owner and requested ride are identified.'},
            information_needs=[{'description':'Pickup instructions for this ride','evidence':[first,second]}])

    def assignment(self,sid,basis='task',aid='access',status='active',subject='owner',**extra):
        return operation('propose_assignment',self.evidence(sid,'participant','subject','actions','information_scope','status','basis',
            *(['authority'] if basis=='explicit' else [])),id=aid,participant='helper',subject=subject,
            activity_id='ride' if basis=='task' else None,basis=basis,authority='owner' if basis=='explicit' else None,
            actions=['search','read','derive'],information_scope='pickup',status=status,
            self_check={'basis_supported':'The identified evidence supports this assignment basis.',
                        'actions_supported':'Only read, search and derive are authorized.',
                        'scope_supported':'Pickup instructions for the identified ride.'},**extra)

    def binding(self,sid,aid='access',spans=None):
        evidence=self.evidence(sid,'source_id','necessity','subject')
        existing=self.updater.inspect()['assignment'].get(aid)
        evidence['authorization']=existing['evidence']['information_scope'] if existing else [sid]
        return operation('bind_information',evidence,assignment_id=aid,
            source_id=sid,spans=spans or [[0,17]],necessity='The established ride requires this pickup time and location.',self_check=checks(),
            evidence_kind='instruction',restriction_decisions=[{'restriction_id':rid,'applies':True,'explanation':'Restriction concerns these instructions.','evidence':[sid]}
                for rid in self.updater.inspect()['restriction']])

    def establish(self):
        first=self.ingest('I am arranging your ride.',actor='helper');self.apply(first)
        second=self.ingest('Pickup is at 9 AM; the appointment concerns a confidential diagnosis. Yes, please arrange the ride.')
        self.apply(second,[self.task(first['source_id'],second['source_id']),self.assignment(second['source_id']),self.binding(second['source_id'])])
        return first,second

    def selected(self):return self.core.search(self.reader,'pickup',k=20)

    def test_exact_views_and_metadata_do_not_include_private_context(self):
        _,source=self.establish()
        records=self.selected();self.assertEqual(len(records),1)
        self.assertEqual(records[0]['text'],'Pickup is at 9 AM')
        self.assertEqual(records[0]['source'],{})
        public=self.core.read(self.reader,records[0]['id'])
        self.assertEqual(public['attributes'],{});self.assertEqual(public['dependencies'],[])
        self.assertNotIn('source_ref',public['payload'])
        self.assertFalse(self.core.allowed(self.reader,source['source_id']))
        self.assertNotIn('diagnosis',str(public));self.assertNotIn('ride coordination',str(public))

    def test_pending_update_blocks_old_access_and_release_until_success(self):
        self.establish();rid=self.selected()[0]['id']
        job=self.ingest('Do not share my pickup information with helper.')
        self.assertFalse(self.core.allowed(self.reader,rid));self.assertEqual(self.selected(),[])
        with self.assertRaises(AccessDenied):self.core.release(self.reader,[rid],{'answer':'old pickup'})
        self.apply(job);self.assertTrue(self.core.allowed(self.reader,rid))

    def test_processor_failure_is_resumable_and_fail_closed(self):
        self.establish();job=self.ingest('Stop sharing.')
        def fail(_):raise ProcessorFailure('unavailable')
        self.model.handler=fail
        with self.assertRaises(ProcessorFailure):self.updater.run(job['job_id'])
        self.assertEqual(self.selected(),[])
        self.assertEqual(self.updater.pending_jobs(),[job['job_id']])
        self.apply(job);self.assertTrue(self.selected())

    def test_no_new_category_binding_without_new_necessity_assessment(self):
        self.establish();job=self.ingest('Updated pickup is at 10 AM.')
        self.apply(job);self.assertNotIn('10 AM',' '.join(r['text'] for r in self.selected()))
        later=self.ingest('The ride coordinator needs the new pickup time.')
        self.apply(later,[self.binding(job['source_id'],spans=[[0,26]])])
        self.assertIn('10 AM',' '.join(r['text'] for r in self.selected()))

    def restriction(self,sid,rid='private',scope='pickup',actions=None,**extra):
        return operation('record_restriction',self.evidence(sid,'issuer','participants','subject','information_scope','actions','reason','supersedes','validity'),
            id=rid,issuer='owner',participants=['helper'],subject='owner',information_scope=scope,
            actions=actions or ['read','search','derive'],reason='Owner restricts pickup disclosure.',effect='deny',
            self_check={'denial_supported':'The owner prohibits this disclosure.',
                        'permitted_scope_excluded':'Only the prohibited information is covered.'},**extra)

    def test_restriction_before_assignment_persists_across_future_assignments(self):
        restricted=self.ingest('Do not share my pickup instructions with helper.')
        self.apply(restricted,[self.restriction(restricted['source_id'])])
        self.establish();self.assertEqual(self.selected(),[])
        next_job=self.ingest('New ride instruction: Pickup at 1 PM.')
        self.apply(next_job,[self.assignment(next_job['source_id'],basis='explicit',aid='second'),
                           self.binding(next_job['source_id'],aid='second',spans=[[0,34]])])
        self.assertEqual(self.selected(),[])
        self.assertEqual(self.updater.inspect()['activity']['ride']['status'],'established')

    def test_scoped_restriction_leaves_activity_and_unrelated_binding_alive(self):
        self.establish()
        j=self.ingest('Do not disclose the diagnostic purpose, but continue ride coordination.')
        self.apply(j,[self.restriction(j['source_id'],scope='diagnosis')])
        self.assertTrue(self.selected());self.assertEqual(self.updater.inspect()['activity']['ride']['status'],'established')

    def test_exact_restriction_overlap_and_actions(self):
        _,source=self.establish();rid=self.selected()[0]['id']
        j=self.ingest('Do not forward or export my pickup instruction.')
        self.apply(j,[self.restriction(j['source_id'],actions=['export','forward'],
                    source_selectors=[{'source_id':source['source_id'],'spans':[[0,17]]}])])
        self.assertTrue(self.core.allowed(self.reader,rid,'read'))
        self.assertFalse(self.core.allowed(self.reader,rid,'export'))
        self.assertFalse(self.core.allowed(self.reader,rid,'write'))
        self.assertFalse(self.core.allowed(self.reader,rid,'delegate'))

    def test_reauthorization_retains_both_restrictions_and_authority_evidence(self):
        self.establish();j=self.ingest('Do not share pickup instructions.')
        self.apply(j,[self.restriction(j['source_id'])]);self.assertFalse(self.selected())
        k=self.ingest('You may share pickup instructions again, but never the diagnostic purpose.')
        self.apply(k,[self.restriction(k['source_id'],rid='replacement',scope='diagnosis',supersedes='private')])
        self.assertTrue(self.selected())
        self.assertEqual(self.updater.inspect()['restriction']['private']['superseded_by'],'replacement')
        self.assertEqual(self.core.db.execute("SELECT COUNT(*) FROM permission_records WHERE kind='restriction'").fetchone()[0],3)

    def test_scheduled_reauthorization_does_not_lift_restriction_early(self):
        self.establish();j=self.ingest('Keep pickup instructions private.')
        self.apply(j,[self.restriction(j['source_id'])])
        k=self.ingest('Tomorrow share pickup again, but keep diagnosis private.',timestamp='2026-10-26T12:00:00Z')
        self.apply(k,[self.restriction(k['source_id'],rid='tomorrow',scope='diagnosis',supersedes='private',
                    validity={'effective_from':'2026-10-27T00:00:00Z'})])
        self.assertFalse(self.selected())
        self.core.clock=lambda:'2026-10-27T01:00:00Z';self.assertTrue(self.selected())

    def test_tentative_cannot_create_task_access(self):
        j=self.ingest('I could help with a ride.',actor='helper')
        self.apply(j,[self.task(j['source_id'],j['source_id'],status='tentative')])
        k=self.ingest('Pickup is at 9 AM.')
        with self.assertRaises(PermissionProposalRejected):self.apply(k,[self.assignment(k['source_id'])])
        self.assertEqual(self.updater.inspect()['activity']['ride']['status'],'tentative')
        self.assertEqual(self.selected(),[])

    def test_corroboration_must_have_authorized_authenticated_source(self):
        j=self.ingest('I am doing the ride.',actor='helper');self.apply(j)
        k=self.ingest('Yes, helper is doing it.',actor='outsider')
        with self.assertRaises(PermissionProposalRejected):self.apply(k,[self.task(j['source_id'],k['source_id'])])

    def test_authorized_handoff_can_establish_activity_before_performance(self):
        j=self.ingest('Helper, please arrange my ride. Pickup is at 9 AM.')
        task=operation('establish_activity',self.evidence(j['source_id'],'participant','subject','activity','status','authority','basis'),
            id='ride',participant='helper',subject='owner',activity='ride coordination',status='established',basis='handoff',authority='owner',
            self_check={'involvement_supported':'The owner requests this particular ride from helper.',
                        'corroboration_supports':'This is an authorized handoff before performance.',
                        'scope_resolved':'The owner identifies their own ride.'})
        self.apply(j,[task,self.assignment(j['source_id']),self.binding(j['source_id'],spans=[[32,50]])])
        self.assertTrue(self.selected())

    def test_ambiguous_subject_is_preserved_without_grant(self):
        j=self.ingest('I am helping someone with a ride.',actor='helper',subject=None)
        task=self.task(j['source_id'],j['source_id'],status='tentative',subject=None)
        task['unresolved']=[{'question':'Which resource is this activity for?','evidence':[j['source_id']]}]
        self.apply(j,[task]);self.assertIsNone(self.updater.inspect()['activity']['ride']['subject'])
        self.assertFalse(self.selected())

    def test_explicit_document_permission_needs_no_task(self):
        j=self.ingest('You may read this pickup instruction: pickup at 9 AM.')
        self.apply(j,[self.assignment(j['source_id'],basis='explicit'),self.binding(j['source_id'])])
        self.assertEqual(self.updater.inspect()['activity'],{});self.assertTrue(self.selected())

    def test_end_activity_expires_task_grant_but_not_independent_permission(self):
        self.establish();j=self.ingest('You may independently read this record even after the ride: pickup at 9 AM.')
        self.apply(j,[self.assignment(j['source_id'],basis='explicit',aid='independent'),self.binding(j['source_id'],aid='independent')])
        k=self.ingest('This delivery is complete.')
        self.apply(k,[operation('end_activity',self.evidence(k['source_id'],'activity_id','reason'),activity_id='ride',reason='Delivery completed.')])
        state=self.updater.inspect();self.assertEqual(state['assignment']['access']['status'],'expired')
        self.assertEqual(state['assignment']['independent']['status'],'active');self.assertTrue(self.selected())

    def test_ending_other_activity_can_bound_explicit_permission(self):
        self.establish();j=self.ingest('You may read this only until this ride is complete.')
        a=self.assignment(j['source_id'],basis='explicit',aid='until-delivery',validity={'end_activity':'ride'})
        a['evidence']['validity']=[j['source_id']]
        self.apply(j,[a,self.binding(j['source_id'],aid='until-delivery')])
        rid=[r['id'] for r in self.selected() if 'You may' in r['text']][0]
        k=self.ingest('This ride is complete.')
        self.apply(k,[operation('end_activity',self.evidence(k['source_id'],'activity_id','reason'),activity_id='ride',reason='Complete')])
        self.assertFalse(self.core.allowed(self.reader,rid))

    def test_relative_expiry_without_date_context_is_rejected(self):
        self.establish();j=self.ingest('Remove ride access after today.')
        with self.assertRaises(PermissionProposalRejected):
            self.apply(j,[operation('schedule_expiry',self.evidence(j['source_id'],'assignment_id','effective_at'),
                                 assignment_id='access',effective_at='2026-10-27T00:00:00Z')])

    def test_scheduled_expiry_and_observed_vs_effective_time(self):
        self.establish();j=self.ingest('Remove ride access after today.',timestamp='2026-10-26T10:00:00Z')
        self.apply(j,[operation('schedule_expiry',self.evidence(j['source_id'],'assignment_id','effective_at'),assignment_id='access',effective_at='2026-10-27T00:00:00Z')])
        self.assertTrue(self.selected());self.core.clock=lambda:'2026-10-27T00:00:00Z';self.assertFalse(self.selected())
        source=self.core.read(self.host,j['source_id'])['payload']['source']
        self.assertNotEqual(source['observed_at'],source['occurred_at'])

    def test_revision_conflict_cannot_publish_stale_model_result(self):
        self.establish();j=self.ingest('New permission decision.')
        def mutate(p):
            self.core.install_policy(self.host,'unrelated',{'allows':[{'principals':['host'],'actions':['read']}],'processors':self.routes})
            return {'revision':p['revision'],'considered_restrictions':[],'operations':[]}
        self.model.handler=mutate
        with self.assertRaises(PermissionRevisionConflict):self.updater.run(j['job_id'])
        self.assertFalse(self.selected());self.apply(j);self.assertTrue(self.selected())

    def test_replay_reuses_recorded_result_and_detects_changed_source(self):
        j=self.ingest();self.apply(j);n=len(self.model.calls)
        repeated=self.updater.ingest(PermissionSourceEvent('1','owner','connector','Please arrange my ride.','owner'))
        self.updater.run(repeated['job_id']);self.assertEqual(len(self.model.calls),n)
        with self.assertRaises(ValueError):self.updater.ingest(PermissionSourceEvent('1','owner','connector','changed','owner'))

    def test_correction_or_deletion_queues_new_job_and_suspends_evidence(self):
        first,_=self.establish();rid=self.selected()[0]['id']
        self.core.delete(self.host,first['source_id'])
        self.assertFalse(self.core.allowed(self.reader,rid))
        original=self.core.db.execute('SELECT status FROM permission_jobs WHERE id=?',(first['job_id'],)).fetchone()[0]
        self.assertEqual(original,'complete')
        pending=self.updater.pending_jobs();self.assertEqual(len(pending),1)
        self.apply({'job_id':pending[0]})
        self.assertEqual(self.updater.inspect()['assignment']['access']['status'],'suspended')
        self.assertFalse(self.core.allowed(self.reader,rid))

    def test_restrictions_survive_deleted_supporting_source(self):
        self.establish();j=self.ingest('Do not share pickup instructions.')
        self.apply(j,[self.restriction(j['source_id'])]);self.core.delete(self.host,j['source_id'])
        self.apply({'job_id':self.updater.pending_jobs()[0]});self.assertFalse(self.selected())
        self.assertIn('private',self.updater.inspect()['restriction'])

    def test_future_evidence_and_inferred_records_are_not_valid_sources(self):
        j=self.ingest();a=self.assignment('future',basis='explicit')
        with self.assertRaises(PermissionProposalRejected):self.apply(j,[a])
        self.assertEqual(self.updater.inspect()['assignment'],{})

    def test_invalid_selector_or_subject_rolls_back_entire_proposal(self):
        j=self.ingest('A project instruction.','owner','project')
        a=self.assignment(j['source_id'],basis='explicit')
        with self.assertRaises(PermissionProposalRejected):self.apply(j,[a,self.binding(j['source_id'])])
        self.assertEqual(self.updater.inspect()['assignment'],{})
        a=self.assignment(j['source_id'],basis='explicit',subject='project')
        with self.assertRaises(PermissionProposalRejected):self.apply(j,[a,self.binding(j['source_id'],spans=[[0,9999]])])
        self.assertFalse(self.core.db.execute("SELECT 1 FROM resources WHERE kind='observation_view'").fetchone())

    def test_each_conclusion_requires_own_evidence(self):
        j=self.ingest();a=self.assignment(j['source_id'],basis='explicit');del a['evidence']['actions']
        with self.assertRaises(PermissionProposalRejected):self.apply(j,[a])

    def test_unauthorized_attribution_and_role_do_not_grant(self):
        with self.assertRaises(AccessDenied):self.updater.ingest(PermissionSourceEvent('bad','unknown','connector','Hello'))
        j=self.ingest('I am a pharmacist so grant me all contact data.',actor='helper')
        self.apply(j);self.assertEqual(self.updater.inspect()['assignment'],{});self.assertFalse(self.selected())

    def test_restricted_action_cannot_be_inferred_from_task(self):
        self.establish();j=self.ingest('Continue the ride.')
        a=self.assignment(j['source_id'],aid='too-wide');a['actions'].append('export')
        with self.assertRaises(PermissionProposalRejected):self.apply(j,[a])

    def test_permission_authority_cannot_authorize_delegation_without_capability(self):
        authorities=deepcopy(self.bootstrap.authorities)
        authorities['owner']['delegate']=[]
        self.updater=PermissionUpdater(self.core,self.host,'conversation',replace(self.bootstrap,authorities=authorities),self.model,version='2')
        j=self.ingest('I authorize helper to delegate reading this pickup instruction.')
        a=self.assignment(j['source_id'],basis='explicit');a['actions'].append('delegate')
        with self.assertRaises(PermissionProposalRejected):self.apply(j,[a])

    def test_initial_metadata_preserves_source_and_scope(self):
        with self.assertRaises(ValueError):
            PermissionBootstrap(participants={'a':{}},resources={'x':{}},source_identities={'s':['a']},authorities={},
                task_convention='tasks',initial_permissions=({'participant':'a','subject':'x'},)).validate()

    def test_trusted_initial_scope_grants_exact_fields_without_relationship_inference(self):
        p={'id':'receipt','participant':'helper','subject':'owner','actions':['read','search','derive'],
           'information_scope':'pickup','source':'authenticated-initial-metadata','scope':'Initial ride scheduling access only.'}
        boot=replace(self.bootstrap,initial_permissions=(p,))
        self.updater=PermissionUpdater(self.core,self.host,'conversation',boot,self.model,version='2')
        j=self.ingest('Pickup is at 9 AM; diagnosis is confidential.')
        a=self.assignment(j['source_id'],basis='initial',initial_permission_id='receipt')
        self.apply(j,[a,self.binding(j['source_id'])]);self.assertTrue(self.selected())
        self.assertEqual(self.updater.inspect()['activity'],{})
        stored=self.core.db.execute('SELECT body FROM permission_bootstrap').fetchone()[0]
        self.assertIn('authenticated-initial-metadata',stored)

    def test_explicit_new_policy_version_blocks_then_suspends_previous_convention(self):
        self.establish();old=self.updater
        changed=replace(self.bootstrap,task_actions=('read',),task_convention='Only explicit narrow reading.')
        with self.assertRaises(AccessDenied):PermissionUpdater(self.core,self.host,'conversation',changed,self.model,version='1')
        self.updater=PermissionUpdater(self.core,self.host,'conversation',changed,self.model,version='2')
        self.assertFalse(self.selected());pending=self.updater.pending_jobs()
        self.apply({'job_id':pending[0]})
        self.assertEqual(self.updater.inspect()['assignment']['access']['status'],'suspended')
        with self.assertRaises(PermissionRevisionConflict):old.ingest(PermissionSourceEvent('stale','owner','connector','New statement','owner'))

    def test_failed_job_can_be_replaced_by_explicit_model_version(self):
        j=self.ingest('Please arrange my ride.')
        self.model.model='old-model'
        with self.assertRaises(ValueError):self.updater.run(j['job_id'])
        self.model.model='new-model'
        old=self.updater
        self.updater=PermissionUpdater(self.core,self.host,'conversation',self.bootstrap,self.model,version='2')
        jobs=self.updater.pending_jobs();self.assertEqual(len(jobs),1)
        self.assertEqual(old.run(j['job_id'])['status'],'superseded')
        self.apply({'job_id':jobs[0]});self.assertFalse(self.updater.pending_jobs())

    def test_correction_is_atomic_and_replayable(self):
        first,second=self.establish()
        correction=PermissionSourceEvent('correction','owner','connector','Pickup moved to 11 AM.','owner')
        result=self.updater.correct(correction,second['source_id'])
        self.assertFalse(self.selected())
        self.assertFalse(self.core._current(second['source_id']))
        for job in self.updater.pending_jobs():self.apply({'job_id':job})
        self.assertEqual(self.updater.correct(correction,second['source_id'])['status'],'complete')
        self.assertFalse(self.selected())

    def test_proposed_assignment_can_be_activated_after_new_permission_evidence(self):
        j=self.ingest('Proposed access to this pickup instruction.')
        self.apply(j,[self.assignment(j['source_id'],basis='explicit',status='proposed'),self.binding(j['source_id'])])
        self.assertFalse(self.selected())
        k=self.ingest('I authorize helper to read the pickup instructions now.')
        self.apply(k,[self.assignment(k['source_id'],basis='explicit')]);self.assertTrue(self.selected())

    def test_binding_decisions_enforce_restriction_even_with_different_scope_name(self):
        j=self.ingest('Do not share pickup information.')
        self.apply(j,[self.restriction(j['source_id'],scope='ride_times')])
        self.establish();self.assertFalse(self.selected())

    def test_semantic_restriction_applies_to_later_repeated_information(self):
        first,second=self.establish();j=self.ingest('Do not share this pickup instruction, including repeated copies.')
        self.apply(j,[self.restriction(j['source_id'],source_selectors=[{'source_id':second['source_id'],'spans':[[0,17]]}])])
        later=self.ingest('Pickup is at 9 AM; repeated instruction.')
        self.apply(later,[self.assignment(later['source_id'],basis='explicit',aid='later'),self.binding(later['source_id'],aid='later')])
        self.assertFalse(self.selected())

    def test_new_exact_restriction_covers_existing_binding_with_different_scope(self):
        _,source=self.establish();j=self.ingest('Do not share this ride time with helper.')
        self.apply(j,[self.restriction(j['source_id'],scope='ride_times',
                    source_selectors=[{'source_id':source['source_id'],'spans':[[0,17]]}])])
        self.assertFalse(self.selected())

    def test_guessed_question_cannot_be_compiled_as_factual_evidence(self):
        j=self.ingest('Was my pickup at 9 AM?')
        b=self.binding(j['source_id']);b['evidence_kind']='question'
        with self.assertRaises(PermissionProposalRejected):self.apply(j,[self.assignment(j['source_id'],basis='explicit'),b])

    def test_missing_semantic_corroboration_review_is_rejected(self):
        j=self.ingest('I am doing the ride.',actor='helper');self.apply(j)
        k=self.ingest('Please stop contacting me.')
        task=self.task(j['source_id'],k['source_id']);task['self_check']['corroboration_supports']=''
        with self.assertRaises(PermissionProposalRejected):self.apply(k,[task])

    def test_old_restriction_source_cannot_reauthorize_itself(self):
        j=self.ingest('Do not share pickup instructions.')
        self.apply(j,[self.restriction(j['source_id'])]);k=self.ingest('A later unrelated observation.')
        op=self.restriction(j['source_id'],rid='invalid',scope='diagnosis',supersedes='private')
        with self.assertRaises(PermissionProposalRejected):self.apply(k,[op])

    def test_inflight_replay_cannot_overwrite_a_completed_job(self):
        j=self.ingest('A source event.')
        def concurrent(payload):
            self.model.handler=None
            self.updater.run(j['job_id'])
            return {'revision':payload['revision'],'considered_restrictions':[],'operations':[]}
        self.model.handler=concurrent
        self.assertEqual(self.updater.run(j['job_id'])['status'],'complete')
        self.assertFalse(self.updater.pending_jobs())

    def test_contract_repair_receives_feedback_and_preserves_attempt_audit(self):
        j=self.ingest('A source event.')
        def repair(payload):
            if 'correction' not in payload:
                return {'revision':payload['revision'],'considered_restrictions':[],'operations':[{'op':'issue_session'}]}
            self.assertIn('Unsupported permission operation',payload['correction']['validation']['reason'])
            return {'revision':payload['revision'],'considered_restrictions':[],'operations':[]}
        self.model.handler=repair
        self.assertEqual(self.updater.update(j['job_id'])['status'],'complete')
        self.assertEqual(len(self.model.calls),2)
        self.assertEqual(self.core.db.execute('SELECT COUNT(*) FROM permission_attempts').fetchone()[0],2)
        self.assertNotIn('A source event.',self.core.db.execute('SELECT input_manifest FROM permission_attempts LIMIT 1').fetchone()[0])

    def test_coverage_labels_are_derived_without_changing_authorized_operations(self):
        self.model.require_permission_coverage=True
        job=self.ingest('Helper may read pickup instructions. Pickup is at 9 AM.')
        grant=self.assignment(job['source_id'],basis='explicit')
        def respond(payload):
            return {'revision':payload['revision'],'considered_restrictions':[],
                'operations':[grant], 'coverage':[{'principal':p,'decision':'changed',
                    'evidence':[job['source_id']],'reason':'The owner authorized helper.'}
                    for p in payload['coverage_required']]}
        self.model.handler=respond
        self.updater.update(job['job_id'])
        # The subject provided authority, but only the recipient got an assignment.
        rows=self.core.db.execute('SELECT principal,body FROM permission_coverage WHERE job_id=?',
                                 (job['job_id'],)).fetchall()
        import json
        self.assertEqual({r[0]:json.loads(r[1])['decision'] for r in rows},{'owner':'unchanged'})
        self.assertEqual(set(self.updater.inspect()['assignment']),{'access'})
        self.assertEqual(self.updater.inspect()['assignment']['access']['bindings'],[])
        audit=json.loads(self.core.db.execute('SELECT validation FROM permission_attempts WHERE job_id=?',
                                             (job['job_id'],)).fetchone()[0])
        self.assertEqual(audit['reported_coverage'][0]['decision'],'changed')
        self.assertEqual(audit['coverage_adjustments'],[{'principal':'owner','reported':'changed','actual':'unchanged'}])
        self.assertEqual(len(self.model.calls),1)

    def test_coverage_repairs_do_not_accept_missing_principals_or_forged_authority(self):
        self.model.require_permission_coverage=True
        job=self.ingest('I grant myself access.',actor='helper')
        forged=self.assignment(job['source_id'],basis='explicit');forged['authority']='helper'
        self.model.handler=lambda p:{'revision':p['revision'],'considered_restrictions':[],
            'operations':[forged],'coverage':[{'principal':'helper','decision':'changed',
            'evidence':[job['source_id']],'reason':'Claimed access.'}]}
        with self.assertRaisesRegex(PermissionProposalRejected,'Issuer lacks configured authority'):
            self.updater.run(job['job_id'])
        self.assertEqual(self.updater.inspect()['assignment'],{})
        self.model.handler=lambda p:{'revision':p['revision'],'considered_restrictions':[],
                                   'operations':[],'coverage':[]}
        with self.assertRaisesRegex(PermissionProposalRejected,'every required principal'):
            self.updater.run(job['job_id'])
        self.assertIn(job['job_id'],self.updater.pending_jobs())

    def test_coverage_cannot_hide_a_validated_recipient_change_as_unchanged(self):
        self.establish();self.model.require_permission_coverage=True
        job=self.ingest('Updated pickup is at 10 AM.')
        binding=self.binding(job['source_id'],spans=[[0,26]])
        self.model.handler=lambda p:{'revision':p['revision'],'considered_restrictions':[],
            'operations':[binding],'coverage':[{'principal':pid,'decision':'unchanged',
                'evidence':[job['source_id']],'reason':'Claimed no change.'} for pid in p['coverage_required']]}
        self.updater.update(job['job_id'])
        import json
        coverage={r[0]:json.loads(r[1])['decision'] for r in self.core.db.execute(
            'SELECT principal,body FROM permission_coverage WHERE job_id=?',(job['job_id'],))}
        self.assertEqual(coverage['helper'],'changed')
        self.assertEqual(coverage['owner'],'unchanged')

    def test_generation_schema_pins_revision_and_prefix_identities(self):
        captured=[];complete=self.model.complete
        def capture(system,payload,*,schema=None):
            captured.append(schema)
            return complete(system,payload,schema=schema)
        self.model.complete=capture
        first=self.ingest('First source event.');self.apply(first)
        second=self.ingest('Second source event.');self.apply(second)
        self.assertEqual([s['properties']['revision']['enum'] for s in captured],[[0],[1]])
        variants={v['properties']['op']['enum'][0]:v['properties'] for v in
                  captured[1]['properties']['operations']['items']['anyOf']}
        observed=variants['observe_participation'];established=variants['establish_activity']
        self.assertEqual(observed['participant']['enum'],['helper','outsider','owner'])
        self.assertEqual(observed['subject']['enum'],[None,'owner','project'])
        self.assertEqual(established['subject']['enum'],['owner','project'])
        self.assertEqual(established['basis']['enum'],['handoff'])
        self.assertEqual(established['status']['enum'],['established'])
        self.assertEqual(variants['bind_information']['source_id']['enum'],sorted([first['source_id'],second['source_id']]))

    def test_schema_requires_assignment_authority_and_evidence_per_basis(self):
        from agentic_memory.privacy.permissions import proposal_schema
        variants=proposal_schema(bootstrap=self.bootstrap)['properties']['operations']['items']['anyOf']
        assignments={v['properties']['basis']['enum'][0]:v for v in variants
                     if v['properties']['op']['enum']==['propose_assignment']}
        self.assertEqual(set(assignments),{'task','explicit'})
        explicit=assignments['explicit'];task=assignments['task']
        self.assertIn('authority',explicit['required'])
        self.assertIn('authority',explicit['properties']['evidence']['required'])
        self.assertIn('status',explicit['properties']['evidence']['required'])
        self.assertIn('activity_id',task['required'])
        self.assertNotIn('authority',task['required'])
        established={v['properties']['basis']['enum'][0]:v for v in variants
                     if v['properties']['op']['enum']==['establish_activity']}
        self.assertIn('performance',established['corroborated']['required'])
        self.assertIn('corroboration',established['corroborated']['required'])
        self.assertIn('authority',established['handoff']['required'])

    def test_wrong_revision_is_rejected_without_advancing_or_publishing(self):
        job=self.ingest('A source event.')
        for wrong in (1,'0',True):
            with self.subTest(revision=wrong):
                self.model.handler=lambda payload:{'revision':wrong,'considered_restrictions':[],'operations':[]}
                with self.assertRaisesRegex(PermissionProposalRejected,'expected integer 0'):
                    self.updater.run(job['job_id'])
                self.assertEqual(self.core.db.execute('SELECT revision FROM permission_bootstrap').fetchone()[0],0)
                self.assertEqual(self.updater.inspect()['assignment'],{})
                self.assertIn(job['job_id'],self.updater.pending_jobs())

    def test_revision_retry_explains_exact_value_and_keeps_failed_attempt(self):
        job=self.ingest('A source event.')
        def repair(payload):
            if 'correction' not in payload:
                return {'revision':payload['revision']+1,'considered_restrictions':[],'operations':[]}
            reason=payload['correction']['validation']['reason']
            self.assertIn('expected integer 0, received 1',reason)
            self.assertIn('do not increment',reason)
            return {'revision':payload['revision'],'considered_restrictions':[],'operations':[]}
        self.model.handler=repair
        self.assertEqual(self.updater.update(job['job_id'])['status'],'complete')
        rows=self.core.db.execute('SELECT proposal,validation FROM permission_attempts ORDER BY attempt').fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual(self.core.db.execute('SELECT revision FROM permission_bootstrap').fetchone()[0],1)
        self.assertIn('Stale permission revision',rows[0]['validation'])

    def test_publication_failure_rolls_back_ledger_and_views(self):
        j=self.ingest('Pickup is at 9 AM.')
        self.core._publication_hook=lambda _:(_ for _ in ()).throw(RuntimeError('injected failure'))
        with self.assertRaises(RuntimeError):self.apply(j,[self.assignment(j['source_id'],basis='explicit'),self.binding(j['source_id'])])
        self.assertEqual(self.updater.inspect()['assignment'],{})
        self.assertFalse(self.core.db.execute("SELECT 1 FROM resources WHERE kind='observation_view'").fetchone())
        status=self.core.db.execute('SELECT status FROM permission_jobs WHERE id=?',(j['job_id'],)).fetchone()[0]
        self.assertEqual(status,'publication_error');self.assertFalse(self.selected())

    def test_deleted_reauthorization_evidence_restores_original_restriction(self):
        self.establish();j=self.ingest('Do not share pickup instructions.')
        self.apply(j,[self.restriction(j['source_id'])])
        k=self.ingest('Allow pickup again, but keep the diagnostic purpose private.')
        self.apply(k,[self.restriction(k['source_id'],rid='replacement',scope='diagnosis',supersedes='private')])
        self.assertTrue(self.selected())
        self.core.delete(self.host,k['source_id'])
        for job in self.updater.pending_jobs():self.apply({'job_id':job})
        self.assertFalse(self.selected())

    def test_event_based_restriction_ends_only_on_supported_completion(self):
        self.establish();j=self.ingest('Keep pickup private until this ride is complete.')
        self.apply(j,[self.restriction(j['source_id'],validity={'end_activity':'ride'})]);self.assertFalse(self.selected())
        k=self.ingest('The ride is complete.')
        # An independently authorized read permission can outlast the task.
        self.apply(k,[self.assignment(k['source_id'],basis='explicit',aid='independent'),self.binding(k['source_id'],aid='independent'),
            operation('end_activity',self.evidence(k['source_id'],'activity_id','reason'),activity_id='ride',reason='Complete')])
        self.assertTrue(self.selected())

    def test_no_checkpoint_or_future_fields_in_model_input(self):
        j=self.ingest();self.apply(j)
        payload=self.model.calls[-1]
        self.assertEqual(set(payload),{'revision','bootstrap','new_source_id','sources','permission_state','context','operation_contract','contract_notes','binding_authorizations'})
        self.assertEqual(payload['binding_authorizations'],{})
        self.assertEqual(len(payload['sources']),1)
        self.assertNotIn('checkpoint',str(payload))

    def test_subject_response_establishes_worker_task_and_retrievable_information(self):
        worker=self.ingest('Please give me your pickup time.',actor='helper');self.apply(worker)
        owner=self.ingest('Pickup is at 9 AM; yes, arrange that ride.')
        self.apply(owner,[self.task(worker['source_id'],owner['source_id']),
                          self.assignment(owner['source_id']),self.binding(owner['source_id'])])
        self.assertEqual(self.selected()[0]['text'],'Pickup is at 9 AM')

    def test_republishing_binding_reuses_embedding_and_reports_only_new_fields(self):
        from unittest.mock import patch
        _,source=self.establish()
        j=self.ingest('The authorized pickup instruction is unchanged.')
        with patch.object(self.core.embedder,'encode',wraps=self.core.embedder.encode) as encode:
            published=self.apply(j,[self.binding(source['source_id'])])
            embedded=[text for call in encode.call_args_list for text in call.args[0]]
            self.assertNotIn('Pickup is at 9 AM',embedded)
        self.assertEqual(published['changes'][0]['resource_ids'],[])
        self.assertEqual(self.selected()[0]['text'],'Pickup is at 9 AM')

    def test_allowance_cannot_be_installed_as_restriction(self):
        j=self.ingest('Helper may read pickup details.')
        restriction=self.restriction(j['source_id']);restriction['effect']='allow'
        with self.assertRaisesRegex(PermissionProposalRejected,'encode permission as an assignment'):
            self.apply(j,[restriction])
        self.assertEqual(self.updater.inspect()['restriction'],{})

    def test_new_fact_alone_cannot_supply_its_own_authorization(self):
        _,original=self.establish()
        j=self.ingest('Pickup is at 10 AM; confidential callback is 555-0186.')
        binding=self.binding(j['source_id'],spans=[[0,18]])
        binding['evidence']['authorization']=[j['source_id']]
        with self.assertRaisesRegex(PermissionProposalRejected,'original scope evidence'):
            self.apply(j,[binding])
        self.apply(j)
        a=self.updater.inspect()['assignment']['access']
        self.assertEqual(a['scope_evidence'][0]['source_id'],original['source_id'])
        self.assertIn('Pickup is at 9 AM',a['scope_evidence'][0]['text'])

    def test_accepted_task_self_use_requires_separate_binding_and_ends_with_task(self):
        self.bootstrap=replace(self.bootstrap,self_use_actions=('search','read','derive'))
        self.updater=PermissionUpdater(self.core,self.host,'conversation',self.bootstrap,self.model,version='2')
        first,second=self.establish()
        self_use=next(a for a in self.updater.inspect()['assignment'].values() if a.get('self_use_activity'))
        self.assertEqual(self_use['participant'],'owner');self.assertEqual(self_use['bindings'],[])
        owner=self.core.issue_session('owner','answer',actions=['read','search','derive'],
                                     compartments=['conversation'],processing_routes=self.routes)
        self.assertEqual(self.core.search(owner,'pickup'),[])
        j=self.ingest('Arrive ten minutes early at the west gate.',actor='helper')
        self.apply(j,[self.binding(j['source_id'],aid=self_use['id'],spans=[[0,41]])])
        self.assertIn('ten minutes early',self.core.search(owner,'arrival')[0]['text'])
        k=self.ingest('This ride is finished.')
        self.apply(k,[operation('end_activity',self.evidence(k['source_id'],'activity_id','reason'),
                               activity_id='ride',reason='The accepted ride is complete.')])
        self.assertEqual(self.core.search(owner,'arrival'),[])

    def test_exact_quote_binding_computes_offsets_without_exposing_other_text(self):
        j=self.ingest('Private diagnosis. Pickup is at 9 AM; keep diagnosis private.')
        binding=self.binding(j['source_id']);binding.pop('spans');binding['quote']='Pickup is at 9 AM'
        self.apply(j,[self.assignment(j['source_id'],basis='explicit'),binding])
        self.assertEqual(self.selected()[0]['text'],'Pickup is at 9 AM')

    def test_quote_binding_rejects_ambiguous_or_inexact_text(self):
        for quote in ('Pickup','pickup is at 9 AM','a fabricated quote'):
            with self.subTest(quote=quote):
                j=self.ingest('Pickup is at 9 AM; Pickup is confirmed.')
                binding=self.binding(j['source_id']);binding.pop('spans');binding['quote']=quote
                with self.assertRaisesRegex(PermissionProposalRejected,'unique exact substring'):
                    self.apply(j,[self.assignment(j['source_id'],basis='explicit',aid=str(self.index)),binding])
                self.apply(j)

    def test_compact_wire_aliases_are_expanded_before_authority_validation(self):
        self.model.compact_permission_input=True
        j=self.ingest('Helper may read this. Pickup is at 9 AM; confidential diagnosis.')
        def respond(payload):
            self.assertEqual(payload['new_source_id'],'s1')
            self.assertEqual(payload['sources'][0]['text'],'Helper may read this. Pickup is at 9 AM; confidential diagnosis.')
            assignment=self.assignment('s1',basis='explicit')
            binding=self.binding('s1');binding.pop('spans');binding['quote']='Pickup is at 9 AM'
            return {'revision':payload['revision'],'considered_restrictions':[], 'operations':[assignment,binding]}
        self.model.handler=respond
        self.updater.run(j['job_id'])
        self.assertEqual(self.selected()[0]['text'],'Pickup is at 9 AM')
        record=self.updater.inspect()['assignment']['access']
        self.assertEqual(record['evidence']['authority'],[j['source_id']])
        self.assertEqual(record['bindings'][0]['source_id'],j['source_id'])

    def test_discriminators_precede_basis_specific_fields_in_response_schema(self):
        from agentic_memory.privacy.permissions import proposal_schema
        variants=proposal_schema(bootstrap=self.bootstrap)['properties']['operations']['items']['anyOf']
        for variant in variants:
            props=variant['properties']
            self.assertEqual(next(iter(props)),'op')
            if props['op']['enum']==['propose_assignment']:
                self.assertEqual(list(props)[1],'basis')
                if props['basis']['enum']==['explicit']:
                    self.assertNotIn('activity_id',props)
                    self.assertNotIn('initial_permission_id',props)

    def test_compact_input_preserves_raw_text_and_all_restrictions(self):
        from agentic_memory.privacy.permissions import compact_input,proposal_schema
        j=self.ingest('s1 is literal text; permission-source: is literal too.');self.apply(j)
        payload=self.model.calls[-1]
        payload['permission_state']['restriction']={'r':{'id':'r','reason':'Do not disclose s1.',
            'evidence':{'issuer':[j['source_id']]}}}
        wire,schema,expand=compact_input(payload,proposal_schema(source_ids=[j['source_id']]))
        self.assertEqual(wire['sources'][0]['text'],payload['sources'][0]['text'])
        self.assertEqual(wire['permission_state']['restriction']['r']['reason'],'Do not disclose s1.')
        self.assertEqual(wire['permission_state']['restriction']['r']['evidence']['issuer'],['s1'])
        self.assertEqual(expand({'operations':[],'id':'s1','explanation':'s1','evidence':{'subject':['s1']}}),
                         {'operations':[],'id':'s1','explanation':'s1','evidence':{'subject':[j['source_id']]}})
        binding=next(v for v in schema['properties']['operations']['items']['anyOf']
                     if v['properties']['op']['enum']==['bind_information'])
        self.assertIn('quote',binding['required']);self.assertNotIn('spans',binding['properties'])

    def test_question_only_prefix_cannot_select_a_factual_binding(self):
        from agentic_memory.privacy.permissions import compact_input,proposal_schema
        job=self.ingest("If she's at work, can I get the appointment time so I can drive her?")
        self.apply(job)
        payload=self.model.calls[-1]
        wire,schema,_=compact_input(payload,proposal_schema(source_ids=[job['source_id']]))
        self.assertEqual(wire['sources'][0]['bindable_spans'],[])
        variants=schema['properties']['operations']['items']['anyOf']
        self.assertFalse(any(v['properties']['op']['enum']==['bind_information'] for v in variants))
        # Questions still establish requests/consent when properly authenticated.
        grant=next(v for v in variants if v['properties']['op']['enum']==['propose_assignment'])
        self.assertEqual(grant['properties']['evidence']['properties']['authority']['items']['enum'],['s1'])

    def test_mixed_source_keeps_assertion_and_excludes_question_only_source(self):
        from agentic_memory.privacy.permissions import compact_input,proposal_schema
        from agentic_memory.privacy.lifecycle import assertive_spans
        first=self.ingest('Was pickup at 7 AM?');self.apply(first)
        text='Was pickup at 8 AM? Pickup is at 9 AM. Can you confirm?'
        second=self.ingest(text);self.apply(second)
        wire,schema,_=compact_input(self.model.calls[-1],proposal_schema())
        spans=wire['sources'][1]['bindable_spans']
        self.assertEqual([text[a:b] for a,b in spans],['Pickup is at 9 AM.'])
        binding=next(v for v in schema['properties']['operations']['items']['anyOf']
                     if v['properties']['op']['enum']==['bind_information'])
        self.assertEqual(binding['properties']['source_id']['enum'],['s2'])
        self.assertEqual(assertive_spans(''),[])
        self.assertEqual(assertive_spans(' \n '),[])

    def test_question_repair_removes_binding_without_suppressing_host_validation(self):
        self.establish()
        job=self.ingest("If she's at work, can I get the appointment time so I can drive her?")
        def respond(payload):
            if 'correction' in payload:
                details=payload['correction']['validation']['details']
                self.assertEqual(details['allowed_assertive_spans'],[])
                self.assertEqual(details['assignment_id'],'access')
                self.assertIn('Remove this binding',details['repair'])
                ops=[]
            else:
                ops=[self.binding(job['source_id'],spans=[[18,65]])]
            return {'revision':payload['revision'],'considered_restrictions':[], 'operations':ops}
        self.model.handler=respond
        self.updater.update(job['job_id'])
        self.assertEqual(self.core.db.execute('SELECT attempts FROM permission_jobs WHERE id=?',
            (job['job_id'],)).fetchone()[0],2)
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 9 AM'])

    def test_mixed_event_host_still_rejects_question_and_accepts_separate_assertion(self):
        self.establish()
        text='Was pickup at 7 AM? Pickup is at 10 AM.'
        job=self.ingest(text)
        bad=self.binding(job['source_id'],spans=[[4,18]])
        with self.assertRaisesRegex(PermissionProposalRejected,'Questions cannot establish factual memory'):
            self.apply(job,[bad])
        start=text.index('Pickup is')
        good=self.binding(job['source_id'],spans=[[start,len(text)]])
        self.apply(job,[good])
        selected=' '.join(r['text'] for r in self.selected())
        self.assertIn('Pickup is at 10 AM.',selected)
        self.assertNotIn('7 AM',selected)

    def test_compact_historical_bindings_keep_selectors_and_policy_without_mutating_ledger(self):
        from agentic_memory.privacy.permissions import compact_input,proposal_schema
        self.establish()
        job=self.ingest('Any news?');self.apply(job)
        payload=self.model.calls[-1];original=deepcopy(payload)
        wire,_,_=compact_input(payload,proposal_schema())
        assignment=wire['permission_state']['assignment']['access']
        self.assertEqual(assignment['scope_evidence'][0]['text'],
            original['permission_state']['assignment']['access']['scope_evidence'][0]['text'])
        self.assertEqual(assignment['evidence']['information_scope'],['s2'])
        binding=assignment['bindings'][0]
        self.assertEqual(binding['spans'],[[0,17]])
        self.assertEqual(binding['source_id'],'s2')
        self.assertTrue(binding['necessity'])
        self.assertNotIn('evidence',binding)
        self.assertEqual(payload,original)
        self.assertIn('evidence',self.updater.inspect()['assignment']['access']['bindings'][0])

    def test_compact_schema_pairs_resource_authority_and_authenticated_evidence(self):
        from agentic_memory.privacy.permissions import compact_input,proposal_schema
        first=self.ingest('Owner authorizes pickup only.');self.apply(first)
        second=self.ingest('Helper is performing pickup.',actor='helper');self.apply(second)
        wire,schema,_=compact_input(self.model.calls[-1],proposal_schema(bootstrap=self.bootstrap,
                                    source_ids=[first['source_id'],second['source_id']]))
        for variant in schema['properties']['operations']['items']['anyOf']:
            p=variant['properties'];op=p['op']['enum'][0]
            if op=='propose_assignment':self.assertEqual(p['status']['enum'],['active'])
            if op=='record_restriction':
                self.assertEqual(p['issuer']['enum'],['owner'])
                self.assertEqual(p['evidence']['properties']['issuer']['items']['enum'],['s1'])
            if op=='establish_activity' and p['basis']['enum']==['corroborated']:
                self.assertEqual(p['corroboration']['items']['enum'],['s1'])

    def history(self, count=12):
        for i in range(count):
            self.apply(self.ingest(f'Unrelated stream observation {i}.'))

    def test_incremental_window_does_not_grow_with_unbound_history(self):
        from agentic_memory.privacy.permission_context import RECENT_TURNS
        self.history(24)
        payload=self.model.calls[-1]
        self.assertEqual(len(payload['sources']),RECENT_TURNS)
        self.assertEqual(payload['context']['live_source_count'],24)
        self.assertNotIn('Unrelated stream observation 0.',str(payload))

    def test_original_authorization_and_event_condition_survive_long_history(self):
        first,scope=self.establish()
        self.history()
        job=self.ingest('Pickup is at 4 PM.')
        self.apply(job,[self.binding(job['source_id'])])
        payload=self.model.calls[-1]
        self.assertLess(len(payload['sources']),payload['context']['live_source_count'])
        loaded={s['id'] for s in payload['sources']}
        self.assertTrue({first['source_id'],scope['source_id']}<=loaded)
        assignment=payload['permission_state']['assignment']['access']
        self.assertEqual(assignment['validity'],{'end_activity':'ride'})
        self.assertEqual(assignment['scope_evidence'][0]['text'],
                         self.core.read(self.host,scope['source_id'])['payload']['text'])
        self.assertIn('4 PM',' '.join(r['text'] for r in self.selected()))
        done=self.ingest('The ride is complete.')
        self.apply(done,[operation('end_activity',self.evidence(done['source_id'],'activity_id','reason'),
                                  activity_id='ride',reason='Completed ride.')])
        self.assertEqual(self.updater.inspect()['assignment']['access']['status'],'expired')
        self.assertFalse(self.selected())

    def test_historical_bindings_are_bounded_without_pruning_the_ledger(self):
        self.establish()
        for hour in range(10,20):
            job=self.ingest(f'Pickup is at {hour}:00.')
            self.apply(job,[self.binding(job['source_id'])])
        self.apply(self.ingest('The ride remains scheduled.'))
        assignment=self.model.calls[-1]['permission_state']['assignment']['access']
        self.assertEqual(assignment['binding_count'],11)
        self.assertEqual(len(assignment['bindings']),2)
        canonical=self.updater.inspect()['assignment']['access']
        self.assertEqual(len(canonical['bindings']),11)
        self.assertEqual(canonical['status'],'active')
        self.assertEqual(len(self.selected()),11)

    def test_old_pending_decision_keeps_the_request_for_yes_please(self):
        first=self.ingest('May I arrange your ride?',actor='helper')
        tentative=self.task(first['source_id'],first['source_id'],status='tentative')
        tentative['unresolved']=[{'question':'Does the owner accept the ride?','evidence':[first['source_id']]}]
        self.apply(first,[tentative]);self.history()
        yes=self.ingest('Yes, please.')
        self.apply(yes,[self.task(first['source_id'],yes['source_id']),self.assignment(yes['source_id'])])
        payload=self.model.calls[-1]
        self.assertIn(first['source_id'],{s['id'] for s in payload['sources']})
        self.assertEqual(payload['permission_state']['activity']['ride']['status'],'tentative')
        self.assertEqual(self.updater.inspect()['assignment']['access']['status'],'active')

    def test_owner_can_revoke_another_recipient_after_they_leave_recent_window(self):
        self.establish();self.history()
        job=self.ingest('Stop sharing my ride information with helper.')
        self.apply(job,[operation('revoke_assignment',self.evidence(job['source_id'],'assignment_id','issuer','reason'),
                                  assignment_id='access',issuer='owner',reason='Owner stopped sharing.')])
        self.assertIn('access',self.model.calls[-1]['permission_state']['assignment'])
        self.assertEqual(self.updater.inspect()['assignment']['access']['status'],'revoked')
        self.assertFalse(self.selected())

    def test_targeted_search_precedes_atomic_publication_and_is_audited(self):
        import json
        self.establish()
        self.apply(self.ingest('An unrelated observation before the old instruction.'))
        old=self.ingest('Pickup is at the violet gate.');self.apply(old)
        self.history();job=self.ingest('Use the earlier violet gate pickup instruction.')
        revision=self.core.db.execute('SELECT revision FROM permission_bootstrap').fetchone()[0]
        self.model.require_permission_coverage=True
        def respond(payload):
            self.assertEqual(payload['revision'],revision)
            self.assertFalse(self.selected())
            if not payload['context']['retrieval_results']:
                self.assertNotIn(old['source_id'],{s['id'] for s in payload['sources']})
                return {'revision':revision,'considered_restrictions':[],'operations':[],
                        'coverage':[], 'context_requests':[{'kind':'search','reference':'violet gate','offset':0}]}
            self.assertIn(old['source_id'],{s['id'] for s in payload['sources']})
            return {'revision':revision,'considered_restrictions':[],
                    'operations':[self.binding(old['source_id'],spans=[[0,28]])],
                    'coverage':[{'principal':pid,'decision':'unchanged','evidence':[job['source_id']],
                                 'reason':'Reviewed current source.'} for pid in payload['coverage_required']]}
        self.model.handler=respond
        self.assertEqual(self.updater.run(job['job_id'])['status'],'complete')
        self.assertEqual(self.core.db.execute('SELECT revision FROM permission_bootstrap').fetchone()[0],revision+1)
        attempts=self.core.db.execute('SELECT * FROM permission_attempts WHERE job_id=? ORDER BY attempt',(job['job_id'],)).fetchall()
        self.assertEqual(len(attempts),2)
        self.assertTrue(json.loads(attempts[0]['validation'])['context_loaded'])
        self.assertNotIn(old['source_id'],json.loads(attempts[0]['input_manifest'])['source_digests'])
        self.assertIn(old['source_id'],json.loads(attempts[1]['input_manifest'])['source_digests'])
        self.assertIn('violet gate',' '.join(r['text'] for r in self.selected()))

    def test_unloaded_evidence_requires_retrieval_even_if_its_id_is_guessed(self):
        self.establish();self.apply(self.ingest('An unrelated observation.'))
        old=self.ingest('Pickup is at 8 PM.');self.apply(old)
        self.history();job=self.ingest('Use an earlier pickup instruction.')
        with self.assertRaisesRegex(PermissionProposalRejected,'Retrieve missing evidence'):
            self.apply(job,[self.binding(old['source_id'])])
        self.assertFalse(self.selected())
        self.assertEqual(len(self.updater.inspect()['assignment']['access']['bindings']),1)
        def respond(payload):
            if not payload['context']['retrieval_results']:
                return {'revision':payload['revision'],'considered_restrictions':[],'operations':[],
                        'context_requests':[{'kind':'source','reference':old['source_id'],'offset':0}]}
            return {'revision':payload['revision'],'considered_restrictions':[],
                    'operations':[self.binding(old['source_id'])]}
        self.model.handler=respond;self.updater.run(job['job_id'])
        self.assertIn('8 PM',' '.join(r['text'] for r in self.selected()))

    def test_context_request_cannot_mix_changes_or_fetch_unknown_sources(self):
        self.establish();job=self.ingest('A new source.')
        for reference in ('future-source','another-compartment-source'):
            self.model.handler=lambda p:{'revision':p['revision'],'operations':[],
                'context_requests':[{'kind':'source','reference':reference,'offset':0}]}
            with self.assertRaisesRegex(PermissionProposalRejected,'live in this job prefix'):
                self.updater.run(job['job_id'])
        self.model.handler=lambda p:{'revision':p['revision'],'considered_restrictions':[],
            'operations':[operation('revoke_assignment',self.evidence(job['source_id'],'assignment_id','issuer','reason'),
                                    assignment_id='access',issuer='owner',reason='Stop sharing.')],
            'context_requests':[{'kind':'search','reference':'pickup','offset':0}]}
        with self.assertRaisesRegex(PermissionProposalRejected,'no operations'):
            self.updater.run(job['job_id'])
        self.assertEqual(self.updater.inspect()['assignment']['access']['status'],'active')
        self.assertFalse(self.selected())

    def test_context_retrieval_survives_processor_failure_and_updater_restart(self):
        self.establish();self.apply(self.ingest('An unrelated observation.'))
        old=self.ingest('Pickup is at 8 PM.');self.apply(old)
        self.history();job=self.ingest('Recall the old pickup instruction.')
        def respond(payload):
            if not payload['context']['retrieval_results']:
                return {'revision':payload['revision'],'operations':[],
                        'context_requests':[{'kind':'source','reference':old['source_id'],'offset':0}]}
            raise ProcessorFailure('Temporary outage after retrieval')
        self.model.handler=respond
        with self.assertRaises(ProcessorFailure):self.updater.run(job['job_id'])
        self.assertFalse(self.selected())
        self.updater=PermissionUpdater(self.core,self.host,'conversation',self.bootstrap,self.model,version='1')
        def finish(payload):
            self.assertEqual(payload['context']['rounds_remaining'],2)
            self.assertIn(old['source_id'],{s['id'] for s in payload['sources']})
            return {'revision':payload['revision'],'considered_restrictions':[],
                    'operations':[self.binding(old['source_id'])]}
        self.model.handler=finish;self.updater.run(job['job_id'])
        self.assertIn('8 PM',' '.join(r['text'] for r in self.selected()))

    def test_retrieval_round_budget_cannot_be_reset_by_retry(self):
        self.establish();job=self.ingest('An ambiguous reference.')
        self.model.handler=lambda p:{'revision':p['revision'],'operations':[],
            'context_requests':[{'kind':'search','reference':'unavailable evidence','offset':0}]}
        for _ in range(2):
            with self.assertRaisesRegex(PermissionProposalRejected,'budget exceeded'):
                self.updater.run(job['job_id'])
        self.assertEqual(self.core.db.execute('SELECT attempts FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0],5)
        self.assertFalse(self.selected())

    def test_compact_context_source_aliases_expand_without_changing_search_text(self):
        self.establish();job=self.ingest('A new instruction.')
        self.model.compact_permission_input=True
        def respond(payload):
            if not payload['context']['retrieval_results']:
                return {'revision':payload['revision'],'operations':[],
                    'context_requests':[{'kind':'source','reference':'s1','offset':0},
                                        {'kind':'search','reference':'s1','offset':0}]}
            self.assertEqual(payload['context']['retrieval_results'][0]['reference'],'s1')
            self.assertEqual(payload['context']['retrieval_results'][1]['reference'],'s1')
            return {'revision':payload['revision'],'considered_restrictions':[],'operations':[]}
        self.model.handler=respond;self.updater.run(job['job_id'])

    def test_incremental_retrieval_still_detects_concurrent_policy_changes(self):
        self.establish();job=self.ingest('Recall an old instruction.')
        def mutate(payload):
            self.core.install_policy(self.host,'changed',{'allows':[], 'processors':self.routes})
            return {'revision':payload['revision'],'operations':[],
                    'context_requests':[{'kind':'search','reference':'pickup','offset':0}]}
        self.model.handler=mutate
        with self.assertRaises(PermissionRevisionConflict):self.updater.run(job['job_id'])
        self.assertFalse(self.selected())

    def test_omitted_ledger_record_is_discoverable_but_must_be_loaded_to_change(self):
        grant_text='Outsider may read project pickup instructions. Pickup is at 9 AM.'
        grant=self.ingest(grant_text,subject='project')
        assignment=self.assignment(grant['source_id'],basis='explicit',aid='remote',subject='project')
        assignment['participant']='outsider'
        start=grant_text.index('Pickup is')
        self.apply(grant,[assignment,self.binding(grant['source_id'],aid='remote',spans=[[start,len(grant_text)]])])
        for i in range(10):
            self.apply(self.ingest(f'Routine pickup observation {i}.',actor='helper'))
        canonical=self.updater.inspect()['assignment']['remote']
        self.assertEqual(canonical['status'],'active')
        self.assertEqual(len(canonical['bindings']),1)
        job=self.ingest('Suspend the remote project assignment for review.',actor='helper')
        suspend=operation('suspend_assignment',self.evidence(job['source_id'],'assignment_id','reason'),
                          assignment_id='remote',reason='Pending scope review.')
        with self.assertRaisesRegex(PermissionProposalRejected,'Retrieve the affected ledger record'):
            self.apply(job,[suspend])
        self.assertNotIn('remote',self.model.calls[-1]['permission_state']['assignment'])
        self.assertIn('remote',self.model.calls[-1]['context']['ledger_index']['assignment'])
        def respond(payload):
            if not payload['context']['retrieval_results']:
                return {'revision':payload['revision'],'operations':[],
                        'context_requests':[{'kind':'assignment','reference':'remote','offset':0}]}
            self.assertIn('remote',payload['permission_state']['assignment'])
            self.assertIn(grant['source_id'],{s['id'] for s in payload['sources']})
            return {'revision':payload['revision'],'considered_restrictions':[],'operations':[suspend]}
        self.model.handler=respond;self.updater.run(job['job_id'])
        self.assertEqual(self.updater.inspect()['assignment']['remote']['status'],'suspended')

    def test_binding_pages_retrieve_older_facts_without_loading_every_binding(self):
        self.establish()
        for hour in range(10,25):
            job=self.ingest(f'Pickup is at {hour}:00.')
            self.apply(job,[self.binding(job['source_id'])])
        self.history();job=self.ingest('Review earlier pickup instructions.')
        def respond(payload):
            assignment=payload['permission_state']['assignment']['access']
            if not payload['context']['retrieval_results']:
                self.assertEqual(len(assignment['bindings']),2)
                return {'revision':payload['revision'],'operations':[],
                        'context_requests':[{'kind':'bindings','reference':'access','offset':3}]}
            page=payload['context']['retrieval_results'][0]
            self.assertEqual(page['total_matches'],16)
            self.assertEqual(len(page['source_ids']),6)
            self.assertEqual(len(assignment['bindings']),8)
            self.assertTrue(set(page['source_ids'])<={s['id'] for s in payload['sources']})
            return {'revision':payload['revision'],'considered_restrictions':[],'operations':[]}
        self.model.handler=respond;self.updater.run(job['job_id'])
        self.assertEqual(len(self.updater.inspect()['assignment']['access']['bindings']),16)


if __name__=='__main__':unittest.main()
