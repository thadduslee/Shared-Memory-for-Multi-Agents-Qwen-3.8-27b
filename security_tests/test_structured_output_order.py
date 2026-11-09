"""Provider key sorting must preserve decision order and canonical validation."""
from copy import deepcopy
import io
import json
import unittest
from unittest.mock import patch

import jsonschema

from agentic_memory.privacy.processors import (
    CompletionProcessor, ProcessorContractFailure, ordered_response_keys,
)


def obj(properties):
    return {'type':'object','properties':properties,'required':list(properties),
            'additionalProperties':False}


class StructuredOutputOrderTests(unittest.TestCase):
    def test_sorted_wire_preserves_review_before_decision_and_all_values(self):
        schema=obj({'review':{'type':'string'},'c':{'type':'array','items':{'type':'integer'}},
            'b':{'type':'array','items':obj({'q':{'type':'string'},'f':{'type':'string'},
                'r':{'type':'string'},'d':{'type':'string','enum':['allow','outside']}})}})
        before=deepcopy(schema);wire,restore=ordered_response_keys(schema)
        value={'001_review':'A source says 001_review verbatim.','002_c':[0],
            '003_b':[{'001_q':'001_review is literal data','002_f':'f12',
                '003_r':'This field is outside scope.','004_d':'outside'}]}
        sorted_value=json.loads(json.dumps(value,sort_keys=True))
        jsonschema.validate(sorted_value,wire)
        result=restore(sorted_value);jsonschema.validate(result,schema)
        self.assertEqual(list(result),['review','c','b'])
        self.assertEqual(list(result['b'][0]),['q','f','r','d'])
        self.assertEqual(result['b'][0]['q'],'001_review is literal data')
        self.assertEqual(result['b'][0]['f'],'f12')
        self.assertEqual(schema,before)

    def test_conflicting_alias_and_canonical_key_are_rejected(self):
        _,restore=ordered_response_keys(obj({'action':{'type':'string'}}))
        with self.assertRaises(ProcessorContractFailure):
            restore({'001_action':'refuse','action':'answer'})

    def test_source_transport_keeps_budget_and_source_text(self):
        schema=obj({'review':{'type':'string'},'c':{'type':'array','items':{'type':'integer'}},
            'p':{'type':'array','items':{'type':'string'}},
            'f':{'type':'array','items':{'type':'string'}},
            'b':{'type':'array','items':{'type':'string'}}})
        payload={'permission_version':'16','stage':'combined_incremental',
            'sources':[{'id':'s1','text':'Preserve 001_review and 002_c literally.'}]}
        before=deepcopy(payload);captured={}
        def respond(request,timeout=None):
            body=json.loads(request.data);captured.update(body)
            result={'001_review':'No consent.','002_c':[0],'003_p':[],'004_f':[],'005_b':[]}
            return io.BytesIO(json.dumps({'model':'test:model','provider':'test',
                'usage':{'completion_tokens':10},'choices':[{'finish_reason':'stop',
                'message':{'content':json.dumps(result,sort_keys=True)}}]}).encode())
        model=CompletionProcessor('test:ordered','https://openrouter.ai/api/v1','test:model','dummy',max_tokens=8192)
        with patch('urllib.request.urlopen',side_effect=respond):
            result=model.complete('Review then decide.',payload,schema=schema)
        self.assertEqual(captured['max_tokens'],2048)
        self.assertEqual(json.loads(captured['messages'][1]['content']),payload)
        self.assertEqual(payload,before)
        self.assertEqual(list(result),['review','c','p','f','b'])
        self.assertTrue(model.last_wire_key_ordering)
        self.assertEqual(result['p'],[])

    def test_answer_key_ordering_composes_with_citation_aliases(self):
        schema=obj({'evidence_roles':obj({'0':{'type':'string','enum':['current']}}),
            'answer_structured':obj({'current_items':{'type':'array','items':{'type':'string'}}}),
            'action':{'type':'string'},'answer':{'type':'string'},
            'used_record_ids':{'type':'array','items':{'type':'string','enum':['note:canonical']}}})
        payload={'query':'What is the event time?','evidence':[{'id':'note:canonical','text':'Friday at 11:20.'}]}
        def respond(request,timeout=None):
            body=json.loads(request.data)
            self.assertEqual(json.loads(body['messages'][1]['content'])['evidence'][0]['id'],'m1')
            result={'001_evidence_roles':{'001_0':'current'},
                '002_answer_structured':{'001_current_items':['Friday at 11:20.']},
                '003_action':'answer','004_answer':'Friday at 11:20.','005_used_record_ids':['m1']}
            jsonschema.validate(result,body['response_format']['json_schema']['schema'])
            return io.BytesIO(json.dumps({'model':'test:model','usage':{'completion_tokens':10},
                'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result,sort_keys=True)}}]}).encode())
        model=CompletionProcessor('test:ordered','https://openrouter.ai/api/v1','test:model','dummy')
        with patch('urllib.request.urlopen',side_effect=respond):
            result=model.complete('Make the plan before prose.',payload,schema=schema)
        jsonschema.validate(result,schema)
        self.assertEqual(list(result)[0:2],['evidence_roles','answer_structured'])
        self.assertEqual(result['used_record_ids'],['note:canonical'])
        self.assertEqual(result['answer'],'Friday at 11:20.')


if __name__=='__main__':unittest.main()
