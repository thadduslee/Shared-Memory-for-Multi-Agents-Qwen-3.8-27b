"""Evolution repair, atomicity, grounding, and protected diagnostics contracts."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from agentic_memory.privacy import PrivacyMemorySystem,AccessDenied
from agentic_memory.privacy.evolution import ProposalRejected
from agentic_memory.privacy.processors import HashEmbedder,ScriptedProcessor,ProcessorFailure


class SequenceProcessor(ScriptedProcessor):
    def __init__(self,responses):
        super().__init__()
        self.responses=iter(responses)
        self.schemas=[]

    def complete(self,system,payload,*,schema=None):
        self.calls.append(payload)
        self.schemas.append(schema)
        response=next(self.responses)
        if isinstance(response,Exception):raise response
        return response


class EvolutionContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.core=PrivacyMemorySystem(Path(self.temp.name)/'memory.sqlite',HashEmbedder())
        actions=['read','search','derive','write','link','delete','ingest','manage_policy','audit']
        self.admin=self.core.issue_session('issuer','agent',actions=actions,compartments=['c'],
            processing_routes=['test:hash','test:completion'])
        self.reader=self.core.issue_session('reader','agent',actions=['read','search','derive'],compartments=['c'],
            processing_routes=['test:hash','test:completion'])
        for policy,principals in [('public',['issuer','reader']),('private',['issuer'])]:
            self.core.install_policy(self.admin,policy,{'allows':[{'principals':principals,'actions':actions}],
                'processors':['test:hash','test:completion']})
        self.note=self.core.ingest(self.admin,'source','Appointment Friday 11:20 AM; arrival 11:00 AM.',
            policy_id='public',compartment='c',attributes={'sequence':7,'information_scope':'appointments'})
        self.secret=self.core.ingest(self.admin,'secret','Private contact 555-0100.',policy_id='private',compartment='c')

    def tearDown(self):
        self.core.close();self.temp.cleanup()

    def valid(self):
        return {'updates':[{'id':self.note,'context':'arrival 11:00 AM.',
                            'keywords':['arrival'],'tags':['Appointment']}],'edges':[]}

    def evolve(self,responses,**kw):
        model=SequenceProcessor(responses)
        self.core.register_processor(self.admin,model)
        return self.core.evolve(self.admin,model,[self.note],job_id='job',k=0,**kw),model

    def test_rejected_target_corrected_with_exact_feedback(self):
        result,model=self.evolve([{'updates':[{'id':'invented'}],'edges':[]},self.valid()])
        self.assertEqual(result,{'status':'applied','attempts':2})
        self.assertEqual(model.calls[1]['correction']['code'],'unknown_target')
        self.assertEqual(model.calls[1]['correction']['path'],'updates[0].id')
        self.assertEqual(model.schemas[0]['properties']['updates']['items']['properties']['id']['enum'],[self.note])
        history=list(self.core.db.execute('SELECT status,error_code FROM evolution_attempts ORDER BY attempt'))
        self.assertEqual([tuple(r) for r in history],[('rejected','unknown_target'),('applied',None)])

    def test_exhaustion_keeps_sources_and_is_idempotent(self):
        invalid={'updates':[],'edges':[],'policy':'public'}
        result,model=self.evolve([invalid]*3)
        self.assertEqual(result,{'status':'rejected','attempts':3,'error_code':'forbidden_operation'})
        self.assertEqual(self.core.read(self.reader,self.note)['payload']['text'],
                         'Appointment Friday 11:20 AM; arrival 11:00 AM.')
        self.assertFalse(self.core.db.execute('SELECT 1 FROM provenance').fetchone())
        self.assertFalse(self.core.db.execute("SELECT 1 FROM resources WHERE kind='enrichment'").fetchone())
        repeat=self.core.evolve(self.admin,model,[self.note],job_id='job',k=0)
        self.assertEqual(result,repeat)
        self.assertEqual(len(model.calls),3)
        diagnostic=self.core.read(self.admin,'evolution-attempt:job:3')
        self.assertEqual(json.loads(diagnostic['payload']['proposal']),invalid)

    def test_rejected_late_update_cannot_publish_earlier_update(self):
        invalid={'updates':[self.valid()['updates'][0],{'id':'missing'}],'edges':[]}
        self.evolve([invalid]*3)
        self.assertFalse(self.core.db.execute("SELECT 1 FROM resources WHERE kind='enrichment'").fetchone())
        self.assertFalse(self.core.db.execute('SELECT 1 FROM provenance').fetchone())

    def test_unsupported_appointment_interpretation_requires_correction(self):
        invalid=self.valid()
        invalid['updates'][0]['context']='The appointment is Friday at 11:00 AM.'
        result,model=self.evolve([invalid,self.valid()])
        self.assertEqual(result['attempts'],2)
        self.assertEqual(model.calls[1]['correction']['code'],'ungrounded_context')
        overlay=self.core.read(self.reader,'enrichment:job:0')
        self.assertEqual(overlay['payload']['text'],self.core.read(self.reader,self.note)['payload']['text'])
        self.assertEqual(overlay['attributes']['sequence'],7)

    def test_wrong_types_and_excessive_terms_are_repairable(self):
        malformed={'updates':'not an array','edges':[]}
        invalid=self.valid();invalid['updates'][0]['tags']=['scheduling']*9
        result,model=self.evolve([malformed,invalid,self.valid()])
        self.assertEqual(result['status'],'applied')
        self.assertEqual([c['correction']['code'] for c in model.calls[1:]],['proposal_limit','invalid_terms'])
        self.assertEqual(model.calls[2]['correction']['previous_proposal'],invalid)

    def test_descriptive_tags_are_retrieval_metadata_not_answer_facts(self):
        proposal=self.valid()
        proposal['updates'][0]['tags']=['scheduling','visit logistics']
        self.evolve([proposal])
        overlay=self.core.read(self.reader,'enrichment:job:0')
        self.assertEqual(overlay['payload']['text'],self.core.read(self.reader,self.note)['payload']['text'])
        self.assertEqual(overlay['payload']['semantic']['tags'],['scheduling','visit logistics'])

    def test_diagnostics_inherit_full_input_privacy_and_deletion(self):
        model=SequenceProcessor([{'updates':[{'id':'invalid'}],'edges':[]}]*3)
        self.core.register_processor(self.admin,model)
        self.core.evolve(self.admin,model,[self.note,self.secret],job_id='mixed',k=0)
        rid='evolution-attempt:mixed:3'
        with self.assertRaises(AccessDenied):self.core.read(self.reader,rid)
        self.assertFalse(self.core.db.execute('SELECT 1 FROM representations WHERE id=?',(rid,)).fetchone())
        self.core.delete(self.admin,'secret')
        self.assertEqual(self.core.db.execute('SELECT payload FROM resources WHERE id=?',(rid,)).fetchone()[0],'{}')
        with self.assertRaises(AccessDenied):self.core.read(self.admin,rid)

    def test_neighbor_canonicalization_retains_private_overlay_lineage(self):
        model=SequenceProcessor([self.valid(),self.valid()])
        self.core.register_processor(self.admin,model)
        inputs=[self.note,self.secret]
        first=self.core.process(self.admin,model,inputs,'test',{})
        self.core.apply_evolution(self.admin,'private-selection',inputs,first,model.processor_id)
        overlay='enrichment:private-selection:0'
        with patch.object(self.core,'search',return_value=[{'id':overlay}]):
            self.core.evolve(self.admin,model,[self.note],job_id='later')
        self.assertEqual(model.calls[1]['notes'],[{'id':self.note,
            'text':'Appointment Friday 11:20 AM; arrival 11:00 AM.'}])
        generated='enrichment:later:0'
        dependencies=json.loads(self.core.db.execute('SELECT dependencies FROM resources WHERE id=?',(generated,)).fetchone()[0])
        self.assertIn(overlay,dependencies)
        with self.assertRaises(AccessDenied):self.core.read(self.reader,generated)
        self.core.delete(self.admin,'secret')
        with self.assertRaises(AccessDenied):self.core.read(self.admin,generated)

    def test_transport_error_is_saved_and_remains_an_error(self):
        with self.assertRaises(ProcessorFailure):self.evolve([ProcessorFailure('transport')])
        row=self.core.db.execute('SELECT status,error_code FROM evolution_attempts').fetchone()
        self.assertEqual(tuple(row),('processor_error','ProcessorFailure'))
        self.assertFalse(self.core.db.execute('SELECT 1 FROM provenance').fetchone())

    def test_policy_revocation_is_not_retried_as_model_error(self):
        outer=self
        class Revoking(SequenceProcessor):
            def complete(self,system,payload,*,schema=None):
                self.calls.append(payload)
                outer.core.revoke_session(outer.admin)
                return outer.valid()
        model=Revoking([])
        self.core.register_processor(self.admin,model)
        with self.assertRaises(AccessDenied):self.core.evolve(self.admin,model,[self.note],job_id='job',k=0)
        self.assertEqual(len(model.calls),1)
        self.assertFalse(self.core.db.execute("SELECT 1 FROM resources WHERE kind='enrichment'").fetchone())

    def test_atomic_publication_includes_attempt_and_job_status(self):
        def fail(rid):
            if rid.startswith('evolution-attempt:'):raise RuntimeError('injected diagnostic failure')
        self.core._publication_hook=fail
        with self.assertRaises(RuntimeError):self.evolve([self.valid()])
        self.assertFalse(self.core.db.execute('SELECT 1 FROM provenance').fetchone())
        self.assertFalse(self.core.db.execute('SELECT 1 FROM evolution_attempts').fetchone())
        self.assertFalse(self.core.db.execute("SELECT 1 FROM resources WHERE kind='enrichment'").fetchone())

    def test_retry_budget_is_bounded(self):
        for attempts in [0,6,True]:
            with self.subTest(attempts=attempts),self.assertRaises(ValueError):
                self.evolve([],max_attempts=attempts)


if __name__=='__main__':unittest.main()
