"""Canonical note memory, deterministic authority, protected graph and evolution.

The trusted host issues sessions after upstream authentication. Source text and
model output cannot issue sessions, install policies or set security metadata.
SQLite and embeddings are private service storage, never an agent-facing API.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid


class AccessDenied(PermissionError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def instant(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('A trusted timezone is required')
    return parsed.timestamp()


@dataclass(frozen=True)
class Context:
    principal_id: str
    agent_id: str
    session_id: str


class PrivacyMemorySystem:
    def __init__(self, path, embedder, *, clock=None, privacy_path=None):
        from .storage import open_pair
        self.db, self.path, self.privacy_path = open_pair(path, privacy_path)
        self.embedder = embedder
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self._lock = threading.RLock()
        self._sessions = {}
        self._processors = {embedder.processor_id: embedder}
        self._tickets = {}

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            if self.db.in_transaction:
                yield
                return
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def issue_session(self, principal_id, agent_id, *, actions, compartments, purpose='',
                      expires_in=3600, delegations=(), processing_routes=()):
        """Trusted authentication adapter ONLY; never exported as an agent tool.

        Agent scope and every delegation attenuate the principal's policy rights.
        Delegations are host-verified action/compartment/expiry constraints.
        """
        if not principal_id or not agent_id or not actions or not compartments:
            raise AccessDenied('Invalid authenticated context')
        ctx = Context(principal_id, agent_id, uuid.uuid4().hex)
        self._sessions[ctx.session_id] = (ctx, frozenset(actions), frozenset(compartments),
                                         purpose, time.time() + expires_in, tuple(delegations),frozenset(processing_routes))
        return ctx

    def register_processor(self,ctx,processor):
        self._session(ctx,'manage_policy')
        self._processors[processor.processor_id]=processor

    def _processing_route(self,ctx,processor):
        session=self._session(ctx,'derive')
        if processor.processor_id not in session[6] or self._processors.get(processor.processor_id) is not processor:
            raise AccessDenied('Unapproved processing route')

    def revoke_session(self, ctx):
        self._sessions.pop(ctx.session_id, None)

    def _session(self, ctx, action, compartment=None):
        record = self._sessions.get(getattr(ctx, 'session_id', None))
        if not record or record[0] != ctx or record[4] <= time.time():
            raise AccessDenied('Unavailable')
        if action not in record[1] or (compartment is not None and compartment not in record[2]):
            raise AccessDenied('Unavailable')
        for grant in record[5]:
            if (action not in grant.get('actions', ()) or
                    (compartment is not None and compartment not in grant.get('compartments', ())) or
                    float(grant.get('expires_at', 0)) <= time.time()):
                raise AccessDenied('Unavailable')
        return record

    def install_policy(self, ctx, policy_id, body):
        self._session(ctx, 'manage_policy')
        if not isinstance(body, dict) or set(body) - {'allows', 'denies', 'processors'}:
            raise ValueError('Invalid policy')
        for field in ('allows','denies','processors'):
            if field in body and not isinstance(body[field],list):
                raise ValueError('Policy fields must be explicit arrays')
        if any(not isinstance(p,str) or not p for p in body.get('processors',[])):
            raise ValueError('Invalid processor identity')
        for rule in body.get('allows', []) + body.get('denies', []):
            if not isinstance(rule, dict) or not rule.get('actions') or not (rule.get('principals') or rule.get('groups')):
                raise ValueError('A rule requires explicit subjects and actions')
            if set(rule) - {'actions', 'principals', 'groups', 'scope', 'resource_ids', 'kinds', 'purpose', 'from', 'until', 'id'}:
                raise ValueError('Unsupported policy condition')
            for field in ('actions','principals','groups','resource_ids','kinds'):
                if field in rule and (not isinstance(rule[field],list) or not rule[field] or any(not isinstance(v,str) or not v for v in rule[field])):
                    raise ValueError('Invalid subject/action/resource list')
            if 'scope' in rule and not isinstance(rule['scope'],dict):
                raise ValueError('Invalid authoritative scope')
            if 'purpose' in rule and not isinstance(rule['purpose'],str):
                raise ValueError('Invalid purpose')
            for key in ('from', 'until'):
                if key in rule:
                    if not isinstance(rule[key],str) or not rule[key]:
                        raise ValueError('Invalid validity interval')
                    instant(rule[key])
        with self.transaction():
            self._claim_authority(ctx,'policy',policy_id)
            for rule in body.get('allows',[])+body.get('denies',[]):
                for group in rule.get('groups',[]):
                    self.db.execute('INSERT OR IGNORE INTO authority_owners VALUES (\'group\',?,?)',(group,ctx.principal_id))
            self._tickets.clear()
            self.db.execute('INSERT INTO policies VALUES (?,1,?) ON CONFLICT(id) DO UPDATE SET revision=revision+1,body=excluded.body',
                            (policy_id, encoded(body)))
            self._processing_route(ctx, self.embedder)
            vector = self.embedder.encode([encoded(body)])[0]
            self.db.execute('INSERT OR REPLACE INTO privacy_vectors VALUES (?,?,?)',
                            (policy_id, self.embedder.space_id, encoded(vector)))

    def install_assignment(self, ctx, assignment_id, *, principals=(), groups=(),
                           subject, compartment, information_scope, task,
                           actions, provenance, conditions=None, relationship='',
                           collection='', delegation=None):
        """Trusted authority API. Participation and roles are never core grants.

        Authority provenance is a host-verified source/ACL/event receipt, retained
        for audit. Conditions use the same closed rule grammar as base policies.
        A delegation is a live attenuating parent assignment, not a copied grant.
        """
        self._session(ctx, 'manage_policy')
        self._session(ctx, 'ingest', compartment)
        if not all(isinstance(x, str) and x for x in (subject, compartment, information_scope, task)):
            raise ValueError('Exact subject, collection route, information scope and task required')
        if not isinstance(provenance, dict) or not provenance.get('authority') or not provenance.get('evidence'):
            raise ValueError('Verified authority and supporting provenance required')
        conditions = dict(conditions or {})
        if set(conditions) - {'from', 'until', 'purpose'}:
            raise ValueError('Unsupported assignment condition')
        rule = {'actions': list(actions), 'scope': {'subject': subject, 'information_scope': information_scope}, **conditions}
        if collection: rule['scope']['collection'] = collection
        if principals: rule['principals'] = list(principals)
        if groups: rule['groups'] = list(groups)
        policy_id = 'assignment:' + assignment_id
        body = dict(subject=subject, compartment=compartment, information_scope=information_scope,
                    task=task, relationship=relationship, collection=collection, provenance=provenance,
                    delegation=delegation, rule=rule)
        with self.transaction():
            self._claim_authority(ctx, 'assignment', assignment_id)
            if delegation:
                parent = self.db.execute('SELECT body,state,issuer FROM assignments WHERE id=?', (delegation,)).fetchone()
                if not parent or parent['state'] != 'active' or parent['issuer'] != ctx.principal_id:
                    raise AccessDenied('Inactive delegation authority')
                parent_body = json.loads(parent['body'])
                if any(parent_body[k] != body[k] for k in ('subject', 'compartment', 'information_scope')):
                    raise AccessDenied('Delegation cannot widen resource scope')
                if any(rule['scope'].get(k) != v for k,v in parent_body['rule']['scope'].items()):
                    raise AccessDenied('Delegation cannot widen selector scope')
                if not set(actions) <= set(parent_body['rule']['actions']):
                    raise AccessDenied('Delegation cannot widen action scope')
                seen = {assignment_id}
                ancestor = delegation
                while ancestor:
                    if ancestor in seen: raise ValueError('Cyclic delegation')
                    seen.add(ancestor)
                    record = self.db.execute('SELECT body FROM assignments WHERE id=?', (ancestor,)).fetchone()
                    ancestor = json.loads(record[0]).get('delegation') if record else None
            self.install_policy(ctx, policy_id, {'allows': [rule], 'denies': [],
                                                'processors': list(self._session(ctx, 'derive')[6])})
            self.db.execute("INSERT INTO assignments VALUES (?,?,?,?,'active') ON CONFLICT(id) DO UPDATE SET body=excluded.body,state='active'",
                            (assignment_id, policy_id, ctx.principal_id, encoded(body)))
            self.db.execute('DELETE FROM access_edges WHERE assignment_id=?', (assignment_id,))
            for row in self.db.execute("SELECT id FROM resources WHERE compartment=? AND state='active'", (compartment,)).fetchall():
                self._bind_assignments(row[0])

    def _assignment_live(self, assignment_id, session, action, seen=None):
        seen = set(seen or ())
        if assignment_id in seen: return False
        seen.add(assignment_id)
        row = self.db.execute('SELECT body,state FROM assignments WHERE id=?', (assignment_id,)).fetchone()
        if not row or row['state'] != 'active': return False
        body = json.loads(row['body'])
        rule = body['rule']
        if action not in rule['actions']: return False
        now = instant(self.clock())
        start, end = instant(rule.get('from')), instant(rule.get('until'))
        if (start is not None or end is not None) and now is None: return False
        if (start is not None and now < start) or (end is not None and now >= end): return False
        if rule.get('purpose') and rule['purpose'] != session[3]: return False
        parent = body.get('delegation')
        if parent:
            parent_row = self.db.execute('SELECT body FROM assignments WHERE id=?', (parent,)).fetchone()
            if not parent_row: return False
            parent_body = json.loads(parent_row[0])
            if any(parent_body[k] != body[k] for k in ('subject', 'compartment', 'information_scope')): return False
            if any(rule['scope'].get(k) != v for k,v in parent_body['rule']['scope'].items()): return False
            return self._assignment_live(parent, session, action, seen)
        return True

    def _bind_assignments(self, resource_id):
        row = self.db.execute('SELECT * FROM resources WHERE id=?', (resource_id,)).fetchone()
        attrs = json.loads(row['attributes'])
        for a in self.db.execute("SELECT * FROM assignments WHERE state='active'").fetchall():
            body = json.loads(a['body'])
            owner = self.db.execute("SELECT issuer FROM authority_owners WHERE kind='policy' AND id=?", (row['policy_id'],)).fetchone()
            if (owner and owner[0] == a['issuer'] and row['compartment'] == body['compartment'] and
                    all(attrs.get(k) == v for k, v in body['rule']['scope'].items())):
                self.db.execute('INSERT OR IGNORE INTO access_edges VALUES (?,?,?)', (a['policy_id'], resource_id, a['id']))

    def revoke_assignment(self, ctx, assignment_id):
        self._session(ctx, 'manage_policy')
        with self.transaction():
            self._claim_authority(ctx, 'assignment', assignment_id)
            self.db.execute("UPDATE assignments SET state='revoked' WHERE id=?", (assignment_id,))
            self._tickets.clear()

    def unresolved_assignment(self, ctx, diagnostic_id, details):
        self._session(ctx, 'manage_policy')
        with self.transaction():
            self._claim_authority(ctx, 'diagnostic', diagnostic_id)
            self.db.execute('INSERT OR REPLACE INTO assignment_diagnostics VALUES (?,?,?)',
                            (diagnostic_id, ctx.principal_id, encoded(details)))

    def discover_policies(self, ctx, query, k=8):
        """Issuer-only convenience discovery. Never called by authorization."""
        self._session(ctx, 'manage_policy')
        self._processing_route(ctx, self.embedder)
        with self.transaction():
            query_vector = self.embedder.encode([query])[0]
            results = []
            for row in self.db.execute("SELECT v.* FROM privacy_vectors v JOIN authority_owners a ON a.id=v.id AND a.kind='policy' WHERE a.issuer=? AND v.space=?", (ctx.principal_id, self.embedder.space_id)):
                vector = json.loads(row['vector'])
                if len(vector) != len(query_vector): raise ValueError('Representation space mismatch')
                results.append((sum(x*y for x,y in zip(vector, query_vector)), row['id']))
            return [rid for _,rid in sorted(results, reverse=True)[:max(0, min(k, 100))]]

    def set_member(self, ctx, group, principal, present=True):
        self._session(ctx, 'manage_policy')
        with self.transaction():
            self._claim_authority(ctx,'group',group)
            self._tickets.clear()
            if present:
                self.db.execute('INSERT OR IGNORE INTO memberships VALUES (?,?)', (group, principal))
            else:
                self.db.execute('DELETE FROM memberships WHERE group_id=? AND principal_id=?', (group, principal))

    def _claim_authority(self,ctx,kind,identifier):
        owner=self.db.execute('SELECT issuer FROM authority_owners WHERE kind=? AND id=?',(kind,identifier)).fetchone()
        if owner and owner[0]!=ctx.principal_id:
            raise AccessDenied('Issuer is outside this authority scope')
        self.db.execute('INSERT OR IGNORE INTO authority_owners VALUES (?,?,?)',(kind,identifier,ctx.principal_id))

    def _rule_matches(self, rule, ctx, action, row, session):
        subjects = rule.get('principals', [])
        group = any(self.db.execute('SELECT 1 FROM memberships WHERE group_id=? AND principal_id=?',
                                    (g, ctx.principal_id)).fetchone() for g in rule.get('groups', []))
        if ctx.principal_id not in subjects and not group:
            return False
        if action not in rule.get('actions', []):
            return False
        if rule.get('kinds') and row['kind'] not in rule['kinds']:
            return False
        if rule.get('resource_ids') and row['id'] not in rule['resource_ids']:
            return False
        attrs = json.loads(row['attributes'])
        if any(attrs.get(k) != v for k, v in rule.get('scope', {}).items()):
            return False
        if rule.get('purpose') and session[3] != rule['purpose']:
            return False
        try:
            now = instant(self.clock())
            start, end = instant(rule.get('from')), instant(rule.get('until'))
            if (start is not None or end is not None) and now is None:
                return False
            return not ((start is not None and now < start) or (end is not None and now >= end))
        except (ValueError, TypeError):
            return False

    def _policy_allows(self, body, ctx, action, row, session, processor=None):
        if processor and processor not in body.get('processors', []):
            return False
        if any(self._rule_matches(r, ctx, action, row, session) for r in body.get('denies', [])):
            return False
        return any(self._rule_matches(r, ctx, action, row, session) for r in body.get('allows', []))

    def allowed(self, ctx, resource_id, action='read', *, processor=None, visited=None):
        with self._lock:
            return self._allowed(ctx, resource_id, action, processor=processor, visited=visited)

    def _allowed(self, ctx, resource_id, action='read', *, processor=None, visited=None):
        try:
            row = self.db.execute('SELECT * FROM resources WHERE id=?', (resource_id,)).fetchone()
            if row is None or row['state'] != 'active':
                return False
            session = self._session(ctx, action, row['compartment'])
            visited = set(visited or ())
            if resource_id in visited:
                return False
            visited.add(resource_id)
            # Exact directed bindings select every applicable policy, never vectors.
            policies = [json.loads(p[0]) for p in self.db.execute(
                'SELECT p.body,e.assignment_id FROM access_edges e JOIN policies p ON p.id=e.policy_id '
                'LEFT JOIN assignments a ON a.id=e.assignment_id '
                "WHERE e.memory_id=? AND (e.assignment_id='' OR a.state='active')", (resource_id,))
                if not p[1] or self._assignment_live(p[1], session, action)]
            base = self.db.execute('SELECT body FROM policies WHERE id=?', (row['policy_id'],)).fetchone()
            if not base or not policies:
                return False
            # Base processor approval and explicit denials remain mandatory even
            # when an additional assignment supplies the allow.
            base = json.loads(base[0])
            if processor and processor not in base.get('processors', []):
                return False
            if any(self._rule_matches(r, ctx, action, row, session)
                   for body in policies + [base] for r in body.get('denies', [])):
                return False
            if not any(self._policy_allows(body, ctx, action, row, session, processor) for body in policies):
                return False
            # Authoritative field views inherit source *lifecycle*, not source
            # whole-document read permission. Missing/deleted sources fail closed.
            for source in self.db.execute('SELECT source_id FROM source_views WHERE view_id=?', (resource_id,)):
                if not self._source_live(source[0]):
                    return False
            for ceiling in json.loads(row['policy_ceiling']):
                captured_row = ceiling.get('resource', row)
                captured_policy = ceiling.get('policy', ceiling)
                if not self._policy_allows(captured_policy, ctx, action, captured_row, session, processor):
                    return False
            # Dependency authorization checks the input's read/process right,
            # not a write privilege on every source of a derived artifact.
            for parent in json.loads(row['dependencies']):
                if not self.allowed(ctx, parent, 'derive' if processor else 'read', processor=processor, visited=visited):
                    return False
            return True
        except (AccessDenied, ValueError, TypeError, KeyError, RecursionError):
            return False

    def require(self, ctx, resource_id, action='read', processor=None):
        if not self.allowed(ctx, resource_id, action, processor=processor):
            raise AccessDenied('Unavailable')

    def _resource(self, ctx, resource_id, action='read', processor=None):
        self.require(ctx, resource_id, action, processor)
        row = dict(self.db.execute('SELECT * FROM resources WHERE id=?', (resource_id,)).fetchone())
        for field in ('payload', 'attributes', 'dependencies', 'policy_ceiling'):
            row[field] = json.loads(row[field])
        return row

    def read(self, ctx, resource_id):
        with self.transaction():
            view=self._resource(ctx, resource_id)
            # Policy references are inspectable opaque bindings; authoritative
            # rule bodies and captured ceilings are control-plane data.
            view.pop('policy_ceiling',None)
            return view

    def create(self, ctx, resource_id, kind, payload, *, policy_id, compartment,
               attributes=None, dependencies=(), generated=False):
        self._session(ctx, 'ingest', compartment)
        dependencies = list(dict.fromkeys(dependencies))
        if resource_id in dependencies:
            raise ValueError('Cyclic dependency')
        ceilings = []
        for parent in dependencies:
            self.require(ctx, parent, 'read')
            if generated:
                row = self.db.execute('SELECT * FROM resources WHERE id=?', (parent,)).fetchone()
                bodies = [json.loads(p[0]) for p in self.db.execute(
                    'SELECT p.body FROM access_edges e JOIN policies p ON p.id=e.policy_id '
                    "LEFT JOIN assignments a ON a.id=e.assignment_id WHERE e.memory_id=? AND (e.assignment_id='' OR a.state='active')",(parent,))]
                base = json.loads(self.db.execute('SELECT body FROM policies WHERE id=?',(row['policy_id'],)).fetchone()[0])
                ceilings.append({'policy': {'allows':[r for b in bodies for r in b.get('allows',[])],
                    'denies':[r for b in bodies for r in b.get('denies',[])], 'processors':base.get('processors',[])},
                    'resource':{k:row[k] for k in ('id','kind','attributes')}})
        # All resources have immutable identity/payload; changes are new IDs.
        values = (resource_id, kind, compartment, policy_id, encoded(payload),
                  encoded(attributes or {}), encoded(dependencies), encoded(ceilings))
        with self.transaction():
            existing = self.db.execute('SELECT * FROM resources WHERE id=?', (resource_id,)).fetchone()
            if existing:
                previous = tuple(existing[k] for k in ('id','kind','compartment','policy_id','payload','attributes','dependencies','policy_ceiling'))
                if previous != values:
                    raise ValueError('Immutable resource ID reused with different data')
                return resource_id
            if self.db.execute('SELECT 1 FROM tombstones WHERE id=?', (resource_id,)).fetchone():
                raise ValueError('Tombstoned identity cannot be reused')
            owner = self.db.execute("SELECT issuer FROM authority_owners WHERE kind='policy' AND id=?", (policy_id,)).fetchone()
            if not owner or owner[0] != ctx.principal_id:
                raise AccessDenied('Only the resource policy authority may publish bindings')
            self.db.execute('INSERT INTO resources VALUES (?,?,?,?,?,?,?,?,\'staged\',?)', values + (time.time(),))
            self.db.execute('INSERT INTO access_edges VALUES (?,?,\'\')', (policy_id, resource_id))
            self._bind_assignments(resource_id)
            self._publication_hook(resource_id)
            self.db.execute("UPDATE resources SET state='active' WHERE id=?", (resource_id,))
        return resource_id

    def ingest(self, ctx, observation_id, text, *, policy_id, compartment, source=None, attributes=None,dependencies=()):
        obs = self.create(ctx, observation_id, 'observation', {'text': text, 'source': source or {}},
                          policy_id=policy_id, compartment=compartment, attributes=attributes,dependencies=dependencies)
        note_id = 'note:' + observation_id
        self.create(ctx, note_id, 'memory', {'text': text, 'semantic': {}},
                    policy_id=policy_id, compartment=compartment, attributes=attributes, dependencies=[obs])
        self.index(ctx, note_id)
        return note_id

    def ingest_authoritative_view(self,ctx,source_id,view_id,spans,*,policy_id,attributes=None,field_labels=None):
        """Trusted field-policy adapter only: exact selected character spans.

        The source authority assigns this view its own field policy BEFORE any
        model sees it. This is not model-driven sanitization/declassification.
        Source lifecycle lineage is retained separately from whole-document read
        policy because field and whole-document views can have different audiences.
        """
        self._session(ctx,'manage_policy')
        with self.transaction():
            source=self._resource(ctx,source_id)
            owner=self.db.execute("SELECT issuer FROM authority_owners WHERE kind='policy' AND id=?",(source['policy_id'],)).fetchone()
            if not owner or owner[0] != ctx.principal_id:
                raise AccessDenied('Only the source authority may approve a different field audience')
            text=source['payload'].get('text')
            if text is None and source['payload'].get('media_type')=='text/plain':
                text=base64.b64decode(source['payload']['payload_base64']).decode('utf-8')
            if not isinstance(text,str):
                raise ValueError('Character projections require a text source adapter')
            if not spans or len(spans)>32 or any(not isinstance(a,int) or not isinstance(b,int) or a<0 or b<=a or b>len(text) for a,b in spans):
                raise ValueError('Invalid exact source selectors')
            if field_labels is not None and (len(field_labels) != len(spans) or
                    any(not isinstance(label,str) or not re.fullmatch(r'[a-z][a-z0-9_.]{0,80}',label) for label in field_labels)):
                raise ValueError('Invalid authoritative field labels')
            selected='; '.join((field_labels[i]+': ' if field_labels else '')+text[a:b] for i,(a,b) in enumerate(spans))
            view=self.create(ctx,view_id,'observation_view',{'text':selected,'source_ref':{'observation_id':source_id,'character_spans':spans,'field_labels':field_labels}},
                             policy_id=policy_id,compartment=source['compartment'],attributes=attributes or source['attributes'])
            self.db.execute('INSERT OR IGNORE INTO source_views VALUES (?,?)',(view,source_id))
            note='note:'+view
            self.create(ctx,note,'memory',{'text':selected,'semantic':{},'source_view_id':view},
                        policy_id=policy_id,compartment=source['compartment'],attributes=attributes or source['attributes'],dependencies=[view])
            self.index(ctx,note)
            return note

    def _source_live(self, resource_id, seen=None):
        seen = set(seen or ())
        if resource_id in seen:
            return False
        seen.add(resource_id)
        row = self.db.execute('SELECT state FROM resources WHERE id=?', (resource_id,)).fetchone()
        if not row or row[0] != 'active':
            return False
        return all(self._source_live(r[0], seen) for r in self.db.execute(
            'SELECT source_id FROM source_views WHERE view_id=?', (resource_id,)))

    def _publication_hook(self, resource_id):
        # Failure injection seam: raising here rolls back both databases.
        pass

    def index(self, ctx, resource_id):
        self._processing_route(ctx,self.embedder)
        row = self._resource(ctx, resource_id, 'derive', self.embedder.processor_id)
        text = row['payload'].get('text', '')
        if row['kind'] == 'enrichment':
            # Generated metadata affects ranking, never the factual answer text.
            text += '\n' + encoded(row['payload'].get('semantic', {}))
        vector = self.embedder.encode([text])[0]
        if not vector or not all(math.isfinite(x) for x in vector):
            raise ValueError('Invalid representation')
        with self.transaction():
            self.require(ctx, resource_id, 'derive', self.embedder.processor_id)
            self.db.execute('INSERT OR REPLACE INTO representations VALUES (?,?,?)',
                            (resource_id, self.embedder.space_id, encoded(vector)))

    def search(self, ctx, query, *, compartments=None, k=8, processor=None):
        session = self._session(ctx, 'search')
        self._processing_route(ctx,self.embedder)
        routes = set(compartments) if compartments is not None else set(session[2])
        if not routes <= session[2]:
            raise AccessDenied('Unavailable')
        # Query itself is a request-private processing input; trusted registry
        # approval is mandatory, even when there are no searchable records.
        if processor is not None and processor != self.embedder.processor_id:
            raise AccessDenied('Unsupported query processor')
        with self.transaction():
            candidates = []
            for route in sorted(routes):
                for row in self.db.execute('SELECT r.id,v.vector FROM resources r JOIN representations v ON v.id=r.id WHERE r.compartment=? AND r.state=\'active\' AND v.space=? AND r.kind IN (\'memory\',\'enrichment\')',
                                           (route, self.embedder.space_id)):
                    if (self.allowed(ctx, row['id'], 'search') and self.allowed(ctx, row['id'], 'read') and
                            self.allowed(ctx, row['id'], 'derive', processor=self.embedder.processor_id)):
                        candidates.append((row['id'], json.loads(row['vector'])))
            # Multi-part retrieval competes only within the fully authorized set.
            facets = [q.strip(' ?') for q in re.split(r'\band\b|[?;]|, (?=what|which|where|when)', query, flags=re.I) if len(q.strip()) > 8]
            facets = list(dict.fromkeys([query] + facets))[:6]
            vectors = self.embedder.encode(facets)
            ranked = []
            current_query = bool(re.search(r'\b(current|active|now|latest|still|updated)\b', query, re.I))
            for rid, vector in candidates:
                if len(vector) != len(vectors[0]):
                    raise ValueError('Representation space mismatch')
                if not self._current(rid):
                    continue
                view = self._resource(ctx, rid)
                text = view['payload'].get('text', '')
                words = set(re.findall(r'\w+', (text+' '+view['attributes'].get('retrieval_terms','')).lower()))
                scores = []
                for facet, qv in zip(facets, vectors):
                    terms = set(re.findall(r'\w+', facet.lower())) - {'what','which','who','where','when','why','how','the','a','an','are','is','was','were','as','of','for','now','current','active','latest','should','at','this','that','point','and','to','in','on','it','i','we','you','me','my','our'}
                    scores.append(sum(x*y for x,y in zip(vector, qv)) + .55 * len(words & terms) / max(1,len(terms)))
                turn = view['attributes'].get('sequence', 0)
                ranked.append({'score': max(scores), 'scores': scores, 'view': view, 'words': words, 'sequence': turn})
            latest = max((r['sequence'] for r in ranked), default=0) or 1
            for record in ranked:
                bonus = .18 * record['sequence'] / latest if current_query else 0
                record['score'] += bonus
                record['scores'] = [s + bonus for s in record['scores']]
            ranked.sort(key=lambda r: (-r['score'], r['view']['id']))
            selected, bases, texts = [], set(), set()
            def select(record):
                view = record['view']
                base = view['payload'].get('base_memory_id', view['id'])
                text = ' '.join(view['payload'].get('text', '').lower().split())
                if base in bases or text in texts:
                    return False
                # Similar overlays and verbatim repetitions cannot consume the budget.
                if any(len(record['words'] & old['words']) / max(1,len(record['words'] | old['words'])) > .90 for old in selected):
                    return False
                bases.add(base); texts.add(text); selected.append(record)
                return True
            budget = max(0,min(k,100))
            if budget:
                # Reserve evidence for each requested part before filling by score.
                for index in range(1,len(facets)):
                    if len(selected) >= budget: break
                    for record in sorted(ranked, key=lambda r:(-r['scores'][index],r['view']['id'])):
                        if select(record): break
                for record in ranked:
                    if len(selected) >= budget: break
                    select(record)
            result = []
            for record in selected:
                view = record['view']; rid = view['id']
                self.require(ctx, rid)
                result.append({'id': rid, 'score': record['score'], 'text': view['payload'].get('text',''),
                               'kind': view['kind'], 'semantic': view['payload'].get('semantic', {}),
                               'source': view['attributes'], 'current': True})
            return result

    def _current(self, resource_id, seen=None):
        seen = set(seen or ())
        if resource_id in seen: return False
        seen.add(resource_id)
        if self.db.execute('SELECT 1 FROM revisions WHERE old_id=?', (resource_id,)).fetchone(): return False
        row = self.db.execute('SELECT dependencies FROM resources WHERE id=?', (resource_id,)).fetchone()
        parents = json.loads(row[0]) if row else []
        parents += [r[0] for r in self.db.execute('SELECT source_id FROM source_views WHERE view_id=?', (resource_id,))]
        return all(self._current(p,seen) for p in parents)

    def supersede(self, ctx, old_id, new_id):
        """Explicit revision event; history stays addressable but stops ranking."""
        with self.transaction():
            old = self._resource(ctx,old_id,'write'); new = self._resource(ctx,new_id,'write')
            if old['compartment'] != new['compartment'] or old['attributes'].get('subject') != new['attributes'].get('subject'):
                raise AccessDenied('Revision crosses a resource scope')
            if old_id == new_id: raise ValueError('Cyclic revision')
            cursor = new_id
            while True:
                row = self.db.execute('SELECT new_id FROM revisions WHERE old_id=?',(cursor,)).fetchone()
                if not row: break
                cursor = row[0]
                if cursor == old_id: raise ValueError('Cyclic revision')
            self.db.execute('INSERT INTO revisions VALUES (?,?)',(old_id,new_id))
            self._tickets.clear()

    def link(self, ctx, edge_id, source, target, predicate, *, policy_id, compartment, dependencies=()):
        for rid in (source, target):
            self.require(ctx, rid, 'link')
        return self.create(ctx, edge_id, 'edge', {'from': source, 'to': target, 'predicate': predicate},
                           policy_id=policy_id, compartment=compartment,
                           dependencies=list(dependencies) + [source,target], generated=True)

    def edges(self, ctx, resource_id, *, reverse=False):
        self.require(ctx, resource_id)
        visible = []
        for row in self.db.execute('SELECT id,payload FROM resources WHERE kind=\'edge\' AND state=\'active\''):
            if not self.allowed(ctx, row['id']):
                continue
            payload = json.loads(row['payload'])
            if payload['to' if reverse else 'from'] == resource_id and all(self.allowed(ctx,payload[x]) for x in ('from','to')):
                visible.append({'id': row['id'], **payload})
        return visible

    def traverse(self, ctx, resource_id, *, limit=20):
        self.require(ctx, resource_id)
        queue, seen, result = [resource_id], set(), []
        while queue and len(result) < min(limit,100):
            rid = queue.pop(0)
            if rid in seen:
                continue
            seen.add(rid)
            result.append(self.read(ctx, rid))
            queue.extend(e['to'] for e in self.edges(ctx, rid))
        return result

    def retrieve(self,ctx,query,*,k=8,graph_budget=4):
        """Authorized vector retrieval plus bounded, independently checked edges.

        The packet includes edge dependencies even when only target text enters
        the prompt: using a private relationship must not produce a public answer.
        """
        evidence=self.search(ctx,query,k=k)
        inputs=[r['id'] for r in evidence]
        seen=set(inputs)
        added=0
        for seed in list(evidence):
            for edge in self.edges(ctx,seed['id']):
                if added>=graph_budget:break
                target=edge['to']
                if target in seen:continue
                row=self.read(ctx,target)
                if row['kind'] not in {'memory','enrichment'}:continue
                evidence.append({'id':target,'text':row['payload'].get('text',''),
                                 'kind':row['kind'],'semantic':row['payload'].get('semantic',{}),
                                 'via_edge_id':edge['id']})
                inputs.extend([edge['id'],target]);seen.add(target);added+=1
            if added>=graph_budget:break
        return {'evidence':evidence,'input_ids':list(dict.fromkeys(inputs))}

    def process(self, ctx, processor, input_ids, system, payload, *, schema=None):
        self._processing_route(ctx,processor)
        input_ids = list(dict.fromkeys(input_ids))
        with self.transaction():
            for rid in input_ids:
                self.require(ctx, rid, 'derive', processor.processor_id)
        response = (processor.complete(system, payload, schema=schema) if schema is not None
                    else processor.complete(system, payload))
        with self.transaction():
            for rid in input_ids:
                self.require(ctx, rid, 'derive', processor.processor_id)
            self._tickets[id(response)]=(response,ctx.session_id,tuple(input_ids),processor.processor_id,digest(response),digest({'system':system,'payload':payload,'schema':schema}))
        return response

    def evolve(self, ctx, processor, new_ids, *, job_id, k=4, max_attempts=3):
        from .evolution import evolve
        return evolve(self,ctx,processor,new_ids,job_id=job_id,k=k,max_attempts=max_attempts)

    def apply_evolution(self, ctx, job_id, input_ids, proposal, processor_id):
        ticket=self._tickets.pop(id(proposal),None)
        if not ticket or ticket[0] is not proposal or ticket[1:5]!=(ctx.session_id,tuple(dict.fromkeys(input_ids)),processor_id,digest(proposal)):
            raise AccessDenied('Missing or altered full-input processing manifest')
        with self.transaction():
            rows = {rid: self._resource(ctx,rid,'derive',processor_id) for rid in input_ids}
            from .evolution import validate_proposal
            validate_proposal(proposal,rows)
            updates, edges = proposal.get('updates',[]), proposal.get('edges',[])
            # Validate the entire proposal before publishing any resources.
            for update in updates:
                self.require(ctx,update['id'],'write')
            for edge in edges:
                for rid in (edge['from'],edge['to']):self.require(ctx,rid,'link')
            outputs = []
            for i, update in enumerate(updates):
                row = rows[update['id']]
                rid = f'enrichment:{job_id}:{i}'
                # Never mutate the original node, metadata or vector with mixed input.
                self.create(ctx,rid,'enrichment',{'base_memory_id':row['id'],
                    'text':row['payload'].get('text',''),
                    'semantic':{x:update.get(x,[] if x!='context' else '') for x in ('keywords','tags','context')}},
                    policy_id=row['policy_id'],compartment=row['compartment'],attributes=row['attributes'],
                    dependencies=input_ids,generated=True)
                self.index(ctx,rid)
                outputs.append(rid)
            for i, edge in enumerate(edges):
                row = rows[edge['from']]
                rid = f'edge:{job_id}:{i}'
                self.link(ctx,rid,edge['from'],edge['to'],edge['predicate'],policy_id=row['policy_id'],
                          compartment=row['compartment'],dependencies=input_ids)
                outputs.append(rid)
            self.db.execute('INSERT OR IGNORE INTO provenance VALUES (?,?,?,?,?)',
                            (job_id,encoded(input_ids),encoded(outputs),processor_id,ticket[5]))

    def release(self, ctx, input_ids, output, *, recipient=None):
        # Buffered release linearizes under the same write lock/transaction as
        # policy, membership and deletion changes. No model text streams early.
        with self.transaction():
            self._session(ctx,'read')
            if recipient is not None and recipient != ctx.principal_id:
                raise AccessDenied('Explicit recipient session required for export')
            for rid in input_ids:
                self.require(ctx, rid)
            return json.loads(encoded(output))

    def export(self, ctx, recipient_ctx, resource_ids):
        with self.transaction():
            for rid in resource_ids:
                self.require(ctx,rid,'export')
                self.require(recipient_ctx,rid)
            return [self.read(recipient_ctx,rid) for rid in resource_ids]

    def delete(self, ctx, resource_id):
        with self.transaction():
            self.require(ctx,resource_id,'delete')
            self._tickets.clear()
            affected = {resource_id}
            changed = True
            rows = list(self.db.execute('SELECT id,dependencies FROM resources WHERE state!=\'deleted\''))
            while changed:
                before = len(affected)
                affected.update(row['id'] for row in rows if set(json.loads(row['dependencies'])) & affected)
                affected.update(row[0] for row in self.db.execute('SELECT view_id,source_id FROM source_views') if row[1] in affected)
                changed = len(affected) != before
            for rid in affected:
                self.db.execute('INSERT OR IGNORE INTO tombstones VALUES (?,?,?)',(rid,ctx.principal_id,time.time()))
                self.db.execute('UPDATE resources SET state=\'deleted\',payload=\'{}\',attributes=\'{}\' WHERE id=?',(rid,))
                self.db.execute('DELETE FROM representations WHERE id=?',(rid,))
            return len(affected)

    def event(self, ctx, event_id):
        session=self._session(ctx,'ingest')
        row = self.db.execute('SELECT * FROM events WHERE id=?',(event_id,)).fetchone()
        if row:
            scopes=set(json.loads(row['details']).get('_authorized_compartments',[]))
            if not scopes or not scopes<=session[2]:
                raise AccessDenied('Unavailable')
        return dict(row) if row else None

    def record_event(self, ctx, event_id, content_digest, state, details):
        session=self._session(ctx,'ingest')
        with self.transaction():
            old = self.event(ctx,event_id)
            if old and old['digest'] != content_digest:
                raise ValueError('Idempotency conflict')
            self.db.execute('INSERT INTO events VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,details=excluded.details',
                            (event_id,content_digest,state,encoded({**details,'_authorized_compartments':sorted(session[2])})))

    def inventory(self, ctx):
        self._session(ctx,'audit')
        counts={}
        for row in self.db.execute("SELECT id,kind FROM resources WHERE state='active'"):
            if self.allowed(ctx,row['id']):counts[row['kind']]=counts.get(row['kind'],0)+1
        return counts

    def diagnose(self, ctx, requester, resource_id, action='read'):
        """Operator-only explanations. Never attached to an unauthorized response."""
        self._session(ctx,'audit')
        with self.transaction():
            row = self.db.execute('SELECT * FROM resources WHERE id=?',(resource_id,)).fetchone()
            if not row: return 'no_relevant_evidence'
            self._session(ctx,'audit',row['compartment'])
            owner = self.db.execute("SELECT issuer FROM authority_owners WHERE kind='policy' AND id=?",(row['policy_id'],)).fetchone()
            if not owner or owner[0] != ctx.principal_id: raise AccessDenied('Unavailable')
            if self.allowed(requester,resource_id,action): return 'permitted'
            try: session = self._session(requester,action,row['compartment'])
            except AccessDenied: return 'unknown_identity_or_inactive_session'
            if row['state'] != 'active': return 'expired_or_revoked_permission'
            policies = [json.loads(p[0]) for p in self.db.execute(
                'SELECT p.body FROM access_edges e JOIN policies p ON p.id=e.policy_id WHERE e.memory_id=?',(resource_id,))]
            if any(self._rule_matches(r,requester,action,row,session) for b in policies for r in b.get('denies',[])):
                return 'explicit_denial'
            for a in self.db.execute('SELECT a.id,a.body FROM assignments a JOIN access_edges e ON a.id=e.assignment_id WHERE e.memory_id=?',(resource_id,)):
                rule = json.loads(a['body'])['rule']
                # Check identity/scope independently of validity to identify stale grants.
                untimed = {k:v for k,v in rule.items() if k not in {'from','until','purpose'}}
                if self._rule_matches(untimed,requester,action,row,session) and not self._assignment_live(a['id'],session,action):
                    return 'expired_or_revoked_permission'
            if any(not self.allowed(requester,p,'read') for p in json.loads(row['dependencies'])):
                return 'blocked_dependency'
            return 'missing_or_unresolved_assignment'

    def rebuild_index(self,ctx,compartments):
        # Old spaces never rank, even when dimensions agree. Rebuild approved
        # resources and policies; tombstones and non-active resources stay absent.
        rebuilt=0
        with self.transaction():
            for compartment in compartments:
                self._session(ctx,'ingest',compartment)
                ids=[r[0] for r in self.db.execute("SELECT id FROM resources WHERE compartment=? AND kind IN ('memory','enrichment') AND state='active'",(compartment,))]
                for rid in ids:
                    if self.allowed(ctx,rid,'derive',processor=self.embedder.processor_id):
                        self.index(ctx,rid);rebuilt+=1
            self._session(ctx,'manage_policy')
            for row in self.db.execute("SELECT p.id,p.body FROM policies p JOIN authority_owners a ON a.kind='policy' AND a.id=p.id WHERE a.issuer=?",(ctx.principal_id,)).fetchall():
                self.db.execute('INSERT OR REPLACE INTO privacy_vectors VALUES (?,?,?)',
                    (row['id'],self.embedder.space_id,encoded(self.embedder.encode([row['body']])[0])))
        return rebuilt
