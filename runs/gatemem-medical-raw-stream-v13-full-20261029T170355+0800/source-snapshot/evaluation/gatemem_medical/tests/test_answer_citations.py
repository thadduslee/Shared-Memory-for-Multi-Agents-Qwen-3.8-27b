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

    def test_empty_evidence_distinguishes_absent_fact_from_no_authorized_assignment(self):
        for authorized,expected in [(True,'no_memory'),(False,'refuse')]:
            with self.subTest(authorized=authorized):
                self.adapter.answer_authorization=lambda visible,context=None:{
                    'has_authorized_assignment':authorized,'authorized_scopes':['contact'] if authorized else []}
                model=Model({})
                with patch.object(self.core,'retrieve',return_value={'evidence':[],'input_ids':[]}):
                    result=answer(self.core,self.adapter,self.query,model)
                self.assertEqual(result['output']['action'],expected)
                self.assertIsNone(model.schema)

    def test_out_of_scope_plan_forces_generic_refusal_and_discards_unsupported_output(self):
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


if __name__ == '__main__':
    unittest.main()
