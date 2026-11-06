"""Exercise real HTTP response decoding, citation aliases and local validation."""
from copy import deepcopy
from io import BytesIO
import json

import pytest

from agentic_memory.privacy import processors
from agentic_memory.privacy.processors import CompletionProcessor, ProcessorContractFailure


IDS = ['note:permission-view:' + 'a'*64, 'note:permission-view:' + 'b'*64]


def answer_case(version='21'):
    citations = {'type': 'array', 'uniqueItems': True,
                 'items': {'type': 'string', 'enum': IDS}}
    part = {'type': 'object', 'additionalProperties': False, 'properties': {
        'request': {'type': 'string'},
        'status': {'type': 'string', 'enum': ['supported', 'missing']},
        'evidence_ids': deepcopy(citations)},
        'required': ['request', 'status', 'evidence_ids']}
    schema = {'type': 'object', 'additionalProperties': False, 'properties': {
        'action': {'type': 'string', 'enum': ['answer']},
        'answer': {'type': 'string'},
        'request_parts': {'type': 'array', 'items': part},
        'used_record_ids': deepcopy(citations)},
        'required': ['action', 'answer', 'request_parts', 'used_record_ids']}
    payload = {'permission_version': version, 'stage': 'answer',
        'query': 'When and where is my appointment?', 'evidence': [
            {'id': IDS[0], 'text': 'The appointment is Friday at 14:00.'},
            {'id': IDS[1], 'text': 'Bring the referral unless already uploaded.'}]}
    response = {'action': 'answer', 'answer': 'Friday at 14:00. The location is unavailable.',
        'request_parts': [
            {'request': 'When?', 'status': 'supported', 'evidence_ids': ['m1']},
            {'request': 'Where?', 'status': 'missing', 'evidence_ids': []}],
        'used_record_ids': ['m1']}
    return payload, schema, response


def wire_keys(value, contract):
    """Have the fake endpoint obey the numeric property names actually sent."""
    if isinstance(value, list):
        return [wire_keys(v, contract.get('items', {})) for v in value]
    if isinstance(value, dict):
        props = contract.get('properties', {})
        result = {}
        for key, item in value.items():
            name = next((n for n in props if n == key or n.partition('_')[2] == key), key)
            result[name] = wire_keys(item, props.get(name, {}))
        return result
    return value


def endpoint(monkeypatch, response, captured):
    def request(req, timeout):
        body = json.loads(req.data)
        payload = json.loads(body['messages'][1]['content'])
        contract = payload.get('output_contract') or body['response_format']['json_schema']['schema']
        captured.append((body, payload, contract))
        return BytesIO(json.dumps({'model': 'mock', 'choices': [{
            'message': {'content': json.dumps(wire_keys(response, contract))},
            'finish_reason': 'stop'}]}).encode())
    monkeypatch.setattr('urllib.request.urlopen', request)


@pytest.mark.parametrize('version', ['19', '20', '21'])
@pytest.mark.parametrize('mode', ['strict', 'json', 'portable'])
def test_answer_aliases_validate_before_expansion(monkeypatch, version, mode):
    monkeypatch.setenv('AMEM_SOURCE_SCHEMA_MODE', mode)
    payload, schema, response = answer_case(version)
    original_payload, original_schema = deepcopy(payload), deepcopy(schema)
    captured = []
    endpoint(monkeypatch, response, captured)
    result = CompletionProcessor('test:http', 'http://localhost/v1', 'mock').complete(
        'Answer only from supplied evidence.', payload, schema=schema)
    assert result['request_parts'][0]['evidence_ids'] == [IDS[0]]
    assert result['request_parts'][1]['evidence_ids'] == []
    assert result['used_record_ids'] == [IDS[0]]
    assert captured[0][1]['evidence'][0]['id'] == 'm1'
    assert any(k.startswith('001_') for k in captured[0][2]['properties'])
    assert payload == original_payload and schema == original_schema
    assert len(captured) == 1


@pytest.mark.parametrize('field', ['evidence_ids', 'used_record_ids'])
@pytest.mark.parametrize('invalid', ['m999', IDS[0], 'note:unprovided'])
def test_invalid_wire_citations_remain_rejected(monkeypatch, field, invalid):
    monkeypatch.setenv('AMEM_SOURCE_SCHEMA_MODE', 'json')
    payload, schema, response = answer_case()
    if field == 'evidence_ids':
        response['request_parts'][0]['evidence_ids'] = [invalid]
    else:
        response['used_record_ids'] = [invalid]
    captured = []
    endpoint(monkeypatch, response, captured)
    with pytest.raises(ProcessorContractFailure, match='enum'):
        CompletionProcessor('test:http', 'http://localhost/v1', 'mock').complete(
            'Use supplied aliases.', payload, schema=schema)
    assert len(captured) == 1


def test_portable_mode_still_validates_duplicate_citations_locally(monkeypatch):
    monkeypatch.setenv('AMEM_SOURCE_SCHEMA_MODE', 'portable')
    payload, schema, response = answer_case()
    response['used_record_ids'] = ['m1', 'm1']
    captured = []
    endpoint(monkeypatch, response, captured)
    with pytest.raises(ProcessorContractFailure, match='uniqueItems'):
        CompletionProcessor('test:http', 'http://localhost/v1', 'mock').complete(
            'Return citations.', payload, schema=schema)


def test_restored_ids_are_checked_against_original_schema(monkeypatch):
    monkeypatch.setenv('AMEM_SOURCE_SCHEMA_MODE', 'json')
    payload, schema, response = answer_case()
    original = processors.compact_answer_references
    def corrupt_restore(payload, schema):
        wire, contract, restore = original(payload, schema)
        def wrong(value):
            restored = restore(value)
            restored['used_record_ids'] = ['note:unprovided']
            return restored
        return wire, contract, wrong
    monkeypatch.setattr(processors, 'compact_answer_references', corrupt_restore)
    endpoint(monkeypatch, response, [])
    with pytest.raises(ProcessorContractFailure, match='canonical'):
        CompletionProcessor('test:http', 'http://localhost/v1', 'mock').complete(
            'Return citations.', payload, schema=schema)


@pytest.mark.parametrize('mode', ['strict', 'json', 'portable'])
def test_evolution_references_use_the_same_validation_order(monkeypatch, mode):
    monkeypatch.setenv('AMEM_SOURCE_SCHEMA_MODE', mode)
    ref = {'type': 'string', 'enum': [IDS[0]]}
    schema = {'type': 'object', 'additionalProperties': False, 'properties': {
        'updates': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'properties': {'id': ref, 'context': {'type': 'string'}}, 'required': ['id', 'context']}},
        'edges': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'properties': {'from': ref, 'to': ref}, 'required': ['from', 'to']}}},
        'required': ['updates', 'edges']}
    payload = {'permission_version': '21', 'stage': 'evolution',
        'notes': [{'id': IDS[0], 'text': 'The appointment is Friday.'}], 'new_note_ids': [IDS[0]]}
    response = {'updates': [{'id': 'm1', 'context': 'Scheduling.'}],
        'edges': [{'from': 'm1', 'to': 'm1'}]}
    endpoint(monkeypatch, response, [])
    result = CompletionProcessor('test:http', 'http://localhost/v1', 'mock').complete(
        'Propose metadata only.', payload, schema=schema)
    assert result['updates'][0]['id'] == IDS[0]
    assert result['edges'] == [{'from': IDS[0], 'to': IDS[0]}]
