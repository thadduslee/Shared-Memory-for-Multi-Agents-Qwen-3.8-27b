"""Explicit processors. Network models and local embeddings are separate routes."""
from __future__ import annotations

import hashlib
import json
import math
import os
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
    space_id = 'test-hash-v1'

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
        self.model = SentenceTransformer(model_path or self.space_id,
                                         device='cpu', local_files_only=True)

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

    def complete(self, system, payload):
        self.calls.append(payload)
        return self.response
