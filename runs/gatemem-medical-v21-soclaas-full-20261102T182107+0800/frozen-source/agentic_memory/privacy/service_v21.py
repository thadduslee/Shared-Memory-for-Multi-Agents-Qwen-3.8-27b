"""Portable authenticated memory service with JSON and WSGI adapters.

Client registration is a TRUSTED host API, not an agent tool. Data-plane
requests never accept a principal, agent, connector or policy authority ID.
The same registry serves heterogeneous clients without granting them host rights.
"""
import json
import secrets
import threading
from .system import AccessDenied, digest
from .permissions import PermissionSourceEvent, PermissionRepairExhausted
from .answering import PermissionQueryService


class _BoundQuery(PermissionQueryService):
    def __init__(self, updater, context):
        super().__init__(updater)
        self.context = context

    def query_context(self, visible):
        self.core._session(self.context, 'read', self.compartment)
        if visible['asker']['principal_id'] != self.context.principal_id:
            raise AccessDenied('Unavailable')
        return self.context


class MemoryService:
    """One registry, multiple authenticated user/agent sessions.

    The deployment host authenticates its callers, then registers their exact
    identity and attenuated actions here. Bearer tokens are opaque capabilities,
    not user-supplied identity claims. TLS/transport hosting is deployment-owned.
    """
    def __init__(self, updater, answer_processor=None):
        if updater.version.split('-', 1)[0] != '21':
            raise ValueError('MemoryService requires an explicitly configured V21 registry')
        self.updater = updater
        self.core = updater.core
        self.answer_processor = answer_processor or updater.processor
        self._clients = {}
        self._request_lock = threading.RLock()

    def register_authenticated_client(self, principal_id, agent_id, *, actions=('search', 'read', 'derive'),
                                      connector=None, expires_in=3600, purpose=''):
        """Host-only authentication adapter. Never expose this as a tool/route."""
        self.core._session(self.updater.ctx, 'manage_policy', self.updater.compartment)
        actions = tuple(actions)
        allowed = {'search', 'read', 'derive', 'ingest', 'write', 'delete', 'audit'}
        if not set(actions) <= allowed:
            raise ValueError('Unsupported data-plane action')
        if connector is not None:
            boot = self.updater.bootstrap
            members = boot.source_identities.get(connector)
            if members is None or (principal_id not in members and connector not in boot.discover_self_owned_participants):
                raise AccessDenied('Unattested connector')
        if 'ingest' in actions and connector is None:
            raise ValueError('Ingestion requires a host-attested connector')
        context = self.core.issue_session(principal_id, agent_id, actions=actions,
            compartments=[self.updater.compartment], expires_in=expires_in, purpose=purpose,
            processing_routes=[self.core.embedder.processor_id, self.answer_processor.processor_id])
        token = secrets.token_urlsafe(32)
        self._clients[token] = (context, connector)
        return token

    def revoke_client(self, token):
        record = self._clients.pop(token, None)
        if record:
            self.core.revoke_session(record[0])

    def dispatch(self, token, operation, payload):
        # A single service instance owns a single updater/processor. Serialize
        # requests without a deadline; SQLite still validates publication and
        # release against live state. Distributed hosting is a separate concern.
        with self._request_lock:
            return self._dispatch(token, operation, payload)

    def _dispatch(self, token, operation, payload):
        record = self._clients.get(token)
        if record is None:
            raise AccessDenied('Unavailable')
        context, connector = record
        required = {
            'ingest': ('ingest', {'event_id', 'text'}),
            'search': ('search', {'query'}), 'read': ('read', {'resource_id'}),
            'answer': ('read', {'query'}), 'delete': ('delete', {'resource_id'}),
            'supersede': ('write', {'old_id', 'new_id'}), 'diagnostics': ('audit', set()),
        }
        if operation not in required:
            raise ValueError('Unsupported memory operation')
        action, fields = required[operation]
        self.core._session(context, action, self.updater.compartment)
        if not isinstance(payload, dict) or set(payload) != fields or any(
                not isinstance(v, str) or not v.strip() or len(v) > 100000 for v in payload.values()):
            raise ValueError('Invalid operation payload')
        if operation == 'ingest':
            # Authenticated speaker, connector and observed time are host values.
            # Scope idempotency keys to the authenticated person/connector, not
            # the caller's agent. A second platform can retry the SAME receipt.
            event_id = 'service-event:' + digest([context.principal_id, connector, payload['event_id']])
            source_id = 'permission-source:' + digest([self.updater.compartment, event_id])
            prior = self.core.db.execute('SELECT payload FROM resources WHERE id=?', (source_id,)).fetchone()
            if prior:
                original = json.loads(prior[0])
                source = original.get('source', {})
                if (original.get('text') != payload['text'] or source.get('participant') != context.principal_id
                        or source.get('source_identity') != connector):
                    raise ValueError('Immutable event replay conflict')
                occurred, origin = source['occurred_at'], source['context']
            else:
                occurred, origin = self.core.clock(), {'agent_id': context.agent_id}
            event = PermissionSourceEvent(event_id, context.principal_id, connector,
                payload['text'], occurred_at=occurred, context=origin)
            job = self.updater.ingest(event)
            try:
                result = self.updater.update(job['job_id'])
            except PermissionRepairExhausted:
                self.updater.skip_exhausted(job['job_id'], max_attempts=2)
                return {'status': 'pending'}
            return {'status': result['status']}
        if operation == 'search':
            packet = self.core.retrieve(context, payload['query'], k=24)
            # Recheck the full graph/input lineage at the release boundary.
            return self.core.release(context, packet['input_ids'],
                [{'id': r['id'], 'text': r['text']} for r in packet['evidence']])
        if operation == 'read':
            view = self.core.read(context, payload['resource_id'])
            return self.core.release(context, [view['id']], {'id': view['id'], 'payload': view['payload']})
        if operation == 'answer':
            adapter = _BoundQuery(self.updater, context)
            result = adapter.answer(context.principal_id, payload['query'], self.answer_processor)
            # Control-plane manifests, diagnoses and queues never leave here.
            return result['output']
        if operation == 'delete':
            self.core.delete(context, payload['resource_id'])
            return {'status': 'deleted'}
        if operation == 'supersede':
            self.core.supersede(context, payload['old_id'], payload['new_id'])
            return {'status': 'superseded'}
        from .diagnostics_v21 import diagnose
        return diagnose(self.updater)

    def wsgi_app(self, environ, start_response):
        """Small transport adapter; caller identities cannot be set in JSON."""
        status = '200 OK'
        try:
            if environ.get('REQUEST_METHOD') != 'POST':
                raise ValueError('POST required')
            auth = environ.get('HTTP_AUTHORIZATION', '')
            if not auth.startswith('Bearer '):
                raise AccessDenied('Unavailable')
            length = int(environ.get('CONTENT_LENGTH') or 0)
            if not 0 < length <= 131072:
                raise ValueError('Invalid request size')
            request = json.loads(environ['wsgi.input'].read(length))
            if not isinstance(request, dict) or set(request) != {'operation', 'payload'}:
                raise ValueError('Invalid request')
            output = self.dispatch(auth[7:], request['operation'], request['payload'])
        except AccessDenied:
            status, output = '403 Forbidden', {'error': 'Unavailable'}
        except (ValueError, TypeError, KeyError):
            status, output = '400 Bad Request', {'error': 'Invalid request'}
        except Exception:
            # Provider/host failures are visible failures, not fabricated answers
            # or permission to advance an event. Details stay in the host ledger.
            status, output = '503 Service Unavailable', {'error': 'Processing failed; retry the same event'}
        body = json.dumps(output, ensure_ascii=False).encode('utf-8')
        start_response(status, [('Content-Type', 'application/json'), ('Content-Length', str(len(body)))])
        return [body]


class JSONClient:
    """Framework-neutral client, usable with in-process or HTTP transports."""
    def __init__(self, token, transport):
        self.token, self.transport = token, transport

    def request(self, operation, **payload):
        return self.transport(self.token, {'operation': operation, 'payload': payload})


def in_process_transport(service):
    return lambda token, request: service.dispatch(token, request['operation'], request['payload'])
