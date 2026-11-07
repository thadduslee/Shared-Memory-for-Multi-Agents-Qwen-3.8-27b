"""Causal-prefix regressions for the v14 host/inference boundary.

Scripted semantic decisions measure the compiler and lifecycle, not LLM accuracy.
No fixture conventions enter production inference.
"""
from copy import deepcopy
import json
import unittest
import jsonschema

from security_tests import test_permission_inference as fixtures
from agentic_memory.privacy.permissions import PermissionUpdater, PermissionProposalRejected
from agentic_memory.privacy.fact_candidates import segments
from agentic_memory.privacy.system import AccessDenied


class DurableCandidates(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PermissionInference();self.f.setUp();f=self.f
        f.updater=PermissionUpdater(f.core,f.host,'conversation',f.bootstrap,f.model,version='14')
        self.policy=lambda p:[]
        self.classify=lambda p,s:{'index':s['index'],'kind':'instruction','subject':'owner',
            'subject_source':p['source_id'],'subject_quote':s['quote'],
            'topics':['instructions'],'relationships':[],'reason':'The source states this instruction.'}
        self.bind=lambda p:{'in_scope':True,'subject_supported':True,'blocked_by':[],
                            'reason':'Responsive instruction within this assignment scope.'}
        self.decisions=[];self.schemas=[]
        self.gap=lambda p:{'diagnoses':[{'candidate_id':c['id'],'category':'no_supported_authorization',
            'reason':'No additional authorization is supported by the current source prefix.'} for c in p['candidates']],
            'permission_changes':[]}
        def complete(system,payload,*,schema=None):
            self.schemas.append(deepcopy(schema));f.model.calls.append(deepcopy(payload))
            stage=payload['stage']
            if stage=='extract_candidates':
                value={'segments':[self.classify(payload,s) for s in payload['segments']]}
            elif stage=='permission_policy':value={'permission_changes':self.policy(payload),'new_facts':[]}
            elif stage=='task_recipient':
                aid=next(iter(payload['assignments']),None)
                value={'decision':'allowed' if aid else 'unresolved','assignment_id':aid,
                       'reason':'This recipient has their own narrow supported grant.'}
            elif stage=='candidate_binding':
                self.decisions.append(deepcopy(payload));value=self.bind(payload)
            elif stage=='authorization_gap_review':value=self.gap(payload)
            else:raise AssertionError(stage)
            jsonschema.validate(value,schema)
            return value
        f.model.complete=complete

    def tearDown(self):self.f.tearDown()

    def turn(self,text,actor='owner',policy=None):
        self.policy=policy or (lambda p:[])
        job=self.f.ingest(text,actor=actor)
        self.f.updater.update(job['job_id'])
        return job

    def grant(self,p,recipient='helper',slot=0,scope='Medication instructions'):
        return {'op':'propose_assignment','id':p['new_ids']['grant'][slot],
            'participant':recipient,'subject':'owner','basis':'explicit',
            'authority_source':p['new_source_id'],'actions':['search','read','derive'],
            'information_scope':scope,'status':'active','reason':'The owner authorizes this narrow instruction access.',
            'evidence':[p['new_source_id']]}

    def selected(self,reader=None):
        return self.f.core.search(reader or self.f.reader,'instructions',k=100)

    def test_unbound_durable_omitted_dose_reconsidered_after_window_and_restart(self):
        self.turn('Take nifedipine ER 30 mg nightly. Stop labetalol. Do not take it if dizzy.',actor='helper')
        self.assertEqual(len(self.f.updater.fact_candidates()),3)
        self.assertFalse(self.selected())
        for i in range(7):
            self.classify=lambda p,s:{'index':s['index'],'kind':'context','subject':None,
                'subject_source':None,'subject_quote':None,'topics':[],'relationships':[],
                'reason':'Social dialogue contains no factual instruction.'}
            self.turn('Thanks.')
        self.f.updater=PermissionUpdater(self.f.core,self.f.host,'conversation',self.f.bootstrap,self.f.model,version='14')
        self.turn('Helper may read my medication instructions.',policy=lambda p:[self.grant(p)])
        text=' '.join(x['text'] for x in self.selected())
        for expected in ['30 mg nightly','Stop labetalol','if dizzy']:self.assertIn(expected,text)
        self.assertTrue(all(len(p.get('sources',[]))<=6 for p in self.f.model.calls if p['stage']=='permission_policy'))
        self.assertEqual(len(self.f.updater.inspect()['assignment']),1)

    def test_extraction_receipt_does_not_claim_permission_or_disappear_on_rejection(self):
        job=self.f.ingest('Take 2.5 mg with food.',actor='helper')
        self.policy=lambda p:[{**self.grant(p),'authority_source':p['new_source_id']}]
        with self.assertRaises(PermissionProposalRejected):self.f.updater.run(job['job_id'])
        self.assertEqual(self.f.updater.fact_candidates()[0]['quote'],'Take 2.5 mg with food.')
        self.assertFalse(self.selected())
        self.policy=lambda p:[]
        self.f.updater.update(job['job_id'])
        status=self.f.core.db.execute('SELECT status FROM fact_extractions WHERE job_id=?',(job['job_id'],)).fetchone()[0]
        self.assertEqual(status,'complete')
        self.assertEqual(len([p for p in self.f.model.calls if p['stage']=='extract_candidates']),1)

    def test_question_and_request_guesses_are_not_evidence(self):
        self.classify=lambda p,s:{'index':s['index'],'kind':'context' if s['quote'].startswith('Please') else 'instruction',
            'subject':'owner','subject_source':p['source_id'],'subject_quote':s['quote'],
            'topics':[],'relationships':[],'reason':'Guessed requests do not assert the value.'}
        self.turn('Is my dose 80 mg? Please confirm the guessed 90 mg dose. Take 30 mg nightly.',policy=lambda p:[self.grant(p)])
        self.assertEqual([r['text'] for r in self.selected()],['Take 30 mg nightly.'])

    def test_all_segments_required_extraction_failure_separate_from_permission(self):
        original=self.f.model.complete
        def omit(system,payload,*,schema=None):
            if payload['stage']=='extract_candidates':return {'segments':[]}
            return original(system,payload,schema=schema)
        self.f.model.complete=omit
        job=self.turn('Take 30 mg nightly.')
        row=self.f.core.db.execute('SELECT status,attempts FROM fact_extractions WHERE job_id=?',(job['job_id'],)).fetchone()
        self.assertEqual(tuple(row),('incomplete',3))
        self.assertEqual(self.f.core.db.execute('SELECT status FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0],'complete')
        audit=json.loads(self.f.core.db.execute('SELECT validation FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0])
        self.assertEqual(audit['extraction']['status'],'incomplete')

    def test_multi_recipient_decisions_explicit_unrelated_excluded_later_reuses_grants(self):
        def bindings(p):
            allowed='Private' not in p['candidate']['quote']
            return {'in_scope':allowed,'subject_supported':True,'blocked_by':[],
                    'reason':'Medication instruction fits.' if allowed else 'Unrelated information is outside medication scope.'}
        self.bind=bindings
        self.turn('Take 30 mg nightly. Private door code is 1234.',
            policy=lambda p:[self.grant(p,'helper',0),self.grant(p,'owner',1)])
        owner=self.f.core.issue_session('owner','answer',actions=['read','search','derive'],
            compartments=['conversation'],processing_routes=self.f.routes)
        self.turn('Take with food unless fasting.',actor='helper')
        self.assertEqual(len(self.f.updater.inspect()['assignment']),2)
        for reader in [owner,self.f.reader]:
            text=' '.join(r['text'] for r in self.selected(reader))
            self.assertIn('30 mg',text);self.assertIn('unless fasting',text);self.assertNotIn('1234',text)
        cid=self.f.updater.fact_candidates()[0]['id']
        self.assertFalse(self.f.core.allowed(owner,cid))
        with self.assertRaises(AccessDenied):self.f.core.read(owner,cid)
        self.assertEqual(len([d for d in self.decisions if 'unless fasting' in d['candidate']['quote']]),2)

    def test_replacement_cancellation_contact_context_and_similarity_history(self):
        self.turn('Appointment Monday at 9 AM.',policy=lambda p:[self.grant(p,scope='Appointment and contact instructions')])
        first=self.f.updater.fact_candidates()[0]['id']
        def classify(p,s):
            q=s['quote'];rels=[]
            if q.startswith('Cancel'):rels=[{'kind':'cancels','target_id':first,'evidence_quote':q}]
            return {'index':s['index'],'kind':'instruction','subject':'owner','subject_source':p['source_id'],
                'subject_quote':q,'topics':['appointment','contact'],'relationships':rels,'reason':'Exact source instructions.'}
        self.classify=classify
        self.turn('Cancel Monday at 9 AM. Appointment Friday at 11:20 AM. Arrive at 11 AM. Call 628-555-0162; no detailed voicemail.',actor='helper')
        text=' '.join(r['text'] for r in self.selected())
        self.assertNotIn('Appointment Monday',text)
        for detail in ['Cancel Monday','Friday at 11:20','Arrive at 11 AM','628-555-0162; no detailed voicemail']:self.assertIn(detail,text)
        # A similar later statement without an explicit relation does not hide history.
        self.turn('Appointment Friday at 11:30 AM.',actor='helper')
        text=' '.join(r['text'] for r in self.selected())
        self.assertIn('11:20',text);self.assertIn('11:30',text)

    def test_deletion_erasure_and_revocation_cannot_release_candidates(self):
        job=self.turn('Use contact 628-555-0162; no voicemail.',policy=lambda p:[self.grant(p,scope='Contact instructions')])
        note=self.selected()[0]['id'];cid=self.f.updater.fact_candidates()[0]['id']
        self.f.core.delete(self.f.host,job['source_id'])
        self.assertFalse(self.selected());self.assertFalse(self.f.core.allowed(self.f.reader,note))
        row=self.f.core.db.execute('SELECT state,body FROM fact_candidates WHERE id=?',(cid,)).fetchone()
        self.assertEqual(tuple(row),('invalidated','{}'))

    def test_restriction_replacement_allocates_new_id_and_preserves_history(self):
        self.turn('Pickup is at 4 PM.',policy=lambda p:[self.grant(p,scope='Pickup instructions')])
        def restriction(p,replace=False):
            op={'op':'replace_restriction' if replace else 'record_restriction',
                'authority_source':p['new_source_id'],'participants':['helper'],'subject':'owner',
                'information_scope':'secret code' if replace else 'Pickup instructions','actions':['search','read','derive'],
                'reason':'The owner states this precise restriction.','effect':'deny','blocked_assignments':[],
                'evidence':[p['new_source_id']]}
            if replace:op['restriction_id']=next(iter(self.f.updater.inspect()['restriction']))
            else:
                op['id']=p['new_ids']['restriction'][0]
                op['blocked_assignments']=list(self.f.updater.inspect()['assignment'])
            return op
        self.turn('Do not share pickup instructions with helper.',policy=lambda p:[restriction(p)])
        self.assertFalse(self.selected())
        old=next(iter(self.f.updater.inspect()['restriction']))
        self.turn('Replace that restriction: helper may read pickup instructions, but not the secret code.',
            policy=lambda p:[restriction(p,True),self.grant(p,scope='Pickup instructions')])
        state=self.f.updater.inspect()['restriction']
        self.assertEqual(len(state),2);self.assertNotEqual(state[old]['superseded_by'],old)
        self.assertTrue(self.selected())
        versions=self.f.core.db.execute('SELECT count(*) FROM permission_records WHERE kind=\'restriction\' AND id=?',(old,)).fetchone()[0]
        self.assertEqual(versions,2)

    def test_focused_policy_repair_keeps_independent_valid_operation(self):
        self.classify=lambda p,s:{'index':s['index'],'kind':'context','subject':None,
            'subject_source':None,'subject_quote':None,'topics':[],'relationships':[],'reason':'Permission statement.'}
        def policy(p):
            if 'focused_correction' in p:
                self.assertEqual(p['focused_correction']['operation_index'],1)
                return []
            return [self.grant(p,slot=0),{**self.grant(p,slot=1),'participant':'outsider','subject':'project'}]
        # owner controls project too, so use unsupported task instead for op 1.
        def invalid(p):
            if 'focused_correction' in p:return policy(p)
            op=self.grant(p,slot=1);op.pop('authority_source');op.update(basis='task',activity_id=p['new_ids']['activity'][0])
            return [self.grant(p,slot=0),op]
        self.turn('Helper may read medication instructions.',policy=invalid)
        self.assertEqual(len(self.f.updater.inspect()['assignment']),1)
        self.assertEqual(len([p for p in self.f.model.calls if p['stage']=='permission_policy']),2)

    def test_sentence_spans_preserve_decimal_units_and_exceptions(self):
        text='Take 2.5 mg nightly unless fasting. Do not double it.'
        self.assertEqual([text[a:b] for a,b in segments(text)],['Take 2.5 mg nightly unless fasting.','Do not double it.'])
        text="Stop St. John's wort. Take 3 tablets daily with food for 8 weeks."
        self.assertEqual([text[a:b] for a,b in segments(text)],
            ["Stop St. John's wort.",'Take 3 tablets daily with food for 8 weeks.'])

    def test_accepted_activity_supported_instructions_and_later_responsive_facts(self):
        self.classify=lambda p,s:{'index':s['index'],'kind':'instruction' if s['quote'].startswith('Pickup') else 'context',
            'subject':'owner','subject_source':p['source_id'],'subject_quote':s['quote'],
            'topics':['ride'],'relationships':[],'reason':'Separate task context from its factual instructions.'}
        def pending(p):
            return [{'op':'observe_participation','id':p['new_ids']['activity'][0],
                'participant':'helper','requester':None,'subject':'owner','activity':'Arrange this ride',
                'status':'tentative','basis':'performance','unresolved':['Awaiting owner acceptance'],
                'reason':'The helper offers this particular ride.','evidence':[p['new_source_id']]}]
        self.turn('I can arrange your ride.',actor='helper',policy=pending)
        activity=next(iter(self.f.updater.inspect()['activity']))
        def accepted(p):
            assignment=self.grant(p,scope='Pickup instructions for this ride')
            assignment.pop('authority_source');assignment.update(basis='task',activity_id=activity)
            return [{'op':'establish_activity','id':activity,'participant':'helper','requester':'owner',
                'subject':'owner','activity':'Arrange this ride','status':'established','basis':'corroborated',
                'corroboration':[p['new_source_id']],'reason':'The owner accepts this specific ride.',
                'evidence':[p['new_source_id']]},assignment,self.grant(p,'owner',1,scope='Pickup instructions for this ride')]
        job=self.turn('Yes, please arrange my ride. Pickup is at 4 PM.',policy=accepted)
        self.turn('Pickup is outside the east entrance.',actor='helper')
        self.assertEqual(len(self.f.updater.inspect()['assignment']),2)
        self.assertEqual(len(self.selected()),2)
        validation=json.loads(self.f.core.db.execute('SELECT validation FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0])
        self.assertEqual({a['binding_status'] for a in validation['task_access_assessments'][0]['recipient_access']},{'bound'})
        self.assertEqual(self.f.core.db.execute("SELECT count(*) FROM permission_jobs WHERE status!='complete'").fetchone()[0],0)

    def test_revocation_remains_effective_for_old_candidates_and_derived_reads(self):
        self.turn('Take 30 mg nightly.',policy=lambda p:[self.grant(p)])
        note=self.selected()[0]['id'];aid=next(iter(self.f.updater.inspect()['assignment']))
        job=self.f.ingest('Revoke helper access to my instructions.')
        # The pending revocation blocks existing evidence before any model runs.
        self.assertFalse(self.f.core.allowed(self.f.reader,note))
        self.policy=lambda p:[{'op':'revoke_assignment','assignment_id':aid,
            'authority_source':p['new_source_id'],'reason':'The owner revokes this assignment.',
            'evidence':[p['new_source_id']]}]
        self.f.updater.update(job['job_id'])
        self.turn('Take with food.',actor='helper')
        self.assertFalse(self.selected())
        self.assertEqual(self.f.updater.inspect()['assignment'][aid]['status'],'revoked')
        self.assertGreater(len(self.f.updater.fact_candidates()),0)
        with self.assertRaises(AccessDenied):self.f.core.release(self.f.reader,[note],{'answer':'30 mg'})
        cid=self.f.updater.fact_candidates()[0]['id']
        before=len(self.f.model.calls)
        with self.assertRaises(AccessDenied):self.f.core.process(self.f.reader,self.f.model,[cid],'answer',{})
        self.assertEqual(len(self.f.model.calls),before)
        with self.assertRaises(AccessDenied):self.f.core.traverse(self.f.reader,note)

    def test_erasure_scrubs_unbound_candidates_and_repeated_value_cannot_return(self):
        self.turn('Use contact 628-555-0162; no voicemail.')
        cid=self.f.updater.fact_candidates()[0]['id']
        def erase(p):
            return [{'op':'erase_information','authority_source':p['new_source_id'],'subject':'owner',
                'selectors':[{'source_id':p['sources'][0]['id'],'quote':'628-555-0162'}],
                'reason':'The owner requests erasure of this contact value.','evidence':[p['new_source_id']]}]
        self.turn('Erase 628-555-0162 from my saved contact details.',policy=erase)
        row=self.f.core.db.execute('SELECT state,body FROM fact_candidates WHERE id=?',(cid,)).fetchone()
        self.assertEqual(tuple(row),('erased','{}'))
        self.turn('Use contact 628-555-0162; no voicemail.',policy=lambda p:[self.grant(p,scope='Contact instructions')])
        self.assertFalse(self.selected())
        self.assertFalse(any('628-555-0162' in p['quote'] for p in self.f.updater.fact_candidates()))

    def test_unprivileged_restriction_replacement_cannot_lift_old_deny(self):
        self.turn('Do not disclose my instructions.',policy=lambda p:[{
            'op':'record_restriction','id':p['new_ids']['restriction'][0],
            'authority_source':p['new_source_id'],'participants':['helper'],'subject':'owner',
            'information_scope':'*','actions':['read','search','derive'],'reason':'Owner prohibits disclosure.',
            'effect':'deny','blocked_assignments':[],'evidence':[p['new_source_id']]}])
        old=next(iter(self.f.updater.inspect()['restriction']))
        self.turn('Replace that denial with permission to read.',actor='helper',policy=lambda p:[{
            'op':'replace_restriction','restriction_id':old,'authority_source':p['new_source_id'],
            'participants':['helper'],'subject':'owner','information_scope':'secret code','actions':['read','search','derive'],
            'reason':'Helper claims a new restriction.','effect':'deny','blocked_assignments':[],
            'evidence':[p['new_source_id']]}])
        state=self.f.updater.inspect()['restriction']
        self.assertNotIn('superseded_by',state[old])
        self.assertIn('unresolved',{r.get('status') for r in state.values()})

    def test_topic_labels_cannot_grant_access_or_bypass_restriction(self):
        self.classify=lambda p,s:{'index':s['index'],'kind':'instruction','subject':'owner',
            'subject_source':p['source_id'],'subject_quote':s['quote'],
            'topics':['public','authorized','pickup'],'relationships':[],'reason':'Source-grounded assertion.'}
        self.turn('Private code is 1234.')
        self.assertFalse(self.selected());self.assertFalse(self.f.updater.inspect()['assignment'])
        self.bind=lambda p:{'in_scope':False,'subject_supported':True,'blocked_by':[],
                            'reason':'Private code is not a pickup instruction regardless of topics.'}
        self.turn('Helper may read pickup instructions.',policy=lambda p:[self.grant(p,scope='Pickup instructions')])
        self.assertFalse(self.selected())

    def test_focused_binding_correction_does_not_regenerate_policy(self):
        calls=[]
        def binding(p):
            calls.append(p)
            if len(calls)==2:
                from agentic_memory.privacy.processors import ProcessorContractFailure
                raise ProcessorContractFailure('Malformed binding JSON needs a focused correction.')
            return {'in_scope':True,'subject_supported':True,'blocked_by':['unknown'] if len(calls)==1 else [],
                    'reason':'This instruction fits the supported scope.'}
        # Host validation is exercised without a schema-enforcing test double.
        original=self.f.model.complete
        def complete(system,payload,*,schema=None):
            if payload['stage']=='candidate_binding':return binding(payload)
            return original(system,payload,schema=schema)
        self.f.model.complete=complete
        self.turn('Take 30 mg nightly.',policy=lambda p:[self.grant(p)])
        self.assertEqual(len(calls),3);self.assertIn('correction',calls[-1])
        self.assertEqual(len([p for p in self.f.model.calls if p['stage']=='permission_policy']),1)
        self.assertTrue(self.selected())

    def test_original_omitted_plan_preserves_all_doses_units_and_negations(self):
        text=('Medication plan after initial clinician review: stop ibuprofen; use acetaminophen 650 mg q6h PRN '
            'with 3,000 mg/day max; continue prenatal vitamin and levothyroxine 75 mcg daily; '
            'start doxylamine 12.5 mg nightly and pyridoxine 25 mg BID.')
        self.turn(text,actor='helper')
        self.assertEqual([p['quote'] for p in self.f.updater.fact_candidates()],[text])
        self.turn('Helper may read my medication plan.',policy=lambda p:[self.grant(p)])
        self.assertIn(text,[r['text'] for r in self.selected()])

    def test_historical_candidate_erasure_does_not_require_source_repetition(self):
        self.turn('Temporary contact is 628-555-0162.',actor='helper')
        cid=self.f.updater.fact_candidates()[0]['id']
        for _ in range(7):self.turn('Thanks.')
        self.turn('Erase my temporary contact.',policy=lambda p:[{'op':'erase_candidates',
            'candidate_ids':[cid],'subject':'owner','authority_source':p['new_source_id'],
            'reason':'The owner requests deletion of this temporary contact.',
            'evidence':[p['new_source_id']]}])
        row=self.f.core.db.execute('SELECT state,body FROM fact_candidates WHERE id=?',(cid,)).fetchone()
        self.assertEqual(tuple(row),('erased','{}'))

    def test_unbound_replacement_does_not_change_another_recipients_projection(self):
        self.turn('Appointment Monday at 9 AM.',policy=lambda p:[self.grant(p,scope='Appointment instructions')])
        first=self.f.updater.fact_candidates()[0]['id']
        self.classify=lambda p,s:{'index':s['index'],'kind':'instruction','subject':'owner',
            'subject_source':p['source_id'],'subject_quote':s['quote'],'topics':['appointment'],
            'relationships':[{'kind':'cancels','target_id':first,'evidence_quote':s['quote']}],
            'reason':'The source explicitly claims this cancellation.'}
        self.bind=lambda p:{'in_scope':p['candidate']['id']==first,'subject_supported':True,
            'blocked_by':[],'reason':'This assignment does not support the new cancellation instruction.'}
        self.turn('Cancel Monday at 9 AM.',actor='outsider')
        self.assertIn('Appointment Monday at 9 AM.',[r['text'] for r in self.selected()])

    def test_lifetime_expiry_cannot_be_renewed_by_new_facts_or_topics(self):
        def grant(p):
            op=self.grant(p);op['validity']={'effective_until':'2026-10-26T13:00:00+00:00'}
            return [op]
        job=self.f.ingest('Helper may read instructions until 1 PM. Take 30 mg nightly.',
                          timestamp='2026-10-26T12:00:00+00:00')
        self.policy=grant;self.f.updater.update(job['job_id'])
        self.assertTrue(self.selected())
        self.f.core.clock=lambda:'2026-10-26T14:00:00+00:00'
        self.turn('Take with food.',actor='helper')
        self.assertFalse(self.selected())
        self.assertEqual(len(self.f.updater.inspect()['assignment']),1)

    def test_missed_supported_grant_is_resolved_in_triggering_event(self):
        self.turn('Take 30 mg nightly.',actor='helper')
        def review(p):
            return {'diagnoses':[{'candidate_id':c['id'],'category':'missed_supported_authorization',
                'reason':'The current authenticated owner requests helper access to these instructions.'}
                for c in p['candidates']],
                'permission_changes':[self.grant(p)]}
        self.gap=review
        job=self.turn('Please give helper access to my medication instructions.')
        self.assertTrue(self.selected())
        self.assertEqual(self.f.core.db.execute('SELECT status,attempts FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[:],('complete',1))
        validation=json.loads(self.f.core.db.execute('SELECT validation FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0])
        self.assertEqual(validation['authorization_gap_reviews'][0]['category'],'missed_supported_authorization')

    def test_existing_self_use_compiler_binds_beneficiary_in_same_event(self):
        from dataclasses import replace
        self.f.bootstrap=replace(self.f.bootstrap,self_use_actions=('read','search','derive'))
        self.f.updater=PermissionUpdater(self.f.core,self.f.host,'conversation',self.f.bootstrap,self.f.model,version='14-self')
        self.classify=lambda p,s:{'index':s['index'],'kind':'instruction' if s['quote'].startswith('Pickup') else 'context',
            'subject':'owner','subject_source':p['source_id'],'subject_quote':s['quote'],
            'topics':['ride'],'relationships':[],'reason':'Task context is distinct from responsive factual instructions.'}
        def pending(p):
            return [{'op':'observe_participation','id':p['new_ids']['activity'][0],
                'participant':'helper','requester':None,'subject':'owner','activity':'Arrange this ride',
                'status':'tentative','basis':'performance','unresolved':['Awaiting owner acceptance'],
                'reason':'The helper offers this ride.','evidence':[p['new_source_id']]}]
        self.turn('I can arrange your ride.',actor='helper',policy=pending)
        activity=next(iter(self.f.updater.inspect()['activity']))
        def accepted(p):
            assignment=self.grant(p,scope='Pickup instructions for this ride')
            assignment.pop('authority_source');assignment.update(basis='task',activity_id=activity)
            return [{'op':'establish_activity','id':activity,'participant':'helper','requester':'owner',
                'subject':'owner','activity':'Arrange this ride','status':'established','basis':'corroborated',
                'corroboration':[p['new_source_id']],'reason':'The owner accepts this ride.',
                'evidence':[p['new_source_id']]},assignment]
        job=self.turn('Yes, please arrange my ride. Pickup is at 4 PM.',policy=accepted)
        owner=self.f.core.issue_session('owner','answer',actions=['read','search','derive'],
            compartments=['conversation'],processing_routes=self.f.routes)
        self.assertEqual([r['text'] for r in self.selected(owner)],['Pickup is at 4 PM.'])
        self.assertEqual(len(self.f.updater.inspect()['assignment']),2)
        self.assertEqual(self.f.core.db.execute('SELECT attempts FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0],1)

    def test_partial_extraction_is_durable_and_only_omission_is_corrected(self):
        original=self.f.model.complete;calls=[]
        def partial(system,payload,*,schema=None):
            if payload['stage']=='extract_candidates':
                calls.append(deepcopy(payload))
                if len(calls)==1:
                    return {'segments':[self.classify(payload,payload['segments'][0])]}
                self.assertEqual([s['index'] for s in payload['segments']],[1])
                self.assertEqual(len(self.f.updater.fact_candidates()),1)
            return original(system,payload,schema=schema)
        self.f.model.complete=partial
        job=self.turn('Take 30 mg nightly. Do not double the dose.')
        self.assertEqual(len(calls),2)
        self.assertEqual(len(self.f.updater.fact_candidates()),2)
        self.assertEqual(self.f.core.db.execute('SELECT status FROM fact_extractions WHERE job_id=?',(job['job_id'],)).fetchone()[0],'complete')

    def test_exhausted_binding_cannot_publish_partial_grant_or_clear_hold(self):
        original=self.f.model.complete
        def invalid(system,payload,*,schema=None):
            if payload['stage']=='candidate_binding':
                return {'in_scope':True,'subject_supported':True,'blocked_by':['missing-restriction'],
                        'reason':'This unsupported restriction reference must be corrected.'}
            return original(system,payload,schema=schema)
        self.f.model.complete=invalid
        self.policy=lambda p:[self.grant(p)]
        job=self.f.ingest('Take 30 mg nightly.')
        with self.assertRaises(PermissionProposalRejected):self.f.updater.update(job['job_id'])
        self.assertEqual(len(self.f.updater.fact_candidates()),1)
        self.assertFalse(self.f.updater.inspect()['assignment'])
        self.assertFalse(self.selected())
        self.assertIn(job['job_id'],self.f.updater.pending_jobs())
        # Failed binding reviews reuse the saved policy proposal.
        self.assertEqual(len([p for p in self.f.model.calls if p['stage']=='permission_policy']),1)
        self.assertEqual(self.f.core.db.execute("SELECT COUNT(*) FROM fact_attempts WHERE stage='candidate_binding'").fetchone()[0],3)

    def test_erasure_covers_candidate_topics_and_context_metadata(self):
        self.classify=lambda p,s:{'index':s['index'],'kind':'instruction','subject':'owner',
            'subject_source':p['source_id'],'subject_quote':s['quote'],'topics':['628-555-0162'],
            'relationships':[],'reason':'Scripted source-derived contact topic.'}
        self.turn('Use 628-555-0162. Leave no voicemail.',policy=lambda p:[self.grant(p,scope='Contact instructions')])
        original_ids=[p['id'] for p in self.f.updater.fact_candidates()]
        self.classify=lambda p,s:{'index':s['index'],'kind':'context','subject':None,
            'subject_source':None,'subject_quote':None,'topics':[],'relationships':[],'reason':'Erasure request.'}
        self.turn('Erase 628-555-0162.',policy=lambda p:[{'op':'erase_information',
            'authority_source':p['new_source_id'],'subject':'owner',
            'selectors':[{'source_id':p['sources'][0]['id'],'quote':'628-555-0162'}],
            'reason':'The owner erases this contact value.','evidence':[p['new_source_id']]}])
        for cid in original_ids:
            row=self.f.core.db.execute('SELECT state,body FROM fact_candidates WHERE id=?',(cid,)).fetchone()
            self.assertEqual(tuple(row),('erased','{}'))
        self.assertFalse(self.selected())

    def test_future_grant_prebinds_without_early_access_or_source_repetition(self):
        def grant(p):
            op=self.grant(p);op['validity']={'effective_from':'2026-10-26T13:00:00+00:00'}
            return [op]
        job=self.f.ingest('Helper may read these instructions from 1 PM. Take 30 mg nightly.',
                          timestamp='2026-10-26T12:00:00+00:00')
        self.policy=grant;self.f.updater.update(job['job_id'])
        self.assertFalse(self.selected())
        self.f.core.clock=lambda:'2026-10-26T14:00:00+00:00'
        self.assertIn('Take 30 mg nightly.',[r['text'] for r in self.selected()])


if __name__=='__main__':unittest.main()
