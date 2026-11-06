"""Optional enrichment survives restart without blocking source publication."""
import unittest

from security_tests import test_permission_inference as fixtures
from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.permissions import PermissionUpdater
from agentic_memory.privacy.processors import HashEmbedder, ProcessorFailure
from evaluation.gatemem_medical.evolution_queue import EvolutionQueue


class EvolutionQueueTests(unittest.TestCase):
    def setUp(self):
        self.c=fixtures.PermissionInference();self.c.setUp();self.c.establish()
        self.queue=EvolutionQueue(self.c.core,self.c.host,'conversation')

    def tearDown(self):self.c.tearDown()

    def fields(self,count=4):
        c=self.c
        for i in range(count-1):
            job=c.ingest(f'Pickup is at {i+1} PM; unrelated private detail.')
            c.apply(job,[c.binding(job['source_id'],spans=[[0,17]])])
        return [r['id'] for r in c.selected()]

    def organizer(self,payload):return {'updates':[],'edges':[]}

    def test_fields_are_readable_before_enrichment_and_four_fields_share_one_call(self):
        c=self.c;fields=self.fields();before=c.updater.inspect()
        self.queue.enqueue('source-job',fields,c.model)
        self.queue.enqueue('source-job',fields,c.model)
        self.assertEqual(c.core.db.execute('SELECT COUNT(*) FROM evolution_queue').fetchone()[0],4)
        self.assertEqual(len(c.selected()),4)
        calls=len(c.model.calls);c.model.handler=self.organizer
        self.assertEqual(self.queue.drain(c.model),1)
        self.assertEqual(len(c.model.calls),calls+1)
        self.assertEqual(c.updater.inspect(),before)
        self.assertEqual(self.queue.drain(c.model,flush=True),0)

    def test_partial_batch_waits_until_explicit_flush(self):
        c=self.c;self.queue.enqueue('job',self.fields(1),c.model);c.model.handler=self.organizer
        calls=len(c.model.calls)
        self.assertEqual(self.queue.drain(c.model),0)
        self.assertEqual(len(c.model.calls),calls)
        self.assertEqual(self.queue.drain(c.model,flush=True),1)

    def test_failed_optional_model_stays_queued_and_later_source_can_commit(self):
        c=self.c;self.queue.enqueue('job',self.fields(1),c.model)
        def offline(payload):raise ProcessorFailure('offline')
        c.model.handler=offline
        self.assertEqual(self.queue.drain(c.model,flush=True),0)
        self.assertEqual(c.core.db.execute('SELECT status,error FROM evolution_queue').fetchone()[:],
                         ('pending','ProcessorFailure'))
        job=c.ingest('A later ordinary source.');c.apply(job)
        self.assertFalse(c.updater.pending_jobs());self.assertTrue(c.selected())
        c.model.handler=self.organizer
        self.assertEqual(self.queue.drain(c.model,flush=True),1)

    def test_resume_reuses_committed_job_after_lost_response(self):
        c=self.c;self.queue.enqueue('job',self.fields(1),c.model);c.model.handler=self.organizer
        evolve=c.core.evolve
        def lost_response(*args,**kwargs):
            evolve(*args,**kwargs)
            raise ProcessorFailure('response lost after commit')
        c.core.evolve=lost_response
        self.assertEqual(self.queue.drain(c.model,flush=True),0)
        batch_id=c.core.db.execute('SELECT batch_id FROM evolution_queue').fetchone()[0]
        calls=len(c.model.calls);path=c.core.path;c.core.close()
        c.core=PrivacyMemorySystem(path,HashEmbedder(),clock=lambda:'2026-10-26T12:00:00+00:00')
        c.model.core=c.core
        c.host=c.core.issue_session('host','ingest',actions=['manage_policy','ingest','read','search',
            'derive','audit','write','delete','link'],compartments=['conversation'],processing_routes=c.routes)
        c.core.register_processor(c.host,c.model)
        c.updater=PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version=c.updater.version)
        self.queue=EvolutionQueue(c.core,c.host,'conversation')
        self.assertEqual(self.queue.drain(c.model,flush=True),1)
        self.assertEqual(len(c.model.calls),calls)
        self.assertEqual(c.core.db.execute('SELECT batch_id,status FROM evolution_queue').fetchone()[:],
                         (batch_id,'complete'))

    def test_revoked_or_deleted_fields_are_discarded_without_model_call(self):
        c=self.c;fields=self.fields(1);self.queue.enqueue('job',fields,c.model)
        assignment=c.core.db.execute('SELECT id FROM assignments').fetchone()[0]
        c.core.revoke_assignment(c.host,assignment)
        c.model.handler=self.organizer;calls=len(c.model.calls)
        self.assertEqual(self.queue.drain(c.model,flush=True),0)
        self.assertEqual(len(c.model.calls),calls)
        self.assertEqual(c.core.db.execute('SELECT status FROM evolution_queue').fetchone()[0],'stale')

    def test_pending_permission_update_prevents_queue_from_using_unresolved_state(self):
        c=self.c;self.queue.enqueue('job',self.fields(1),c.model);c.ingest('Stop sharing.')
        c.model.handler=self.organizer;calls=len(c.model.calls)
        self.assertEqual(self.queue.drain(c.model,flush=True),0)
        self.assertEqual(len(c.model.calls),calls)
        self.assertEqual(c.core.db.execute('SELECT status FROM evolution_queue').fetchone()[0],'pending')

    def test_deletion_during_model_call_discards_result(self):
        c=self.c;fields=self.fields(1);self.queue.enqueue('job',fields,c.model)
        def delete_input(payload):
            c.core.delete(c.host,fields[0])
            return {'updates':[],'edges':[]}
        c.model.handler=delete_input
        self.assertEqual(self.queue.drain(c.model,flush=True),0)
        self.assertEqual(c.core.db.execute('SELECT status FROM evolution_queue').fetchone()[0],'stale')
        self.assertEqual(c.core.db.execute("SELECT COUNT(*) FROM resources WHERE kind='enrichment'").fetchone()[0],0)
