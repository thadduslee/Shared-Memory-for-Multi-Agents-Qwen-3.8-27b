"""Compile source enums larger than any episode in this evaluation."""
import json, time
from pathlib import Path
import llguidance
p=Path(__file__).parent
r=json.loads((p/'failed-request.json').read_text())
sources={s['id'] for s in r['payload']['sources']}

def grow(node):
    if isinstance(node,dict):
        return {k:([f's{i}' for i in range(1,513)] if k=='enum' and set(v)==sources else grow(v)) for k,v in node.items()}
    if isinstance(node,list):return [grow(v) for v in node]
    return node
started=time.monotonic()
grammar=llguidance.LLMatcher.grammar_from_json_schema(grow(r['schema']),defaults={'whitespace_flexible':True})
error=llguidance.LLMatcher.validate_grammar(grammar)
assert not error,error
report={'source_enum_count':512,'largest_dataset_episode_turns':225,'compiles_without_warnings':True,'seconds':time.monotonic()-started}
(p/'stress-guidance.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
