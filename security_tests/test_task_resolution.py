"""Task omissions, durable corroboration and exact recipient bindings."""
from copy import deepcopy
import json
import unittest

from security_tests import test_permission_inference as fixtures
from agentic_memory.privacy.permissions import PermissionUpdater, PermissionProposalRejected, PermissionRevisionConflict
from agentic_memory.privacy import task_tracking as tasks


class TaskResolutionTests(unittest.TestCase):
    def setUp(self):
        self.c=fixtures.PermissionInference();self.c.setUp();c=self.c
        c.updater=PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version='12')
        original=c.binding
        def binding(*args,**kwargs):
            value=original(*args,**kwargs);value['evidence'].pop('authorization');return value
        c.binding=binding

    def tearDown(self):self.c.tearDown()

    def apply(self,job,ops=(),reviews=None):
        c=self.c
        def respond(p):
            result={'revision':p['revision'],'considered_restrictions':list(p['permission_state']['restriction']),
                    'operations':deepcopy(list(ops)), 'task_reviews':tasks.empty_reviews(p)}
            if reviews:reviews(p,result['task_reviews'])
            return result
        c.model.handler=respond
        return c.updater.run(job['job_id'])

    def pending(self,p,reviews):
        for r in reviews:
            candidate=next(c for c in p['task_candidates'] if c['id']==r['candidate_id'])
            if candidate['participant']=='helper':
                r.update(decision='unresolved',reason_code='awaiting_acceptance',subject='owner',
                         task='Pickup coordination',reason='The subject has not accepted this particular pickup task.')

    def offer(self):
        job=self.c.ingest('May I arrange the pickup?',actor='helper')
        self.apply(job,reviews=self.pending)
        row=next(r for r in tasks.rows(self.c.core,'conversation').values() if r['participant']=='helper')
        return job,row

    def authorized(self,job,aid='access',binding_sources=None):
        def review(p,reviews):
            for r in reviews:
                cand=next(c for c in p['task_candidates'] if c['id']==r['candidate_id'])
                if cand['participant']=='helper':
                    r.update(decision='authorized',subject='owner',task='Pickup coordination',
                        reason_code='accepted_task',reason='The subject accepted this particular pickup task.',
                        activity_id=aid+'-activity',assignment_id=aid,binding_status='bound',
                        binding_sources=binding_sources or [job['source_id']])
        return review

    def accept(self):
        first,row=self.offer();c=self.c
        job=c.ingest('Yes, please. Pickup is at 4 PM; private project code is 1234.')
        activity=c.task(first['source_id'],job['source_id'],activity='access-activity')
        grant=c.assignment(job['source_id']);grant['activity_id']='access-activity'
        start='Yes, please. '.__len__()
        self.apply(job,[activity,grant,c.binding(job['source_id'],spans=[[start,start+17]])],self.authorized(job))
        return first,job

    def test_question_is_durable_control_evidence_without_grant_or_factual_memory(self):
        c=self.c;job,row=self.offer()
        activity=c.updater.inspect()['activity'][row['activity_id']]
        self.assertEqual(activity['status'],'tentative')
        self.assertEqual(activity['performance'],[job['source_id']])
        self.assertEqual(c.updater.access_state()['assignment'],{})
        self.assertEqual(c.selected(),[])
        self.assertFalse(c.core.db.execute("SELECT 1 FROM resources WHERE kind='memory'").fetchone())

    def test_question_followed_by_acceptance_produces_only_necessary_recipient_facts(self):
        c=self.c;self.accept()
        self.assertEqual([r['text'] for r in c.selected()],['Pickup is at 4 PM'])
        for actor in ('outsider','owner'):
            ctx=c.core.issue_session(actor,'answer',actions=['read','search','derive'],compartments=['conversation'],processing_routes=c.routes)
            self.assertEqual(c.core.search(ctx,'pickup'),[])
        self.assertNotIn('1234',str(c.selected()))

    def test_reviewed_unchanged_cannot_conceal_an_omitted_task_assignment(self):
        c=self.c;self.offer();job=c.ingest('Yes, please. Pickup is at 4 PM.')
        c.model.handler=lambda p:{'revision':p['revision'],'considered_restrictions':[], 'operations':[]}
        with self.assertRaisesRegex(PermissionProposalRejected,'explicit resolution'):
            c.updater.run(job['job_id'])
        self.assertFalse(c.selected())
        self.assertTrue(any(r['status']=='unresolved' for r in tasks.rows(c.core,'conversation').values()))

    def test_authorized_resolution_without_assignment_is_rejected(self):
        c=self.c;self.offer();job=c.ingest('Yes, please. Pickup is at 4 PM.')
        with self.assertRaisesRegex(PermissionProposalRejected,'own validated active'):
            self.apply(job,reviews=self.authorized(job))

    def test_task_review_must_actually_consider_the_new_reply(self):
        c=self.c;first,row=self.offer();job=c.ingest('Yes, please arrange pickup.')
        def stale_review(p,rs):
            self.pending(p,rs)
            for r in rs:
                if r['candidate_id']==row['id']:r['evidence']=[first['source_id']]
        with self.assertRaisesRegex(PermissionProposalRejected,'evaluate the current reply'):
            self.apply(job,reviews=stale_review)
        self.assertFalse(c.selected())

    def test_v12_instruction_allows_bounded_facts_without_old_consent(self):
        instruction=self.c.updater.instruction
        self.assertIn('bounded context_requests',instruction)
        self.assertIn('historical fact retrieval does not supply fresh consent',instruction)
        self.assertNotIn('Never request or reconstruct that history',instruction)

    def test_empty_assignment_cannot_claim_necessary_fact_bindings(self):
        c=self.c;first,row=self.offer();job=c.ingest('Yes, please. Pickup is at 4 PM.')
        activity=c.task(first['source_id'],job['source_id'],activity='access-activity')
        grant=c.assignment(job['source_id']);grant['activity_id']='access-activity'
        with self.assertRaisesRegex(PermissionProposalRejected,'bind the declared necessary'):
            self.apply(job,[activity,grant],self.authorized(job))
        self.assertEqual(c.updater.access_state()['assignment'],{})

    def test_empty_assignment_cannot_complete_with_not_needed(self):
        c=self.c;first,row=self.offer();job=c.ingest('Yes, please arrange pickup.')
        activity=c.task(first['source_id'],job['source_id'],activity='access-activity')
        grant=c.assignment(job['source_id']);grant['activity_id']='access-activity'
        def review(p,rs):
            self.authorized(job)(p,rs)
            for r in rs:
                if r['decision']=='authorized':r.update(binding_status='not_needed',binding_sources=[])
        with self.assertRaisesRegex(PermissionProposalRejected,'empty task assignment'):
            self.apply(job,[activity,grant],review)
        self.assertEqual(c.updater.access_state()['assignment'],{})

    def test_pending_task_survives_speaker_window_and_restart_then_uses_new_corroboration(self):
        c=self.c;first,row=self.offer()
        for i in range(8):self.apply(c.ingest(f'Unrelated event {i}.',actor='outsider'),reviews=self.pending)
        c.updater=PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version='12')
        job=c.ingest('Yes, please arrange it. Pickup is at 4 PM.')
        activity=c.task(first['source_id'],job['source_id'],activity=row['activity_id']);activity.pop('performance')
        activity['information_needs'][0]['evidence']=[job['source_id']]
        grant=c.assignment(job['source_id']);grant['activity_id']=row['activity_id']
        def reviews(p,rs):
            self.assertNotIn(first['source_id'],[s['id'] for s in p['sources']])
            self.assertIn(row['id'],[r['candidate_id'] for r in rs])
            self.authorized(job)(p,rs)
            for r in rs:
                if r['decision']=='authorized':r['activity_id']=row['activity_id']
        start=len('Yes, please arrange it. ')
        self.apply(job,[activity,grant,c.binding(job['source_id'],spans=[[start,start+17]])],reviews)
        self.assertTrue(c.selected())

    def test_authorized_claim_cannot_point_to_another_recipient_assignment(self):
        c=self.c;self.accept();job=c.ingest('I can help too.',actor='outsider')
        def lie(p,rs):
            for r in rs:
                cand=next(c for c in p['task_candidates'] if c['id']==r['candidate_id'])
                if cand['participant']=='outsider':
                    r.update(decision='authorized',subject='owner',reason_code='existing_grant',
                        reason='Claimed that the helper grant also covers this other participant.',
                        activity_id='access-activity',assignment_id='access',binding_status='not_needed')
        with self.assertRaisesRegex(PermissionProposalRejected,'own validated active'):self.apply(job,reviews=lie)

    def test_unresolved_cannot_be_reclassified_as_no_access_for_lack_of_factual_span(self):
        c=self.c;job=c.ingest('May I arrange pickup?',actor='helper')
        def lie(p,rs):
            for r in rs:r.update(decision='no_authorization',reason_code='awaiting_acceptance',
                                reason='A question supplies no asserted factual span.')
        with self.assertRaisesRegex(PermissionProposalRejected,'reason_code disagree'):self.apply(job,reviews=lie)

    def test_revocation_and_deleted_authorization_still_remove_access(self):
        c=self.c;_,job=self.accept()
        revoke=c.ingest('Do not share pickup instructions anymore.')
        self.apply(revoke,[fixtures.operation('revoke_assignment',c.evidence(revoke['source_id'],'assignment_id','issuer','reason'),
            assignment_id='access',issuer='owner',reason='The owner revoked access.')])
        self.assertFalse(c.selected())
        c.core.delete(c.host,job['source_id'])
        self.assertFalse(c.selected())

    def test_pending_budget_retires_without_creating_access(self):
        c=self.c;_,row=self.offer()
        for i in range(tasks.IDLE_TURNS+1):
            self.apply(c.ingest(f'Unrelated event {i}.',actor='outsider'),reviews=self.pending)
        self.assertEqual(tasks.rows(c.core,'conversation')[row['id']]['status'],'dormant')
        self.assertNotIn(row['activity_id'],c.updater.access_state()['activity'])
        self.assertFalse(c.selected())

    def test_coverage_only_unresolved_cannot_be_accepted_without_durable_involvement(self):
        c=self.c;c.model.require_permission_coverage=True
        job=c.ingest('May I arrange pickup?',actor='helper')
        def respond(p):
            return {'revision':p['revision'],'considered_restrictions':[], 'operations':[],
                'task_reviews':tasks.empty_reviews(p),
                'coverage':[{'principal':pid,'decision':'unresolved' if pid=='helper' else 'unchanged',
                             'evidence':[job['source_id']],'reason':'The subject has not accepted this task.'}
                            for pid in p['coverage_required']]}
        c.model.handler=respond
        with self.assertRaisesRegex(PermissionProposalRejected,'Coverage-only unresolved'):
            c.updater.run(job['job_id'])

    def test_targeted_historical_facts_are_bound_only_after_validated_task_acceptance(self):
        c=self.c
        text='Pickup uses the violet gate; private project code is 1234.'
        old=c.ingest(text);self.apply(old)
        for i in range(8):self.apply(c.ingest(f'Unrelated event {i}.',actor='outsider'))
        first,row=self.offer();job=c.ingest('Yes, please arrange the pickup.')
        calls=len(c.model.calls)
        def respond(p):
            reviews=tasks.empty_reviews(p)
            if not p['context']['retrieval_results']:
                self.assertNotIn(old['source_id'],[s['id'] for s in p['sources']])
                return {'revision':p['revision'],'operations':[],'task_reviews':[],
                    'context_requests':[{'candidate_id':row['id'],'kind':'search','reference':'violet gate','offset':0}]}
            self.assertLessEqual(len(p['sources']),12)
            activity=c.task(first['source_id'],job['source_id'],activity='access-activity')
            grant=c.assignment(job['source_id']);grant['activity_id']='access-activity'
            self.authorized(job,binding_sources=[old['source_id']])(p,reviews)
            return {'revision':p['revision'],'considered_restrictions':[], 'task_reviews':reviews,
                'operations':[activity,grant,c.binding(old['source_id'],spans=[[0,text.index(';')]])]}
        c.model.handler=respond;c.updater.run(job['job_id'])
        self.assertEqual(len(c.model.calls)-calls,2)
        self.assertEqual([r['text'] for r in c.selected()],['Pickup uses the violet gate'])
        self.assertNotIn('1234',str(c.selected()))

    def test_historical_fact_lookup_cannot_be_used_as_fresh_permission_evidence(self):
        c=self.c;old=c.ingest('Helper may arrange pickup.');self.apply(old)
        for i in range(8):self.apply(c.ingest(f'Unrelated event {i}.',actor='outsider'))
        _,row=self.offer();job=c.ingest('An unrelated response.')
        def respond(p):
            if not p['context']['retrieval_results']:
                return {'revision':p['revision'],'operations':[],'task_reviews':[],
                    'context_requests':[{'candidate_id':row['id'],'kind':'source','reference':old['source_id'],'offset':0}]}
            return {'revision':p['revision'],'considered_restrictions':[], 'task_reviews':tasks.empty_reviews(p),
                'operations':[c.assignment(old['source_id'],basis='explicit')]}
        c.model.handler=respond
        with self.assertRaisesRegex(PermissionProposalRejected,'cannot supply fresh authority'):
            c.updater.run(job['job_id'])

    def test_fact_lookup_budget_survives_reconstruction(self):
        c=self.c;_,row=self.offer();job=c.ingest('Please find the earlier detail.')
        def respond(p):
            return {'revision':p['revision'],'operations':[],'task_reviews':[],
                'context_requests':[{'candidate_id':row['id'],'kind':'search','reference':'missing fact','offset':0}]}
        c.model.handler=respond
        with self.assertRaisesRegex(PermissionProposalRejected,'budget exceeded'):c.updater.run(job['job_id'])
        self.assertEqual(len(c.model.calls),4)  # initial offer + two retrievals + rejection
        c.updater=PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version='12')
        with self.assertRaisesRegex(PermissionProposalRejected,'budget exceeded'):c.updater.run(job['job_id'])
        self.assertEqual(c.model.calls[-1]['context']['rounds_remaining'],0)

    def test_pending_projection_has_a_fixed_queue_and_per_turn_review_bound(self):
        c=self.c
        for i in range(tasks.MAX_PENDING+4):
            self.apply(c.ingest(f'Could I arrange pickup task {i}?',actor='helper'),reviews=self.pending)
            self.assertLessEqual(len(c.model.calls[-1]['task_candidates']),tasks.MAX_AFFECTED)
        rows=tasks.rows(c.core,'conversation')
        self.assertLessEqual(sum(r['status']=='unresolved' for r in rows.values()),tasks.MAX_PENDING)
        self.assertLessEqual(len(c.updater.access_state()['activity']),tasks.MAX_PENDING)
        self.assertLessEqual(c.core.db.execute("SELECT COUNT(*) FROM permission_access_state WHERE kind='activity'").fetchone()[0],tasks.MAX_PENDING)
        self.assertFalse(c.selected())

    def test_deleting_acceptance_removes_previously_accessible_task_facts(self):
        c=self.c;_,accepted=self.accept();self.assertTrue(c.selected())
        c.core.delete(c.host,accepted['source_id'])
        self.assertFalse(c.selected());self.assertTrue(c.updater.pending_jobs())

    def test_unresolved_accepted_question_gets_one_focused_reassessment_then_grant(self):
        c=self.c;first,row=self.offer();job=c.ingest('Yes, please. Pickup is at 4 PM.')
        calls=len(c.model.calls)
        def respond(p):
            reviews=tasks.empty_reviews(p)
            if not p['task_followup']:
                self.pending(p,reviews)
                return {'revision':p['revision'],'considered_restrictions':[], 'operations':[], 'task_reviews':reviews}
            activity=c.task(first['source_id'],job['source_id'],activity='access-activity')
            grant=c.assignment(job['source_id']);grant['activity_id']='access-activity'
            self.authorized(job)(p,reviews)
            return {'revision':p['revision'],'considered_restrictions':[], 'task_reviews':reviews,
                'operations':[activity,grant,c.binding(job['source_id'],spans=[[13,30]])]}
        c.model.handler=respond;c.updater.run(job['job_id'])
        self.assertEqual(len(c.model.calls)-calls,2)
        self.assertTrue(c.selected())
        self.assertTrue(tasks.rows(c.core,'conversation')[row['id']]['reassessment_done'])

    def test_focused_reassessment_does_not_turn_actual_uncertainty_into_access(self):
        c=self.c;_,row=self.offer();job=c.ingest('I am not sure what you mean.')
        calls=len(c.model.calls)
        self.apply(job,reviews=self.pending)
        self.assertEqual(len(c.model.calls)-calls,2)
        self.assertFalse(c.selected())
        self.assertEqual(tasks.rows(c.core,'conversation')[row['id']]['status'],'unresolved')
        c.updater=PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version='12')
        later=c.ingest('Still unsure.');calls=len(c.model.calls)
        self.apply(later,reviews=self.pending)
        self.assertEqual(len(c.model.calls)-calls,1)

    def test_focused_reassessment_receipt_survives_a_processor_error_and_restart(self):
        c=self.c;_,row=self.offer();job=c.ingest('I am not sure what you mean.')
        def respond(p):
            if p['task_followup']:raise RuntimeError('interrupted focused call')
            rs=tasks.empty_reviews(p);self.pending(p,rs)
            return {'revision':p['revision'],'considered_restrictions':[], 'operations':[], 'task_reviews':rs}
        c.model.handler=respond
        with self.assertRaisesRegex(RuntimeError,'interrupted focused call'):c.updater.run(job['job_id'])
        c.updater=PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version='12')
        calls=len(c.model.calls)
        self.apply(job,reviews=self.pending)
        self.assertEqual(len(c.model.calls)-calls,1)
        self.assertTrue(tasks.rows(c.core,'conversation')[row['id']]['reassessment_done'])
        self.assertFalse(c.selected())

    def test_task_candidate_change_during_inference_prevents_stale_publication(self):
        c=self.c;_,row=self.offer();job=c.ingest('Yes, please. Pickup is at 4 PM.')
        def respond(p):
            rs=tasks.empty_reviews(p);self.atomic_review(p,rs)
            updated=tasks.rows(c.core,'conversation');updated[row['id']]['reason']='Concurrent task review changed.'
            with c.core.transaction():tasks.publish(c.core,'conversation',updated)
            return {'revision':p['revision'],'considered_restrictions':[], 'operations':[], 'task_reviews':rs}
        c.model.handler=respond
        with self.assertRaisesRegex(PermissionRevisionConflict,'changed during inference'):
            c.updater.run(job['job_id'])
        self.assertEqual(c.updater.access_state()['assignment'],{})
        self.assertFalse(c.selected())

    def atomic_review(self,p,rs,quote='Pickup is at 4 PM',corroboration=None):
        sid=p['new_source_id']
        for r in rs:
            candidate=next(c for c in p['task_candidates'] if c['id']==r['candidate_id'])
            if candidate['participant']!='helper':continue
            r.update(decision='authorized',subject='owner',task='Pickup coordination',
                reason_code='accepted_task',reason='Subject accepted this pickup task and supplied its necessary details.',
                activity_id=None,assignment_id=None,binding_status='bound',binding_sources=[],
                authorization={'information_scope':'Pickup time and pickup location for the accepted ride only.',
                    'actions':['read','search','derive'],'corroboration':corroboration or [sid],
                    'checks':{'involvement_supported':'Worker offered this specific pickup task.',
                              'corroboration_supports':'Subject accepted the pickup task.',
                              'scope_supported':'Only the supplied pickup instructions.'},
                    'bindings':[{'source_id':sid,'quote':quote,'necessity':'Pickup requires its time and location.',
                        'scope_supported':'Within accepted pickup scope.','context_preserved':'Complete pickup clause.',
                        'metadata_safe':'Opaque field identifiers only.','evidence_kind':'instruction',
                        'evidence':{'necessity':[sid],'subject':[sid]},'restriction_decisions':[]}]})

    def test_atomic_authorization_cannot_omit_its_assignment_or_necessary_facts(self):
        c=self.c;self.offer();job=c.ingest('Yes, please. Pickup is at 4 PM; secret code is 1234.')
        self.apply(job,reviews=self.atomic_review)
        self.assertEqual([r['text'] for r in c.selected()],['Pickup is at 4 PM'])
        assignment=next(a for a in c.updater.inspect()['assignment'].values() if a['participant']=='helper')
        self.assertEqual(assignment['status'],'active');self.assertEqual(len(assignment['bindings']),1)
        self.assertEqual(assignment['actions'],['read','search','derive'])
        self.assertFalse(c.updater.access_state()['activity'][assignment['activity_id']].get('unresolved'))

    def test_atomic_question_stays_unbindable_and_all_publication_rolls_back(self):
        c=self.c;self.offer();job=c.ingest('Yes, please. Is pickup at 4 PM?')
        def review(p,rs):self.atomic_review(p,rs,quote='Is pickup at 4 PM?')
        with self.assertRaisesRegex(PermissionProposalRejected,'Questions cannot establish'):
            self.apply(job,reviews=review)
        self.assertEqual(c.updater.access_state()['assignment'],{})

    def test_atomic_supported_expiry_is_enforced(self):
        c=self.c;self.offer()
        job=c.ingest('Yes, please. Pickup is at 4 PM; permission ends 2026-10-26T13:00:00Z.')
        def review(p,rs):
            self.atomic_review(p,rs)
            for r in rs:
                if 'authorization' in r:
                    r['authorization'].update(validity={'effective_until':'2026-10-26T13:00:00Z'},
                        validity_evidence=[p['new_source_id']])
        self.apply(job,reviews=review);self.assertTrue(c.selected())
        c.core.clock=lambda:'2026-10-26T13:00:00Z'
        self.assertFalse(c.selected())

    def test_atomic_repeat_reuses_exact_current_policy_without_new_grant_or_extension(self):
        c=self.c;self.offer()
        def review(p,rs):
            self.atomic_review(p,rs)
            for r in rs:
                if 'authorization' in r:
                    r['authorization'].update(validity={'effective_until':'2026-10-26T13:00:00Z'},
                        validity_evidence=[p['new_source_id']])
        job=c.ingest('Yes, please. Pickup is at 4 PM; access ends 2026-10-26T13:00:00Z.')
        self.apply(job,reviews=review)
        before=deepcopy(c.updater.inspect()['assignment']);gid=next(iter(before))
        later=c.ingest('Yes, still please. Pickup is at 4 PM; access ends 2026-10-26T13:00:00Z.')
        self.apply(later,reviews=review)
        after=c.updater.inspect()['assignment']
        self.assertEqual(set(after),{gid})
        self.assertEqual(after[gid]['validity'],before[gid]['validity'])
        self.assertEqual(after[gid]['evidence'],before[gid]['evidence'])
        self.assertTrue(c.selected())
        c.core.clock=lambda:'2026-10-26T13:00:00Z'
        self.assertFalse(c.selected())

    def test_atomic_reacceptance_never_reuses_a_revoked_grant(self):
        c=self.c;self.offer();job=c.ingest('Yes, please. Pickup is at 4 PM.')
        self.apply(job,reviews=self.atomic_review);gid=next(iter(c.updater.inspect()['assignment']))
        revoke=c.ingest('Stop sharing pickup instructions.')
        self.apply(revoke,[fixtures.operation('revoke_assignment',c.evidence(revoke['source_id'],'assignment_id','issuer','reason'),
            assignment_id=gid,issuer='owner',reason='Owner revoked this task access.')])
        self.assertFalse(c.selected())
        later=c.ingest('Yes, please arrange pickup again. Pickup is at 4 PM.')
        self.apply(later,reviews=self.atomic_review)
        assignments=c.updater.inspect()['assignment']
        self.assertEqual(assignments[gid]['status'],'revoked')
        self.assertEqual(sum(a['status']=='active' for a in assignments.values()),1)
        self.assertTrue(c.selected())

    def test_atomic_third_party_response_is_not_subject_corroboration(self):
        c=self.c;self.offer();job=c.ingest('Yes, please. Pickup is at 4 PM.',actor='outsider')
        with self.assertRaisesRegex(PermissionProposalRejected,'Corroboration must be from subject'):
            self.apply(job,reviews=self.atomic_review)

    def test_atomic_contract_schema_and_source_aliases_round_trip(self):
        import jsonschema
        c=self.c;self.offer();job=c.ingest('Yes, please. Pickup is at 4 PM.')
        c.model.compact_permission_input=True
        complete=c.model.complete
        def check(system,payload,*,schema=None):
            rs=tasks.empty_reviews(payload);self.atomic_review(payload,rs)
            result={'revision':payload['revision'],'considered_restrictions':[],'operations':[], 'task_reviews':rs}
            jsonschema.validate(result,schema)
            return result
        c.model.complete=check;c.updater.run(job['job_id'])
        self.assertTrue(c.selected())

    def test_atomic_other_subject_facts_cannot_cross_the_subject_boundary(self):
        c=self.c;self.offer();job=c.ingest('Yes, please. Pickup is at 4 PM.',subject='project')
        with self.assertRaisesRegex(PermissionProposalRejected,'crosses subject boundary'):
            self.apply(job,reviews=self.atomic_review)
        self.assertEqual(c.updater.access_state()['assignment'],{})

    def test_atomic_new_restriction_suspends_previously_bound_information(self):
        c=self.c;self.offer();job=c.ingest('Yes, please. Pickup is at 4 PM.')
        self.apply(job,reviews=self.atomic_review)
        gid=next(iter(c.updater.inspect()['assignment']))
        deny=c.ingest('Stop sharing pickup information.')
        restriction=c.restriction(deny['source_id'])
        restriction['assignment_decisions']=[{'assignment_id':gid,'applies':True,
            'explanation':'The prohibition overlaps the pickup task.','evidence':[deny['source_id']]}]
        suspend=fixtures.operation('suspend_assignment',c.evidence(deny['source_id'],'assignment_id','reason'),
            assignment_id=gid,reason='The subject prohibited this disclosure.')
        self.apply(deny,[restriction,suspend])
        self.assertFalse(c.selected())

    def test_atomic_publication_failure_keeps_pending_candidate_and_grant_atomic(self):
        c=self.c;_,row=self.offer();job=c.ingest('Yes, please. Pickup is at 4 PM.')
        publish=c.core._publication_hook
        c.core._publication_hook=lambda _:(_ for _ in ()).throw(RuntimeError('injected failure'))
        with self.assertRaisesRegex(RuntimeError,'injected failure'):self.apply(job,reviews=self.atomic_review)
        self.assertEqual(tasks.rows(c.core,'conversation')[row['id']]['status'],'unresolved')
        self.assertEqual(c.updater.access_state()['assignment'],{})
        c.core._publication_hook=publish
        self.apply(job,reviews=self.atomic_review);self.assertTrue(c.selected())

    def test_new_grant_decoder_ids_are_reserved_and_reason_codes_are_coupled(self):
        import jsonschema
        c=self.c;job=c.ingest('Could I arrange pickup?',actor='helper');c.model.compact_permission_input=True
        def check(system,p,*,schema=None):
            rs=tasks.empty_reviews(p)
            proposal={'revision':p['revision'],'considered_restrictions':[],'operations':[], 'task_reviews':rs}
            jsonschema.validate(proposal,schema)
            bad=deepcopy(proposal);bad['task_reviews'][0]['decision']='unresolved'
            self.assertFalse(jsonschema.Draft202012Validator(schema).is_valid(bad))
            grant_variants=[v for v in schema['properties']['operations']['items']['anyOf']
                if v['properties']['op']['enum']==['propose_assignment']]
            self.assertTrue(all(v['properties']['id']['enum']==p['new_assignment_ids'] for v in grant_variants))
            return proposal
        c.model.complete=check;c.updater.run(job['job_id'])

    def test_decoder_pins_every_review_to_the_actual_new_reply(self):
        import jsonschema
        c=self.c;first,row=self.offer();job=c.ingest('Yes, please arrange pickup.')
        c.model.compact_permission_input=True
        def check(system,p,*,schema=None):
            rs=tasks.empty_reviews(p)
            result={'revision':p['revision'],'considered_restrictions':[],'operations':[], 'task_reviews':rs}
            jsonschema.validate(result,schema)
            old=next(s['id'] for s in p['sources'] if s['id']!=p['new_source_id'])
            result['task_reviews'][0]['evidence']=[old]
            self.assertFalse(jsonschema.Draft202012Validator(schema).is_valid(result))
            result['task_reviews'][0]['evidence']=[p['new_source_id']]
            return result
        c.model.complete=check;c.updater.run(job['job_id'])


if __name__=='__main__':unittest.main()
