"""Authenticated host-only JIT task admission. Never invoked by raw dialogue.

HMAC authenticates an external dispatcher; the issuer policy independently
authorizes its tenant, subjects, recipients, actions, task types and lifetime.
Keys stay in the host process, outside prompts, receipts and persisted policy.
"""
from copy import deepcopy
import hashlib
import hmac
import json

from .system import AccessDenied, digest, encoded, instant
from . import task_contracts_v19 as contracts

OPERATIONS = {'assign', 'reassign', 'handoff', 'complete', 'expire', 'revoke', 'cancel'}
TERMINAL = {'complete': 'COMPLETED', 'expire': 'EXPIRED', 'revoke': 'REVOKED', 'cancel': 'CANCELLED'}
EVENT_FIELDS = {'event_id', 'task_id', 'revision', 'operation', 'issuer', 'key_id',
                'namespace', 'tenant', 'authority', 'occurred_at', 'payload'}
PAYLOAD_FIELDS = {'subject', 'worker', 'requester', 'beneficiary', 'task_category',
                  'items', 'activation', 'expiry', 'closed_categories', 'excluded_categories',
                  'authority_envelope', 'recipient_limits'}


class HostTaskGateway:
    """Construct only inside the trusted authentication/policy host.

    apply(host_ctx, event, signature) is deliberately absent from semantic tools.
    Use TLS and a secret manager for transport/keys; signature is hex HMAC-SHA256
    over UTF-8 system.encoded(event), including event ID, revision and namespace.
    """
    def __init__(self, updater, issuer_policies, verification_keys):
        if updater.version.split('-', 1)[0] != '19':raise contracts.TaskContractError('V19 only')
        self.updater = updater
        c = updater.core;c._session(updater.ctx, 'manage_policy', updater.compartment)
        self._policies = deepcopy(issuer_policies)
        self._keys = dict(verification_keys)
        registry = contracts.registry_for(c, updater.compartment)
        if registry is None:raise contracts.TaskContractError('Task registry is disabled')
        if not self._policies:raise contracts.TaskContractError('Explicit trusted issuers required')
        for issuer, policy in self._policies.items():
            if set(policy) != {'key_id', 'authority', 'tenant', 'subjects', 'workers',
                              'task_categories', 'actions', 'categories', 'max_lifetime_seconds'}:
                raise contracts.TaskContractError('Invalid issuer policy')
            if (not issuer or not policy['authority'] or policy['tenant'] != registry.body['tenant']
                    or not policy['subjects'] or not policy['workers']
                    or not policy['actions'] or not set(policy['actions']) <= contracts.READ_ACTIONS
                    or type(policy['max_lifetime_seconds']) is not int or policy['max_lifetime_seconds'] <= 0):
                raise contracts.TaskContractError('Invalid issuer authority ceiling')
            for name in policy['task_categories']:registry.task(name)
            registry.closure(policy['categories'])
            key = self._keys.get(policy['key_id'])
            if not isinstance(key, bytes) or len(key) < 32:
                raise contracts.TaskContractError('A host-held verification key of at least 32 bytes is required')
        with c.transaction():
            c.db.execute('''CREATE TABLE IF NOT EXISTS privacy.host_task_policies_v19(
                compartment TEXT PRIMARY KEY, body TEXT NOT NULL)''')
            c.db.execute('''CREATE TABLE IF NOT EXISTS privacy.host_task_events_v19(
                compartment TEXT NOT NULL, event_id TEXT NOT NULL, event_hash TEXT NOT NULL,
                body TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(compartment,event_id))''')
            c.db.execute('''CREATE TABLE IF NOT EXISTS privacy.host_task_streams_v19(
                compartment TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                issuer TEXT NOT NULL, receipt_id TEXT NOT NULL, state TEXT NOT NULL,
                PRIMARY KEY(compartment,task_id))''')
            pin = {'issuers': self._policies, 'key_fingerprints': {
                p['key_id']: hashlib.sha256(self._keys[p['key_id']]).hexdigest() for p in self._policies.values()}}
            old = c.db.execute('SELECT body FROM host_task_policies_v19 WHERE compartment=?', (updater.compartment,)).fetchone()
            if old and json.loads(old[0]) != pin:
                raise contracts.TaskContractError('Host issuer/key change requires explicit migration')
            c.db.execute('INSERT OR IGNORE INTO host_task_policies_v19 VALUES (?,?)', (updater.compartment, encoded(pin)))

    def _authenticate(self, host_ctx, event, signature):
        u = self.updater;c = u.core
        c._session(host_ctx, 'manage_policy', u.compartment)
        c._session(host_ctx, 'ingest', u.compartment)
        if host_ctx.principal_id != u.ctx.principal_id:raise AccessDenied('Wrong policy host')
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise contracts.TaskContractError('Invalid host event contract')
        policy = self._policies.get(event['issuer'])
        if policy is None or event['key_id'] != policy['key_id']:
            raise AccessDenied('Unverified host issuer/key')
        expected = hmac.new(self._keys[event['key_id']], encoded(event).encode(), hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
            raise AccessDenied('Unauthenticated host event')
        if (event['namespace'] != u.compartment or event['tenant'] != policy['tenant']
                or event['authority'] != policy['authority']):
            raise AccessDenied('Unsupported host authority/tenant/namespace')
        if event['operation'] not in OPERATIONS or type(event['revision']) is not int or event['revision'] < 1:
            raise contracts.TaskContractError('Unknown operation or invalid revision')
        for k in ('event_id', 'task_id'):
            if not isinstance(event[k], str) or not event[k] or len(event[k]) > 256:
                raise contracts.TaskContractError('Bounded stable event/task identity required')
        now, occurred = instant(c.clock()), instant(event['occurred_at'])
        if now is None or occurred is None or occurred > now:
            raise contracts.TaskContractError('Future/unclocked host event rejected')
        return policy

    def _receipt(self, event, policy, original):
        u = self.updater;r = contracts.registry_for(u.core, u.compartment)
        p = event['payload']
        if not isinstance(p, dict) or set(p) != PAYLOAD_FIELDS:
            raise contracts.TaskContractError('Invalid assignment payload')
        if (p['subject'] not in policy['subjects'] or p['worker'] not in policy['workers']
                or p['task_category'] not in policy['task_categories']):
            raise AccessDenied('Issuer cannot assign this subject/worker/task')
        if (not isinstance(p['recipient_limits'], list) or p['worker'] not in p['recipient_limits']
                or not set(p['recipient_limits']) <= set(policy['workers'])):
            raise AccessDenied('Assignment recipient limits exceed issuer policy')
        e = p['authority_envelope']
        if not isinstance(e, dict) or set(e) != {'subject','worker','actions','categories','activation','expiry'}:
            raise contracts.TaskContractError('Invalid assignment envelope')
        if not set(e['actions']) <= set(policy['actions']) or not set(e['categories']) <= set(policy['categories']):
            raise AccessDenied('Assignment dependencies/actions exceed issuer authority')
        start, end = instant(p['activation']), instant(p['expiry'])
        envelope_start, envelope_end = instant(e['activation']), instant(e['expiry'])
        if (None in (start, end, envelope_start, envelope_end)
                or end-start > policy['max_lifetime_seconds']
                or envelope_end-envelope_start > policy['max_lifetime_seconds']
                or start < instant(event['occurred_at'])):
            raise AccessDenied('Assignment lifetime exceeds issuer authority')
        if original:
            # Handoff/reassignment transfers only residual original authority.
            # A wider service needs a separately authenticated new task.
            oe = original['authority_envelope'];oc = original['operational_constraints']
            if (p['subject'] != original['subject'] or p['task_category'] != original['task_category']
                    or p['requester'] != original['requester'] or p['beneficiary'] != original['beneficiary']
                    or not set(p['items']) <= set(oc['items'])
                    or p['worker'] not in oc['recipient_limits']
                    or not set(p['recipient_limits']) <= set(oc['recipient_limits'])
                    or not set(e['categories']) <= set(oe['categories'])
                    or not set(e['actions']) <= set(oe['actions'])
                    or start < instant(original['activation']) or end > instant(original['expiry'])
                    or not set(original['explicit_exclusions']) <= set(p['excluded_categories'])
                    or oc['closed_categories'] is not None and (p['closed_categories'] is None
                        or not set(p['closed_categories']) <= set(oc['closed_categories']))):
                raise AccessDenied('Handoff/reassignment expands original authority or recipient limits')
        row = contracts.build_receipt(r, task_id=event['task_id'] + ':r' + str(event['revision']),
            namespace=u.compartment, subject=p['subject'], worker=p['worker'], requester=p['requester'],
            beneficiary=p['beneficiary'], authority=event['authority'], category=p['task_category'],
            items=p['items'], activation=p['activation'], expiry=p['expiry'], closed=p['closed_categories'],
            exclusions=p['excluded_categories'], envelope=e, original_evidence={
                'mode':'authenticated_host', 'issuer':event['issuer'], 'event_id':event['event_id'],
                'key_id':event['key_id'], 'assignment':{'event_digest':digest(event)}})
        row['logical_task_id'] = event['task_id']
        row['operational_constraints']['recipient_limits'] = p['recipient_limits']
        if original:row['predecessor_receipt_id'] = original['id']
        return row

    def apply(self, host_ctx, event, signature):
        """Atomic, ordered, idempotent event admission; makes zero model calls."""
        event = deepcopy(event)
        u = self.updater;c = u.core
        with c.transaction():
            policy = self._authenticate(host_ctx,event,signature)
            u._current_configuration()
            old = c.db.execute('SELECT event_hash,result FROM host_task_events_v19 WHERE compartment=? AND event_id=?',
                               (u.compartment,event['event_id'])).fetchone()
            if old:
                if old[0] != digest(event):raise contracts.TaskContractError('Conflicting host event replay')
                return json.loads(old[1])
            if c.db.execute("SELECT 1 FROM permission_jobs WHERE compartment=? AND status NOT IN ('complete','superseded','skipped','quarantined')",
                            (u.compartment,)).fetchone():
                raise contracts.TaskContractError('Complete preceding source controls before host event admission')
            stream = c.db.execute('SELECT * FROM host_task_streams_v19 WHERE compartment=? AND task_id=?',
                                  (u.compartment,event['task_id'])).fetchone()
            expected_revision = stream['revision']+1 if stream else 1
            if event['revision'] != expected_revision:raise contracts.TaskContractError('Stale or out-of-order host event')
            op = event['operation']
            if (stream is None) != (op == 'assign'):raise contracts.TaskContractError('Assignment stream already exists or is missing')
            if stream and stream['issuer'] != event['issuer']:raise AccessDenied('Different stream issuer')
            original = contracts.receipts(c,u.compartment).get(stream['receipt_id']) if stream else None
            receipt = None
            if op in {'assign','reassign','handoff'}:
                if original and (original['state'] != 'ACTIVE' or instant(c.clock()) >= instant(original['expiry'])):
                    raise contracts.TaskContractError('Ended task cannot be reassigned or handed off')
                if original:
                    a=next((a for a in u._state()['assignment'].values()
                        if a.get('task_receipt_v19')==original['id']),{})
                    if a.get('status')!='active' or not contracts.gate(c,u.compartment,a):
                        raise contracts.TaskContractError('Original assignment support is no longer valid')
                receipt = self._receipt(event,policy,original)
            elif event['payload'] != {}:
                raise contracts.TaskContractError('Terminal events have an empty payload')
            if op == 'expire' and instant(c.clock()) < instant(original['expiry']):
                raise contracts.TaskContractError('Premature expiry event')
            # Private evidence is a distinct resource type, never raw dialogue,
            # an inferred fact, or fresh consent attributed to the worker.
            sid = 'host-task-evidence:' + digest([u.compartment,event['event_id']])
            body = {'text':encoded(event), 'source':{'participant':event['issuer'],
                'source_identity':'authenticated-host-task-v19', 'event_id':event['event_id'],
                'observed_at':c.clock(), 'occurred_at':event['occurred_at'],
                'subject':event['payload'].get('subject') if receipt else original['subject'],
                'workflow_evidence':True}}
            c.create(u.ctx,sid,'host_task_evidence',body,policy_id=u.source_policy,
                     compartment=u.compartment,attributes={'host_task_event':True})
            proof = contracts.evidence(sid,body)
            if receipt:
                receipt['original_evidence']['verification']={'algorithm':'HMAC-SHA256',
                    'signature':signature,'issuer_policy_digest':digest(policy)}
                receipt['original_evidence']['assignment'].update(proof)
                if original:contracts.end(u,original,'HANDED_OFF' if op=='handoff' else 'REASSIGNED',proof)
                contracts.publish(u,receipt)
            else:
                receipt = contracts.end(u,original,TERMINAL[op],proof)
            result = {'task_id':event['task_id'], 'revision':event['revision'],
                      'receipt_id':receipt['id'], 'state':receipt['state'], 'provenance':'authenticated_host'}
            c.db.execute('INSERT INTO host_task_streams_v19 VALUES (?,?,?,?,?,?) ON CONFLICT(compartment,task_id) '
                         'DO UPDATE SET revision=excluded.revision,receipt_id=excluded.receipt_id,state=excluded.state',
                (u.compartment,event['task_id'],event['revision'],event['issuer'],receipt['id'],receipt['state']))
            c.db.execute('INSERT INTO host_task_events_v19 VALUES (?,?,?,?,?)',
                (u.compartment,event['event_id'],digest(event),encoded(event),encoded(result)))
            c._tickets.clear()
            return result


def evidence_sources(updater, sources):
    """Private host proofs join binding evidence, never the recent control window."""
    c=updater.core
    extra={r['id']:json.loads(r['payload']) for r in c.db.execute(
        "SELECT id,payload FROM resources WHERE compartment=? AND kind='host_task_evidence' AND state='active'",
        (updater.compartment,)) if c._current(r['id'])}
    return {**sources,**extra}


def extend_identities(updater, body):
    """An authenticated assignment attests identities, never role/ownership grants."""
    for r in contracts.receipts(updater.core,updater.compartment).values():
        if r['original_evidence']['mode']!='authenticated_host':continue
        for key in ('worker','subject','requester','beneficiary'):
            body['participants'].setdefault(r[key],{})
        body['resources'].setdefault(r['subject'],{})
