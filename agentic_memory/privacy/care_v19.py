"""Host-compiled staff access from cited care involvement, scoped by role.

A role alone grants nothing. A grant needs a resolved fact in which the model says
this worker is doing care or service work for the subject, and its citation must
stay live. The model tags each fact with its information types, and a host table
says which types each role may read, so a new grant needs no per-pair review call.
An owner's access to their own attributed fact needs no review call either.
Restrictions always go to a model review. Revocation, expiry and erasure still
apply, and a revoked grant never reactivates.

Optionally (learned_access, off unless the host sets it) the table also grows from
the source stream: a role that keeps writing a type while working may read that
type, and a reply may let the person it answers read the reply's types. Learned
cells are host state, never shown to the model, and they never widen past a grant's
restrictions, revocation, expiry or erasure.
"""
from dataclasses import asdict
from datetime import datetime, timedelta
import json
import re
from zoneinfo import ZoneInfo

from .system import digest, encoded
from . import fact_candidates as facts
from .attribution_v19 import record_owners

BATCH = 24
ROUNDS = 8
# Asking to be given information is not doing the work, however the rest reads.
REQUEST = re.compile(r"\b(?:send|give|show|tell|remind|forward|share)\s+(?:me|us)\b", re.I)


def identifier(subject, worker):
    return 'care-read:' + digest([subject, worker])[:24]


def labels(bootstrap, decision, author, named, disclosed, workers, source_text=''):
    """Keep only labels that the host can check against this fact."""
    subjects = disclosed - workers
    speaker_can_attest = author in workers or author in subjects
    role = lambda person: bootstrap.directory.get(person, {}).get('role')
    def attested(field):
        return sorted({p for p in decision.get(field) or []
            if speaker_can_attest and p in workers and role(p) in bootstrap.care_scopes and (p == author or p in named)})
    # The whole turn counts, so "I am covering. Send me the note." establishes nothing.
    requesting = author if REQUEST.search(source_text) else None
    people = [p for p in attested('care_participants') if p != requesting]
    types = [t for t in decision.get('information_types') or [] if t in bootstrap.information_types]
    result = {'participants': people, 'types': sorted(set(types))}
    if bootstrap.temporary_care_hours:
        result.update(temporary=attested('temporary_workers'), ended=attested('ended_workers'))
    return result


def compile_grants(updater, bootstrap, state, touched, sources):
    for candidate in facts.rows(updater, prefix=sources):
        care = candidate.get('care') or {}
        if not care.get('participants') or not facts.applicable(updater.core, candidate['id']):
            continue
        subjects = [s for s in record_owners(candidate) if bootstrap.directory.get(s, {}).get('role') not in bootstrap.worker_roles]
        if len(subjects) != 1:  # A mixed fact does not establish care for either person.
            continue
        subject = subjects[0]
        for worker in care['participants']:
            aid = identifier(subject, worker)
            if worker == subject or aid in state['assignment']:
                continue  # In particular, never reactivate a revoked care grant.
            scope = bootstrap.care_scopes[bootstrap.directory[worker]['role']]
            evidence = [candidate['source_id']]
            state['assignment'][aid] = {
                'id': aid, 'participant': worker, 'subject': subject, 'authority': subject,
                'basis': 'explicit', 'care_v19': True,
                'actions': ['search', 'read', 'derive'], 'information_scope': scope,
                'status': 'active', 'bindings': [], 'validity': {},
                'evidence': {'information_scope': evidence},
                'scope_evidence': [{'source_id': candidate['source_id'], 'text': candidate['quote']}],
                'explanation': 'Host care convention: cited involvement in this subject\'s care, limited by role.',
                'policy_hash': digest(asdict(updater.bootstrap)), 'inference_version': updater.version,
            }
            touched.add(('assignment', aid))
    if bootstrap.temporary_care_hours:
        limit_temporary_grants(updater, bootstrap, state, touched, sources)


def event_time(bootstrap, source):
    value = source['source'].get('occurred_at')
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(bootstrap.timezone or 'UTC'))


def limit_temporary_grants(updater, bootstrap, state, touched, sources):
    """Expire time-limited involvement, measured from the worker's last involvement.

    Attribution does not matter here, since these statements describe the worker.
    Once a role has ended it stays ended, so later remarks cannot reopen the chart.
    """
    temporary, ended, involved = set(), {}, {}
    for candidate in facts.rows(updater, prefix=sources):
        care = candidate.get('care') or {}
        when = event_time(bootstrap, sources[candidate['source_id']])
        if when is None:
            continue
        temporary.update(care.get('temporary') or [])
        for worker in care.get('ended') or []:
            if worker not in ended or when < ended[worker][0]:
                ended[worker] = (when, candidate['source_id'])
        for worker in care.get('participants') or []:
            if worker not in involved or when >= involved[worker][0]:
                involved[worker] = (when, candidate['source_id'])
    for aid, assignment in state['assignment'].items():
        worker = assignment.get('participant')
        if not assignment.get('care_v19') or assignment.get('status') != 'active':
            continue
        if worker in ended:
            until, source_id = ended[worker]
        elif worker in temporary and worker in involved:
            when, source_id = involved[worker]
            until = when + timedelta(hours=bootstrap.temporary_care_hours)
        else:
            continue
        validity = {'effective_until': until.isoformat()}
        if assignment.get('validity') != validity:
            assignment['validity'] = validity
            assignment['evidence']['validity'] = [source_id]
            touched.add(('assignment', aid))


