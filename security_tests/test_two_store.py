"""Domain-free contracts for assignments, field authority and paired storage."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agentic_memory.privacy import PrivacyMemorySystem, AccessDenied
from agentic_memory.privacy.processors import HashEmbedder, ScriptedProcessor

ACTIONS=['read','search','derive','write','link','delete','export']


class TwoStoreContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'memory.sqlite'
        self.now=['2026-10-22T00:00:00Z'];self.embed=HashEmbedder()
        self.core=PrivacyMemorySystem(self.path,self.embed,clock=lambda:self.now[0])
        self.routes=[self.embed.processor_id,'test:completion']
        self.admin=self.ctx('authority',ACTIONS+['ingest','manage_policy','audit'])
        self.worker=self.ctx('warehouse-worker');self.other=self.ctx('other-worker')
        self.body={'allows':[{'principals':['authority'],'actions':ACTIONS}], 'denies':[], 'processors':self.routes}
        self.core.install_policy(self.admin,'order',self.body)
        self.core.install_policy(self.admin,'shipping-fields',self.body)
        self.source=self.core.ingest(self.admin,'source','Dispatch order A at Dock 2. Private margin 41%.',
            policy_id='order',compartment='orders',attributes={'subject':'order:A'})
        self.view=self.core.ingest_authoritative_view(self.admin,'source','shipping',[(0,27)],policy_id='shipping-fields',
            attributes={'subject':'order:A','information_scope':'shipping'})

    def tearDown(self):self.core.close();self.temp.cleanup()

    def ctx(self,pid,actions=ACTIONS,**kw):
        return self.core.issue_session(pid,'agent',actions=actions,compartments=['orders'],
                                       processing_routes=self.routes,**kw)

    def assign(self,aid='dispatch',**kw):
        params=dict(principals=['warehouse-worker'],subject='order:A',compartment='orders',information_scope='shipping',
            task='dispatch',actions=['search','read','derive','link'],
            provenance={'authority':'verified-order-ACL-v1','evidence':[{'source_id':'source'}]})
        params.update(kw);self.core.install_assignment(self.admin,aid,**params)

    def test_two_physical_stores_with_independent_vectors(self):
        for path,required,absent in [(self.path,{'resources','representations'},{'policies','access_edges'}),
                                    (self.core.privacy_path,{'policies','privacy_vectors','access_edges'},{'resources','representations'})]:
            db=sqlite3.connect(path)
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue(required <= tables);self.assertFalse(tables & absent);db.close()
        self.assertEqual(self.core.db.execute('PRAGMA journal_mode').fetchone()[0],'delete')

    def test_nonmedical_assignment_and_source_lifecycle(self):
        self.assertFalse(self.core.allowed(self.worker,self.view));self.assign()
        self.assertIn('Dock 2',self.core.read(self.worker,self.view)['payload']['text'])
        self.assertFalse(self.core.allowed(self.worker,self.source))
        self.assertFalse(self.core.allowed(self.other,self.view))
        self.assertNotIn('margin',' '.join(r['text'] for r in self.core.search(self.worker,'dispatch')))
        receipt=json.loads(self.core.db.execute("SELECT body FROM assignments WHERE id='dispatch'").fetchone()[0])
        self.assertEqual(receipt['provenance']['evidence'][0]['source_id'],'source')
        self.core.delete(self.admin,'source')
        self.assertFalse(self.core.allowed(self.worker,self.view))
        self.assertEqual(self.core.rebuild_index(self.admin,['orders']),0)
        with self.assertRaises(ValueError):
            self.core.ingest(self.admin,'source','Dispatch order A at Dock 2. Private margin 41%.',policy_id='order',compartment='orders')

    def test_role_authorship_and_semantic_links_are_not_assignments(self):
        self.core.link(self.admin,'semantic',self.source,self.view,'assigned_to',policy_id='order',compartment='orders')
        self.assertFalse(self.core.allowed(self.worker,self.view))
        self.assertEqual(self.core.search(self.worker,'Dispatch'),[])

    def test_same_name_other_subject_and_scope_isolation(self):
        self.assign()
        for subject,scope in [('order:B','shipping'),('order:A','finance')]:
            note=self.core.ingest(self.admin,subject+scope,'A same-name customer secret',policy_id='shipping-fields',compartment='orders',
                attributes={'subject':subject,'information_scope':scope})
            self.assertFalse(self.core.allowed(self.worker,note))

    def test_collection_selector_is_enforced_without_an_access_hierarchy(self):
        self.assign(collection='dock:A')
        for collection in ('dock:A','dock:B'):
            note=self.core.ingest(self.admin,collection,'Dispatch item',policy_id='shipping-fields',compartment='orders',
                attributes={'subject':'order:A','information_scope':'shipping','collection':collection})
            self.assertEqual(self.core.allowed(self.worker,note),collection=='dock:A')

    def test_expiry_revocation_purpose_and_group_changes(self):
        self.assign(conditions={'until':'2026-10-23T00:00:00Z','purpose':'dispatch'})
        ctx=self.ctx('warehouse-worker',purpose='dispatch')
        self.assertFalse(self.core.allowed(self.worker,self.view));self.assertTrue(self.core.allowed(ctx,self.view))
        self.now[0]='2026-10-23T00:00:00Z';self.assertFalse(self.core.allowed(ctx,self.view))
        self.now[0]='2026-10-22T00:00:00Z'
        self.core.revoke_assignment(self.admin,'dispatch');self.assertFalse(self.core.allowed(ctx,self.view))
        self.assign('group',principals=[],groups=['dispatchers']);self.core.set_member(self.admin,'dispatchers','warehouse-worker')
        self.assertTrue(self.core.allowed(self.worker,self.view))
        self.core.set_member(self.admin,'dispatchers','warehouse-worker',False)
        self.assertFalse(self.core.allowed(self.worker,self.view))

    def test_deny_precedence_even_when_policy_vector_is_missing(self):
        self.assign()
        body={**self.body,'denies':[{'principals':['warehouse-worker'],'actions':['read','derive','search']}]}
        self.core.install_policy(self.admin,'shipping-fields',body)
        self.core.db.execute('DELETE FROM privacy_vectors')
        self.assertFalse(self.core.allowed(self.worker,self.view))
        self.assertEqual(self.core.diagnose(self.admin,self.worker,self.view),'explicit_denial')

    def test_delegation_cannot_widen_and_parent_revocation_is_live(self):
        self.assign('parent',principals=['authority'])
        self.assign('child',delegation='parent')
        self.assertTrue(self.core.allowed(self.worker,self.view))
        with self.assertRaises(AccessDenied):self.assign('bad',delegation='parent',actions=['delete'])
        self.core.revoke_assignment(self.admin,'parent');self.assertFalse(self.core.allowed(self.worker,self.view))

    def test_generated_artifact_inherits_every_input_including_uncited(self):
        self.assign()
        out=self.core.create(self.admin,'derived','enrichment',{'text':'Dispatch summary'},policy_id='shipping-fields',compartment='orders',
            attributes={'subject':'order:A','information_scope':'shipping'},dependencies=[self.view],generated=True)
        self.assertTrue(self.core.allowed(self.worker,out))
        mixed=self.core.create(self.admin,'mixed','enrichment',{'text':'Dispatch summary'},policy_id='shipping-fields',compartment='orders',
            attributes={'subject':'order:A','information_scope':'shipping'},dependencies=[self.view,self.source],generated=True)
        self.assertFalse(self.core.allowed(self.worker,mixed))
        self.core.revoke_assignment(self.admin,'dispatch');self.assertFalse(self.core.allowed(self.worker,out))

    def test_only_source_authority_can_approve_different_view_audience(self):
        stranger=self.ctx('another-authority',ACTIONS+['manage_policy','ingest'])
        self.core.install_policy(stranger,'stranger',{'allows':[{'principals':['another-authority'],'actions':ACTIONS}],'processors':self.routes})
        self.core.install_policy(self.admin,'order',{**self.body,'allows':self.body['allows']+[{'principals':['another-authority'],'actions':['read']}]})
        with self.assertRaises(AccessDenied):
            self.core.ingest_authoritative_view(stranger,'source','stolen',[(0,27)],policy_id='stranger')

    def test_publication_failure_rolls_back_both_stores(self):
        def fail(rid):raise RuntimeError('simulated cross-store failure')
        self.core._publication_hook=fail
        with self.assertRaises(RuntimeError):
            self.core.ingest(self.admin,'unpublished','never searchable',policy_id='order',compartment='orders')
        self.assertFalse(self.core.db.execute("SELECT 1 FROM resources WHERE id='unpublished'").fetchone())
        self.assertFalse(self.core.db.execute("SELECT 1 FROM access_edges WHERE memory_id='unpublished'").fetchone())

    def test_process_crash_recovers_uncommitted_pair(self):
        self.core.close()
        code='''import os,sys
from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.processors import HashEmbedder
c=PrivacyMemorySystem(sys.argv[1],HashEmbedder())
c.db.execute('BEGIN IMMEDIATE')
c.db.execute("INSERT INTO policies VALUES ('uncommitted',1,'{}')")
c.db.execute("INSERT INTO resources SELECT 'uncommitted',kind,compartment,policy_id,payload,attributes,dependencies,policy_ceiling,state,created FROM resources LIMIT 1")
os._exit(17)
'''
        result=subprocess.run([sys.executable,'-c',code,str(self.path)])
        self.assertEqual(result.returncode,17)
        self.core=PrivacyMemorySystem(self.path,self.embed)
        for table in ('policies','resources'):
            self.assertFalse(self.core.db.execute(f"SELECT 1 FROM {table} WHERE id='uncommitted'").fetchone())

    def test_missing_or_mismatched_store_fails_closed(self):
        privacy=self.core.privacy_path;self.core.close();backup=privacy.with_suffix('.saved');privacy.rename(backup)
        try:
            with self.assertRaisesRegex(ValueError,'Missing privacy store'):PrivacyMemorySystem(self.path,self.embed)
        finally:backup.rename(privacy);self.core=PrivacyMemorySystem(self.path,self.embed)

    def test_mismatched_pair_fails_closed(self):
        another=PrivacyMemorySystem(Path(self.temp.name)/'other.sqlite',self.embed)
        wrong=another.privacy_path;another.close();self.core.close()
        try:
            with self.assertRaisesRegex(ValueError,'Mismatched database pair'):
                PrivacyMemorySystem(self.path,self.embed,privacy_path=wrong)
        finally:self.core=PrivacyMemorySystem(self.path,self.embed)

    def test_assignment_revocation_during_model_processing_blocks_release(self):
        self.assign();outer=self
        class Revoking(ScriptedProcessor):
            def complete(self,system,payload):
                outer.core.revoke_assignment(outer.admin,'dispatch')
                return {'answer':'stale'}
        processor=Revoking();self.core.register_processor(self.admin,processor)
        with self.assertRaises(AccessDenied):
            self.core.process(self.worker,processor,[self.view],'test',{'text':'Dispatch order A'})
        with self.assertRaises(AccessDenied):
            self.core.release(self.worker,[self.view],{'answer':'stale'})

    def test_same_dimension_embedding_change_requires_rebuild_both_indexes(self):
        self.assign();self.embed.space_id='same-dimension-different-model'
        self.assertEqual(self.core.search(self.worker,'dispatch'),[])
        self.assertEqual(self.core.discover_policies(self.admin,'shipping'),[])
        self.core.rebuild_index(self.admin,['orders'])
        self.assertTrue(self.core.search(self.worker,'dispatch'))
        self.assertTrue(self.core.discover_policies(self.admin,'shipping'))

    def test_revision_and_multipart_retrieval(self):
        old=self.core.ingest(self.admin,'old','Current dispatch Tuesday at Dock 1',policy_id='order',compartment='orders')
        new=self.core.ingest(self.admin,'new','Current dispatch Friday at Dock 2',policy_id='order',compartment='orders')
        med=self.core.ingest(self.admin,'route','Routing contact is Sam at 1234',policy_id='order',compartment='orders')
        self.core.supersede(self.admin,'old','new')
        ids={r['id'] for r in self.core.search(self.admin,'What is the current dispatch and routing contact?',k=4)}
        self.assertNotIn(old,ids);self.assertIn(new,ids);self.assertIn(med,ids)
        self.assertTrue(self.core.allowed(self.admin,old))
        with self.assertRaises(ValueError):self.core.supersede(self.admin,'new','old')

if __name__=='__main__':unittest.main()
