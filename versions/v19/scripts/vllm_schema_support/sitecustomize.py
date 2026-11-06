"""Serving-only llguidance compatibility hook, loaded by the model launcher."""
import json
import llguidance

from schema_refs import share_subschemas


_original = llguidance.LLMatcher.grammar_from_json_schema


def _shared_grammar(schema, defaults=None, overrides=None):
    value = json.loads(schema) if isinstance(schema, str) else schema
    return _original(share_subschemas(value), defaults=defaults, overrides=overrides)


llguidance.LLMatcher.grammar_from_json_schema = staticmethod(_shared_grammar)