def restricted(assignment, state):
    return any(r['subject'] == assignment['subject'] and assignment['participant'] in r['participants']
               and set(assignment['actions']) & set(r['actions']) and not r.get('superseded_by')
               for r in state['restriction'].values())


def labelled(candidate, assignment, state):
    """True when no model review is needed; restrictions always get one."""
    return bool((assignment.get('care_v19') or assignment.get('ownership')) and 'care' in candidate
                and not restricted(assignment, state))


LEARNED = 'learned-access:'


def single_subject(bootstrap, candidate):
    subjects = [s for s in record_owners(candidate) if bootstrap.directory.get(s, {}).get('role') not in bootstrap.worker_roles]
    return subjects[0] if len(subjects) == 1 else None


def allowed_types(bootstrap, person, subject, learned):
    """Host table row, plus cells learned for the role, plus types learned for this person on this subject."""
    role = bootstrap.directory.get(person, {}).get('role')
    return (set(bootstrap.role_information_types.get(role, ())) | set(learned.get('role:' + str(role), {}))
            | set(learned.get(f'person:{person}:{subject}', {})))


def stored_learning(updater):
    return {r['key']: json.loads(r['body']) for r in updater.core.db.execute(
        'SELECT key,body FROM fact_processing WHERE job_id=?', (LEARNED + updater.compartment,))}


def learned_access(updater, candidates, sources, state):
    """Open table cells from the source stream itself. Cells only open; they never close.

    Production: a role may read a type once its members, each shown working for the
    fact's single subject in that same turn, wrote facts of that type in
    bootstrap.learned_access_sources different source turns.

    Replies: a source whose context has reply_to answers that earlier turn. Its
    speaker, if already shown working for the subject, may read the reply's types on
    that subject only, and only if the replier owns the record, holds authority over
    it, or may read those types themselves.

    Opened cells are stored with the source IDs that opened them, outside every model
    payload. Returns (learned, changed keys). Keys are 'role:<role>' and
    'person:<participant>:<subject>'; values map type -> source IDs.
    """
    b = updater.bootstrap
    c = updater.core
    if not b.learned_access_sources and not b.reply_access:
        return {}, set()
    learned = stored_learning(updater)
    found = {}
    live = [x for x in candidates if facts.applicable(c, x['id'])]
    if b.learned_access_sources:
        seen = {}
        for candidate in live:
            care = candidate.get('care') or {}
            author = candidate.get('author')
            if single_subject(b, candidate) is None or author not in (care.get('participants') or []):
                continue
            role = b.directory.get(author, {}).get('role')
            if role not in b.care_scopes:
                continue
            for kind in care.get('types') or []:
                if kind in b.role_information_types.get(role, ()):
                    continue
                ids = seen.setdefault(('role:' + role, kind), [])
                if candidate['source_id'] not in ids:
                    ids.append(candidate['source_id'])
        for (key, kind), ids in seen.items():
            if len(ids) >= b.learned_access_sources:
                found.setdefault(key, {})[kind] = ids[:b.learned_access_sources]
    merged = {key: {**kinds, **learned.get(key, {})} for key, kinds in found.items()}
    view = {**learned, **merged}
    if b.reply_access:
        speakers = {s['source'].get('event_id'): s['source'].get('participant') for s in sources.values()}
        for candidate in live:
            source = (sources.get(candidate['source_id']) or {}).get('source') or {}
            reply_to = (source.get('context') or {}).get('reply_to')
            if not reply_to:
                continue
            asker, author = speakers.get(reply_to), candidate.get('author')
            subject = single_subject(b, candidate)
            types = set((candidate.get('care') or {}).get('types') or [])
            if not asker or asker == author or subject is None or not types:
                continue
            grant = state['assignment'].get(identifier(subject, asker))
            if not grant or not grant.get('care_v19') or grant.get('status') != 'active':
                continue  # The asker must already be shown working for this subject.
            replier_may_share = (author == subject
                or author in (b.authorities.get(subject, {}).get('restrict') or [])
                or types <= allowed_types(b, author, subject, view))
            if not replier_may_share:
                continue
            key = f'person:{asker}:{subject}'
            for kind in sorted(types - allowed_types(b, asker, subject, view)):
                merged.setdefault(key, dict(learned.get(key, {}))).setdefault(kind, [candidate['source_id']])
    changed = set()
    for key, kinds in merged.items():
        if kinds != learned.get(key):
            c.db.execute('INSERT OR REPLACE INTO fact_processing VALUES (?,?,?,?)',
                (LEARNED + updater.compartment, key, digest(kinds), encoded(kinds)))
            learned[key] = kinds
            changed.add(key)
    return learned, changed


