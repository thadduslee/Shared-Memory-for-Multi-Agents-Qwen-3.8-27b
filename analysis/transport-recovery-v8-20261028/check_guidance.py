"""Compile and walk the exact failed schema with the serving tokenizer on CPU."""
import json
from pathlib import Path
import time
import llguidance
import llguidance.hf
from transformers import AutoTokenizer

output=Path(__file__).parent
request=json.loads((output/'failed-request.json').read_text())
snapshot='/home/thaddus/.cache/huggingface/hub/models--Qwen--Qwen3.8-27B/snapshots/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0'
tokenizer=AutoTokenizer.from_pretrained(snapshot,local_files_only=True)
ll_tokenizer=llguidance.hf.from_tokenizer(tokenizer)
started=time.monotonic()
grammar=llguidance.LLMatcher.grammar_from_json_schema(request['schema'],defaults={'whitespace_flexible':True})
matcher=llguidance.LLMatcher(ll_tokenizer,grammar)
assert not matcher.get_error(),matcher.get_error()
example={'revision':request['payload']['revision'],
    'considered_restrictions':list(request['payload']['permission_state']['restriction']),
    'operations':[], 'coverage':[{'principal':pid,'decision':'unchanged',
        'evidence':[request['payload']['new_source_id']],'reason':'Schema regression probe.'}
        for pid in request['payload']['coverage_required']]}
tokens=tokenizer.encode(json.dumps(example,separators=(',',':')),add_special_tokens=False)
for token in tokens:
    matcher.compute_bitmask()
    assert matcher.consume_token(token),token
    assert not matcher.get_error(),matcher.get_error()
assert matcher.is_accepting()
report={'backend':'guidance','exact_failed_schema_compiles':True,'token_masks_and_token_consumption_pass':True,
    'valid_completion_accepted':True,'tokens_checked':len(tokens),'elapsed_s':time.monotonic()-started,
    'grammar_warnings':matcher.get_grammar_warnings()}
(output/'guidance-regression.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2),flush=True)
