"""Share repeated JSON subschemas before llguidance compilation.

This runs only in the model service. Request schemas and the evaluation's
prompt hashes stay unchanged. Local $refs retain every validation constraint.
"""
from collections import Counter
import json


_MAPS = {'properties', 'patternProperties', 'dependentSchemas'}
_ARRAYS = {'allOf', 'anyOf', 'oneOf', 'prefixItems'}
_SINGLE = {'items', 'additionalItems', 'additionalProperties', 'contains',
           'propertyNames', 'not', 'if', 'then', 'else', 'unevaluatedItems',
           'unevaluatedProperties', 'contentSchema'}
_SCOPED = {'$id', 'id', '$ref', '$dynamicRef', '$recursiveRef', '$anchor',
           '$dynamicAnchor', '$recursiveAnchor', '$defs', 'definitions', 'x-guidance'}


def share_subschemas(schema):
    """Factor identical schema nodes, preserving property/enum/union order.

    Existing references, IDs and decoder extensions are left alone because
    relocating their nodes could change reference resolution or decoder options.
    Literal data in enum/const/default is never interpreted as a schema.
    """
    if not isinstance(schema, dict):
        return schema
    counts = Counter()
    nodes = {}
    scoped = False

    def children(node, visit):
        result = dict(node)
        for key, value in node.items():
            if key in _MAPS and isinstance(value, dict):
                result[key] = {name: visit(child) for name, child in value.items()}
            elif key in _ARRAYS and isinstance(value, list):
                result[key] = [visit(child) for child in value]
            elif key in _SINGLE:
                result[key] = ([visit(child) for child in value]
                               if isinstance(value, list) else visit(value))
        return result

    def identify(node):
        # Order matters to constrained decoding, even when JSON Schema ignores it.
        return json.dumps(node, ensure_ascii=False, separators=(',', ':'))

    def count(node):
        nonlocal scoped
        if not isinstance(node, dict):
            return node
        scoped = scoped or bool(_SCOPED.intersection(node))
        key = identify(node)
        counts[key] += 1
        nodes[key] = node
        children(node, count)
        return node

    count(schema)
    if scoped:
        return schema
    names = {key: f's{i}' for i, key in enumerate(
        key for key, count in counts.items() if count > 1 and len(key) >= 96)}
    if not names:
        return schema
    definitions = {}

    def replace(node):
        if not isinstance(node, dict):
            return node
        key = identify(node)
        if key in names:
            name = names[key]
            if name not in definitions:
                definitions[name] = children(nodes[key], replace)
            return {'$ref': '#/$defs/' + name}
        return children(node, replace)

    result = children(schema, replace)
    result['$defs'] = definitions
    return result
