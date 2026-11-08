"""Source-grounded V19 control uncertainty; never an access grant.

Uncertainty protection is TEMPORARY: it applies only while its event is still
actively processed. Unknown dimensions are wildcards during that window. Once an
event is finalized (ordinary publication or the bounded independent assessment)
no lasting uncertainty hold remains, whatever its processing status; validated
restrictions, revocations and erasures live in the existing ledgers instead.
Processing and factual outcomes remain in their own ledgers.
"""
from copy import deepcopy
import json
import re

from .system import digest, encoded
from . import revocation_v19 as recipients

PROMPT_VERSION = 'permission-inference-v19-scoped-control-holds-v1'
POLICY_VERSION = 'v19-scoped-control-holds:2'
INSTRUCTION = '''
V19 control uncertainty is distinct from processing failure and factual ambiguity.
Assess the complete original event. Questions may contain adverse access controls.
An invalid grant/reference does not establish a control or retract one. Existing
approved bindings remain subject to current grants, decisions and restrictions,
including restrictions the host validated independently for an earlier failed
event. No approval bypass exists. A later ordinary event cannot resolve an earlier
failed control. Host control assessments are not grant evidence.
'''


def enabled(updater):
    return recipients.enabled(updater)


def installed(core, compartment):
    return bool(core.db.execute('SELECT 1 FROM privacy_metadata WHERE key=?',
        ('control-policy:'+compartment,)).fetchone())


def install(updater):
    c = updater.core
    from .privacy_fallback_v19 import install as install_independent
    install_independent(updater)
    c.db.execute('''CREATE TABLE IF NOT EXISTS privacy.control_assessments_v19(
        compartment TEXT NOT NULL, job_id TEXT NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY(compartment,job_id))''')
    c.db.execute('INSERT OR REPLACE INTO privacy_metadata VALUES (?,?)',
        ('control-policy:'+updater.compartment, POLICY_VERSION))
    c._tickets.clear()


def rows(core, compartment):
    if not installed(core, compartment):
        return []
    return [json.loads(r[0]) for r in core.db.execute(
        'SELECT body FROM control_assessments_v19 WHERE compartment=? ORDER BY job_id', (compartment,))]


def _save(updater, row, transition, reason, evidence=None):
    row = deepcopy(row)
    row['state'] = transition
    item = {'state': transition, 'reason': reason, 'evidence': evidence,
        'policy_version': POLICY_VERSION}
    if not row['history'] or row['history'][-1] != item:
        row['history'].append(item)
    updater.core.db.execute('INSERT OR REPLACE INTO control_assessments_v19 VALUES (?,?,?)',
        (updater.compartment, row['job_id'], encoded(row)))
    if transition == 'VALIDATED_NON_CONTROL' or row.get('finalization'):
        # Existing broad holds are resolved explicitly, retaining their original
        # body/reason and resolution audit. New non-controls create no hold.
        for hold in updater.core.db.execute('SELECT id,body FROM permission_control_holds WHERE compartment=? AND job_id=? AND state!=?',
                (updater.compartment,row['job_id'],'resolved')).fetchall():
            body = json.loads(hold['body'])
            if body.get('resolution'):
                body.setdefault('resolution_history',[]).append(body['resolution'])
            body['resolution']={'job_id':row['job_id'],'source_id':row['source_id'],
                'kind':'original_event_non_control_reassessment' if transition == 'VALIDATED_NON_CONTROL'
                    else 'event_uncertainty_released','reason':reason,'evidence':evidence,
                'policy_version':POLICY_VERSION}
            updater.core.db.execute("UPDATE permission_control_holds SET body=?,state='resolved' WHERE id=?",
                (encoded(body),hold['id']))
    updater.core._tickets.clear()
    return row


