"""Run-local decoder compatibility; application validators retain full schemas."""
import json
import llguidance
from schema_refs import share_subschemas
_original = llguidance.LLMatcher.grammar_from_json_schema
_MAPS = {'properties', 'patternProperties', 'dependentSchemas', '$defs', 'definitions'}
_ARRAYS = {'allOf', 'anyOf', 'oneOf', 'prefixItems'}
_SINGLE = {'items', 'additionalItems', 'additionalProperties', 'contains', 'propertyNames', 'not', 'if', 'then', 'else', 'unevaluatedItems', 'unevaluatedProperties', 'contentSchema'}
def decoder_schema(node):
    if not isinstance(node, dict): return node
    result = dict(node)
    result.pop('uniqueItems', None)
    for key, value in node.items():
        if key in _MAPS and isinstance(value, dict):
            result[key] = {name: decoder_schema(child) for name, child in value.items()}
        elif key in _ARRAYS and isinstance(value, list):
            result[key] = [decoder_schema(child) for child in value]
        elif key in _SINGLE:
            result[key] = [decoder_schema(child) for child in value] if isinstance(value, list) else decoder_schema(value)
    return result

def _compatible_grammar(schema, defaults=None, overrides=None):
    value = json.loads(schema) if isinstance(schema, str) else schema
    return _original(share_subschemas(decoder_schema(value)), defaults=defaults, overrides=overrides)
llguidance.LLMatcher.grammar_from_json_schema = staticmethod(_compatible_grammar)
