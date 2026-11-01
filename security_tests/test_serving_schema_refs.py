"""Serving compaction preserves constraints and constrained-decoder key order."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest


path = Path(__file__).resolve().parents[1] / 'scripts/vllm_schema_support/schema_refs.py'
spec = importlib.util.spec_from_file_location('schema_refs', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
share_subschemas = module.share_subschemas


class ServingSchemaRefs(unittest.TestCase):
    def test_expansion_preserves_schema_and_order_without_mutation(self):
        leaf = {'type': 'string', 'enum': [f'source-{i}' for i in range(30)]}
        schema = {'type': 'object', 'properties': {
            'z': {'type': 'array', 'minItems': 1, 'items': leaf},
            'a': leaf, 'evidence': leaf}, 'required': ['z', 'a'],
            'additionalProperties': False}
        original = deepcopy(schema)
        shared = share_subschemas(schema)

        def expand(node):
            if isinstance(node, dict):
                if set(node) == {'$ref'}:
                    return expand(shared['$defs'][node['$ref'].split('/')[-1]])
                return {k: expand(v) for k, v in node.items() if k != '$defs'}
            if isinstance(node, list):
                return [expand(v) for v in node]
            return node

        self.assertEqual(json.dumps(expand(shared)), json.dumps(original))
        self.assertEqual(schema, original)
        self.assertLess(len(json.dumps(shared)), len(json.dumps(schema)))

    def test_existing_scopes_and_references_are_untouched(self):
        for keyword in ('$id', '$ref', '$defs', 'definitions', 'x-guidance'):
            schema = {'type': 'object', 'properties': {'x': {keyword: 'value'}}}
            self.assertIs(share_subschemas(schema), schema)

    def test_literal_objects_are_not_treated_as_schema_nodes(self):
        literal = {'$ref': 'literal-data', 'properties': {'long': 'x' * 200}}
        schema = {'type': 'object', 'const': literal, 'default': literal,
                  'enum': [literal, literal]}
        self.assertIs(share_subschemas(schema), schema)


if __name__ == '__main__':
    unittest.main()