def _sources(updater, job):
    # Reassessment is of the ORIGINAL source and its authenticated prefix.
    # A later unrelated source or answer query supplies no semantic evidence.
    return {sid: json.loads(r[0]) for sid in json.loads(job['inputs'])
        for r in [updater.core.db.execute('SELECT payload FROM resources WHERE id=?', (sid,)).fetchone()]
        if r and updater.core._source_live(sid) and updater.core._current(sid)}


def register(updater, job):
    existing = next((r for r in rows(updater.core, updater.compartment) if r['job_id'] == job['id']), None)
    if existing:
        return existing
    sources = _sources(updater, job)
    body = sources.get(job['source_id'])
    boot = updater._source_bootstrap(sources)
    actor = body['source']['participant'] if body else None
    subjects = {s for s, caps in boot.authorities.items() if actor in caps.get('restrict', [])}
    for a in updater._state()['activity'].values():
        if actor and actor in {a.get('participant'), a.get('requester')}:
            if a.get('subject'):
                subjects.add(a['subject'])
            subjects.update(g['subject'] for g in updater._state()['assignment'].values()
                if g.get('activity_id') == a['id'])
    if not body or body['source']['source_identity'] in boot.workflow_identities:
        subjects = None
    boundary = {'subjects': sorted(subjects) if subjects is not None else None,
        'recipients': None, 'actions': None, 'fields': None, 'resources': None,
        'timing': {'effective': 'unknown'},
        'unresolved': ['recipients', 'actions', 'fields', 'resources', 'timing', 'meaning']}
    row = {'contract_version': 1, 'job_id': job['id'], 'source_id': job['source_id'],
        'actor': actor, 'original': {'boundary': deepcopy(boundary),
            'source_digest': digest(body) if body else None},
        'boundary': boundary, 'history': []}
    return _save(updater, row, 'UNASSESSED', 'Authenticated authority ceiling; meaning not yet validated')


def _ordinary_medication_question(text):
    # Positive, full-source grammar. Neither '?' nor a question keyword proves
    # harmlessness; no trailing clauses, recipients or access phrases fit this.
    medicine = r'(?:the |my )?(?:prenatal vitamin|iron|vitamins?|nifedipine|labetalol)'
    return bool(re.fullmatch(r'(?:And )?(?:I should|Should I) (?:keep|continue|take|stop) '
        + medicine + r'(?: and ' + medicine + r')?\??', text.strip(), re.I))


