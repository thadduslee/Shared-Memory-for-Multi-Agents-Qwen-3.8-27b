"""Two client platforms, one V21 registry. Requires an explicitly chosen model.

python -m examples.portable_memory_v21 --store /tmp/fresh-v21.sqlite \
  --base-url http://127.0.0.1:19000/v1 --model qwen3.8:27b

The host authenticates Alice/Bob before registration. No client can submit an
identity claim in its JSON operation. This example does not start a server.
"""
import argparse
from io import BytesIO
import json
from pathlib import Path

from agentic_memory.privacy import (
    PrivacyMemorySystem, PermissionBootstrap, PermissionUpdater,
    MemoryService, JSONClient, in_process_transport,
)
from agentic_memory.privacy.processors import HashEmbedder, CompletionProcessor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    if args.store.exists() or args.store.with_suffix('.privacy.sqlite').exists():
        parser.error('Use a fresh registry; existing memory is never overwritten')
    embedder = HashEmbedder()
    model = CompletionProcessor('service:reasoner', args.base_url, args.model, max_tokens=2048)
    with_registry = PrivacyMemorySystem(args.store, embedder)
    try:
        core = with_registry
        host = core.issue_session('authenticated-source-host', 'ingest',
            actions=['manage_policy','ingest','read','search','derive','audit','write','delete','link'],
            compartments=['project'], processing_routes=[embedder.processor_id, model.processor_id])
        core.register_processor(host, model)
        bootstrap = PermissionBootstrap(participants={}, resources={}, authorities={},
            source_identities={'authenticated-chat': []}, discover_self_owned_participants=('authenticated-chat',),
            owner_read_actions=('search','read','derive'), read_pipeline_actions=('search','derive'),
            task_convention='Authenticated people own only their attributed personal information. Other readers need their own explicit or accepted-service grant. Roles and names grant nothing.')
        updater = PermissionUpdater(core, host, 'project', bootstrap, model, version='21')
        service = MemoryService(updater)
        alice_a = service.register_authenticated_client('alice', 'platform-a',
            actions=['ingest','search','read','derive'], connector='authenticated-chat')
        alice_b = service.register_authenticated_client('alice', 'platform-b')
        bob = service.register_authenticated_client('bob', 'platform-c')
        a = JSONClient(alice_a, in_process_transport(service))
        # Platform B exercises the same boundary using the JSON/WSGI transport.
        def wsgi_transport(token, request):
            body = json.dumps(request).encode()
            statuses = []
            result = service.wsgi_app({'REQUEST_METHOD':'POST', 'HTTP_AUTHORIZATION':'Bearer '+token,
                'CONTENT_LENGTH':str(len(body)), 'wsgi.input':BytesIO(body)},
                lambda status, headers: statuses.append(status))
            if statuses != ['200 OK']:
                raise RuntimeError('Service request failed: '+statuses[0])
            return json.loads(b''.join(result))
        b = JSONClient(alice_b, wsgi_transport)
        print(a.request('ingest', event_id='alice-1', text='My project meeting is Tuesday at 14:00 in room Cedar.'))
        print('Alice on platform B:', b.request('search', query='project meeting time and room'))
        print('Bob:', service.dispatch(bob, 'search', {'query':'Alice project meeting'}))
    finally:
        with_registry.close()


if __name__ == '__main__':
    main()
