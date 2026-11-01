"""Explicit processors. Network models and local embeddings are separate routes."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass
from copy import deepcopy


class ProcessorFailure(RuntimeError):
    retryable = True


class ModelContractError(ValueError):
    """A model returned data that failed its host-validated response contract."""


class ProcessorContractFailure(ProcessorFailure, ModelContractError):
    """A successful request returned unusable output; bounded semantic retries."""


class ProcessorConfigurationFailure(ProcessorFailure):
    """Repeating a permanent HTTP failure cannot repair the request/configuration."""
    retryable = False


def compact_note_references(payload,schema):
    """Serialize evolution references compactly, before the core issues a ticket."""
    aliases={n['id']:f'm{i}' for i,n in enumerate(payload['notes'],1)}
    originals={v:k for k,v in aliases.items()}
    def refs(value,mapping,key=None):
        if key in {'id','from','to'} and isinstance(value,str):return mapping.get(value,value)
        if key=='new_note_ids':return [mapping.get(v,v) for v in value]
        if isinstance(value,dict):return {k:refs(v,mapping,k) for k,v in value.items()}
        if isinstance(value,list):return [refs(v,mapping) for v in value]
        return value
    def schema_refs(value):
        if isinstance(value,dict):return {k:schema_refs(v) for k,v in value.items()}
        if isinstance(value,list):return [schema_refs(v) for v in value]
        if isinstance(value,str):return aliases.get(value,value)
        return value
    return refs(payload,aliases),schema_refs(schema),lambda result:refs(result,originals)


def compact_answer_references(payload,schema):
    """Short citation handles; source bytes and answer content stay untouched."""
    aliases={r['id']:f'm{i}' for i,r in enumerate(payload['evidence'],1)}
    originals={v:k for k,v in aliases.items()}
    wire=deepcopy(payload);contract=deepcopy(schema)
    for record in wire['evidence']:record['id']=aliases[record['id']]
    for part in wire.get('assessed_request_parts',[]):
        part['evidence_ids']=[aliases.get(rid,rid) for rid in part['evidence_ids']]
    citations=contract['properties']['used_record_ids']['items']
    if 'enum' in citations:citations['enum']=[aliases.get(rid,rid) for rid in citations['enum']]
    parts=contract['properties'].get('request_parts',{}).get('items',{}).get('properties',{})
    if 'evidence_ids' in parts:
        choices=parts['evidence_ids']['items']
        if 'enum' in choices:choices['enum']=[aliases.get(rid,rid) for rid in choices['enum']]
    def restore(result):
        value=deepcopy(result)
        if isinstance(value.get('used_record_ids'),list):
            value['used_record_ids']=[originals.get(rid,rid) for rid in value['used_record_ids']]
        for part in value.get('request_parts',[]):
            if isinstance(part,dict) and isinstance(part.get('evidence_ids'),list):
                part['evidence_ids']=[originals.get(rid,rid) for rid in part['evidence_ids']]
        return value
    return wire,contract,restore


def parse_object(text):
    if not isinstance(text,str):
        raise ProcessorContractFailure('Processor did not return structured text')
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ProcessorContractFailure('Processor did not return an object')
    return value


def ordered_response_keys(schema):
    """Keep declared structured-output order when a provider sorts object keys.

    This changes wire property names only. IDs, enums, source bytes, schema
    meanings and returned values remain unchanged; the host sees canonical keys.
    Used only for the closed v16 source/answer contracts.
    """
    aliases={}
    def encode(node):
        if isinstance(node,list):return [encode(v) for v in node]
        if not isinstance(node,dict):return deepcopy(node)
        props=node.get('properties')
        names={k:f'{i:03d}_{k}' for i,k in enumerate(props,1)} if isinstance(props,dict) else {}
        aliases.update({v:k for k,v in names.items()})
        result={}
        for key,value in node.items():
            if key=='properties' and names:
                result[key]={names[k]:encode(v) for k,v in value.items()}
            elif key=='required' and names:
                result[key]=[names.get(k,k) for k in value]
            elif key in {'enum','const','default','examples'}:
                result[key]=deepcopy(value)
            else:result[key]=encode(value)
        return result
    wire=encode(schema)
    def restore(value):
        if isinstance(value,list):return [restore(v) for v in value]
        if not isinstance(value,dict):return value
        result={}
        for key,item in value.items():
            canonical=aliases.get(key,key)
            if canonical in result:
                raise ProcessorContractFailure('Conflicting structured response keys')
            result[canonical]=restore(item)
        return result
    return wire,restore


class HashEmbedder:
    """Deterministic test double, explicitly excluded from real evaluation."""
    processor_id = 'test:hash'
    space_id = 'test-hash-v1:sha256-word-lower:128:l2'
    fingerprint = {'model': 'test-hash', 'revision': 'v1', 'dimensions': 128, 'preprocessing': 'unicode-word-lower:l2'}

    def encode(self, texts):
        result = []
        for text in texts:
            vector = [0.0] * 128
            for word in re.findall(r'\w+', text.lower()):
                digest = hashlib.sha256(word.encode()).digest()
                vector[int.from_bytes(digest[:2], 'big') % 128] += 1 if digest[2] % 2 else -1
            length = math.sqrt(sum(x*x for x in vector)) or 1
            result.append([x / length for x in vector])
        return result


class LocalEmbedder:
    processor_id = 'local:minilm'
    space_id = 'sentence-transformers/all-MiniLM-L6-v2'

    def __init__(self, model_path=None):
        # Import lazily: the core and security suite have no ML dependency.
        os.environ.setdefault('HF_HUB_OFFLINE', '1')
        os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')
        from sentence_transformers import SentenceTransformer
        from huggingface_hub import snapshot_download
        candidate = Path(model_path or '')
        snapshot = candidate.resolve() if model_path and candidate.is_dir() else Path(
            snapshot_download(model_path or self.space_id, local_files_only=True)).resolve()
        # Fingerprint actual weights/tokenizer/config bytes, not just dimensions
        # or a mutable model name. This is intentionally paid once at startup.
        files = {}
        for file in sorted(snapshot.rglob('*')):
            if file.is_file() and file.suffix in {'.json', '.txt', '.safetensors', '.bin', '.model'}:
                checksum = hashlib.sha256()
                with file.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1024*1024), b''):
                        checksum.update(chunk)
                files[str(file.relative_to(snapshot))] = checksum.hexdigest()
        if not files: raise ValueError('No local embedding model files')
        self.model = SentenceTransformer(str(snapshot), device='cpu', local_files_only=True)
        import importlib.metadata
        self.fingerprint = {'model': 'sentence-transformers/all-MiniLM-L6-v2',
            'revision': snapshot.name, 'files': files,
            'dimensions': self.model.get_sentence_embedding_dimension(),
            'preprocessing': {'normalize_embeddings': True, 'max_seq_length': self.model.max_seq_length},
            'implementation': {p: importlib.metadata.version(p) for p in
                               ('sentence-transformers', 'transformers', 'tokenizers', 'torch')}}
        self.space_id = 'embedding:' + hashlib.sha256(json.dumps(self.fingerprint, sort_keys=True).encode()).hexdigest()

    def encode(self, texts):
        return self.model.encode(list(texts), normalize_embeddings=True,
                                 show_progress_bar=False).tolist()


def openrouter_routing_preferences():
    """Explicit, reproducible transport preferences; never a model fallback."""
    provider_sort=os.getenv('AMEM_OPENROUTER_PROVIDER_SORT','latency').strip().lower()
    if provider_sort not in {'latency','throughput','price'}:
        raise ProcessorConfigurationFailure('AMEM_OPENROUTER_PROVIDER_SORT must be latency, throughput, or price')
    preferences={'require_parameters':True,'sort':provider_sort}
    ignored=list(dict.fromkeys(p.strip().lower() for p in
        os.getenv('AMEM_OPENROUTER_PROVIDER_IGNORE','').split(',') if p.strip()))
    if ignored:preferences['ignore']=ignored
    ordered=list(dict.fromkeys(p.strip() for p in os.getenv('AMEM_OPENROUTER_PROVIDER_ORDER','').split(',') if p.strip()))
    if ordered:
        preferences['order']=ordered
        preferences['allow_fallbacks']=True
    return preferences


@dataclass
class CompletionProcessor:
    processor_id: str
    base_url: str
    model: str
    api_key: str = ''
    timeout: float = 300
    max_tokens: int = 8192
    usage: dict | None = None
    compact_permission_input: bool = True
    require_permission_coverage: bool = True
    semantic_context_review: bool = True
    require_answer_plan: bool = True
    last_elapsed_seconds: float | None = None
    last_response_model: str | None = None
    last_provider: str | None = None
    last_wire_key_ordering: bool = False

    def complete(self, system, payload, *, schema=None):
        self.usage=None
        self.last_elapsed_seconds=None
        self.last_response_model=None
        self.last_provider=None
        self.last_wire_key_ordering=False
        call_started=time.monotonic()
        restore=lambda value:value
        if (schema and set(schema.get('properties',{}))=={'updates','edges'}
                and isinstance(payload.get('notes'),list)):
            payload,schema,restore=compact_note_references(payload,schema)
        if (schema and 'used_record_ids' in schema.get('properties',{})
                and isinstance(payload.get('evidence'),list)):
            payload,schema,restore=compact_answer_references(payload,schema)
        output_limit=self.max_tokens
        if schema and set(schema.get('properties',{})) in ({'changed','operations'}, {'permission_changes','new_facts'},
                {'control_review','permission_changes','segments','binding_reviews'},{'c','p','f','b'},
                {'review','c','p','f','b'},{'b'}):
            try:output_limit=min(output_limit,int(os.getenv('AMEM_PERMISSION_MAX_TOKENS','3072')))
            except ValueError:raise ProcessorConfigurationFailure('AMEM_PERMISSION_MAX_TOKENS must be a positive integer') from None
            if output_limit<=0:raise ProcessorConfigurationFailure('AMEM_PERMISSION_MAX_TOKENS must be a positive integer')
        if schema and set(schema.get('properties',{})) in ({'c','p','f','b'},{'review','c','p','f','b'},{'b'}):
            family=str(payload.get('permission_version','15')).split('-',1)[0]
            default_limit='2048' if family.isdigit() and int(family)>=16 else '1024'
            if family.isdigit() and int(family)>=17 and set(schema.get('properties',{}))=={'b'}:
                default_limit='3072'
            try:incremental_limit=int(os.getenv('AMEM_INCREMENTAL_MAX_TOKENS',default_limit))
            except ValueError:raise ProcessorConfigurationFailure('AMEM_INCREMENTAL_MAX_TOKENS must be a positive integer') from None
            if incremental_limit<=0:raise ProcessorConfigurationFailure('AMEM_INCREMENTAL_MAX_TOKENS must be a positive integer')
            output_limit=min(output_limit,incremental_limit)
        body = {'model': self.model,
                'messages': [{'role': 'system', 'content': system},
                             {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}],
                'temperature': 0, 'max_tokens': output_limit,
                'response_format': {'type': 'json_object'}}
        if schema is not None:
            body['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': 'processor_response', 'strict': True, 'schema': schema}}
            family=str(payload.get('permission_version','')).split('-',1)[0]
            if ((family.isdigit() and int(family)>=16) or
                    'evidence_roles' in schema.get('properties',{})):
                wire_schema,restore_keys=ordered_response_keys(schema)
                body['response_format']['json_schema']['schema']=wire_schema
                restore_values=restore
                restore=lambda value:restore_values(restore_keys(value))
                body['messages'][0]['content'] += (
                    '\nUse the exact output property names declared in the JSON schema. '
                    'Their numeric prefixes preserve the required order; after each prefix, '
                    'the name has the meaning used above. Prefixes are wire bookkeeping only. '
                    'Do not prefix source IDs, enum values or quoted text.')
                self.last_wire_key_ordering=True
        family=str(payload.get('permission_version','')).split('-',1)[0]
        local_schema=bool(schema and family.isdigit() and int(family)>=17)
        if local_schema:
            mode=os.getenv('AMEM_SOURCE_SCHEMA_MODE','strict').strip().lower()
            if mode not in {'strict','json'}:
                raise ProcessorConfigurationFailure('AMEM_SOURCE_SCHEMA_MODE must be strict or json')
            if mode=='json':
                # The provider supplies JSON; the SAME full grammar is checked
                # locally before canonical authority validation/publication.
                contract=body['response_format']['json_schema']['schema']
                body['response_format']={'type':'json_object'}
                body['messages'][0]['content']+='\nReturn JSON conforming EXACTLY to output_contract; no additional properties.'
                body['messages'][1]['content']=json.dumps({**payload,'output_contract':contract},ensure_ascii=False,separators=(',',':'))
        hostname=urlsplit(self.base_url).hostname or ''
        openrouter=hostname=='openrouter.ai' or hostname.endswith('.openrouter.ai')
        if openrouter:
            body['provider']=openrouter_routing_preferences()
        if 'qwen' in self.model.lower():
            if openrouter:body['reasoning']={'enabled':False}
            else:body['chat_template_kwargs'] = {'enable_thinking': False}
        if self.model.startswith('deepseek/'):
            body['reasoning'] = {'effort': 'low'}
        request = urllib.request.Request(self.base_url.rstrip('/') + '/chat/completions',
                                         data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json',
                                                  'Authorization': 'Bearer ' + self.api_key})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    value = json.load(response)
                self.usage = value.get('usage', {})
                self.last_response_model=value.get('model')
                self.last_provider=value.get('provider')
                choices=value.get('choices') or []
                content=choices[0].get('message',{}).get('content') if choices else None
                refusal=choices[0].get('message',{}).get('refusal') if choices else None
                if content is None and not self.usage and not refusal:
                    raise ProcessorFailure(f'{self.processor_id}: provider returned no completion')
                if openrouter and self.last_response_model!=self.model:
                    raise ProcessorConfigurationFailure(f'{self.processor_id}: unexpected response model; expected {self.model}')
                self.last_elapsed_seconds=time.monotonic()-call_started
                if value['choices'][0].get('finish_reason')=='length':
                    raise ProcessorContractFailure(f'{self.processor_id}: structured response exceeded token budget')
                result=restore(parse_object(value['choices'][0]['message']['content']))
                if local_schema:
                    if int(family)>=18:
                        # These are diagnostic annotations, never field scopes,
                        # recipient IDs, consent quotes, operations or verdicts.
                        # A verbose explanation must not quarantine controls.
                        annotations=[(result,'review',schema.get('properties',{}).get('review',{}))]
                        for group,field in [('c','r'),('f','attribution_reason')]:
                            if not isinstance(result.get(group),dict):continue
                            slots=schema.get('properties',{}).get(group,{}).get('properties',{})
                            annotations.extend((row,field,slots.get(key,{}).get('properties',{}).get(field,{}))
                                for key,row in result[group].items() if isinstance(row,dict))
                        for row,key,contract in annotations:
                            limit=contract.get('maxLength')
                            if isinstance(row.get(key),str) and isinstance(limit,int):
                                row[key]=row[key][:limit]
                    # Nonfacts have no subject, source selector or replacement
                    # relationship. JSON-only providers often emit those unused
                    # fields anyway; discarding them cannot create any fact or
                    # authority. Unknown fields still fail the full grammar.
                    if isinstance(result.get('f'),dict):
                        for key,row in result['f'].items():
                            slot=schema.get('properties',{}).get('f',{}).get('properties',{}).get(key,{})
                            if set(slot.get('properties',{})) not in ({'k'},{'kind'}):
                                continue
                            if (isinstance(row,dict) and row.get('k') in {'question','context'} and
                                    set(row)<={'k','s','e','r','t','x'}):
                                result['f'][key]={'k':row['k']}
                            elif (isinstance(row,dict) and row.get('kind') in {'question','context'} and
                                    set(row)<={'kind','information_subject','attribution_source','attribution_reason',
                                        'topics','relationships','subject_basis'}):
                                result['f'][key]={'kind':row['kind']}
                    from jsonschema import Draft202012Validator, ValidationError
                    try:Draft202012Validator(schema).validate(result)
                    except ValidationError as exc:
                        errors=exc.context or [exc]
                        variants=exc.schema.get('anyOf',[])
                        if variants and isinstance(exc.instance,dict):
                            discriminator=next((key for key in ('o','k','kind') if key in exc.instance),None)
                            matching=[variant for variant in variants if discriminator and exc.instance[discriminator] in
                                variant.get('properties',{}).get(discriminator,{}).get('enum',[])]
                            if matching:
                                closest=min((list(Draft202012Validator(v).iter_errors(exc.instance)) for v in matching),key=len)
                                errors=closest or errors
                        issues=[]
                        for error in errors[:8]:
                            path='/'.join(map(str,error.absolute_path))
                            detail=error.validator
                            if error.validator=='additionalProperties':detail+='; allowed keys='+','.join(error.schema.get('properties',{}))
                            if error.validator=='required':detail+='; required keys='+','.join(error.schema.get('required',[]))
                            if error.validator=='enum' and path.rsplit('/',1)[-1] in {'k','intent','d','via','timing','kind'}:
                                detail+='; allowed='+','.join(map(str,error.schema['enum']))
                            issues.append(path+': '+detail)
                        failure=ProcessorContractFailure('Structured output failed local schema validation: '+' | '.join(issues))
                        # Protected repair input only. This draft has not passed
                        # the grammar and can never be published or executed.
                        failure.details={'schema_path':list(exc.absolute_path),'rejected_output':result}
                        raise failure from None
                return result
            except ProcessorContractFailure:
                raise
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    # The body often identifies compiler/context limits that a
                    # bare status code hides. Keep diagnostics bounded.
                    detail=''
                    try:
                        error=json.loads(exc.read(16384)).get('error',{})
                        if isinstance(error,dict):detail=str(error.get('message',''))[:1000]
                    except (ValueError,AttributeError,OSError):pass
                    failure=ProcessorFailure if exc.code in {429,500,502,503,504} else ProcessorConfigurationFailure
                    raise failure(f'{self.processor_id}: HTTP {exc.code}'+
                                  (': '+detail if detail else '')) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == 2:
                    raise ProcessorFailure(f'{self.processor_id}: transport {type(exc).__name__}') from None
            except (ValueError, KeyError, IndexError, TypeError):
                raise ProcessorContractFailure(f'{self.processor_id}: invalid structured response') from None
            finally:
                self.last_elapsed_seconds=time.monotonic()-call_started
            time.sleep(min(2 ** attempt, 4))


class ScriptedProcessor:
    """Test processor; never selected implicitly by the real runner."""
    processor_id = 'test:completion'

    def __init__(self, response=None):
        self.response = response or {'updates': [], 'edges': []}
        self.calls = []

    def complete(self, system, payload, *, schema=None):
        self.calls.append(payload)
        return self.response
