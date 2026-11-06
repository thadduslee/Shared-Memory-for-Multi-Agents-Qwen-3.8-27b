"""Bound expensive failures instead of retrying the same truncated output."""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from agentic_memory.privacy.processors import CompletionProcessor, ProcessorFailure, ModelContractError, ProcessorConfigurationFailure, compact_answer_references


class CompletionLimits(unittest.TestCase):
    def test_authorization_header_is_omitted_only_when_no_key_is_configured(self):
        response={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}
        for key in ('', 'dummy', 'configured-test-key'):
            with self.subTest(key=key):
                model=CompletionProcessor('test','http://local.invalid/v1','test',api_key=key)
                with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as request:
                    self.assertEqual(model.complete('Return JSON.',{}),{'ok':True})
                headers=dict(request.call_args.args[0].header_items())
                self.assertEqual(headers.get('Authorization'),'Bearer '+key if key else None)

    def test_answer_citation_aliases_preserve_source_bytes_and_restore_only_references(self):
        first='note:'+64*'a';second='note:'+64*'b'
        payload={'query':first,'evidence':[{'id':first,'text':first},{'id':second,'text':'Private m1 is literal text'}],
            'assessed_request_parts':[{'request':'Pickup','status':'supported','evidence_ids':[second]}]}
        schema={'type':'object','properties':{
            'used_record_ids':{'type':'array','items':{'type':'string','enum':[first,second]}},
            'request_parts':{'type':'array','items':{'type':'object','properties':{
                'evidence_ids':{'type':'array','items':{'type':'string','enum':[first,second]}}}}}}}
        wire,contract,restore=compact_answer_references(payload,schema)
        self.assertEqual([r['id'] for r in wire['evidence']],['m1','m2'])
        self.assertEqual(wire['query'],first)
        self.assertEqual(wire['evidence'][0]['text'],first)
        self.assertEqual(wire['assessed_request_parts'][0]['evidence_ids'],['m2'])
        self.assertEqual(contract['properties']['used_record_ids']['items']['enum'],['m1','m2'])
        value=restore({'answer':'m1 is literal text','answer_structured':{'id':'m1'},
            'used_record_ids':['m2'],'request_parts':[{'evidence_ids':['m1']}]})
        self.assertEqual(value['used_record_ids'],[second])
        self.assertEqual(value['request_parts'][0]['evidence_ids'],[first])
        self.assertEqual(value['answer'],'m1 is literal text')
        self.assertEqual(value['answer_structured'],{'id':'m1'})
        from agentic_memory.privacy.answering import validate_answer_output
        bad=restore({'action':'answer','answer':'Unavailable','used_record_ids':['m99']})
        with self.assertRaises(ModelContractError):validate_answer_output(bad,[first,second])

    def test_token_limit_is_reported_after_one_request_with_timing(self):
        result={'usage':{'completion_tokens':32},'choices':[{
            'finish_reason':'length','message':{'content':'{"unfinished":'}}]}
        model=CompletionProcessor('test','http://local.invalid/v1','test',max_tokens=32)
        with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(result).encode())) as request:
            with self.assertRaisesRegex(ProcessorFailure,'exceeded token budget'):
                model.complete('Return JSON.',{})
        self.assertEqual(request.call_count,1)
        self.assertIsNotNone(model.last_elapsed_seconds)
        self.assertEqual(model.usage['completion_tokens'],32)

    def test_http_failure_includes_bounded_server_reason_without_blind_400_retry(self):
        model=CompletionProcessor('test','http://local.invalid/v1','test')
        body=io.BytesIO(json.dumps({'error':{'message':'Grammar error: schema too large'}}).encode())
        error=urllib.error.HTTPError('http://local.invalid',400,'Bad Request',{},body)
        with patch('urllib.request.urlopen',side_effect=error) as request:
            with self.assertRaisesRegex(ProcessorFailure,'HTTP 400: Grammar error: schema too large'):
                model.complete('Return JSON.',{})
        self.assertEqual(request.call_count,1)

    def test_invalid_json_is_a_contract_failure_after_one_request(self):
        response={'choices':[{'message':{'content':'not JSON'}}]}
        model=CompletionProcessor('test','http://local.invalid/v1','test')
        with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as request:
            with self.assertRaises(ModelContractError):model.complete('Return JSON.',{})
        self.assertEqual(request.call_count,1)

    def test_empty_provider_response_is_retryable_without_semantic_rejection(self):
        model=CompletionProcessor('test','http://local.invalid/v1','test')
        response={'choices':[{'message':{'content':None}}]}
        with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as request:
            with self.assertRaises(ProcessorFailure) as caught:model.complete('Return JSON.',{})
        self.assertNotIsInstance(caught.exception,ModelContractError)
        self.assertEqual(request.call_count,1)

    def test_generated_nontext_is_a_bounded_contract_failure(self):
        model=CompletionProcessor('test','http://local.invalid/v1','test')
        response={'usage':{'completion_tokens':12},'choices':[{'message':{'content':None}}]}
        with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as request:
            with self.assertRaises(ModelContractError):model.complete('Return JSON.',{})
        self.assertEqual(request.call_count,1)

    def test_single_pass_budget_does_not_lower_other_stage_budgets(self):
        response={'choices':[{'finish_reason':'stop','message':{'content':'{"permission_changes":[],"new_facts":[]}'}}]}
        model=CompletionProcessor('test','http://local.invalid/v1','test')
        schema={'type':'object','properties':{'permission_changes':{'type':'array'},'new_facts':{'type':'array'}}}
        with patch.dict('os.environ',{'AMEM_PERMISSION_MAX_TOKENS':'3072'}), \
                patch('urllib.request.urlopen',side_effect=lambda *a,**k:io.BytesIO(json.dumps(response).encode())) as request:
            model.complete('Update permission.',{},schema=schema)
            model.complete('Answer.',{},schema={'type':'object','properties':{'answer':{'type':'string'}}})
        self.assertEqual([json.loads(call.args[0].data)['max_tokens'] for call in request.call_args_list],[3072,8192])

    def test_combined_incremental_stage_uses_permission_output_budget(self):
        response={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
        model=CompletionProcessor('test','http://local.invalid/v1','test')
        schema={'type':'object','properties':{key:{'type':'array'} for key in
            ('control_review','permission_changes','segments','binding_reviews')}}
        with patch.dict('os.environ',{'AMEM_PERMISSION_MAX_TOKENS':'3072'}), \
                patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as request:
            model.complete('Update permission.',{},schema=schema)
        self.assertEqual(json.loads(request.call_args.args[0].data)['max_tokens'],3072)

    def test_compact_incremental_budget_is_bounded_without_lowering_query_budget(self):
        response={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
        model=CompletionProcessor('test','http://local.invalid/v1','test')
        schema={'type':'object','properties':{key:{'type':'array'} for key in ('c','p','f','b')}}
        with patch.dict('os.environ',{'AMEM_PERMISSION_MAX_TOKENS':'3072','AMEM_INCREMENTAL_MAX_TOKENS':'1024'}), \
                patch('urllib.request.urlopen',side_effect=lambda *a,**k:io.BytesIO(json.dumps(response).encode())) as request:
            model.complete('Process source.',{},schema=schema)
            model.complete('Answer.',{},schema={'type':'object','properties':{'answer':{'type':'string'}}})
        self.assertEqual([json.loads(call.args[0].data)['max_tokens'] for call in request.call_args_list],[1024,8192])

    def test_openrouter_qwen_uses_supported_routing_and_thinking_controls(self):
        response={'model':'qwen/qwen3.8-27b','provider':'unit-provider',
            'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}
        model=CompletionProcessor('remote','https://openrouter.ai/api/v1','qwen/qwen3.8-27b','test-key')
        with patch.dict('os.environ',{'AMEM_OPENROUTER_PROVIDER_SORT':'throughput'}), \
                patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as request:
            self.assertEqual(model.complete('Return JSON.',{}),{'ok':True})
        body=json.loads(request.call_args.args[0].data)
        self.assertEqual(body['model'],'qwen/qwen3.8-27b')
        self.assertEqual(body['reasoning'],{'enabled':False})
        self.assertEqual(body['provider'],{'require_parameters':True,'sort':'throughput'})
        self.assertNotIn('chat_template_kwargs',body)
        self.assertEqual(model.last_response_model,'qwen/qwen3.8-27b')
        self.assertEqual(model.last_provider,'unit-provider')

    def test_openrouter_routing_override_and_invalid_configuration(self):
        response={'model':'qwen/qwen3.8-27b','choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
        model=CompletionProcessor('remote','https://openrouter.ai/api/v1','qwen/qwen3.8-27b','test-key')
        with patch.dict('os.environ',{'AMEM_OPENROUTER_PROVIDER_SORT':'latency',
                'AMEM_OPENROUTER_PROVIDER_IGNORE':'DeepInfra, deepinfra'}), \
                patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as request:
            model.complete('Return JSON.',{})
        self.assertEqual(json.loads(request.call_args.args[0].data)['provider'],
            {'require_parameters':True,'sort':'latency','ignore':['deepinfra']})
        with patch.dict('os.environ',{'AMEM_OPENROUTER_PROVIDER_SORT':'unsupported'}), \
                patch('urllib.request.urlopen') as request:
            with self.assertRaises(ProcessorConfigurationFailure):model.complete('Return JSON.',{})
            request.assert_not_called()

    def test_v16_recipient_review_still_uses_incremental_budget(self):
        response={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
        model=CompletionProcessor('test','http://local.invalid/v1','test')
        schema={'type':'object','properties':{k:{} for k in ('review','c','p','f','b')}}
        with patch.dict('os.environ',{'AMEM_PERMISSION_MAX_TOKENS':'3072','AMEM_INCREMENTAL_MAX_TOKENS':'2048'}), \
                patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as request:
            model.complete('Update.',{'permission_version':'16'},schema=schema)
        self.assertEqual(json.loads(request.call_args.args[0].data)['max_tokens'],2048)

    def test_openrouter_unexpected_model_stops_without_publishing_or_retry(self):
        response={'model':'different/model','choices':[{'message':{'content':'{"ok":true}'}}]}
        model=CompletionProcessor('remote','https://openrouter.ai/api/v1','openai/gpt-4.1','test-key')
        with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as request:
            with self.assertRaisesRegex(ProcessorConfigurationFailure,'unexpected response model'):
                model.complete('Return JSON.',{})
        self.assertEqual(request.call_count,1)

    def test_v19_answer_verifier_and_source_caps_are_independent(self):
        response={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
        model=CompletionProcessor('test','http://local.invalid/v1','test',max_tokens=8192)
        empty={'type':'object','properties':{}}
        source={'type':'object','properties':{key:{} for key in ('c','p','f','b')}}
        with patch.dict('os.environ',{'AMEM_ANSWER_MAX_TOKENS':'1536','AMEM_VERIFY_MAX_TOKENS':'1024',
                'AMEM_INCREMENTAL_MAX_TOKENS':'2048'}), \
                patch('urllib.request.urlopen',side_effect=lambda *a,**k:io.BytesIO(json.dumps(response).encode())) as request:
            model.complete('Generate.',{'stage':'answer_v19'},schema=empty)
            model.complete('Verify.',{'stage':'verify_answer_v19'},schema=empty)
            model.complete('Source.',{'permission_version':'19'},schema=source)
            model.complete('Legacy.',{},schema=empty)
        self.assertEqual([json.loads(call.args[0].data)['max_tokens'] for call in request.call_args_list],
                         [1536,1024,2048,8192])
        self.assertEqual(request.call_count,4)

    def test_invalid_v19_verification_cap_makes_no_network_request(self):
        model=CompletionProcessor('test','http://local.invalid/v1','test')
        for value in ('0','invalid'):
            with self.subTest(value=value),patch.dict('os.environ',{'AMEM_VERIFY_MAX_TOKENS':value}), \
                    patch('urllib.request.urlopen') as request:
                with self.assertRaises(ProcessorConfigurationFailure):
                    model.complete('Verify.',{'stage':'verify_answer_v19'})
                request.assert_not_called()


if __name__=='__main__':unittest.main()
