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
from dataclasses import dataclass


class ProcessorFailure(RuntimeError):
    pass


def parse_object(text):
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ProcessorFailure('Processor did not return an object')
    return value


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


@dataclass
class CompletionProcessor:
    processor_id: str
    base_url: str
    model: str
    api_key: str = ''
    timeout: float = 300
    max_tokens: int = 8192
    usage: dict | None = None

    def complete(self, system, payload, *, schema=None):
        body = {'model': self.model,
                'messages': [{'role': 'system', 'content': system},
                             {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
                'temperature': 0, 'max_tokens': self.max_tokens,
                'response_format': {'type': 'json_object'}}
        if schema is not None:
            body['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': 'processor_response', 'strict': True, 'schema': schema}}
        if 'qwen' in self.model.lower():
            body['chat_template_kwargs'] = {'enable_thinking': False}
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
                return parse_object(value['choices'][0]['message']['content'])
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise ProcessorFailure(f'{self.processor_id}: HTTP {exc.code}') from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == 2:
                    raise ProcessorFailure(f'{self.processor_id}: transport {type(exc).__name__}') from None
            except (ValueError, KeyError, IndexError, TypeError):
                if attempt == 2:
                    raise ProcessorFailure(f'{self.processor_id}: invalid structured response') from None
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