def reopen_decisions(updater, changed, candidates, state):
    """Drop host-label refusals for readers a newly opened cell may now cover."""
    if not changed:
        return
    b = updater.bootstrap
    roles = {k[len('role:'):] for k in changed if k.startswith('role:')}
    people = {tuple(k[len('person:'):].split(':', 1)) for k in changed if k.startswith('person:')}
    affected = {aid for aid, a in state['assignment'].items() if a.get('care_v19') and (
        b.directory.get(a['participant'], {}).get('role') in roles or (a['participant'], a['subject']) in people)}
    byid = {x['id']: x for x in candidates}
    job = 'incremental-reviews:' + updater.compartment
    for row in updater.core.db.execute('SELECT key,body FROM fact_processing WHERE job_id=?', (job,)).fetchall():
        # Keys are '<candidate id>:<assignment id>', and assignment IDs contain ':' too.
        aid = next((a for a in affected if row['key'].endswith(':' + a)), None)
        cid = row['key'][:-len(aid) - 1] if aid else None
        if aid and cid in byid and json.loads(row['body']).get('in_scope') is False \
                and labelled(byid[cid], state['assignment'][aid], state):
            updater.core.db.execute('DELETE FROM fact_processing WHERE job_id=? AND key=?', (job, row['key']))


def covers(bootstrap, assignment, candidate, learned=None):
    if assignment.get('ownership'):
        return True
    # Every tag must be allowed: one permitted tag cannot carry a fact's other content.
    types = set(candidate['care']['types'])
    return bool(types) and types <= allowed_types(bootstrap, assignment['participant'], assignment['subject'], learned or {})


def apply_labels(updater):
    """Publish label decisions through the normal binding validator, with no model call."""
    from .incremental import pairs, reconcile_binding_reviews, update_binding_status, validate_bindings
    from . import permission_state as sliding
    c = updater.core
    changes = []
    learned = {}
    if updater.bootstrap.learned_access_sources or updater.bootstrap.reply_access:
        with c.transaction():
            sources = {r['id']: json.loads(r['payload']) for r in c.db.execute(
                "SELECT id,payload FROM resources WHERE compartment=? AND kind='observation' AND state='active' ORDER BY rowid",
                (updater.compartment,)) if c._current(r['id'])}
            state = updater._state()
            candidates = facts.rows(updater, prefix=sources)
            learned, changed = learned_access(updater, candidates, sources, state)
            reopen_decisions(updater, changed, candidates, state)
    for _ in range(ROUNDS):
        with c.transaction():
            sources = {r['id']: json.loads(r['payload']) for r in c.db.execute(
                "SELECT id,payload FROM resources WHERE compartment=? AND kind='observation' AND state='active' ORDER BY rowid",
                (updater.compartment,)) if c._current(r['id'])}
            state = updater._state()
            candidates = facts.rows(updater, prefix=sources)
            byid = {p['id']: p for p in candidates}
            from . import scoped_holds_v19
            required = [pair for pair in pairs(updater, candidates, state, sources, include_labelled=True)
                if labelled(byid[pair['candidate_id']], state['assignment'][pair['assignment_id']], state)
                and not scoped_holds_v19.blocks(c, updater.compartment,
                    subject=state['assignment'][pair['assignment_id']]['subject'],
                    recipient=state['assignment'][pair['assignment_id']]['participant'], action='derive',
                    text=byid[pair['candidate_id']]['quote'])][:BATCH]
            if not required:
                return changes
            decisions = [{'candidate_id': r['candidate_id'], 'assignment_id': r['assignment_id'],
                'in_scope': covers(updater.bootstrap, state['assignment'][r['assignment_id']], byid[r['candidate_id']], learned),
                'subject_supported': True, 'blocked_by': [],
                'reason': 'Host table for this fact\'s information types.'} for r in required]
            revision = c.db.execute('SELECT revision FROM permission_bootstrap WHERE compartment=?',
                (updater.compartment,)).fetchone()[0]
            canonical = {'revision': revision, 'considered_restrictions': sorted(state['restriction']), 'operations': []}
            payload = {'revision': revision, 'new_source_id': None, 'sources': [], 'permission_state': sliding.access_state(state)}
            accepted, errors, ops = validate_bindings(updater, decisions, required, candidates, canonical,
                (state, set(), []), state, sources, payload)
            validated = updater._validate({**canonical, 'operations': ops}, state, sources, revision,
                available_source_ids=set(sources), available_records=payload['permission_state'], control_source_ids=set())
            reconcile_binding_reviews(validated, accepted, candidates)
            update_binding_status(validated)
            batch_id = 'care-labels:' + digest([updater.compartment, revision, required])
            changes.extend(updater._publish(validated, batch_id))
            for pair, decision in accepted.items():
                row = next(r for r in required if (r['candidate_id'], r['assignment_id']) == pair)
                c.db.execute('INSERT OR REPLACE INTO fact_processing VALUES (?,?,?,?)',
                    ('incremental-reviews:' + updater.compartment, pair[0] + ':' + pair[1], row['fingerprint'], encoded(decision)))
            c.db.execute('UPDATE permission_bootstrap SET revision=revision+1 WHERE compartment=?', (updater.compartment,))
            if not accepted:
                return changes
    return changes