def _adverse(updater, sources, sid):
    body = sources[sid]
    text = body['text'].strip().replace('\u2019', "'")
    subject = body['source'].get('subject') or body['source']['participant']
    boot = updater._source_bootstrap(sources)
    # Reuse Batch 1's independently grounded revoke compiler, not model IDs.
    try:
        requests = recipients.requests(updater, sources, sid)
    except Exception as exc:
        from .permissions import PermissionProposalRejected
        if not isinstance(exc, PermissionProposalRejected):
            raise
        requests = []
    if len(requests) == 1:
        request = requests[0]
        # Batch 1 can parse one sentence within a compound event. Such a parse
        # cannot narrow the boundary of the remaining unvalidated instructions.
        if request['reference']['quote'].strip().replace('\u2019', "'") == text:
            return {'subjects': [request['subject']], 'recipients': request['recipients'],
                'actions': None if '*' in request['actions'] else request['actions'],
                'fields': None if request['scope']['kind'] == 'all' else [request['scope']['value']],
                'resources': None, 'timing': {'effective': 'now'}, 'unresolved': []}, request
    # Requests in interrogative form still change access. Full-source grammar
    # validates dimensions independently; an unknown name never becomes irrelevant.
    match = re.fullmatch(r'(?:Please |(?:Could|Can|Would) you )?stop sharing '
        r'(?:(?:my )?(.+?) (?:information )?)?with (.+?)(?: (now|tomorrow))?[.?]?', text, re.I)
    if not match or body['source']['participant'] not in boot.authorities.get(subject, {}).get('restrict', []):
        return None
    field, reference, timing = match.groups()
    reference = reference.rstrip('.?')
    parts = re.split(r'\s+and\s+|,\s*', reference)
    # Unresolved identity is narrower uncertainty than an unparsed clause. An
    # unsupported trailing instruction/condition must not hide behind a known
    # field selector. Validate the entire reference grammar before narrowing.
    if any(part not in boot.participants and not re.fullmatch(recipients.NAME, part)
            and not re.fullmatch(r'my (?:'+'|'.join(recipients.RELATIONS)+r')|her|him|them', part)
            for part in parts):
        return None
    # "Sharing" is adverse but does not enumerate primitive kernel actions.
    # Keep that dimension conservative rather than inventing an action exemption.
    unresolved = ['actions']
    selected = None
    if field:
        selected = recipients.SCOPES.get(recipients.norm(field))
        if not selected:
            # Unsupported field text could itself contain another instruction.
            return None
    resolved = []
    proofs = {}
    try:
        names = recipients.identities(boot, sources)
        for part in parts:
            pid, evidence = recipients.resolve_reference(part, body['source']['participant'], subject, names, sources)
            resolved.append(pid); proofs[pid] = evidence
    except Exception as exc:
        from .permissions import PermissionProposalRejected
        if not isinstance(exc, PermissionProposalRejected):
            raise
        resolved = None; unresolved.append('recipients')
    if timing and timing.lower() != 'now':
        unresolved.append('timing')
    return {'subjects': [subject], 'recipients': sorted(set(resolved)) if resolved is not None else None,
        'actions': None, 'fields': [selected] if selected else None, 'resources': None,
        'timing': {'effective': 'unknown' if 'timing' in unresolved else 'now'},
        'unresolved': unresolved}, {'reference': recipients.receipt(sid, body['text'], 0, len(body['text'])),
            'identity_relationship_evidence': proofs}


def reassess(updater, job_id, *, identity_source_ids=()):
    """Explicit local reassessment, preserving original failure and source budget.

    Unsupported semantics do not resolve. Never accepts caller-supplied scope,
    model-selected IDs, replacement prose, timestamps or answer requests as proof.
    """
    c = updater.core
    c._session(updater.ctx, 'manage_policy', updater.compartment)
    with c.transaction():
        job = c.db.execute('SELECT * FROM permission_jobs WHERE id=? AND compartment=?',
            (job_id, updater.compartment)).fetchone()
        if not job or job['version'].split('-', 1)[0] != '19':
            raise ValueError('Unknown V19 event')
        row = register(updater, job)
        sources = _sources(updater, job)
        source = sources.get(job['source_id'])
        if not source:
            return _save(updater, row, 'POSSIBLE_ADVERSE', 'Original event evidence unavailable')
        if not isinstance(identity_source_ids,(list,tuple)) or any(not isinstance(s,str) for s in identity_source_ids):
            raise ValueError('Explicit identity evidence source IDs required')
        for sid in identity_source_ids:
            evidence=c.db.execute("SELECT payload FROM resources WHERE id=? AND compartment=? AND kind='observation'",
                (sid,updater.compartment)).fetchone()
            if not evidence or not c._source_live(sid) or not c._current(sid):
                raise ValueError('Identity evidence must be a live authenticated observation in this compartment')
            sources[sid]=json.loads(evidence[0])
        # A finalized event is never reassessed: queries, restarts and later
        # turns cannot reopen it, reset its budget or recreate its hold.
        if row['state'] in {'CONTROL_VALIDATED_NO_CHANGE','CONTROL_COMMITTED'} or row.get('finalization'):
            return row
        if _ordinary_medication_question(source['text']):
            return _save(updater, row, 'VALIDATED_NON_CONTROL',
                'Complete original event is an ordinary medication-continuation question',
                {'source_id': job['source_id'], 'source_digest': digest(source), 'validator': 'medication-question-v1'})
        adverse = _adverse(updater, sources, job['source_id'])
        if adverse:
            row['boundary'], proof = adverse
            row['boundary_evidence'] = proof
            complete = not set(row['boundary']['unresolved']) - {'actions'}
            return _save(updater, row, 'POSSIBLE_ADVERSE', 'Validated complete adverse boundary' if complete else
                'Adverse boundary with unresolved ' + ', '.join(row['boundary']['unresolved'])
                + '; temporary protection only while the event is processed', proof)
        if re.search(r"\b(?:revoke|revoked|delete|erase|restrict|withdraw)\b|"
                r"\b(?:stop|not|never|don't|no longer)\b.*\b(?:shar\w*|access|read|see|tell|allow|consent|permission)\b",
                source['text'], re.I):
            return _save(updater, row, 'POSSIBLE_ADVERSE',
                'Potential adverse meaning outside the supported complete boundary grammar')
        # No negative keyword heuristic can prove non-control. Retain the ceiling.
        return _save(updater, row, row['state'], 'Original event remains outside the closed local semantic grammar')


