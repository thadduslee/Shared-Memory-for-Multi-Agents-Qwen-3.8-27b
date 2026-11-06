"""Host-compiled staff access from cited care involvement, scoped by role.

A role alone grants nothing. A grant needs a resolved fact in which the model says
this worker is doing care or service work for the subject, and its citation must
stay live. The role then limits the scope. Fact labels decide which facts each
role's scope covers, so a new grant needs no per-pair review call. Restrictions,
revocation, expiry and erasure still apply, and a revoked grant never reactivates.
"""
from dataclasses import asdict
import json

from .system import digest, encoded
from . import fact_candidates as facts

BATCH = 24
ROUNDS = 8


def identifier(subject, worker):
    return 'care-read:' + digest([subject, worker])[:24]


def labels(bootstrap, decision, author, named, disclosed, workers):
    """Keep only labels that the host can check against this fact."""
    subjects = disclosed - workers
    speaker_can_attest = author in workers or author in subjects
    role = lambda person: bootstrap.directory.get(person, {}).get('role')
    people = [p for p in decision.get('care_participants') or []
        if speaker_can_attest and p in workers and role(p) in bootstrap.care_scopes and (p == author or p in named)]
    roles = [r for r in decision.get('care_roles') or [] if r in bootstrap.care_scopes]
    return {'participants': sorted(set(people)), 'roles': sorted(set(roles))}


def compile_grants(updater, bootstrap, state, touched, sources):
    for candidate in facts.rows(updater, prefix=sources):
        care = candidate.get('care') or {}
        if not care.get('participants') or not facts.applicable(updater.core, candidate['id']):
            continue
        subjects = [s for s in candidate['information_subjects']
                    if bootstrap.directory.get(s, {}).get('role') not in bootstrap.worker_roles]
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


def restricted(assignment, state):
    return any(r['subject'] == assignment['subject'] and assignment['participant'] in r['participants']
               and set(assignment['actions']) & set(r['actions']) and not r.get('superseded_by')
               for r in state['restriction'].values())


def labelled(candidate, assignment, state):
    """True when the fact's label decides this pair; restrictions need a model review."""
    return bool(assignment.get('care_v19') and 'care' in candidate and not restricted(assignment, state))


def apply_labels(updater):
    """Publish label decisions through the normal binding validator, with no model call."""
    from .incremental import pairs, reconcile_binding_reviews, update_binding_status, validate_bindings
    from . import permission_state as sliding
    c = updater.core
    role_of = lambda a: updater.bootstrap.directory.get(a['participant'], {}).get('role')
    changes = []
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
                'in_scope': role_of(state['assignment'][r['assignment_id']]) in byid[r['candidate_id']]['care']['roles'],
                'subject_supported': True, 'blocked_by': [],
                'reason': 'Fact label for this role scope.'} for r in required]
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
