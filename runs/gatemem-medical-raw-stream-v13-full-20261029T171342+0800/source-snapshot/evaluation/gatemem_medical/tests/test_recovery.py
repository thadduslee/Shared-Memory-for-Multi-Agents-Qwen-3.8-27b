"""Recovery waits for valid data and preserves immutable experiment lineage."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agentic_memory.privacy import AccessDenied
from agentic_memory.privacy.permissions import PermissionProposalRejected
from agentic_memory.privacy.processors import (
    ProcessorFailure,ModelContractError,ProcessorContractFailure,ProcessorConfigurationFailure,
)
from evaluation.gatemem_medical.recovery import RecoveryController,reconcile_manifest


class RecoveryTests(unittest.TestCase):
    def test_known_model_errors_wait_and_resume_without_skipping(self):
        with tempfile.TemporaryDirectory() as tmp:
            sleeps=[];calls=[]
            failures=[ProcessorFailure('temporary outage'),ProcessorFailure('offline'),
                      ModelContractError('bad schema')]
            def operation():
                calls.append(1)
                if failures:raise failures.pop(0)
                return 'validated'
            retry=RecoveryController(tmp,sleep=sleeps.append,progress=lambda *a,**k:None)
            self.assertEqual(retry(operation,'permission pending'),'validated')
            self.assertEqual(sleeps,[5,10,20]);self.assertEqual(len(calls),4)
            events=[json.loads(line) for line in (Path(tmp)/'recovery-events.jsonl').read_text().splitlines()]
            self.assertEqual([e['state'] for e in events],['retrying']*3+['running'])

    def test_unknown_bugs_denials_and_interrupts_are_never_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            retry=RecoveryController(tmp,sleep=lambda _:self.fail('unexpected sleep'))
            for error in (ValueError('configuration'),RuntimeError('bug'),AccessDenied('denied'),KeyboardInterrupt()):
                def fail():raise error
                with self.assertRaises(type(error)):retry(fail,'stage')

    def test_exhausted_permission_rejection_stops_without_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            def blocked():raise PermissionProposalRejected('unsupported operation')
            retry=RecoveryController(tmp,sleep=lambda _:self.fail('unexpected retry'),progress=lambda *a,**k:None)
            with self.assertRaises(PermissionProposalRejected):retry(blocked,'permission source')
            status=json.loads((Path(tmp)/'recovery.json').read_text())
            self.assertEqual(status['state'],'blocked');self.assertEqual(status['failures'],1)

    def test_contract_budget_survives_controller_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls=[]
            def invalid():calls.append(1);raise ModelContractError('invalid answer')
            for _ in range(2):
                retry=RecoveryController(tmp,sleep=lambda _:None,progress=lambda *a,**k:None)
                with self.assertRaises(ModelContractError):retry(invalid,'answer one')
            self.assertEqual(len(calls),3)
            self.assertEqual(json.loads((Path(tmp)/'recovery.json').read_text())['state'],'blocked')

    def test_transport_wait_remains_interruptible(self):
        with tempfile.TemporaryDirectory() as tmp:
            def unavailable():raise ProcessorFailure('offline')
            def interrupt(_):raise KeyboardInterrupt()
            retry=RecoveryController(tmp,sleep=interrupt,progress=lambda *a,**k:None)
            with self.assertRaises(KeyboardInterrupt):retry(unavailable,'permission source')
            self.assertEqual(json.loads((Path(tmp)/'recovery.json').read_text())['state'],'retrying')

    def test_invalid_processor_output_is_bounded_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls=[]
            def invalid():calls.append(1);raise ProcessorContractFailure('truncated JSON')
            for _ in range(2):
                retry=RecoveryController(tmp,sleep=lambda _:None,progress=lambda *a,**k:None)
                with self.assertRaises(ModelContractError):retry(invalid,'permission pending')
            self.assertEqual(len(calls),3)

    def test_permanent_http_error_stops_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            def invalid():raise ProcessorConfigurationFailure('HTTP 400: invalid schema')
            retry=RecoveryController(tmp,sleep=lambda _:self.fail('unexpected retry'),progress=lambda *a,**k:None)
            with self.assertRaises(ProcessorConfigurationFailure):retry(invalid,'permission pending')
            self.assertEqual(json.loads((Path(tmp)/'recovery.json').read_text())['state'],'blocked')


class ManifestRevisions(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.executescript('CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT); '
            'CREATE TABLE official(id TEXT); CREATE TABLE boundaries(state TEXT); '
            "INSERT INTO official VALUES ('saved'); INSERT INTO boundaries VALUES ('complete');")
        self.original={'code_digest':'old','dataset_digest':'data','models':{'memory':'model'},'permission_version':'8'}
        reconcile_manifest(self.db,self.original,resume=False)

    def tearDown(self):self.db.close()

    def test_explicit_revision_keeps_original_and_supports_later_plain_resume(self):
        current={**self.original,'code_digest':'fixed'}
        original,revisions=reconcile_manifest(self.db,current,resume=True,previous_code_digest='old')
        self.assertEqual(original,self.original)
        self.assertEqual(revisions[0]['preserved_predictions'],1)
        self.assertEqual(reconcile_manifest(self.db,current,resume=True),(original,revisions))
        self.assertEqual(json.loads(self.db.execute("SELECT value FROM state WHERE key='manifest'").fetchone()[0]),self.original)
        self.assertEqual(self.db.execute('SELECT * FROM official').fetchall(),[('saved',)])

    def test_code_changes_require_exact_active_digest_and_cannot_bypass_configuration(self):
        current={**self.original,'code_digest':'fixed'}
        for digest in (None,'wrong'):
            with self.assertRaises(ValueError):
                reconcile_manifest(self.db,current,resume=True,previous_code_digest=digest)
        for field,value in [('dataset_digest','changed'),('models',{'memory':'other'}),('permission_version','9')]:
            altered=deepcopy(current);altered[field]=value
            with self.assertRaisesRegex(ValueError,'causal configuration'):
                reconcile_manifest(self.db,altered,resume=True,previous_code_digest='old')
        self.assertFalse(self.db.execute("SELECT 1 FROM state WHERE key='implementation_revisions'").fetchone())
