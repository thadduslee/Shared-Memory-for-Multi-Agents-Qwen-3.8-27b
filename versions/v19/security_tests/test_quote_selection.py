"""Constrained text/source pairs and bounded arbitrary-excerpt retrieval."""
from copy import deepcopy
import unittest

import jsonschema

from security_tests import test_permission_inference as fixtures
from agentic_memory.privacy.permissions import compact_input,proposal_schema,PermissionProposalRejected
from agentic_memory.privacy.quote_selection import binding_quotes


class QuoteSelectionTests(unittest.TestCase):
    def setUp(self):self.c=fixtures.PermissionInference();self.c.setUp()
    def tearDown(self):self.c.tearDown()

    def test_wrong_source_pair_is_impossible_in_response_schema(self):
        c=self.c;c.establish();job=c.ingest('The ordered labs are being drawn.');c.apply(job)
        payload=c.model.calls[-1]
        wire,schema,_=compact_input(payload,proposal_schema(bootstrap=c.bootstrap,
            source_ids=[s['id'] for s in payload['sources']]))
        branches=[v for v in schema['properties']['operations']['items']['anyOf']
                  if v['properties']['op']['enum']==['bind_information']]
        pickup=next(s for s in wire['sources'] if s['text'].startswith('Pickup'))
        labs=next(s for s in wire['sources'] if s['text'].startswith('The ordered'))
        binding={'op':'bind_information','assignment_id':'access','quote':'Pickup is at 9 AM',
            'source_id':labs['id'],'necessity':'Pickup time is needed.','scope_supported':'Pickup only.',
            'context_preserved':'Time retains pickup association.','metadata_safe':'Opaque field.',
            'evidence_kind':'instruction','restriction_decisions':[],
            'evidence':{k:[pickup['id']] for k in ('authorization','necessity','subject')}}
        validator=jsonschema.Draft202012Validator({'anyOf':branches})
        self.assertFalse(validator.is_valid(binding))
        binding['source_id']=pickup['id']
        validator.validate(binding)
        for branch in branches:
            props=branch['properties'];sid=props['source_id']['enum'][0]
            source=next(s for s in wire['sources'] if s['id']==sid)
            self.assertLess(list(props).index('quote'),list(props).index('source_id'))
            self.assertTrue(all(quote in source['text'] for quote in props['quote']['enum']))

    def test_quote_candidates_exclude_questions_and_preserve_fine_historical_fields(self):
        text='Is the private code 1234? Pickup is at 9 AM; confidential diagnosis.'
        quotes=binding_quotes(text,extra=['at 9 AM','the private code 1234'])
        self.assertIn('Pickup is at 9 AM',quotes)
        self.assertIn('at 9 AM',quotes)
        self.assertNotIn('the private code 1234',quotes)
        self.assertFalse(any('1234' in quote for quote in quotes))
        self.assertEqual(binding_quotes('Is pickup at 9 AM?'),[])

    def test_repeated_short_quotes_are_not_ambiguous_choices(self):
        text='Call today. Call today. Pickup is tomorrow.'
        quotes=binding_quotes(text)
        self.assertNotIn('Call today.',quotes)
        self.assertIn(text,quotes)
        self.assertIn('Pickup is tomorrow.',quotes)

    def test_quote_failure_points_to_actual_source_without_rebinding_it(self):
        c=self.c;c.establish();old=c.ingest('The ride uses the violet gate.');c.apply(old)
        new=c.ingest('The ordered labs are being drawn.')
        bad=c.binding(new['source_id']);bad.pop('spans');bad['quote']='the violet gate'
        before=c.updater.inspect()
        with self.assertRaises(PermissionProposalRejected) as rejected:c.apply(new,[bad])
        details=rejected.exception.details
        self.assertEqual(details['code'],'wrong_source')
        self.assertEqual(details['matching_source_ids'],[old['source_id']])
        self.assertEqual(details['supplied_source_id'],new['source_id'])
        self.assertIn('The ordered labs are being drawn.',details['source_quote_choices'])
        self.assertEqual(c.updater.inspect(),before)

    def test_exact_quote_retrieval_adds_narrow_choice_then_commits_atomically(self):
        c=self.c;c.establish();c.apply(c.ingest('An unrelated observation.'))
        old=c.ingest('Pickup is at the violet gate; confidential diagnosis.');c.apply(old)
        c.history();job=c.ingest('Use the earlier pickup location.')
        c.model.compact_permission_input=True;calls=[]
        def respond(payload):
            calls.append(deepcopy(payload))
            if not payload['context']['retrieval_results']:
                self.assertFalse(any(s['text'].startswith('Pickup is at the violet') for s in payload['sources']))
                return {'revision':payload['revision'],'operations':[],
                    'context_requests':[{'kind':'quote','reference':'the violet gate','offset':0}]}
            source=next(s for s in payload['sources'] if s['text'].startswith('Pickup is at the violet'))
            self.assertIn('the violet gate',source['binding_quotes'])
            binding=c.binding(source['id']);binding.pop('spans');binding['quote']='the violet gate'
            return {'revision':payload['revision'],'considered_restrictions':[],'operations':[binding]}
        c.model.handler=respond;c.updater.update(job['job_id'])
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[1]['context']['retrieval_results'][0]['kind'],'quote')
        self.assertEqual(calls[1]['context']['retrieval_results'][0]['total_matches'],1)
        self.assertIn('the violet gate',[r['text'] for r in c.selected()])
        self.assertEqual(c.updater.inspect()['assignment']['access']['bindings'][-1]['source_id'],old['source_id'])

    def test_nonexistent_quote_cannot_create_a_source_or_binding(self):
        c=self.c;c.establish();job=c.ingest('An ordinary turn.')
        count=c.core.db.execute('SELECT COUNT(*) FROM resources').fetchone()[0]
        def respond(payload):
            if not payload['context']['retrieval_results']:
                return {'revision':payload['revision'],'operations':[],
                    'context_requests':[{'kind':'quote','reference':'SECRET FUTURE VALUE','offset':0}]}
            self.assertEqual(payload['context']['retrieval_results'][0]['total_matches'],0)
            return {'revision':payload['revision'],'considered_restrictions':[],'operations':[]}
        c.model.handler=respond;c.updater.update(job['job_id'])
        self.assertEqual(c.core.db.execute('SELECT COUNT(*) FROM resources').fetchone()[0],count)
        self.assertEqual(len(c.updater.inspect()['assignment']['access']['bindings']),1)

    def test_quote_search_uses_literal_alias_words_without_reference_rewriting(self):
        c=self.c;old=c.ingest('Literal s1 is present here.');c.apply(old)
        job=c.ingest('Read the earlier literal token.');c.model.compact_permission_input=True
        def respond(payload):
            if not payload['context']['retrieval_results']:
                return {'revision':payload['revision'],'operations':[],
                    'context_requests':[{'kind':'quote','reference':'s1','offset':0}]}
            result=payload['context']['retrieval_results'][0]
            self.assertEqual(result['reference'],'s1');self.assertEqual(result['total_matches'],1)
            return {'revision':payload['revision'],'considered_restrictions':[],'operations':[]}
        c.model.handler=respond;c.updater.update(job['job_id'])

    def test_quote_retrieval_budget_is_persistent(self):
        c=self.c;job=c.ingest('An ordinary turn.')
        c.model.handler=lambda p:{'revision':p['revision'],'operations':[],
            'context_requests':[{'kind':'quote','reference':'unavailable text','offset':0}]}
        with self.assertRaisesRegex(PermissionProposalRejected,'budget exceeded'):c.updater.run(job['job_id'])
        attempts=c.core.db.execute('SELECT attempts FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0]
        self.assertEqual(attempts,4)
        with self.assertRaisesRegex(PermissionProposalRejected,'budget exceeded'):c.updater.run(job['job_id'])
        self.assertEqual(c.core.db.execute('SELECT attempts FROM permission_jobs WHERE id=?',(job['job_id'],)).fetchone()[0],5)

    def test_snapshot_acknowledgment_arrays_are_constrained_to_known_values(self):
        c=self.c;job=c.ingest('An ordinary turn.');c.apply(job)
        payload=deepcopy(c.model.calls[-1]);payload['coverage_required']=['owner','helper']
        _,schema,_=compact_input(payload,proposal_schema(bootstrap=c.bootstrap,
            source_ids=[job['source_id']],require_coverage=True))
        reviewed=schema['properties']['coverage']['properties']['reviewed']
        jsonschema.validate(['helper','owner'],reviewed)
        for bad in ([],['owner'],['helper','owner','owner'],['helper','owner','outsider']):
            self.assertFalse(jsonschema.Draft202012Validator(reviewed).is_valid(bad))
        restrictions=schema['properties']['considered_restrictions']
        jsonschema.validate([],restrictions)
        self.assertFalse(jsonschema.Draft202012Validator(restrictions).is_valid(['invented']))
