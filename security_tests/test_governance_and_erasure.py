"""Authority, erasure, and cache invalidation contracts in a nonmedical domain."""
import json
import sqlite3
import time
from dataclasses import replace
import unittest
from unittest.mock import patch

from security_tests import test_permission_inference as fixtures
from agentic_memory.privacy import AccessDenied, CapabilityCandidate, PolicyGovernor, AuthorizationLearner
from agentic_memory.privacy.permissions import PermissionProposalRejected, PermissionUpdater


class GovernanceAndErasure(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PermissionInference()
        self.f.setUp()

    def tearDown(self):
        self.f.tearDown()

    def erase(self, job, anchor, quote='9 AM', **extra):
        sid=job['source_id']
        return fixtures.operation('erase_information',self.f.evidence(sid,'issuer','subject','reason'),
            issuer='owner',subject='owner',selectors=[{'source_id':anchor,'quote':quote}],
            execution_evidence=[sid],reason='Owner requests erasure of this exact pickup value.',
            self_check={'authority_supported':'Owner controls this information.',
                        'scope_supported':'Only the cited pickup value.',
                        'effective_now':'The owner requests immediate execution.'},**extra)

    def test_erasure_removes_fields_descendants_and_blocks_future_republication(self):
        _,source=self.f.establish()
        note=self.f.selected()[0]['id']
        job=self.f.ingest('Erase the pickup time now.')
        self.f.apply(job,[self.erase(job,source['source_id'])])
        self.assertEqual(self.f.selected(),[])
        self.assertEqual(self.f.core.db.execute('SELECT state FROM resources WHERE id=?',(note,)).fetchone()[0],'deleted')
        self.assertGreater(self.f.core.db.execute('SELECT COUNT(*) FROM tombstones').fetchone()[0],0)
        repeated=self.f.ingest('Pickup is at 9 AM; the old time is mentioned in a confirmation.')
        self.f.apply(repeated,[self.f.binding(repeated['source_id'])])
        self.assertEqual(self.f.selected(),[])
        self.assertEqual(self.f.updater.inspect()['assignment']['access']['bindings'],[])
        self.assertEqual(self.f.core.db.execute('SELECT COUNT(*) FROM source_erasures').fetchone()[0],1)

    def test_erasure_normalizes_identifier_punctuation_and_preserves_other_fields(self):
        self.f.establish()
        source=self.f.ingest('Use 415-555-0174 for pickup contact.')
        self.f.apply(source,[self.f.binding(source['source_id'],spans=[[0,len('Use 415-555-0174 for pickup contact.')]])])
        job=self.f.ingest('Delete that contact number now.')
        self.f.apply(job,[self.erase(job,source['source_id'],'415-555-0174')])
        self.assertTrue(self.f.selected())  # Original pickup instruction survives.
        repeated=self.f.ingest('Use 4155550174 for pickup contact.')
        self.f.apply(repeated,[self.f.binding(repeated['source_id'],spans=[[0,len('Use 4155550174 for pickup contact.')]])])
        self.assertTrue(all('415' not in r['text'] for r in self.f.selected()))

    def test_new_grant_cannot_resurrect_erased_values(self):
        _,source=self.f.establish()
        job=self.f.ingest('Erase the pickup time now.')
        self.f.apply(job,[self.erase(job,source['source_id'])])
        core=self.f.core
        core.create(self.f.host,'repeated-time','memory',{'text':'The former pickup was 9 AM.'},
            policy_id=self.f.updater.view_policy,compartment='conversation',
            attributes={'subject':'owner','information_scope':'pickup'})
        core.install_assignment(self.f.host,'fresh-grant',principals=['helper'],subject='owner',
            compartment='conversation',information_scope='pickup',task='ride',
            actions=['search','read','derive'],resource_ids=['repeated-time'],
            provenance={'authority':'owner','evidence':['fresh-receipt']})
        self.assertFalse(core.allowed(self.f.reader,'repeated-time'))

    def test_erasure_cannot_be_issued_by_unrelated_actor(self):
        _,source=self.f.establish()
        job=self.f.ingest('Erase the owner pickup.',actor='outsider')
        op=self.erase(job,source['source_id']);op['issuer']='outsider'
        with self.assertRaisesRegex(PermissionProposalRejected,'Issuer lacks configured authority'):
            self.f.apply(job,[op])
        self.assertEqual(self.f.core.db.execute('SELECT COUNT(*) FROM source_erasures').fetchone()[0],0)

    def test_compact_erasure_expands_execution_evidence_and_selector_aliases(self):
        self.f.establish()
        job=self.f.ingest('Erase the pickup time now.')
        self.f.model.compact_permission_input=True
        def handler(payload):
            sid=payload['new_source_id']
            anchor=next(s['id'] for s in payload['sources'] if 'Pickup is at 9 AM' in s['text'])
            op=self.erase(job,anchor)
            op['evidence']={name:[sid] for name in ['issuer','subject','reason']}
            op['execution_evidence']=[sid]
            return {'revision':payload['revision'],'considered_restrictions':[], 'operations':[op]}
        self.f.model.handler=handler
        self.f.updater.run(job['job_id'])
        self.assertEqual(self.f.selected(),[])
        proposal=json.loads(self.f.core.db.execute('SELECT proposal FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0])
        self.assertEqual(proposal['operations'][0]['execution_evidence'],[job['source_id']])

    def test_future_or_old_execution_evidence_cannot_erase_early(self):
        _,source=self.f.establish()
        job=self.f.ingest('Erase it tomorrow.',timestamp='2026-10-26T12:00:00+00:00')
        op=self.erase(job,source['source_id'],effective_at='2026-10-27T12:00:00+00:00')
        op['evidence']['effective_at']=[job['source_id']]
        with self.assertRaisesRegex(PermissionProposalRejected,'Future erasure'):
            self.f.apply(job,[op])
        op=self.erase(job,source['source_id']);op['execution_evidence']=[source['source_id']]
        with self.assertRaisesRegex(PermissionProposalRejected,'current execution evidence'):
            self.f.apply(job,[op])

    def test_erasure_and_decision_publication_roll_back_together(self):
        _,source=self.f.establish();note=self.f.selected()[0]['id']
        job=self.f.ingest('Erase the pickup now.')
        with patch.object(PolicyGovernor,'record',side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):self.f.apply(job,[self.erase(job,source['source_id'])])
        self.assertEqual(self.f.core.db.execute('SELECT state FROM resources WHERE id=?',(note,)).fetchone()[0],'active')
        self.assertEqual(self.f.core.db.execute('SELECT COUNT(*) FROM source_erasures').fetchone()[0],0)
        self.assertEqual(self.f.core.db.execute('SELECT COUNT(*) FROM tombstones').fetchone()[0],0)

    def test_cache_invalidates_for_mutation_session_revocation_and_clock(self):
        self.f.establish();core=self.f.core;note=self.f.selected()[0]['id']
        with core.transaction():
            self.assertTrue(core.allowed(self.f.reader,note))
            aid=core.db.execute("SELECT id FROM assignments WHERE json_extract(body,'$.rule.principals[0]')='helper'").fetchone()[0]
            core.db.execute("UPDATE assignments SET state='revoked' WHERE id=?",(aid,))
            self.assertFalse(core.allowed(self.f.reader,note))
            core.db.execute("UPDATE assignments SET state='active' WHERE id=?",(aid,))
            self.assertTrue(core.allowed(self.f.reader,note))
            body=json.loads(core.db.execute('SELECT body FROM assignments WHERE id=?',(aid,)).fetchone()[0])
            body['rule']['until']='2026-10-26T13:00:00+00:00'
            core.db.execute('UPDATE assignments SET body=? WHERE id=?',(json.dumps(body),aid))
            self.assertTrue(core.allowed(self.f.reader,note))
            core.clock=lambda:'2026-10-26T14:00:00+00:00'
            self.assertFalse(core.allowed(self.f.reader,note))
            core.clock=lambda:'2026-10-26T12:00:00+00:00'
            self.assertTrue(core.allowed(self.f.reader,note))
            core.revoke_session(self.f.reader)
            self.assertFalse(core.allowed(self.f.reader,note))

    def test_search_reconstructs_ledger_once_per_unchanged_snapshot(self):
        self.f.establish();queries=[]
        self.f.core.db.set_trace_callback(queries.append)
        self.f.selected()
        self.f.core.db.set_trace_callback(None)
        self.assertEqual(sum('SELECT p.kind,p.id,p.body' in q for q in queries),1)

    def test_rollback_drops_cached_denial(self):
        self.f.establish();core=self.f.core;note=self.f.selected()[0]['id']
        with self.assertRaises(RuntimeError):
            with core.transaction():
                core.db.execute("UPDATE assignments SET state='revoked'")
                self.assertFalse(core.allowed(self.f.reader,note))
                raise RuntimeError('rollback')
        self.assertTrue(core.allowed(self.f.reader,note))

    def test_unmanaged_transactions_do_not_reuse_cache_after_external_revocation(self):
        self.f.establish();core=self.f.core;note=self.f.selected()[0]['id']
        core.db.execute('BEGIN')
        try:self.assertTrue(core.allowed(self.f.reader,note))
        finally:core.db.execute('COMMIT')
        with sqlite3.connect(str(core.privacy_path)) as other:
            other.execute("UPDATE assignments SET state='revoked'")
        core.db.execute('BEGIN')
        try:self.assertFalse(core.allowed(self.f.reader,note))
        finally:core.db.execute('COMMIT')

    def test_cached_allow_still_checks_delegation_namespace_attenuation(self):
        self.f.establish();core=self.f.core;note=self.f.selected()[0]['id']
        delegation={'actions':['read'],'compartments':['conversation'],'expires_at':time.time()+60}
        reader=core.issue_session('helper','limited',actions=['read'],compartments=['conversation'],
                                 delegations=[delegation],processing_routes=self.f.routes)
        with core.transaction():
            self.assertTrue(core.allowed(reader,note))
            delegation['compartments']=[]
            self.assertFalse(core.allowed(reader,note))

    def test_current_plan_retrieves_recent_condition_despite_lexical_distractors(self):
        core=self.f.core
        for i in range(12):
            rid='archived-'+str(i)
            core.create(self.f.host,rid,'memory',{'text':f'Current plan next step appointments Friday archival item {i}.'},
                policy_id=self.f.updater.view_policy,compartment='conversation',attributes={'sequence':i+1})
            core.index(self.f.host,rid)
        core.create(self.f.host,'condition','memory',{'text':'No additional measurements unless the symptoms change.'},
            policy_id=self.f.updater.view_policy,compartment='conversation',attributes={'sequence':20})
        core.index(self.f.host,'condition')
        result=core.search(self.f.host,'What is the current plan and next steps?',k=8)
        self.assertIn('condition',{row['id'] for row in result})

    def test_question_cannot_be_reclassified_as_a_fact_or_cropped_to_hide_question_mark(self):
        self.f.establish()
        job=self.f.ingest('Was the pickup at 7 AM?')
        op=self.f.binding(job['source_id'],spans=[[4,21]])
        with self.assertRaisesRegex(PermissionProposalRejected,'Questions cannot establish factual memory'):
            self.f.apply(job,[op])
        self.assertFalse(self.f.core.db.execute("SELECT 1 FROM resources WHERE kind='memory' AND payload LIKE '%7 AM%'").fetchone())

    def test_generic_capability_requires_host_and_matches_exact_attributes(self):
        f=self.f;core=f.core
        source=f.ingest('A repository workflow approves service logs for helper.')
        f.apply(source)
        attrs={'project_id':'project-7','information_class':'service-logs'}
        core.create(f.host,'logs','memory',{'text':'Incident logs.'},policy_id=f.updater.view_policy,
                    compartment='conversation',attributes=attrs)
        core.index(f.host,'logs')
        candidate=CapabilityCandidate('incident-access','helper',
            {'namespace':'conversation','attributes':attrs,'resource_ids':['logs']},
            ('read','search','derive'),'incident-42',(source['source_id'],),'host',{})
        governor=PolicyGovernor(core);receipt={'authority':'repository-workflow','basis':'verified_assignment'}
        with self.assertRaises(AccessDenied):
            governor.activate(f.reader,candidate,receipt=receipt,source_job='test')
        governor.activate(f.host,candidate,receipt=receipt,source_job='test')
        self.assertTrue(core.allowed(f.reader,'logs'))
        self.assertEqual(core.db.execute('SELECT COUNT(*) FROM capability_decisions').fetchone()[0],1)
        core.revoke_assignment(f.host,'incident-access')
        self.assertFalse(core.allowed(f.reader,'logs'))
        decisions=f.updater.learner.feedback('conversation')
        self.assertEqual([d['decision'] for d in decisions],['approved','revoked'])
        with self.assertRaises(AccessDenied):
            AuthorizationLearner(core,f.reader,f.model).feedback('conversation')
        invalid=CapabilityCandidate('too-wide','helper',
            {'namespace':'conversation','attributes':{'project_id':'another'},'resource_ids':['logs']},
            ('read',),'incident-42',(source['source_id'],),'host',{})
        with self.assertRaises(AccessDenied):
            governor.activate(f.host,invalid,receipt=receipt,source_job='test')
        self.assertEqual(core.db.execute('SELECT COUNT(*) FROM capability_decisions').fetchone()[0],2)

    def test_coverage_cannot_silently_omit_a_required_principal(self):
        self.f.model.require_permission_coverage=True
        job=self.f.ingest('Please arrange my pickup.')
        self.f.model.handler=lambda p:{'revision':p['revision'],'considered_restrictions':[],
                                      'operations':[],'coverage':[]}
        with self.assertRaisesRegex(PermissionProposalRejected,'every required principal'):
            self.f.updater.run(job['job_id'])

    def test_mediated_read_is_an_opt_in_adapter_convention(self):
        self.f.bootstrap=replace(self.f.bootstrap,read_pipeline_actions=('search','derive'))
        self.f.updater=PermissionUpdater(self.f.core,self.f.host,'conversation',self.f.bootstrap,self.f.model,version='mediated')
        job=self.f.ingest('Helper may read pickup instructions. Pickup is at 9 AM.')
        grant=self.f.assignment(job['source_id'],basis='explicit');grant['actions']=['read']
        start='Helper may read pickup instructions. '
        self.f.apply(job,[grant,self.f.binding(job['source_id'],spans=[[len(start),len(start)+17]])])
        self.assertEqual([r['text'] for r in self.f.selected()],['Pickup is at 9 AM'])
        self.assertEqual(set(self.f.updater.inspect()['assignment']['access']['actions']),{'read','search','derive'})
        self.assertFalse(self.f.core.allowed(self.f.reader,self.f.selected()[0]['id'],'export'))

    def test_literal_read_stays_read_only_without_mediated_convention(self):
        job=self.f.ingest('Pickup is at 9 AM. Helper may read it.')
        grant=self.f.assignment(job['source_id'],basis='explicit');grant['actions']=['read']
        self.f.apply(job,[grant,self.f.binding(job['source_id'])])
        candidate=self.f.core.db.execute("SELECT id FROM resources WHERE kind='memory'").fetchone()[0]
        self.assertTrue(self.f.core.allowed(self.f.reader,candidate,'read'))
        self.assertFalse(self.f.core.allowed(self.f.reader,candidate,'derive',processor='test:hash'))
        self.assertEqual(self.f.selected(),[])

    def test_unbound_candidate_cannot_claim_another_hosts_assignment_id(self):
        core=self.f.core
        core._claim_authority(self.f.host,'assignment','reserved')
        other=core.issue_session('other-host','app',actions=['manage_policy','ingest','read','derive','audit'],
            compartments=['conversation'],processing_routes=['test:hash'])
        core.install_policy(other,'other-policy',{'allows':[{'principals':['other-host'],'actions':['read','derive']}],
                                                'processors':['test:hash']})
        core.create(other,'other-evidence','observation',{'text':'Independent workflow.'},
                    policy_id='other-policy',compartment='conversation')
        candidate=CapabilityCandidate('reserved','helper',
            {'namespace':'conversation','attributes':{'project_id':'project-7'},'resource_ids':[]},
            ('read',),'review',('other-evidence',),'other-host',{})
        with self.assertRaises(AccessDenied):
            PolicyGovernor(core).record(other,candidate,state='approved_unbound',
                receipt={'authority':'other-host','basis':'own-workflow'},source_job='test')
        self.assertEqual(core.db.execute('SELECT COUNT(*) FROM capability_candidates').fetchone()[0],0)


if __name__=='__main__':unittest.main()
