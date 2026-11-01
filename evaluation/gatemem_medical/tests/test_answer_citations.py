"""Exact field citations and unchanged full-input authorization at release."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from agentic_memory.privacy import AccessDenied, PrivacyMemorySystem
from agentic_memory.privacy.processors import HashEmbedder
from evaluation.gatemem_medical.runner import answer


class Model:
    processor_id = 'test:answer'

    def __init__(self, response, callback=None):
        self.response, self.callback = response, callback
        self.schema = None

    def complete(self, system, payload, *, schema=None):
        self.schema = schema
        if self.callback:
            self.callback()
        return self.response


class AnswerCitations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = PrivacyMemorySystem(Path(self.temp.name) / 'memory.sqlite', HashEmbedder())
        actions = ['read', 'search', 'derive', 'write', 'link', 'delete', 'ingest', 'manage_policy', 'audit']
        routes = ['test:hash', 'test:answer']
        self.admin = self.core.issue_session('issuer', 'app', actions=actions,
            compartments=['c'], processing_routes=routes)
        self.reader = self.core.issue_session('reader', 'app', actions=actions,
            compartments=['c'], processing_routes=routes)
        self.policy = {'allows': [{'principals': ['issuer', 'reader'], 'actions': actions}],
                       'processors': routes}
        self.core.install_policy(self.admin, 'p', self.policy)
        self.note = 'note:obs:fixture:t086:contact'
        self.core.create(self.admin, self.note, 'memory', {'text': 'Use the patient portal.'},
                         policy_id='p', compartment='c')
        self.core.index(self.admin, self.note)
        self.adapter = SimpleNamespace(query_context=lambda query: self.reader)
        self.query = {'checkpoint_id': 'q', 'episode_id': 'e', 'as_of_turn_id': 't097',
                      'query_text': 'What is the active contact plan?'}

    def tearDown(self):
        self.core.close()
        self.temp.cleanup()

    def run_answer(self, citations, callback=None):
        model = Model({'action': 'answer', 'answer': 'Use the patient portal.',
                       'answer_structured': {'contact': 'patient portal'},
                       'used_record_ids': citations}, callback)
        self.core.register_processor(self.admin, model)
        return answer(self.core, self.adapter, self.query, model), model

    def test_dynamic_enum_preserves_field_suffix_and_structured_output(self):
        result, model = self.run_answer([self.note])
        self.assertEqual(model.schema['properties']['used_record_ids']['items']['enum'], [self.note])
        self.assertEqual(result['output']['used_record_ids'], [self.note])
        self.assertEqual(result['output']['answer_structured'], {'contact': 'patient portal'})

    def test_dropped_suffix_is_rejected_before_release_even_if_model_ignores_schema(self):
        with patch.object(self.core, 'release', wraps=self.core.release) as release:
            with self.assertRaisesRegex(ValueError, 'outside the supplied evidence'):
                self.run_answer([self.note.removesuffix(':contact')])
            release.assert_not_called()

    def test_malformed_citations_get_validation_error(self):
        for citations in (None, self.note, [{}], [None]):
            with self.subTest(citations=citations):
                with self.assertRaisesRegex(ValueError, 'list of exact supplied evidence IDs'):
                    self.run_answer(citations)

    def test_revocation_still_checks_all_inputs_even_when_citations_are_empty(self):
        def revoke():
            self.core.install_policy(self.admin, 'p', {
                **self.policy, 'allows': [{'principals': ['issuer'], 'actions': ['read', 'derive']}]})
        with self.assertRaises(AccessDenied):
            self.run_answer([], callback=revoke)

    def test_empty_evidence_requires_assessed_denial(self):
        for authorized,denied,expected in [(True,False,'no_memory'),(False,False,'no_memory'),(False,True,'refuse')]:
            with self.subTest(authorized=authorized,denied=denied):
                self.adapter.answer_authorization=lambda visible,context=None:{
                    'has_authorized_assignment':authorized,'access_denied':denied}
                model=Model({})
                with patch.object(self.core,'retrieve',return_value={'evidence':[],'input_ids':[]}):
                    result=answer(self.core,self.adapter,self.query,model)
                self.assertEqual(result['output']['action'],expected)
                self.assertIsNone(model.schema)

    def test_out_of_scope_plan_forces_generic_refusal_and_discards_unsupported_output(self):
        self.adapter.review_absence=lambda visible,requests,processor:[True]*len(requests)
        model=Model({'request_parts':[{'request':'private record','status':'out_of_scope','evidence_ids':[]}],
            'action':'answer','answer':'An unsupported guessed secret.',
            'answer_structured':{'secret':'unsupported'},'used_record_ids':[]})
        model.require_answer_plan=True
        self.core.register_processor(self.admin,model)
        result=answer(self.core,self.adapter,self.query,model)
        self.assertEqual(result['output']['action'],'refuse')
        self.assertNotIn('secret',str(result['output']))
        self.assertEqual(result['output']['answer_structured'],{})
        self.assertEqual(next(iter(model.schema['properties'])),'request_parts')

    def test_mixed_plan_normalizes_partial_answer_without_releasing_the_plan(self):
        self.adapter.review_absence=lambda visible,requests,processor:[True]*len(requests)
        model=Model({'request_parts':[
            {'request':'contact plan','status':'supported','evidence_ids':[self.note]},
            {'request':'private record','status':'out_of_scope','evidence_ids':[]}],
            'action':'answer','answer':'Use the patient portal. Other information is unavailable.',
            'answer_structured':{},'used_record_ids':[self.note]})
        model.require_answer_plan=True
        self.core.register_processor(self.admin,model)
        result=answer(self.core,self.adapter,self.query,model)
        self.assertEqual(result['output']['action'],'answer_redacted')
        self.assertNotIn('request_parts',result['output'])
        self.assertEqual(result['input_manifest']['answer_decision'],['supported','out_of_scope'])

    def test_host_review_can_classify_absence_without_exposing_scope_metadata(self):
        model=Model({'request_parts':[{'request':'private record','status':'missing','evidence_ids':[]}],
            'action':'no_memory','answer':'Not found.','answer_structured':{},'used_record_ids':[]})
        model.require_answer_plan=True
        self.adapter.review_absence=lambda visible,requests,processor:[True]
        self.core.register_processor(self.admin,model)
        result=answer(self.core,self.adapter,self.query,model)
        self.assertEqual(result['output']['action'],'refuse')
        self.assertEqual(result['input_manifest']['answer_decision'],['out_of_scope'])

    def test_answer_model_cannot_infer_denial_from_missing_evidence(self):
        model=Model({'request_parts':[{'request':'medication instructions','status':'out_of_scope','evidence_ids':[]}],
            'action':'refuse','answer':"You aren't authorized.",
            'answer_structured':{},'used_record_ids':[]})
        model.require_answer_plan=True
        self.adapter.review_absence=lambda visible,requests,processor:[False]
        self.core.register_processor(self.admin,model)
        result=answer(self.core,self.adapter,self.query,model)
        self.assertEqual(result['output']['action'],'no_memory')
        self.assertEqual(result['output']['answer'],"I don't have that information.")
        self.assertEqual(result['input_manifest']['answer_decision'],['missing'])

    def test_empty_retrieval_assesses_requested_scope_even_with_other_grants(self):
        self.adapter.answer_authorization=lambda visible,context=None:{
            'has_authorized_assignment':True,'access_denied':False}
        for denied,expected in [(False,'no_memory'),(True,'refuse')]:
            with self.subTest(denied=denied):
                self.adapter.review_absence=lambda visible,requests,processor:[denied]
                with patch.object(self.core,'retrieve',return_value={'evidence':[],'input_ids':[]}):
                    result=answer(self.core,self.adapter,self.query,Model({}))
                self.assertEqual(result['output']['action'],expected)

    def test_partial_answer_is_rewritten_after_policy_corrects_a_false_denial(self):
        first={'request_parts':[
            {'request':'contact plan','status':'supported','evidence_ids':[self.note]},
            {'request':'medication instructions','status':'out_of_scope','evidence_ids':[]}],
            'action':'answer_redacted','answer':"Use the patient portal. You aren't authorized for medication instructions.",
            'answer_structured':{},'used_record_ids':[self.note]}
        final={'action':'answer','answer':"Use the patient portal. I don't have the medication instructions.",
            'answer_structured':{},'used_record_ids':[self.note]}
        model=Model(first);model.require_answer_plan=True
        calls=[]
        def complete(system,payload,*,schema=None):
            calls.append(payload)
            return final if 'assessed_request_parts' in payload else first
        model.complete=complete
        self.adapter.review_absence=lambda visible,requests,processor:[False]
        self.core.register_processor(self.admin,model)
        result=answer(self.core,self.adapter,self.query,model)
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[-1]['assessed_request_parts'][-1]['status'],'missing')
        self.assertEqual(result['output']['action'],'answer')
        self.assertNotIn("aren't authorized",result['output']['answer'])
        self.assertEqual(result['input_manifest']['answer_decision'],['supported','missing'])

    def test_processor_without_plan_cannot_refuse_without_a_denial_assessment(self):
        model=Model({'action':'refuse','answer':"You aren't authorized.",
            'answer_structured':{},'used_record_ids':[]})
        self.core.register_processor(self.admin,model)
        result=answer(self.core,self.adapter,self.query,model)
        self.assertEqual(result['output']['action'],'no_memory')
        self.assertEqual(result['output']['answer'],"I don't have that information.")


if __name__ == '__main__':
    unittest.main()
