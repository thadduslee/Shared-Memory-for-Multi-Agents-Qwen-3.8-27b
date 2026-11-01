"""Final-state access checks, durable repairs, and compact model responses."""
from copy import deepcopy
from itertools import permutations
import json
import unittest

from security_tests import test_permission_inference as fixtures
from agentic_memory.privacy.permissions import (
    PermissionUpdater, PermissionProposalRejected, PermissionRepairExhausted,
    compact_input, proposal_schema,
)
from agentic_memory.privacy.processors import ProcessorFailure


class PerTurnDeltaTests(unittest.TestCase):
    def setUp(self):
        self.c=fixtures.PermissionInference();self.c.setUp()

    def tearDown(self):self.c.tearDown()

    def proposal(self, job, operations):
        return {'revision':self.c.core.db.execute('SELECT revision FROM permission_bootstrap').fetchone()[0],
                'considered_restrictions':list(self.c.updater.inspect()['restriction']),
                'operations':deepcopy(list(operations))}

    def validate(self, job, proposal):
        c=self.c
        sources={r['id']:json.loads(r['payload']) for r in c.core.db.execute(
            "SELECT id,payload FROM resources WHERE kind='observation' AND state='active'")}
        return c.updater._validate(proposal,c.updater.inspect(),sources,proposal['revision'],
                                   new_source_id=job['source_id'])

    def test_assignment_restriction_and_binding_order_has_same_final_result(self):
        c=self.c;job=c.ingest('Pickup is at 9 AM; do not share my diagnosis.')
        grant=c.assignment(job['source_id'],basis='explicit')
        restriction=c.restriction(job['source_id'],scope='diagnosis')
        binding=c.binding(job['source_id'])
        binding['restriction_decisions']=[{'restriction_id':'private','applies':False,
            'explanation':'The selected pickup time is outside the diagnosis restriction.',
            'evidence':[job['source_id']]}]
        outcomes=[]
        for ops in permutations([grant,restriction,binding]):
            with self.subTest(order=[op['op'] for op in ops]):
                outcomes.append(self.validate(job,self.proposal(job,ops)))
        self.assertTrue(all(outcome==outcomes[0] for outcome in outcomes))
        self.assertEqual(c.updater.inspect()['assignment'],{})
        self.assertEqual(c.updater.inspect()['restriction'],{})

    def test_missing_same_turn_assessment_rejected_in_every_order_with_precise_feedback(self):
        c=self.c;job=c.ingest('Pickup is at 9 AM; do not share my diagnosis.')
        ops=[c.assignment(job['source_id'],basis='explicit'),
             c.restriction(job['source_id'],scope='diagnosis'),c.binding(job['source_id'])]
        before=c.core.db.total_changes
        for order in permutations(ops):
            with self.assertRaises(PermissionProposalRejected) as rejected:
                self.validate(job,self.proposal(job,order))
            self.assertEqual(rejected.exception.details['missing_restriction_ids'],['private'])
            self.assertIn('restriction_decisions',rejected.exception.details['path'])
        self.assertEqual(c.core.db.total_changes,before)

    def test_same_turn_restriction_repair_publishes_atomically(self):
        c=self.c;job=c.ingest('Pickup is at 9 AM; do not share my diagnosis.')
        def respond(payload):
            binding=c.binding(job['source_id'])
            if 'correction' in payload:
                self.assertEqual(c.updater.inspect()['assignment'],{})
                ids=payload['correction']['validation']['details']['missing_restriction_ids']
                binding['restriction_decisions']=[{'restriction_id':rid,'applies':False,
                    'explanation':'Pickup does not disclose diagnosis.','evidence':[job['source_id']]}
                    for rid in ids]
            proposal=self.proposal(job,[binding,c.restriction(job['source_id'],scope='diagnosis'),
                                       c.assignment(job['source_id'],basis='explicit')])
            if 'correction' in payload:proposal['considered_restrictions']=['private']
            return proposal
        c.model.handler=respond;c.updater.update(job['job_id'])
        self.assertEqual(len(c.model.calls),2)
        self.assertEqual([r['text'] for r in c.selected()],['Pickup is at 9 AM'])

    def test_review_may_include_valid_new_restriction_without_losing_final_binding_checks(self):
        c=self.c;job=c.ingest('Pickup is at 9 AM; do not share my diagnosis.')
        binding=c.binding(job['source_id'])
        ops=[binding,c.restriction(job['source_id'],scope='diagnosis'),
             c.assignment(job['source_id'],basis='explicit')]
        proposal=self.proposal(job,ops);proposal['considered_restrictions']=['private']
        with self.assertRaisesRegex(PermissionProposalRejected,'Each binding'):
            self.validate(job,proposal)
        binding['restriction_decisions']=[{'restriction_id':'private','applies':False,
            'explanation':'Pickup is outside diagnosis.','evidence':[job['source_id']]}]
        for order in permutations(ops):
            proposal=self.proposal(job,order);proposal['considered_restrictions']=['private']
            self.validate(job,proposal)

    def test_review_still_requires_all_stored_restrictions(self):
        c=self.c;c.establish();job=c.ingest('Do not disclose diagnosis.')
        c.apply(job,[c.restriction(job['source_id'],scope='diagnosis')])
        new=c.ingest('An ordinary update.');proposal=self.proposal(new,[])
        proposal['considered_restrictions']=[]
        with self.assertRaises(PermissionProposalRejected) as rejected:self.validate(new,proposal)
        self.assertEqual(rejected.exception.details['missing_restriction_ids'],['private'])
        self.assertEqual(rejected.exception.details['path'],'considered_restrictions')

    def test_review_cannot_invent_ids_or_repeat_them(self):
        c=self.c;job=c.ingest('Pickup is at 9 AM.')
        for ids,message in [(['invented'],'unknown'),(['invented','invented'],'distinct')]:
            proposal=self.proposal(job,[c.assignment(job['source_id'],basis='explicit')])
            proposal['considered_restrictions']=ids
            with self.assertRaisesRegex(PermissionProposalRejected,message):self.validate(job,proposal)
        self.assertEqual(c.updater.inspect()['assignment'],{})

    def test_review_does_not_validate_an_unauthorized_new_restriction(self):
        c=self.c;job=c.ingest('Do not disclose diagnosis.',actor='helper')
        restriction=c.restriction(job['source_id'],scope='diagnosis');restriction['issuer']='helper'
        proposal=self.proposal(job,[restriction]);proposal['considered_restrictions']=['private']
        with self.assertRaisesRegex(PermissionProposalRejected,'Issuer lacks configured authority'):
            self.validate(job,proposal)
        self.assertEqual(c.updater.inspect()['restriction'],{})

    def test_binding_cannot_precede_a_revocation_to_escape_final_state(self):
        c=self.c;c.establish();job=c.ingest('Stop sharing. Pickup is at 9 AM.')
        revoke=fixtures.operation('revoke_assignment',c.evidence(job['source_id'],'assignment_id','issuer','reason'),
                                  assignment_id='access',issuer='owner',reason='Owner revoked access.')
        for ops in ([c.binding(job['source_id']),revoke],[revoke,c.binding(job['source_id'])]):
            with self.assertRaisesRegex(PermissionProposalRejected,'live/proposed'):
                self.validate(job,self.proposal(job,ops))

    def test_repair_budget_cannot_reset_after_updater_reconstruction(self):
        c=self.c;c.establish();job=c.ingest('Stop sharing.')
        c.model.handler=lambda p:self.proposal(job,[{'op':'issue_session'}])
        for _ in range(2):
            with self.assertRaises(PermissionRepairExhausted):c.updater.update(job['job_id'])
            c.updater=PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version=c.updater.version)
        self.assertEqual(c.core.db.execute('SELECT attempts FROM permission_jobs WHERE id=?',
                                          (job['job_id'],)).fetchone()[0],3)
        self.assertFalse(c.selected())

    def test_transport_failure_does_not_consume_semantic_repair_budget(self):
        c=self.c;job=c.ingest()
        def unavailable(payload):raise ProcessorFailure('temporary outage')
        c.model.handler=unavailable
        for _ in range(4):
            with self.assertRaises(ProcessorFailure):c.updater.update(job['job_id'])
        c.model.handler=lambda p:self.proposal(job,[])
        self.assertEqual(c.updater.update(job['job_id'])['status'],'complete')

    def test_compact_binding_keeps_exact_scope_and_evidence(self):
        c=self.c;job=c.ingest('Pickup is at 9 AM; confidential diagnosis.')
        c.model.compact_permission_input=True;c.model.require_permission_coverage=True
        def respond(payload):
            grant=c.assignment('s1',basis='explicit')
            original=c.binding('s1')
            binding={k:v for k,v in original.items() if k not in {'spans','self_check','explanation'}}
            binding['evidence'].pop('source_id')
            binding.update(quote='Pickup is at 9 AM',**{k:original['self_check'][k]
                for k in ('scope_supported','context_preserved','metadata_safe')})
            return {'revision':payload['revision'],'considered_restrictions':[],'operations':[grant,binding],
                    'coverage':{'reviewed':payload['coverage_required'],'evidence':['s1'],'unresolved':[]}}
        c.model.handler=respond;c.updater.update(job['job_id'])
        binding=c.updater.inspect()['assignment']['access']['bindings'][0]
        self.assertEqual(binding['evidence']['source_id'],[job['source_id']])
        self.assertEqual(binding['evidence']['authorization'],[job['source_id']])
        self.assertEqual([r['text'] for r in c.selected()],['Pickup is at 9 AM'])

    def test_compact_noop_still_requires_each_recipient_and_live_review_evidence(self):
        c=self.c;job=c.ingest();c.model.compact_permission_input=True;c.model.require_permission_coverage=True
        base={'operations':[],'considered_restrictions':[]}
        for coverage in ({'reviewed':[],'evidence':['s1'],'unresolved':[]},
                         {'reviewed':['owner'],'evidence':[],'unresolved':[]}):
            c.model.handler=lambda p:{**base,'revision':p['revision'],'coverage':coverage}
            with self.assertRaises(PermissionProposalRejected):c.updater.run(job['job_id'])
        c.model.handler=lambda p:{**base,'revision':p['revision'],
            'coverage':{'reviewed':p['coverage_required'],'evidence':['s1'],'unresolved':[]}}
        c.updater.run(job['job_id'])
        self.assertEqual(c.updater.inspect()['assignment'],{})

    def test_wire_schema_drops_only_duplicate_binding_bookkeeping(self):
        c=self.c;job=c.ingest();c.apply(job)
        payload=deepcopy(c.model.calls[-1]);payload['coverage_required']=['owner']
        wire,schema,_=compact_input(payload,proposal_schema(bootstrap=c.bootstrap,
            source_ids=[job['source_id']],require_coverage=True))
        binding=next(v for v in schema['properties']['operations']['items']['anyOf']
                     if v['properties']['op']['enum']==['bind_information'])
        self.assertNotIn('self_check',binding['properties'])
        self.assertNotIn('explanation',binding['properties'])
        for field in ('necessity','scope_supported','context_preserved','metadata_safe',
                      'restriction_decisions','evidence','quote'):
            self.assertIn(field,binding['required'])
        self.assertEqual(wire['contract_notes']['wire'],'compact-delta-v1')
        self.assertEqual(schema['properties']['coverage']['type'],'object')
