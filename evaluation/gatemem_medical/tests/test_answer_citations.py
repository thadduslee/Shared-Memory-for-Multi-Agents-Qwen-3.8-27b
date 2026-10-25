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


if __name__ == '__main__':
    unittest.main()