def validated_no_change(updater, job_id, canonical, wire):
    """Control success survives an independent factual/publication failure."""
    row = next(r for r in rows(updater.core, updater.compartment) if r['job_id'] == job_id)
    if (row['state']=='UNASSESSED' and not canonical['operations'] and not wire['permission_changes']
            and wire['control_review'] and all(r['status']=='no_change' for r in wire['control_review'])):
        _save(updater,row,'CONTROL_VALIDATED_NO_CHANGE',
            'Complete original-event control review and canonical no-change validation succeeded independently of factual processing',
            {'source_id':row['source_id'],'control_review':deepcopy(wire['control_review']),
                'canonical_digest':digest(canonical)})


def published(updater, job_id):
    """Finalize after the complete ordinary control review published atomically.

    A complete local adverse boundary is an independently validated control:
    it is retained prospectively as a durable restriction, so publishing a revoke
    of today's grants cannot exempt tomorrow's grant. A partial local signal is
    uncertainty, released because the complete ordinary review succeeded.
    """
    from .privacy_fallback_v19 import publish_local, close
    row = next(r for r in rows(updater.core, updater.compartment) if r['job_id'] == job_id)
    if row.get('finalization'):
        return
    released = []
    if row['state'] == 'POSSIBLE_ADVERSE' and publish_local(updater, job_id, compile_revocations=False) is None:
        released.append({'kind': 'local_boundary', 'unresolved': row['boundary'].get('unresolved'),
            'reason': 'Partial local adverse signal released after the complete ordinary control review'})
    row = next(r for r in rows(updater.core, updater.compartment) if r['job_id'] == job_id)
    close(updater, row, 'VALIDATED_NON_CONTROL' if row['state'] == 'VALIDATED_NON_CONTROL' else 'CONTROL_COMMITTED',
        'CONTROL_COMMITTED', 'ordinary_publication', unresolved=released)


def proven_fields(text):
    """Closed complete-fact grammar, not a bag of recognized topic keywords."""
    patterns = {
        'billing': r'(?:The )?(?:billing )?invoice is (?:now )?\d+(?:\.\d+)? dollars[.!]?',
        'scheduling': r'(?:The )?appointment is (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) at \d{1,2}(?::\d{2})?(?: AM| PM)?[.!]?',
        'pickup': r'Pickup is at \d{1,2}(?::\d{2})? (?:AM|PM)[.!]?',
    }
    return {field for field, pattern in patterns.items() if re.fullmatch(pattern, text.strip(), re.I)}


def intersects(boundary, *, subject=None, recipient=None, action=None, text='', resource_id=None):
    for key, value in [('subjects', subject), ('recipients', recipient), ('actions', action), ('resources', resource_id)]:
        selected = boundary.get(key)
        if selected is not None and value is not None and value not in selected:
            return False
        if selected == []:
            return False
    selected = boundary.get('fields')
    if selected == []:
        return False
    known = proven_fields(text)
    if selected is not None and known and not known.intersection(selected):
        return False
    # Unknown timing is effective conservatively. No clock or queue progress can
    # resolve meaning; supported controls currently have immediate timing only.
    return True


