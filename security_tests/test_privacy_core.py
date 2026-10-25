"""Independent, domain-free security contracts; no model service is required."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from agentic_memory.privacy import PrivacyMemorySystem,AccessDenied
from agentic_memory.privacy.processors import HashEmbedder,ScriptedProcessor

ACTIONS=['read','search','derive','write','link','delete','export']


class PrivacyContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'memory.sqlite'
        self.embed=HashEmbedder()
        self.core=PrivacyMemorySystem(self.path,self.embed,clock=lambda:'2026-10-20T00:00:00Z')
        self.routes=[self.embed.processor_id,'test:completion']
        self.admin=self.context('issuer',ACTIONS+['manage_policy','ingest','audit'])
        for policy,principals in [('public',['issuer','p0','p1','p2']),('private',['issuer','p1']),('other',['issuer','p2'])]:
            self.policy(policy,principals)
        self.p0=self.context('p0')
        self.p1=self.context('p1')
        self.p2=self.context('p2')
        self.public=self.note('pub','Public API information.','public','c0')
        self.private=self.note('sec','Unreleased secret meeting 451.','private','c1')

    def tearDown(self):
        self.core.close();self.temp.cleanup()

    def context(self,principal,actions=ACTIONS,**kw):
        return self.core.issue_session(principal,'agent',actions=actions,compartments=['c0','c1','c2'],
                                       processing_routes=self.routes,**kw)

    def policy(self,pid,principals,**extra):
        body={'allows':[{'principals':principals,'actions':ACTIONS}],
              'denies':[],'processors':self.routes}
        body.update(extra);self.core.install_policy(self.admin,pid,body)

    def note(self,oid,text,pid,compartment='c1'):
        return self.core.ingest(self.admin,oid,text,policy_id=pid,compartment=compartment)

    def model(self,response=None):
        processor=ScriptedProcessor(response)
        self.core.register_processor(self.admin,processor)
        return processor

    def proposal(self,inputs,response,job='job'):
        model=self.model(response)
        result=self.core.process(self.admin,model,inputs,'test',{'ids':inputs})
        self.core.apply_evolution(self.admin,job,inputs,result,model.processor_id)

    def test_default_deny_and_identical_unavailable(self):
        for rid in [self.private,'does-not-exist']:
            with self.assertRaisesRegex(AccessDenied,'Unavailable'):self.core.read(self.p0,rid)
        with self.assertRaises(AccessDenied):self.core.read(None,self.public)

    def test_authentication_cannot_be_forged(self):
        with self.assertRaises(AccessDenied):self.core.read(replace(self.p0,principal_id='p1'),self.private)

    def test_revoked_session(self):
        self.core.revoke_session(self.p1)
        with self.assertRaises(AccessDenied):self.core.read(self.p1,self.private)

    def test_action_and_agent_scope_intersection(self):
        narrow=self.context('p1',['read'])
        self.core.read(narrow,self.private)
        with self.assertRaises(AccessDenied):self.core.search(narrow,'secret')
        narrow=self.core.issue_session('p1','agent',actions=ACTIONS,compartments=['c0'],processing_routes=self.routes)
        with self.assertRaises(AccessDenied):self.core.read(narrow,self.private)

    def test_delegation_attenuation(self):
        delegated=self.context('p1',delegations=({'actions':['read'],'compartments':['c0'],'expires_at':10**12},))
        self.core.read(delegated,self.public)
        with self.assertRaises(AccessDenied):self.core.read(delegated,self.private)

    def test_deny_overrides_allow_and_no_implicit_owner(self):
        self.policy('private',['issuer','p1'],denies=[{'principals':['p1'],'actions':['read']}])
        with self.assertRaises(AccessDenied):self.core.read(self.p1,self.private)
        noowner=self.core.create(self.admin,'owner-note','memory',{'text':'x'},policy_id='other',compartment='c2',attributes={'owner_id':'p1'})
        with self.assertRaises(AccessDenied):self.core.read(self.p1,noowner)

    def test_groups_overlap_and_revocation_live(self):
        self.core.install_policy(self.admin,'group-policy',{'allows':[{'groups':['g1','g2'],'actions':ACTIONS},
            {'principals':['issuer'],'actions':ACTIONS}],'processors':self.routes})
        note=self.note('group-note','overlap','group-policy')
        self.core.set_member(self.admin,'g1','p1');self.core.set_member(self.admin,'g2','p1')
        self.core.set_member(self.admin,'g1','p1',False)
        self.core.read(self.p1,note)
        self.core.set_member(self.admin,'g2','p1',False)
        with self.assertRaises(AccessDenied):self.core.read(self.p1,note)

    def test_expiry_and_purpose(self):
        self.core.install_policy(self.admin,'timed',{'allows':[
            {'principals':['issuer'],'actions':ACTIONS},
            {'principals':['p1'],'actions':ACTIONS,'until':'2026-10-19T23:59:59Z'},
            {'principals':['p2'],'actions':ACTIONS,'purpose':'purpose-x'}],'processors':self.routes})
        note=self.note('timed-note','time','timed')
        for ctx in [self.p1,self.p2]:
            with self.assertRaises(AccessDenied):self.core.read(ctx,note)
        self.core.read(self.context('p2',purpose='purpose-x'),note)

    def test_scope_uses_authoritative_fields(self):
        self.core.install_policy(self.admin,'scoped',{'allows':[{'principals':['issuer'],'actions':ACTIONS},
            {'principals':['p0'],'actions':['read'],'scope':{'slice':'allowed'}}],'processors':self.routes})
        rid=self.core.create(self.admin,'scope-note','memory',{'text':'secret','semantic':{'slice':'allowed'}},
                             policy_id='scoped',compartment='c0',attributes={'slice':'restricted'})
        with self.assertRaises(AccessDenied):self.core.read(self.p0,rid)

    def test_malformed_policy_not_wildcard(self):
        with self.assertRaises(ValueError):self.core.install_policy(self.admin,'bad',{'allows':[{'actions':['read']}]})
        with self.assertRaises(AccessDenied):self.core.install_policy(self.p0,'bad',{'allows':[]})
        for rule in [{'principals':'p1','actions':['read']},
                     {'principals':['p1'],'actions':['read'],'resource_ids':[]},
                     {'principals':['p1'],'actions':['read'],'until':''}]:
            with self.assertRaises(ValueError):self.core.install_policy(self.admin,'bad',{'allows':[rule]})

    def test_issuer_cannot_modify_another_authority_scope(self):
        other=self.context('other-issuer',ACTIONS+['manage_policy'])
        with self.assertRaises(AccessDenied):self.core.install_policy(other,'private',{'allows':[]})
        self.core.install_policy(self.admin,'group-owner',{'allows':[{'groups':['guarded-group'],'actions':['read']}]})
        with self.assertRaises(AccessDenied):self.core.set_member(other,'guarded-group','p0')

    def test_search_pre_authorizes_no_hidden_displacement(self):
        before=self.core.search(self.p0,'Public API information',k=1)
        for i in range(5):self.note(f'hidden{i}','Public API information.','private')
        after=self.core.search(self.p0,'Public API information',k=1)
        self.assertEqual(before,after)
        self.assertEqual(after[0]['id'],self.public)

    def test_stale_index_policy_not_authority(self):
        self.policy('private',['issuer'])
        self.assertNotIn(self.private,[r['id'] for r in self.core.search(self.p1,'Unreleased secret',k=100)])

    def test_read_returns_immutable_copy(self):
        result=self.core.read(self.p1,self.private);result['payload']['text']='changed'
        self.assertIn('secret',self.core.read(self.p1,self.private)['payload']['text'])

    def test_private_directed_edge_not_public_backlink(self):
        before=self.core.read(self.p0,self.public)
        self.core.link(self.admin,'edge',self.private,self.public,'references',policy_id='private',compartment='c1')
        self.assertEqual(self.core.edges(self.p0,self.public,reverse=True),[])
        self.assertEqual(before,self.core.read(self.p0,self.public))
        self.assertEqual(len(self.core.edges(self.p1,self.private)),1)

    def test_semantic_link_does_not_grant(self):
        self.core.link(self.admin,'edge',self.public,self.private,'member_of',policy_id='public',compartment='c0')
        self.assertEqual(self.core.edges(self.p0,self.public),[])
        self.assertEqual(len(self.core.traverse(self.p0,self.public)),1)

    def test_graph_expansion_preserves_edge_dependencies(self):
        self.core.link(self.admin,'edge',self.public,self.private,'references',policy_id='private',compartment='c1')
        packet=self.core.retrieve(self.p1,'Public API information',k=1)
        self.assertIn('edge',packet['input_ids'])
        self.assertIn(self.private,[r['id'] for r in packet['evidence']])
        self.assertNotIn('edge',self.core.retrieve(self.p0,'Public API information',k=1)['input_ids'])

    def test_evolution_overlay_inherits_all_inputs(self):
        before=self.core.read(self.p0,self.public)
        self.proposal([self.public,self.private],{'updates':[{'id':self.public,'context':'Public API information.','tags':['API']}],'edges':[]})
        overlay='enrichment:job:0'
        with self.assertRaises(AccessDenied):self.core.read(self.p0,overlay)
        self.core.read(self.p1,overlay)
        self.assertEqual(before,self.core.read(self.p0,self.public))
        self.assertNotIn(overlay,[r['id'] for r in self.core.search(self.p0,'private clue')])

    def test_uncited_input_revocation_blocks_overlay_and_release(self):
        self.proposal([self.public,self.private],{'updates':[{'id':self.public,'context':'Public API information.'}],'edges':[]})
        self.policy('private',['issuer'])
        with self.assertRaises(AccessDenied):self.core.read(self.p1,'enrichment:job:0')
        with self.assertRaises(AccessDenied):self.core.release(self.p1,[self.public,self.private],{'used':[self.public]})

    def test_model_permission_and_target_injection_rejected_atomically(self):
        for response in [{'updates':[],'edges':[],'policy':'public'},
                         {'updates':[{'id':'unknown','context':'leak'}],'edges':[]},
                         {'updates':[{'id':self.public,'context':'ok'},{'id':'unknown','context':'leak'}],'edges':[]}]:
            with self.assertRaises(AccessDenied):self.proposal([self.public,self.private],response)
        self.assertFalse(self.core.db.execute('SELECT 1 FROM resources WHERE id=\'enrichment:job:0\'').fetchone())

    def test_caller_cannot_drop_processing_dependencies(self):
        model=self.model({'updates':[],'edges':[]})
        response=self.core.process(self.admin,model,[self.public,self.private],'x',{})
        with self.assertRaises(AccessDenied):
            self.core.apply_evolution(self.admin,'job',[self.public],response,model.processor_id)

    def test_unapproved_model_does_not_receive_input(self):
        model=ScriptedProcessor()
        with self.assertRaises(AccessDenied):self.core.process(self.p1,model,[self.private],'x',{})
        self.assertEqual(model.calls,[])

    def test_revocation_during_model_call_is_rechecked(self):
        outer=self
        class Revoking(ScriptedProcessor):
            def complete(self,system,payload):
                outer.policy('private',['issuer'])
                return {'answer':'stale'}
        model=Revoking();self.core.register_processor(self.admin,model)
        with self.assertRaises(AccessDenied):self.core.process(self.p1,model,[self.private],'x',{})

    def test_delete_fans_out_and_cannot_resurrect(self):
        self.proposal([self.private,self.public],{'updates':[{'id':self.public,'context':'Public API information.'}],'edges':[]})
        self.core.delete(self.admin,'sec')
        for rid in [self.private,'enrichment:job:0']:
            with self.assertRaises(AccessDenied):self.core.read(self.p1,rid)
            self.assertFalse(self.core.db.execute('SELECT 1 FROM representations WHERE id=?',(rid,)).fetchone())
        with self.assertRaises(ValueError):self.note('sec','Unreleased secret meeting 451.','private')

    def test_idempotency_conflicts(self):
        self.core.record_event(self.admin,'e','hash1','complete',{})
        self.core.record_event(self.admin,'e','hash1','complete',{})
        with self.assertRaises(ValueError):self.core.record_event(self.admin,'e','hash2','complete',{})

    def test_export_checks_sender_and_recipient(self):
        with self.assertRaises(AccessDenied):self.core.export(self.p1,self.p0,[self.private])
        self.assertEqual(len(self.core.export(self.admin,self.p1,[self.private])),1)

    def test_restart_preserves_authority_and_tombstones(self):
        self.core.delete(self.admin,'sec');self.core.close()
        self.core=PrivacyMemorySystem(self.path,self.embed)
        ctx=self.context('p1')
        with self.assertRaises(AccessDenied):self.core.read(ctx,self.private)
        self.core.read(ctx,self.public)


if __name__=='__main__':unittest.main()
