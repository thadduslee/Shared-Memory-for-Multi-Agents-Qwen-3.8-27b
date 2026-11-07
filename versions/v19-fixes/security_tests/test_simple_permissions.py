"""Compact deltas, bounded repairs, persistent tasks, and unchanged enforcement."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import jsonschema

from security_tests import test_permission_inference as fixtures
from agentic_memory.privacy.permissions import PermissionUpdater, PermissionProposalRejected, PermissionRepairExhausted
from agentic_memory.privacy.simple_permissions import PROMPT_VERSION
from agentic_memory.privacy.system import digest
from agentic_memory.privacy.permissions import PermissionRevisionConflict
from agentic_memory.privacy.processors import ProcessorContractFailure


class SimplePermissionTests(unittest.TestCase):
    def setUp(self):
        self.c = fixtures.PermissionInference(); self.c.setUp()
        c = self.c
        c.model.require_permission_coverage = True  # v13 must ignore the old review workflow.
        c.updater = PermissionUpdater(c.core, c.host, 'conversation', c.bootstrap, c.model, version='13')
        self.schemas = []
        original = c.model.complete
        self.response = {'permission_changes': [], 'new_facts': []}

        def respond(system, payload, *, schema=None):
            self.schemas.append(deepcopy(schema))
            value = self.response(payload) if callable(self.response) else deepcopy(self.response)
            c.model.handler = lambda _: value
            result = original(system, payload, schema=schema)
            jsonschema.validate(result, schema)
            return result
        c.model.complete = respond

    def tearDown(self):self.c.tearDown()

    def apply(self, job, ops=(), *, max_attempts=3):
        self.response = lambda p:self.report(p,ops(p) if callable(ops) else ops)
        return self.c.updater.update(job['job_id'], max_attempts=max_attempts)

    def report(self, p, ops):
        """Build independent wire decisions from this suite's operation fixtures."""
        changes=[];facts=[]
        for op in deepcopy(list(ops)):
            bindings=op.pop('bindings',[]) if op['op']=='propose_assignment' else []
            if op['op']=='bind_information':
                bindings=[op]
            else:changes.append(op)
            for b in bindings:
                facts.append({'source_id':b['source_id'],'quote':b['quote'],'evidence_kind':b['evidence_kind'],
                    'evidence':op['evidence'],'make_available_under':[{
                        'assignment_id':op.get('assignment_id',op.get('id')),
                        'blocked_by':b['blocked_by'],'reason':op['reason'],'in_scope':True}]})
        assignments={**self.c.updater.inspect()['assignment'],
            **{op['id']:op for op in changes if op['op']=='propose_assignment'}}
        unavailable={op['assignment_id'] for op in changes
                     if op['op'] in {'suspend_assignment','revoke_assignment'}}
        from agentic_memory.privacy.lifecycle import question_spans
        for op in changes:
            if op['op'] not in {'observe_participation','establish_activity'}:continue
            op.setdefault('requester',op['subject'])
            if op['op']!='establish_activity':continue
            if 'recipient_access' in op:continue
            reviews=[]
            for recipient in sorted({op['participant'],op['subject'],op['requester']}-{None}):
                matches=[aid for aid,a in assignments.items() if a['participant']==recipient
                    and a['subject']==op['subject'] and a['status']=='active' and aid not in unavailable]
                aid=next((aid for aid in reversed(matches) if assignments[aid].get('activity_id')==op['id']),
                         matches[-1] if matches else None)
                bound=aid and bool(assignments[aid].get('bindings'))
                for fact in facts:
                    body=next(s['text'] for s in p['sources'] if s['id']==fact['source_id'])
                    start=body.find(fact['quote']);end=start+len(fact['quote'])
                    question=any(start<right and left<end for left,right in question_spans(body))
                    bound=bound or any(d['assignment_id']==aid and not d['blocked_by'] and not question
                                       for d in fact['make_available_under'])
                reviews.append({'participant':recipient,'decision':'allowed' if aid else 'unresolved',
                    'assignment_id':aid,'binding_status':('bound' if bound else 'awaiting_facts') if aid else 'not_applicable',
                    'reason':'Assess this recipient independently using the narrow assignment.'})
            op['recipient_access']=reviews
        return {'permission_changes':changes,'new_facts':facts}

    def op(self, name, p, **fields):
        if 'issuer' in fields or 'authority' in fields:
            fields.pop('issuer',None);fields.pop('authority',None)
            fields.setdefault('authority_source',p['new_source_id'])
        return {'op': name, 'evidence': [p['new_source_id']],
                'reason': 'The identified statements support this narrow change.', **fields}

    def pending(self):
        c = self.c; job = c.ingest('Can I pick you up?', actor='helper')
        self.apply(job, lambda p: [self.op('observe_participation', p, id='pickup', participant='helper',
            subject='owner', activity='Arrange the owner pickup', status='tentative', basis='performance',
            unresolved=['Awaiting owner acceptance of this pickup task'])])
        return job

    def grant(self, p, **fields):
        fields.setdefault('bindings',[{'source_id':p['new_source_id'],'quote':'Pickup is at 4 PM.',
                                      'evidence_kind':'instruction','blocked_by':[]}])
        return self.op('propose_assignment', p, id='access', participant='helper', subject='owner',
                       basis='task', activity_id='pickup', status='active',
                       actions=['search','read','derive'], information_scope='Pickup time and location for this ride', **fields)

    def binding(self, p, quote='Pickup is at 4 PM.', **fields):
        fields.setdefault('blocked_by',[])
        return self.op('bind_information', p, assignment_id='access', source_id=p['new_source_id'],
                       quote=quote, evidence_kind='instruction', **fields)

    def accept(self):
        c = self.c; self.pending()
        job = c.ingest('Yes, please pick me up. Pickup is at 4 PM. Private code is 1234.')
        self.apply(job, lambda p: [self.op('establish_activity', p, id='pickup', participant='helper',
            subject='owner', activity='Arrange the owner pickup', status='established', basis='corroborated',
            corroboration=[p['new_source_id']]), self.grant(p)])
        return job

    def test_no_change_is_tiny_and_one_call_even_with_pending_tasks(self):
        c = self.c; self.pending()
        for i in range(10):
            job = c.ingest(f'Unrelated observation {i}.', actor='outsider')
            before = len(c.model.calls); self.apply(job)
            self.assertEqual(len(c.model.calls), before + 1)
        p = c.model.calls[-1]
        self.assertEqual(c.updater.version, '13')
        self.assertEqual(len(p['sources']), 6)
        self.assertEqual(p['permission_state']['activity']['pickup']['status'], 'tentative')
        self.assertNotIn('task_candidates', p); self.assertNotIn('coverage_required', p)
        self.assertNotIn('ledger_index', str(p)); self.assertNotIn('scope_evidence', str(p))
        self.assertNotIn('binding_authorizations', p)
        self.assertNotIn('evidence', json.dumps(p['permission_state']))
        self.assertEqual(c.updater.access_state()['assignment'], {})
        self.assertEqual(c.core.db.execute('SELECT COUNT(*) FROM permission_task_candidates').fetchone()[0], 0)
        self.assertEqual(c.core.db.execute('SELECT prompt_version FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0], PROMPT_VERSION)

    def medication_task(self, *, facts=True):
        c=self.c
        offer=c.ingest('I can send your medication instructions.',actor='helper')
        self.apply(offer,lambda p:[self.op('observe_participation',p,id='medication',participant='helper',
            requester=None,subject='owner',activity='Send the patient their medication instructions',
            status='tentative',basis='performance',unresolved=['Awaiting patient request or acceptance'])])
        job=c.ingest('Please send me the medication instructions.'+(' Take 1 tablet with food.' if facts else ''))
        def ops(p):
            binding=[{'source_id':p['new_source_id'],'quote':'Take 1 tablet with food.',
                      'evidence_kind':'instruction','blocked_by':[]}] if facts else []
            return [self.op('establish_activity',p,id='medication',participant='helper',requester='owner',
                subject='owner',activity='Send the patient their medication instructions',status='established',
                basis='corroborated',corroboration=[p['new_source_id']]),
                self.op('propose_assignment',p,id='nurse-meds',participant='helper',subject='owner',
                    basis='task',activity_id='medication',status='active',actions=['search','read','derive'],
                    information_scope='Medication instructions for this task',bindings=deepcopy(binding)),
                self.op('propose_assignment',p,id='patient-meds',participant='owner',subject='owner',
                    basis='explicit',authority='owner',status='active',actions=['search','read','derive'],
                    information_scope='Medication instructions for this task',bindings=deepcopy(binding))]
        self.apply(job,ops)
        return job

    def patient_reader(self):
        return self.c.core.issue_session('owner','answer',actions=['read','search','derive'],
            compartments=['conversation'],processing_routes=self.c.routes)

    def test_requester_worker_and_beneficiary_are_separate_with_facts_for_both(self):
        c=self.c;job=self.medication_task()
        activity=c.updater.access_state()['activity']['medication']
        self.assertEqual((activity['requester'],activity['participant'],activity['subject']),('owner','helper','owner'))
        for reader in [c.reader,self.patient_reader()]:
            self.assertEqual([r['text'] for r in c.core.search(reader,'medication')],['Take 1 tablet with food.'])
        self.assertFalse(c.core.allowed(self.patient_reader(),job['source_id']))
        validation=json.loads(c.core.db.execute('SELECT validation FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0])
        self.assertEqual({r['participant']:r['binding_status'] for r in
            validation['task_access_assessments'][0]['recipient_access']},{'helper':'bound','owner':'bound'})

    def test_fact_only_turn_fills_both_existing_empty_permissions_without_acl_changes(self):
        c=self.c;self.medication_task(facts=False)
        self.assertFalse(c.core.search(self.patient_reader(),'medication'))
        job=c.ingest('Take 1 tablet with food.',actor='helper')
        def report(p):
            return {'permission_changes':[],'new_facts':[{'source_id':p['new_source_id'],
                'quote':'Take 1 tablet with food.','evidence_kind':'instruction','evidence':[p['new_source_id']],
                'make_available_under':[{'assignment_id':aid,'in_scope':True,'blocked_by':[],
                    'reason':'The instruction fits this recipient medication permission.'}
                    for aid in ['patient-meds','nurse-meds']]}]}
        self.response=report;before=len(c.model.calls)
        c.updater.update(job['job_id'])
        self.assertEqual(len(c.model.calls),before+1)
        saved=json.loads(c.core.db.execute('SELECT proposal FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0])
        self.assertEqual([o['op'] for o in saved['operations']],['bind_information','bind_information'])
        for reader in [c.reader,self.patient_reader()]:
            self.assertEqual([r['text'] for r in c.core.search(reader,'medication')],['Take 1 tablet with food.'])

    def test_later_worker_facts_cannot_silently_skip_patient_scope_assessment(self):
        c=self.c;self.medication_task(facts=False)
        job=c.ingest('Take 1 tablet with food.',actor='helper')
        self.response=lambda p:{'permission_changes':[],'new_facts':[{'source_id':p['new_source_id'],
            'quote':'Take 1 tablet with food.','evidence_kind':'instruction','evidence':[p['new_source_id']],
            'make_available_under':[{'assignment_id':'nurse-meds','in_scope':True,'blocked_by':[],
                'reason':'Instructions needed by the nurse.'}]}]}
        with self.assertRaisesRegex(PermissionRepairExhausted,'every allowed task recipient'):
            c.updater.update(job['job_id'])
        self.assertEqual(c.updater.inspect()['assignment']['patient-meds']['bindings'],[])
        self.assertEqual(c.updater.inspect()['assignment']['nurse-meds']['bindings'],[])

    def test_each_fact_recipient_keeps_independent_restriction_checks(self):
        c=self.c;self.medication_task(facts=False)
        job=c.ingest('Do not share the medication instructions with helper. Take 1 tablet with food.')
        def report(p):
            restriction=self.op('record_restriction',p,id='nurse-limit',issuer='owner',participants=['helper'],
                subject='owner',information_scope='Medication instructions',actions=['read','search','derive'],
                effect='deny',blocked_assignments=['nurse-meds'])
            return {'permission_changes':[restriction],'new_facts':[{'source_id':p['new_source_id'],
                'quote':'Take 1 tablet with food.','evidence_kind':'instruction','evidence':[p['new_source_id']],
                'make_available_under':[{'assignment_id':aid,'in_scope':True,'blocked_by':blocked,
                    'reason':'Assess the instruction against this recipient scope and restriction.'}
                    for aid,blocked in [('nurse-meds',['nurse-limit']),('patient-meds',[])]]}]}
        self.response=report;c.updater.update(job['job_id'])
        self.assertEqual(c.core.search(c.reader,'medication'),[])
        self.assertEqual([r['text'] for r in c.core.search(self.patient_reader(),'medication')],['Take 1 tablet with food.'])

    def test_scope_mismatch_is_reviewed_without_copying_worker_access(self):
        c=self.c;self.medication_task(facts=False)
        job=c.ingest('Staff must log the dispensing audit internally.',actor='helper')
        self.response=lambda p:{'permission_changes':[],'new_facts':[{'source_id':p['new_source_id'],
            'quote':'Staff must log the dispensing audit internally.','evidence_kind':'instruction',
            'evidence':[p['new_source_id']],'make_available_under':[
                {'assignment_id':'nurse-meds','in_scope':False,'blocked_by':[],
                 'reason':'An internal dispensing audit is outside medication instructions.'},
                {'assignment_id':'patient-meds','in_scope':False,'blocked_by':[],
                 'reason':'The patient permission covers their medication instructions only.'}]}]}
        c.updater.update(job['job_id'])
        self.assertFalse(c.core.search(c.reader,'audit'))
        self.assertFalse(c.core.search(self.patient_reader(),'audit'))

    def test_missing_recipient_feedback_can_retain_fact_without_a_matching_permission(self):
        c=self.c;self.medication_task(facts=False)
        job=c.ingest('Go to urgent care if you faint.',actor='helper')
        def report(p):
            destinations=[{'assignment_id':'patient-meds','in_scope':True,'blocked_by':[],
                'reason':'The patient needs their own instructions.'}]
            if 'correction' in p:
                details=p['correction']['validation']['details']
                self.assertEqual(details['missing_assignment_ids'],['nurse-meds'])
                self.assertEqual({r['information_scope'] for r in details['recipient_scopes']},
                                 {'Medication instructions for this task'})
                self.assertIn('none fits',details['repair'])
                destinations=[]  # Urgent care warning signs need their own supported scope.
            return {'permission_changes':[],'new_facts':[{'source_id':p['new_source_id'],
                'quote':'Go to urgent care if you faint.','evidence_kind':'instruction',
                'evidence':[p['new_source_id']],'make_available_under':destinations}]}
        self.response=report;before=len(c.model.calls)
        result=c.updater.update(job['job_id'])
        self.assertEqual(result['status'],'complete');self.assertEqual(len(c.model.calls),before+2)
        for a in c.updater.inspect()['assignment'].values():self.assertEqual(a['bindings'],[])
        self.assertEqual(c.core.read(c.host,job['source_id'])['payload']['text'],'Go to urgent care if you faint.')

    def test_skipping_exhaustion_advances_ingestion_without_releasing_stale_access(self):
        c=self.c;self.accept()
        job=c.ingest('Stop sharing my pickup information with helper.')
        with self.assertRaises(PermissionRepairExhausted):
            self.apply(job,lambda p:[self.binding(p,quote='An invented instruction.')])
        attempts=[tuple(r) for r in c.core.db.execute('SELECT * FROM permission_attempts WHERE job_id=?',(job['job_id'],))]
        before=len(c.model.calls)
        self.assertEqual(c.updater.skip_exhausted(job['job_id']),{'status':'skipped','changes':[]})
        self.assertEqual(c.updater.pending_jobs(),[])
        self.assertEqual(c.selected(),[])  # An unprocessed revocation must not reopen old grants.
        following=c.ingest('Pickup is at 5 PM.')
        self.apply(following,lambda p:[self.binding(p,quote='Pickup is at 5 PM.')])
        self.assertEqual(c.selected(),[])
        c.updater=PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version='13')
        calls=len(c.model.calls)
        self.assertEqual(c.updater.update(job['job_id']),{'status':'skipped','changes':[]})
        self.assertEqual(c.updater.skip_exhausted(job['job_id']),{'status':'skipped','changes':[]})
        self.assertEqual(len(c.model.calls),calls)
        self.assertEqual(calls,before+1)
        self.assertEqual([tuple(r) for r in c.core.db.execute(
            'SELECT * FROM permission_attempts WHERE job_id=?',(job['job_id'],))],attempts)
        validation=json.loads(c.core.db.execute('SELECT validation FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0])
        self.assertTrue(validation['unresolved_access_hold']);self.assertEqual(validation['rejections'],3)

    def test_cannot_skip_before_exhausting_budget(self):
        job=self.c.ingest('An ordinary source event.')
        with self.assertRaisesRegex(ValueError,'exhausted'):
            self.c.updater.skip_exhausted(job['job_id'])
        self.assertEqual(self.c.updater.pending_jobs(),[job['job_id']])

    def test_pending_offer_survives_window_restart_then_accepts_in_one_call(self):
        c = self.c; first = self.pending()
        for i in range(8):self.apply(c.ingest(f'Unrelated observation {i}.', actor='outsider'))
        c.updater = PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version='13')
        job = c.ingest('Yes, please pick me up. Pickup is at 4 PM. Private code is 1234.')
        before = len(c.model.calls)
        self.apply(job, lambda p: [self.op('establish_activity',p,id='pickup',participant='helper',
            subject='owner',activity='Arrange the owner pickup',status='established',basis='corroborated',
            corroboration=[p['new_source_id']]), self.grant(p)])
        self.assertEqual(len(c.model.calls), before + 1)
        self.assertNotIn('Can I pick you up?', str(c.model.calls[-1]))
        state = c.updater.inspect()
        self.assertEqual(state['activity']['pickup']['performance'], [first['source_id']])
        self.assertEqual(state['activity']['pickup']['unresolved'], [])
        self.assertEqual([r['text'] for r in c.selected()], ['Pickup is at 4 PM.'])
        self.assertNotIn('1234', str(c.selected()))

    def test_new_fact_reuses_grant_without_replaying_old_evidence(self):
        c = self.c; old = self.accept()
        for i in range(8):self.apply(c.ingest(f'Unrelated observation {i}.',actor='outsider'))
        job = c.ingest('Pickup is at 5 PM.')
        self.apply(job, lambda p: [self.binding(p, quote='Pickup is at 5 PM.')])
        self.assertNotIn(old['source_id'], str(c.model.calls[-1]))
        self.assertEqual(len(c.updater.inspect()['assignment']), 1)
        self.assertEqual(len(c.updater.inspect()['assignment']['access']['bindings']), 2)
        self.assertEqual(c.updater.inspect()['assignment']['access']['bindings'][-1]['evidence']['authorization'], [old['source_id']])

    def test_new_task_can_use_worker_receipt_from_shared_recent_evidence(self):
        c=self.c;self.apply(c.ingest('I can arrange pickup.',actor='helper'))
        job=c.ingest('Yes, please. Pickup is at 4 PM.')
        def ops(p):
            activity=self.op('establish_activity',p,id='pickup',participant='helper',subject='owner',
                activity='Arrange the owner pickup',status='established',basis='corroborated',
                corroboration=[p['new_source_id']])
            activity['evidence']=[p['sources'][0]['id'],p['new_source_id']]
            return [activity,self.grant(p)]
        before=len(c.model.calls);self.apply(job,ops)
        self.assertEqual(len(c.model.calls),before+1)
        self.assertEqual([r['text'] for r in c.selected()],['Pickup is at 4 PM.'])

    def test_pending_meaning_can_update_after_worker_leaves_window(self):
        c=self.c;first=self.pending()
        for i in range(8):self.apply(c.ingest(f'Unrelated observation {i}.',actor='outsider'))
        job=c.ingest('I am still deciding about your pickup offer.')
        self.apply(job,lambda p:[self.op('observe_participation',p,id='pickup',participant='helper',
            subject='owner',activity='Arrange the owner pickup',status='tentative',basis='performance',
            unresolved=['Owner is still deciding'])])
        self.assertEqual(c.updater.inspect()['activity']['pickup']['performance'],[first['source_id']])
        self.assertEqual(c.updater.access_state()['assignment'],{})

    def test_validity_uses_shared_evidence_without_a_repair_call(self):
        c = self.c; self.pending(); job = c.ingest('Yes, please arrange pickup until 13:00.',timestamp='2026-10-26T12:00:00Z')
        before = len(c.model.calls)
        self.apply(job,lambda p:[self.op('establish_activity',p,id='pickup',participant='helper',subject='owner',
            activity='Arrange the owner pickup',status='established',basis='corroborated',corroboration=[p['new_source_id']]),
            self.grant(p,bindings=[],validity={'effective_until':'2026-10-26T13:00:00Z'})])
        self.assertEqual(len(c.model.calls),before+1)
        self.assertEqual(c.updater.inspect()['assignment']['access']['evidence']['validity'],[job['source_id']])
        self.assertEqual(c.selected(), [])  # Accepted task still needs factual bindings.

    def test_unauthorized_grant_stops_after_three_attempts_and_keeps_access_blocked(self):
        c=self.c;job=c.ingest('I grant myself access.',actor='outsider');before=len(c.model.calls)
        with self.assertRaisesRegex(PermissionRepairExhausted,'configured authority'):
            self.apply(job, lambda p:[self.op('propose_assignment',p,id='forged',participant='outsider',subject='owner',
                basis='explicit',authority='outsider',status='active',actions=['read'],information_scope='*',bindings=[])])
        self.assertEqual(len(c.model.calls),before+3)
        self.assertEqual(c.updater.access_state()['assignment'],{})
        with self.assertRaises(PermissionRepairExhausted):c.updater.update(job['job_id'])
        self.assertEqual(len(c.model.calls),before+3)
        self.assertFalse(c.selected())

    def test_concurrent_policy_change_has_a_durable_three_attempt_limit(self):
        c=self.c;job=c.ingest('An ordinary turn.');before=len(c.model.calls)
        def changed_state(p):
            with c.core.transaction():
                c.core.db.execute('UPDATE permission_bootstrap SET revision=revision+1 WHERE compartment=?',('conversation',))
            return {'permission_changes':[],'new_facts':[]}
        self.response=changed_state
        with self.assertRaisesRegex(PermissionRepairExhausted,'changed during inference'):
            c.updater.update(job['job_id'])
        with self.assertRaises(PermissionRepairExhausted):c.updater.update(job['job_id'])
        self.assertEqual(len(c.model.calls),before+3)

    def test_wrong_quote_still_fails_closed(self):
        for text, quote in [('Pickup is at 5 PM.','Pickup is at 6 PM.')]:
            with self.subTest(text=text):
                # Each invalid turn has its own fresh store; failures cannot be skipped.
                if self.c.updater.pending_jobs():
                    self.tearDown();self.setUp()
                c=self.c;self.accept();job=c.ingest(text);before=len(c.model.calls)
                with self.assertRaises(PermissionRepairExhausted):
                    self.apply(job,lambda p:[self.binding(p,quote=quote)])
                self.assertEqual(len(c.model.calls),before+3)
                self.assertFalse(c.selected())

    def test_retry_receives_feedback_and_commits_only_the_corrected_proposal(self):
        c=self.c;self.accept();job=c.ingest('Pickup is at 5 PM.');before=len(c.model.calls)
        def ops(p):
            if 'correction' not in p:
                return [self.binding(p,quote='Pickup is at 6 PM.')]
            self.assertIn('unique exact source quote',p['correction']['validation']['reason'])
            self.assertEqual(p['correction']['attempt'],2)
            self.assertNotIn('previous_proposal',p['correction'])
            return [self.binding(p,quote='Pickup is at 5 PM.')]
        self.apply(job,ops)
        self.assertEqual(len(c.model.calls),before+2)
        self.assertEqual(len(c.updater.inspect()['assignment']['access']['bindings']),2)
        attempts=c.core.db.execute('SELECT validation FROM permission_attempts WHERE job_id=? ORDER BY attempt',
                                  (job['job_id'],)).fetchall()
        self.assertFalse(json.loads(attempts[0][0])['accepted'])
        self.assertTrue(json.loads(attempts[1][0])['accepted'])

    def test_failed_attempt_before_restart_counts_toward_three_attempt_limit(self):
        c=self.c;self.accept();job=c.ingest('Pickup is at 5 PM.');before=len(c.model.calls)
        with self.assertRaises(PermissionRepairExhausted):
            self.apply(job,lambda p:[self.binding(p,quote='Pickup is at 6 PM.')],max_attempts=1)
        first=c.core.db.execute('SELECT validation FROM permission_attempts WHERE job_id=? AND attempt=1',
                               (job['job_id'],)).fetchone()[0]
        c.updater=PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version='13')
        with self.assertRaises(PermissionRepairExhausted):c.updater.update(job['job_id'])
        self.assertEqual(len(c.model.calls),before+3)
        self.assertEqual(c.core.db.execute('SELECT validation FROM permission_attempts WHERE job_id=? AND attempt=1',
                                          (job['job_id'],)).fetchone()[0],first)
        with self.assertRaises(PermissionRepairExhausted):c.updater.update(job['job_id'])
        self.assertEqual(len(c.model.calls),before+3)

    def test_malformed_model_responses_share_the_durable_repair_budget(self):
        c=self.c;job=c.ingest('An ordinary turn.');before=len(c.model.calls)
        def malformed(*args,**kwargs):
            c.model.calls.append(1)
            raise ProcessorContractFailure('invalid structured response')
        c.model.complete=malformed
        with self.assertRaisesRegex(PermissionRepairExhausted,'invalid structured response'):
            c.updater.update(job['job_id'])
        self.assertEqual(len(c.model.calls),before+3)
        with self.assertRaises(PermissionRepairExhausted):c.updater.update(job['job_id'])
        self.assertEqual(len(c.model.calls),before+3)

    def test_malformed_model_response_retry_receives_feedback(self):
        c=self.c;job=c.ingest('An ordinary turn.');original=c.model.complete
        def malformed_once(system,payload,*,schema=None):
            if 'correction' not in payload:
                raise ProcessorContractFailure('invalid structured response')
            self.assertEqual(payload['correction']['attempt'],2)
            self.assertEqual(payload['correction']['validation']['reason'],'invalid structured response')
            return original(system,payload,schema=schema)
        c.model.complete=malformed_once
        self.assertEqual(c.updater.update(job['job_id'])['status'],'complete')
        self.assertEqual(c.core.db.execute('SELECT attempts FROM permission_jobs WHERE id=?',
                                          (job['job_id'],)).fetchone()[0],2)

    def test_question_binding_is_omitted_without_becoming_factual_memory(self):
        c=self.c;self.accept();job=c.ingest('Is pickup at 5 PM?');before=len(c.model.calls)
        self.apply(job,lambda p:[self.binding(p,quote='Is pickup at 5 PM?')])
        self.assertEqual(len(c.model.calls),before+1)
        self.assertEqual([r['text'] for r in c.selected()],['Pickup is at 4 PM.'])
        validation=json.loads(c.core.db.execute('SELECT validation FROM permission_jobs WHERE id=?',
                                               (job['job_id'],)).fetchone()[0])
        self.assertEqual(validation['binding_omissions'][0]['reason'],'question_text')

    def test_restriction_and_revocation_remove_existing_access(self):
        c=self.c;self.accept();job=c.ingest('Do not share pickup information with helper anymore.')
        def ops(p):
            return [self.op('record_restriction',p,id='private',issuer='owner',participants=['helper'],subject='owner',
                information_scope='Pickup time and location for this ride',actions=['read','search','derive'],effect='deny',
                blocked_assignments=['access']),
                self.op('revoke_assignment',p,assignment_id='access',issuer='owner')]
        self.apply(job,ops)
        self.assertEqual(c.updater.access_state()['assignment']['access']['status'],'revoked')
        self.assertFalse(c.selected())

    def test_exhaustive_overlap_list_suspends_without_another_model_call(self):
        c=self.c;self.accept();job=c.ingest('Stop sharing my pickup information.')
        before=len(c.model.calls)
        self.apply(job,lambda p:[self.op('record_restriction',p,id='private',issuer='owner',
            participants=['helper'],subject='owner',information_scope='Pickup time and location for this ride',
            actions=['read','search','derive'],effect='deny',blocked_assignments=['access'])])
        self.assertEqual(len(c.model.calls),before+1)
        self.assertEqual(c.updater.access_state()['assignment']['access']['status'],'suspended')
        self.assertFalse(c.selected())

    def test_duplicate_assignment_ids_suspend_once_without_a_retry(self):
        c=self.c;self.accept();job=c.ingest('Stop sharing my pickup information.');before=len(c.model.calls)
        self.apply(job,lambda p:[self.op('record_restriction',p,id='private',issuer='owner',
            participants=['helper'],subject='owner',information_scope='Pickup time and location for this ride',
            actions=['read','search','derive'],effect='deny',blocked_assignments=['access']*128)])
        self.assertEqual(len(c.model.calls),before+1)
        self.assertEqual(c.updater.access_state()['assignment']['access']['status'],'suspended')
        saved=json.loads(c.core.db.execute('SELECT proposal FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0])
        self.assertEqual(len(saved['operations'][0]['assignment_decisions']),1)
        self.assertEqual(sum(op['op']=='suspend_assignment' for op in saved['operations']),1)
        self.assertFalse(c.selected())

    def test_duplicate_restriction_ids_still_omit_direct_and_nested_prohibited_facts(self):
        c=self.c;self.accept();job=c.ingest('Pickup is at 5 PM. Keep pickup details private.')
        def ops(p):
            return [self.binding(p,quote='Pickup is at 5 PM.',blocked_by=['private']*128),
                self.op('propose_assignment',p,id='second',participant='helper',subject='owner',
                    basis='explicit',authority='owner',status='active',actions=['read'],information_scope='pickup',
                    bindings=[{'source_id':p['new_source_id'],'quote':'Pickup is at 5 PM.',
                               'evidence_kind':'instruction','blocked_by':['private']*128}]),
                self.op('record_restriction',p,id='private',issuer='owner',participants=['helper'],subject='owner',
                    information_scope='Pickup time and location for this ride',actions=['read','search','derive'],
                    effect='deny',blocked_assignments=['access']*128)]
        before=len(c.model.calls);self.apply(job,ops)
        self.assertEqual(len(c.model.calls),before+1)
        self.assertEqual(len(c.updater.inspect()['assignment']['access']['bindings']),1)
        self.assertEqual(c.updater.inspect()['assignment']['second']['bindings'],[])
        self.assertFalse(c.selected())

    def test_duplicate_unknown_restriction_ids_are_still_rejected(self):
        c=self.c;self.accept();job=c.ingest('Pickup is at 5 PM.')
        with self.assertRaisesRegex(PermissionRepairExhausted,'unknown restriction'):
            self.apply(job,lambda p:[self.binding(p,quote='Pickup is at 5 PM.',blocked_by=['unknown']*2)])
        self.assertEqual(len(c.updater.inspect()['assignment']['access']['bindings']),1)
        self.assertFalse(c.selected())

    def test_new_grant_compiles_its_nested_facts_in_the_same_call(self):
        c=self.c;self.pending();job=c.ingest('Yes, please. Pickup is at 4 PM. Private code is 1234.')
        def ops(p):
            return [self.op('establish_activity',p,id='pickup',participant='helper',subject='owner',
                activity='Arrange the owner pickup',status='established',basis='corroborated',
                corroboration=[p['new_source_id']]),self.grant(p,bindings=[{'source_id':p['new_source_id'],
                    'quote':'Pickup is at 4 PM.','evidence_kind':'instruction','blocked_by':[]}])]
        before=len(c.model.calls);self.apply(job,ops)
        self.assertEqual(len(c.model.calls),before+1)
        self.assertEqual([r['text'] for r in c.selected()],['Pickup is at 4 PM.'])

    def test_new_final_restriction_is_assessed_even_when_binding_comes_first(self):
        c=self.c;self.accept();job=c.ingest('Pickup is at 5 PM. My diagnosis remains private.')
        self.apply(job,lambda p:[self.binding(p,quote='Pickup is at 5 PM.'),
            self.op('record_restriction',p,id='clinical',issuer='owner',participants=['helper'],subject='owner',
                information_scope='diagnosis',actions=['search','read','derive'],effect='deny',blocked_assignments=[])])
        decisions=c.updater.inspect()['assignment']['access']['bindings'][-1]['restriction_decisions']
        self.assertEqual([(d['restriction_id'],d['applies']) for d in decisions],[('clinical',False)])
        self.assertEqual(len(c.selected()),2)

    def test_prohibited_new_binding_is_not_published_on_a_suspended_grant(self):
        c=self.c;self.accept();job=c.ingest('Pickup is at 5 PM. Stop sharing pickup information.')
        self.apply(job,lambda p:[self.binding(p,quote='Pickup is at 5 PM.',blocked_by=['private']),
            self.op('record_restriction',p,id='private',issuer='owner',participants=['helper'],subject='owner',
                information_scope='Pickup time and location for this ride',actions=['search','read','derive'],
                effect='deny',blocked_assignments=['access'])])
        self.assertEqual(c.updater.access_state()['assignment']['access']['status'],'suspended')
        self.assertEqual(len(c.updater.inspect()['assignment']['access']['bindings']),1)
        self.assertFalse(c.selected())

    def test_restriction_cannot_suspend_a_grant_outside_its_recipient_set(self):
        c=self.c;self.accept();job=c.ingest('Outsider must not receive my pickup details.')
        self.apply(job,lambda p:[self.op('record_restriction',p,id='private',issuer='owner',
            participants=['outsider'],subject='owner',information_scope='pickup',actions=['read','search','derive'],
            effect='deny',blocked_assignments=['access'])])
        self.assertEqual(c.updater.access_state()['assignment']['access']['status'],'active')
        self.assertTrue(c.selected())

    def test_deleted_support_suspends_access_without_replaying_history(self):
        c=self.c;old=self.accept()
        for i in range(8):self.apply(c.ingest(f'Unrelated observation {i}.',actor='outsider'))
        c.core.delete(c.host,old['source_id'])
        self.apply({'job_id':c.updater.pending_jobs()[0]})
        self.assertEqual(c.updater.access_state()['assignment']['access']['status'],'suspended')
        self.assertFalse(c.selected())

    def unaccepted(self,p,task='pickup',grant='access',receipt=None):
        sid=receipt or p['new_source_id']
        activity=self.op('establish_activity',p,id=task,participant='helper',subject='owner',
            activity='Arrange pickup',basis='corroborated',status='established',
            performance=[sid],corroboration=[sid])
        activity['evidence']=[sid]
        assignment=self.grant(p)
        assignment.update(id=grant,activity_id=task,evidence=[sid])
        return [activity,assignment]

    def test_worker_self_corroboration_saves_pending_without_grants_or_fields(self):
        c=self.c;job=c.ingest('I will arrange pickup. Pickup is at 4 PM.',actor='helper');before=len(c.model.calls)
        self.apply(job,self.unaccepted)
        self.assertEqual(len(c.model.calls),before+1)
        state=c.updater.inspect()
        self.assertEqual(state['activity']['pickup']['status'],'tentative')
        self.assertEqual(state['activity']['pickup']['performance'],[job['source_id']])
        self.assertEqual(state['assignment'],{})
        self.assertFalse(c.selected())
        row=c.core.db.execute('SELECT status,validation FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()
        self.assertEqual(row['status'],'complete')
        self.assertEqual(json.loads(row['validation'])['authorization_deferrals'][0]['withheld_assignment_ids'],['access'])

    def test_deferred_task_accepts_after_eight_turns_without_an_extra_call(self):
        c=self.c;first=c.ingest('I will arrange pickup. Pickup is at 4 PM.',actor='helper')
        self.apply(first,self.unaccepted)
        for i in range(8):self.apply(c.ingest(f'Unrelated event {i}.',actor='outsider'))
        c.updater=PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version='13')
        job=c.ingest('Yes, please. Pickup is at 4 PM. Private code is 1234.');before=len(c.model.calls)
        self.apply(job,lambda p:[self.op('establish_activity',p,id='pickup',participant='helper',subject='owner',
            activity='Arrange pickup',basis='corroborated',status='established',corroboration=[p['new_source_id']]),self.grant(p)])
        self.assertEqual(len(c.model.calls),before+1)
        self.assertNotIn(first['source_id'],str(c.model.calls[-1]))
        self.assertEqual([r['text'] for r in c.selected()],['Pickup is at 4 PM.'])

    def test_deferred_grant_does_not_block_independent_revocation_and_restriction(self):
        c=self.c;self.accept();self.apply(c.ingest('I will arrange another delivery.',actor='helper'))
        job=c.ingest('Stop sharing my pickup information with helper.');before=len(c.model.calls)
        def ops(p):
            receipt=p['sources'][-2]['id']
            pending=self.unaccepted(p,task='delivery',grant='delivery-access',receipt=receipt)
            pending[1]['bindings']=[]
            return [*pending,self.op('revoke_assignment',p,assignment_id='access',issuer='owner'),
                self.op('record_restriction',p,id='private',issuer='owner',participants=['helper'],subject='owner',
                    information_scope='pickup',actions=['read','search','derive'],effect='deny',blocked_assignments=['access'])]
        self.apply(job,ops)
        self.assertEqual(len(c.model.calls),before+1)
        state=c.updater.access_state()
        self.assertEqual(state['activity']['delivery']['status'],'tentative')
        self.assertEqual(state['assignment']['access']['status'],'revoked')
        self.assertNotIn('delivery-access',state['assignment'])
        self.assertIn('private',state['restriction'])
        self.assertFalse(c.selected())

    def test_unsupported_edit_preserves_existing_accepted_task_and_grant(self):
        c=self.c;self.accept();old=deepcopy(c.updater.access_state()['activity']['pickup'])
        job=c.ingest('I will now also review all private records.',actor='helper')
        def ops(p):
            values=self.unaccepted(p,grant='wider');values[1]['bindings']=[]
            values[0]['activity']='Review private records'
            return values
        self.apply(job,ops)
        state=c.updater.access_state()
        self.assertEqual(state['activity']['pickup'],old)
        self.assertEqual(state['assignment']['access']['status'],'active')
        self.assertNotIn('wider',state['assignment'])
        self.assertEqual(sum(a['status']=='tentative' for a in state['activity'].values()),1)
        self.assertEqual([r['text'] for r in c.selected()],['Pickup is at 4 PM.'])

    def test_wrong_worker_receipt_is_not_converted_to_pending(self):
        c=self.c;job=c.ingest('Helper should pick me up.',actor='owner')
        def ops(p):
            values=self.unaccepted(p);values[1]['bindings']=[]
            return values
        with self.assertRaisesRegex(PermissionRepairExhausted,'Performance must cite'):
            self.apply(job,ops)
        self.assertEqual(c.updater.access_state()['activity'],{})

    def test_grant_for_an_existing_unconfirmed_task_stays_pending(self):
        c=self.c;self.pending();job=c.ingest('Pickup is at 4 PM.',actor='helper')
        self.apply(job,lambda p:[self.grant(p)])
        self.assertEqual(c.updater.access_state()['activity']['pickup']['status'],'tentative')
        self.assertEqual(c.updater.access_state()['assignment'],{})
        self.assertFalse(c.selected())

    def rejected_saved_proposal(self):
        c=self.c;job=c.ingest('I will arrange pickup. Pickup is at 4 PM.',actor='helper')
        with patch.object(c.updater,'_validate',side_effect=PermissionProposalRejected('Old implementation rejected missing corroboration')):
            with self.assertRaises(PermissionRepairExhausted):self.apply(job,self.unaccepted,max_attempts=1)
        row=c.core.db.execute('SELECT proposal FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()
        return job,digest(json.loads(row[0]))

    def test_reviewed_replay_preserves_failed_attempt_and_makes_no_model_call(self):
        c=self.c;job,proposal_digest=self.rejected_saved_proposal();before=len(c.model.calls)
        old=c.core.db.execute('SELECT validation FROM permission_attempts WHERE job_id=? AND attempt=1',(job['job_id'],)).fetchone()[0]
        result=c.updater.replay_rejected(job['job_id'],proposal_digest=proposal_digest)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(len(c.model.calls),before)
        self.assertEqual(c.updater.access_state()['activity']['pickup']['status'],'tentative')
        self.assertEqual(c.updater.access_state()['assignment'],{})
        attempts=c.core.db.execute('SELECT validation FROM permission_attempts WHERE job_id=? ORDER BY attempt',(job['job_id'],)).fetchall()
        self.assertEqual(len(attempts),2);self.assertEqual(attempts[0][0],old)
        self.assertFalse(json.loads(attempts[1][0])['model_called'])
        c.updater.replay_rejected(job['job_id'],proposal_digest=proposal_digest)
        self.assertEqual(len(c.model.calls),before)

    def test_reviewed_replay_requires_exact_proposal_and_unchanged_policy(self):
        c=self.c;job,proposal_digest=self.rejected_saved_proposal();before=len(c.model.calls)
        with self.assertRaisesRegex(ValueError,'digest'):
            c.updater.replay_rejected(job['job_id'],proposal_digest='wrong')
        with c.core.transaction():c.core.db.execute('UPDATE permission_bootstrap SET revision=revision+1')
        with self.assertRaisesRegex(PermissionRevisionConflict,'snapshot changed'):
            c.updater.replay_rejected(job['job_id'],proposal_digest=proposal_digest)
        self.assertEqual(len(c.model.calls),before)
        self.assertEqual(c.core.db.execute('SELECT attempts FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0],1)

    def restriction(self,p,**fields):
        values=dict(id='confirm-limit',issuer='owner',participants=['helper'],subject='owner',
            information_scope='diagnosis',actions=['search','read','derive'],effect='deny',blocked_assignments=[])
        values.update(fields)
        return self.op('record_restriction',p,**values)

    def hold(self):
        job=self.c.ingest('To confirm, may helper see scheduling only?',actor='outsider')
        self.apply(job,lambda p:[self.restriction(p)])
        return job

    def test_authority_is_derived_from_source_not_a_model_identity(self):
        c=self.c;self.accept();before=len(c.model.calls);job=self.hold()
        self.assertEqual(len(c.model.calls),before+1)
        row=c.updater.inspect()['restriction']['confirm-limit']
        self.assertEqual(row['status'],'unresolved')
        self.assertIsNone(row['issuer'])
        self.assertEqual(row['proposed_issuer'],'outsider')
        self.assertEqual(row['evidence']['issuer'],[job['source_id']])
        schema=self.schemas[-1]
        variant=next(v for v in schema['properties']['permission_changes']['items']['anyOf']
                     if v['properties']['op']['enum']==['record_restriction'])
        self.assertNotIn('issuer',variant['properties'])
        self.assertIn('authority_source',variant['required'])
        self.assertNotIn('evidence',json.dumps(c.updater.access_state()['restriction']))

    def test_pending_restriction_blocks_existing_and_new_grants_without_rewriting_them(self):
        c=self.c;self.accept();self.hold()
        self.assertEqual(c.updater.inspect()['assignment']['access']['status'],'active')
        self.assertFalse(c.selected())
        job=c.ingest('Pickup is at 5 PM. Helper may read pickup details.')
        self.apply(job,lambda p:[self.op('propose_assignment',p,id='new-access',participant='helper',subject='owner',
            basis='explicit',authority='owner',status='active',actions=['read'],information_scope='pickup',
            bindings=[{'source_id':p['new_source_id'],'quote':'Pickup is at 5 PM.',
                       'evidence_kind':'instruction','blocked_by':[]}])])
        self.assertEqual(c.updater.inspect()['assignment']['new-access']['status'],'active')
        self.assertFalse(c.selected())

    def test_pending_restriction_allows_independent_updates_and_other_recipients(self):
        c=self.c;self.accept()
        first=c.ingest('Private diagnosis only; helper must not see it.')
        self.apply(first,lambda p:[self.restriction(p,id='accepted-limit')])
        job=c.ingest('To confirm, is the diagnosis still private?',actor='outsider');before=len(c.model.calls)
        self.apply(job,lambda p:[self.restriction(p),
            self.op('record_restriction',p,id='other-confirm',issuer='owner',participants=['outsider'],
                subject='owner',information_scope='account details',actions=['read'],effect='deny',blocked_assignments=[])])
        self.assertEqual(len(c.model.calls),before+1)
        state=c.updater.inspect()
        self.assertNotIn('status',state['restriction']['accepted-limit'])
        self.assertEqual(state['restriction']['confirm-limit']['status'],'unresolved')
        self.assertEqual(c.core.db.execute('SELECT status FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0],'complete')

    def test_authority_answer_resolves_hold_after_window_restart(self):
        c=self.c;self.accept();self.hold()
        for i in range(8):self.apply(c.ingest(f'Unrelated event {i}.',actor='outsider'))
        c.updater=PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version='13')
        job=c.ingest('That was only a question. Keep my existing pickup permission unchanged.')
        before=len(c.model.calls)
        self.apply(job,lambda p:[self.op('resolve_restriction',p,restriction_id='confirm-limit',issuer='owner')])
        self.assertEqual(len(c.model.calls),before+1)
        self.assertEqual(c.updater.inspect()['restriction']['confirm-limit']['status'],'resolved')
        self.assertTrue(c.selected())

    def test_resolution_cannot_remove_an_accepted_restriction(self):
        c=self.c;self.accept();job=c.ingest('Helper must not see pickup instructions.')
        self.apply(job,lambda p:[self.restriction(p,id='accepted-limit',information_scope='pickup',blocked_assignments=['access'])])
        job=c.ingest('Keep that restriction.')
        # The model cannot even select an accepted restriction for resolution.
        self.apply(job)
        self.assertFalse(any(v['properties']['op']['enum']==['resolve_restriction']
            for v in self.schemas[-1]['properties']['permission_changes']['items']['anyOf']))
        self.assertFalse(c.selected())

    def test_unauthorized_reply_cannot_resolve_hold(self):
        c=self.c;self.accept();self.hold()
        job=c.ingest('Remove that hold.',actor='outsider')
        with self.assertRaisesRegex(PermissionRepairExhausted,'configured authority'):
            self.apply(job,lambda p:[self.op('resolve_restriction',p,restriction_id='confirm-limit',issuer='owner')])
        self.assertEqual(c.updater.inspect()['restriction']['confirm-limit']['status'],'unresolved')
        self.assertFalse(c.selected())

    def test_pending_restriction_cannot_supersede_accepted_policy(self):
        c=self.c;self.accept();job=c.ingest('Helper must not see my diagnosis.')
        self.apply(job,lambda p:[self.restriction(p,id='accepted-limit')])
        job=c.ingest('Can we replace that with a different limit?',actor='outsider')
        self.apply(job,lambda p:[self.restriction(p,supersedes='accepted-limit',blocked_assignments=['access'])])
        state=c.updater.inspect()
        self.assertNotIn('superseded_by',state['restriction']['accepted-limit'])
        self.assertEqual(state['restriction']['confirm-limit']['proposed_supersedes'],'accepted-limit')
        self.assertEqual(state['assignment']['access']['status'],'active')
        self.assertFalse(c.selected())

    def test_pure_authority_question_is_not_an_accepted_restriction(self):
        c=self.c;self.accept();job=c.ingest('Should helper stop seeing pickup details?')
        self.apply(job,lambda p:[self.restriction(p)])
        self.assertEqual(c.updater.inspect()['restriction']['confirm-limit']['status'],'unresolved')
        self.assertFalse(c.selected())

    def test_authorized_confirmation_can_supersede_a_pending_hold(self):
        c=self.c;self.accept();self.hold()
        job=c.ingest('Yes, keep my diagnosis private; pickup details are still allowed.')
        self.apply(job,lambda p:[self.restriction(p,id='confirmed-limit',supersedes='confirm-limit')])
        state=c.updater.inspect()['restriction']
        self.assertEqual(state['confirm-limit']['superseded_by'],'confirmed-limit')
        self.assertNotIn('status',state['confirmed-limit'])
        self.assertEqual(state['confirmed-limit']['issuer'],'owner')
        self.assertTrue(c.selected())

    def test_resolving_hold_keeps_independent_confirmed_limits(self):
        c=self.c;self.accept();job=c.ingest('Stop sharing pickup information with helper.')
        self.apply(job,lambda p:[self.restriction(p,id='accepted-limit',information_scope='pickup',blocked_assignments=['access'])])
        self.hold();job=c.ingest('The confirmation adds no new restriction. Keep my earlier limit.')
        self.apply(job,lambda p:[self.op('resolve_restriction',p,restriction_id='confirm-limit',issuer='owner')])
        self.assertEqual(c.updater.inspect()['restriction']['confirm-limit']['status'],'resolved')
        self.assertNotIn('superseded_by',c.updater.inspect()['restriction']['accepted-limit'])
        self.assertFalse(c.selected())

    def test_old_authority_statement_cannot_resolve_a_later_hold(self):
        c=self.c;old=self.accept();self.hold()
        job=c.ingest('Please lift the hold.',actor='outsider')
        def ops(p):
            receipt=next(s['id'] for s in p['sources'] if s['source']['event_id']=='2')
            return [self.op('resolve_restriction',p,restriction_id='confirm-limit',issuer='owner',authority_source=receipt)]
        with self.assertRaisesRegex(PermissionRepairExhausted,'later authenticated'):
            self.apply(job,ops)
        self.assertFalse(c.selected())

    def test_deleting_resolution_evidence_reinstates_hold(self):
        c=self.c;self.accept();self.hold()
        job=c.ingest('The confirmation adds no new limit. Keep pickup permission.')
        self.apply(job,lambda p:[self.op('resolve_restriction',p,restriction_id='confirm-limit',issuer='owner')])
        self.assertTrue(c.selected())
        c.core.delete(c.host,job['source_id'])
        self.apply({'job_id':c.updater.pending_jobs()[0]})
        self.assertEqual(c.updater.inspect()['restriction']['confirm-limit']['status'],'unresolved')
        self.assertFalse(c.selected())


if __name__ == '__main__':unittest.main()