def _boundary(core, row):
    # Loss of narrowing evidence widens back to the original authority ceiling.
    proof = row.get('boundary_evidence', {})
    evidence = {row['source_id']}
    evidence.update(p['source_id'] for proofs in proof.get('identity_relationship_evidence', {}).values() for p in proofs)
    return row['boundary'] if all(core._source_live(s) and core._current(s) for s in evidence) else row['original']['boundary']


def blocks(core, compartment, **binding):
    """Temporary uncertainty protection for events still actively processed.

    Finalized events never hold, whatever their failed/quarantined/pending
    processing status. Validated controls are enforced by the permission gate.
    """
    from .privacy_fallback_v19 import installed as current_policy, holding
    if not current_policy(core, compartment):
        return _historical_blocks(core, compartment, **binding)
    from .control_holds import rows as legacy_rows
    assessments = {r['job_id']: r for r in rows(core, compartment)}
    statuses = {r['id']: r['status'] for r in core.db.execute("SELECT id,status FROM permission_jobs WHERE compartment=? "
        "AND status NOT IN ('complete','superseded','quarantined','skipped')", (compartment,))}
    legacy = legacy_rows(core, compartment)
    for jid in set(statuses) | {h['job_id'] for h in legacy}:
        row = assessments.get(jid)
        if row is None:
            # Unregistered active work, e.g. reassessment after evidence loss.
            holds = [h for h in legacy if h['job_id'] == jid]
            if not holds or any(intersects({'subjects': [h['subject']] if h['subject'] else None}, **binding) for h in holds):
                return True
        elif holding(core, compartment, row, statuses.get(jid)) and intersects(_boundary(core, row), **binding):
            return True
    return False


def _historical_blocks(core, compartment, **binding):
    """Unchanged Batch 4 predicate for historical stores read by newer code."""
    assessments = {r['job_id']: r for r in rows(core, compartment)}
    # Old unresolved jobs/holds require explicit reassessment. Never infer that
    # migration, restart or approval supplies a missing semantic assessment.
    from .control_holds import rows as legacy_rows
    jobs = {r['id'] for r in core.db.execute("SELECT id FROM permission_jobs WHERE compartment=? "
        "AND status NOT IN ('complete','superseded')", (compartment,))}
    legacy = legacy_rows(core, compartment)
    jobs.update(h['job_id'] for h in legacy)
    jobs.update(r['job_id'] for r in assessments.values() if r['state'] == 'POSSIBLE_ADVERSE')
    for jid in jobs:
        row = assessments.get(jid)
        if row:
            if row['state'] in {'VALIDATED_NON_CONTROL', 'CONTROL_VALIDATED_NO_CHANGE', 'CONTROL_COMMITTED'}:
                if core._source_live(row['source_id']) and core._current(row['source_id']):
                    continue
            proof = row.get('boundary_evidence', {})
            evidence = {row['source_id']}
            evidence.update(p['source_id'] for proofs in proof.get('identity_relationship_evidence', {}).values() for p in proofs)
            boundary = row['boundary'] if all(core._source_live(s) and core._current(s) for s in evidence) else row['original']['boundary']
            if intersects(boundary, **binding):
                return True
        else:
            holds = [h for h in legacy if h['job_id'] == jid]
            if not holds or any(intersects({'subjects': [h['subject']] if h['subject'] else None}, **binding) for h in holds):
                return True
    return False


def validate_resolution(updater, hold):
    from .permissions import PermissionProposalRejected
    row = next((r for r in rows(updater.core, updater.compartment) if r['job_id'] == hold['job_id']), None)
    if not row or row['state'] != 'VALIDATED_NON_CONTROL':
        raise PermissionProposalRejected('Explicit reassessment of the original event is required; unresolved adverse protection cannot be cleared')
