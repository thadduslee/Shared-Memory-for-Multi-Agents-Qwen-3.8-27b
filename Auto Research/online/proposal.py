"""Shared proposal wire schema and content-free, actionable shape diagnostics."""
from copy import deepcopy

CONTRACT_VERSION = 'online-proposal-v2'

PROPOSAL_SCHEMA = {
    'type': 'object',
    'required': ['decision', 'failure_classification', 'repair_component', 'diagnosis',
                 'specific_hypothesis', 'focused_goal', 'repair_scope', 'work_order',
                 'safety_preservation_checks', 'trace_citations',
                 'addressed_decision_rule', 'predicted_replay_change'],
    'properties': {
        'contract_version': {'type': 'string'},
        'incident_id': {'type': 'string'},
        'input_examples': {'type':'array','maxItems':3,'items':{'type':'object','properties':{
            'source_turn_id':{'type':'string'},'json':{'type':'string'},'source_digest':{'type':'string'},
            'truncated':{'type':'boolean'}}}},
        **{name: {'type': 'string', 'minLength': 1} for name in (
            'decision', 'failure_classification', 'repair_component', 'diagnosis',
            'specific_hypothesis', 'focused_goal')},
        'repair_scope': {'type': 'object', 'required': ['subsystem', 'file', 'function'],
                         'properties': {k: {'type': 'string', 'minLength': 1}
                                        for k in ('subsystem', 'file', 'function')}},
        'work_order': {'type': 'array', 'minItems': 1, 'maxItems': 12,
                       'items': {'type': 'string', 'minLength': 1}},
        'safety_preservation_checks': {'type': 'array', 'minItems': 1,
                                        'items': {'type': 'string', 'minLength': 1}},
        'intended_target_ids': {'type': 'array', 'minItems': 0,
                                'items': {'type': 'string', 'minLength': 1}},
        'trace_citations': {'type': 'array', 'minItems': 1,
                             'items': {'type': 'string', 'minLength': 1}},
        'addressed_decision_rule': {'type': 'string', 'minLength': 1},
        'failure_mechanism': {'type': 'string'},
        'change_type': {'type': 'string'},
        'confirmed_observations': {'type': 'array', 'items': {'type': 'object',
            'properties': {'observation': {'type': 'string'},
                           'observable': {'type': 'string'}, 'observed_value': {'type': 'string'},
                           'evidence_references': {'type': 'array', 'items': {'type': 'string'}}}}},
        'alternatives': {'type': 'array', 'items': {'type': 'string'}},
        'uncertainties': {'type': 'array', 'items': {'type': 'string'}},
        'acceptance_checks': {'type': 'array', 'items': {'type': 'string'}},
        'schema_changes': {'type': 'array', 'items': {'type': 'object', 'properties': {
            k: {'type': 'string'} for k in ('kind', 'name', 'reason', 'expected_benefit', 'rollback')}}},
        'requires_reingestion': {'type': 'boolean'},
        'requires_migration': {'type': 'boolean'},
        'if_cause_unconfirmed': {'type': 'string'},
        'diagnostic_request': {'type': 'string'},
        'escalation_reason': {'type': 'string'},
        'predicted_replay_change': {'type': 'object', 'required': ['observable', 'expected_change'],
            'properties': {k: {'type': 'string', 'minLength': 1}
                           for k in ('observable', 'expected_change')}},
        'migration_sql': {'type': 'string'},
        'staged_repair_plan': {'type': 'array', 'minItems': 1, 'maxItems': 3,
            'items': {'type': 'object', 'required': ['component', 'hypothesis', 'verification_gate'],
                      'properties': {k: {'type': 'string', 'minLength': 1} for k in
                                     ('component', 'hypothesis', 'verification_gate')}}},
    },
}


def normalize_proposal(value):
    if not isinstance(value, dict):
        return value
    value = deepcopy(value)
    value.setdefault('contract_version', CONTRACT_VERSION)
    if not value.get('repair_component'):
        kind = value.get('failure_classification')
        if kind in {'INGESTION', 'AUTHORIZATION', 'RETRIEVAL', 'FACT_SELECTION'}:
            value['repair_component'] = 'memory'
        elif kind == 'RENDERING':
            value['repair_component'] = 'renderer'
    work = value.get('work_order')
    if isinstance(work, str) and work.strip():
        value['work_order'] = [work.strip()]
    checks = value.get('safety_preservation_checks')
    if isinstance(checks, dict) and checks and all(
            isinstance(k, str) and isinstance(v, str) and v.strip()
            for k, v in checks.items()):
        value['safety_preservation_checks'] = [f'{k}: {v}' for k, v in sorted(checks.items())]
    return value


def shape_errors(value, *, require_targets=False):
    """Never echo received values (which could contain hidden or denied text)."""
    errors = []
    types = {'object': dict, 'array': list, 'string': str, 'boolean': bool}

    def check(item, schema, path, missing=False):
        expected = schema['type']
        received = 'missing' if missing else (
            'null' if item is None else
            next((k for k, t in types.items() if isinstance(item, t)), type(item).__name__))
        guidance = None
        if missing or not isinstance(item, types[expected]):
            guidance = f'Return {path} as {expected}; preserve the causal hypothesis.'
        elif expected == 'string' and len(item.strip()) < schema.get('minLength', 0):
            guidance = f'Provide a non-empty string at {path}.'
        elif expected == 'array':
            if not schema.get('minItems', 0) <= len(item) <= schema.get('maxItems', float('inf')):
                guidance = f'Provide {schema.get("minItems", 0)} to {schema.get("maxItems", "unbounded")} items at {path}.'
            for i, child in enumerate(item):
                check(child, schema.get('items', {'type': 'string'}), f'{path}[{i}]')
        elif expected == 'object':
            required = set(schema.get('required', []))
            if path == '$' and require_targets:
                required.add('intended_target_ids')
            for key, child_schema in schema.get('properties', {}).items():
                if key in item or key in required:
                    if path == '$' and key == 'intended_target_ids' and require_targets:
                        child_schema = {**child_schema, 'minItems': 1}
                    check(item.get(key), child_schema, f'{path}.{key}', key not in item)
        if guidance:
            errors.append({'field_path': path, 'expected_type': expected,
                           'received_type': received, 'guidance': guidance})
    if isinstance(value, dict) and value.get('contract_version', CONTRACT_VERSION) != CONTRACT_VERSION:
        errors.append({'field_path': '$.contract_version', 'expected_type': 'string',
                       'received_type': 'string', 'guidance': f'Use {CONTRACT_VERSION}.'})
    if isinstance(value, dict) and value.get('decision') in {'REQUEST_DIAGNOSTIC', 'INSUFFICIENT_EVIDENCE', 'ESCALATE'}:
        check(value, {'type': 'object', 'required': ['decision', 'diagnosis'],
                      'properties': PROPOSAL_SCHEMA['properties']}, '$')
    else:
        check(value, PROPOSAL_SCHEMA, '$')
    return errors[:16]


def proposal_projection(value):
    """Only schema-declared fields may be returned for wire correction."""
    def project(item, schema):
        kind = schema['type']
        if kind == 'object':
            if not isinstance(item, dict):
                return None
            return {k: project(item[k], sub) for k, sub in schema.get('properties', {}).items()
                    if k in item}
        if kind == 'array':
            if not isinstance(item, list):
                return None
            return [project(x, schema.get('items', {'type': 'string'})) for x in item[:16]]
        if kind == 'boolean':
            return item if isinstance(item, bool) else None
        return item[:4000] if isinstance(item, str) else None
    return project(value, PROPOSAL_SCHEMA)
