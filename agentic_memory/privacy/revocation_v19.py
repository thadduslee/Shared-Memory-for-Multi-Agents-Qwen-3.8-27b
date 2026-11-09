"""Fail-closed, source-grounded recipient revocation for V19.

This is a deliberately closed reference grammar, not a second identity model.
Authenticated self introductions bind names; only a subject's authenticated
statement binds that subject's kinship. Neither roles nor grant ownership nor
the active worker is reference evidence. Unsupported language stays unresolved.
All receipts are derived from live, compartment-local source bytes.
"""
from copy import deepcopy
import re


def enabled(updater):
    return updater.version.split('-', 1)[0] == '19'


def fail(message):
    from .permissions import PermissionProposalRejected
    raise PermissionProposalRejected('Unresolved V19 revocation: ' + message,
        {'stage': 'recipient_resolution', 'unresolved_access_hold': True})


def norm(value):
    return ' '.join(value.casefold().replace('\u2019', "'").split())


# Closed information-field vocabulary. Unsupported scopes remain unresolved.
# These are language rules, never benchmark entities, roles, or annotations.
FIELDS = {
    'scheduling': (r'\b(?:schedul\w*|appointments?|parking|arrival|check-in|logistics|'
        r'visits?|meetings?|consultations?|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b'
        r'|\b\d{1,2}:\d{2}\b'),
    'billing': r'\b(?:bill\w*|invoices?|payments?|insurance|statements?)\b',
    'contact': r'\b(?:contact|phone|numbers?|voicemail|callback|portal)\b',
    'clinical': r'\b(?:clinical|results?|medications?|diagnos\w*|symptoms?|labs?)\b',
    'budget': r'\b(?:budget|prices?|costs?|financ\w*)\b',
    'pickup': r'\b(?:pickup|parcel|delivery|entrance|loading)\b',
}
SCOPES = {'scheduling': 'scheduling', 'scheduling logistics': 'scheduling',
    'appointment details': 'scheduling', 'appointment times': 'scheduling',
    'billing': 'billing', 'contact': 'contact', 'clinical': 'clinical',
    'budget': 'budget', 'pickup instructions': 'pickup'}
RELATIONS = {'mother': 'mother', 'mom': 'mother', 'father': 'father',
    'dad': 'father', 'sister': 'sister', 'brother': 'brother',
    'wife': 'wife', 'husband': 'husband', 'partner': 'partner'}
NAME = r"[A-Z][\w-]*(?: [A-Z][\w-]*){0,3}"


def receipt(sid, text, start, end, **extra):
    return {'source_id': sid, 'spans': [[start, end]],
        'quote': text[start:end], **extra}


def identities(bootstrap, sources):
    """Names are authenticated self claims, including their original spans.

    Exact participant IDs are also usable when literally present in a request.
    No substring/role matching against opaque ID spellings is allowed.
    """
    names = {}
    for pid in bootstrap.participants:
        names.setdefault(norm(pid), {}).setdefault(pid, [])
    # A trusted directory identifies people who never introduced themselves. Two
    # people sharing an alias stay ambiguous, so a request cannot pick one.
    for pid, person in bootstrap.directory.items():
        for alias in person.get('aliases', []):
            names.setdefault(norm(alias), {}).setdefault(pid, [])
    for sid, body in sources.items():
        actor = body['source']['participant']
        text = body['text'].replace('\u2019', "'")
        # Case-sensitive name grammar keeps the remainder of an ordinary
        # "I am ..." sentence out of the identity registry.
        # A relationship word in a self introduction ("I am his sister Nina")
        # names only the speaker; it never proves the relationship itself.
        patterns = [r"\b(?:I'm|I am|My name is) (" + NAME + r")(?=[,.!?]|$)",
            r"\bI'm (?:her|his|their) (?:mom|mother|dad|father), (" + NAME + r")(?=[.!?]|$)",
            r"\b(?:I'm|I am) (?:her|his|their) (?:" + '|'.join(RELATIONS) + r"),? (" + NAME + r")(?=[,.!?]|$)"]
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                name = match.group(1)
                proof = receipt(sid, body['text'], *match.span(), participant=actor)
                for alias in {norm(name), norm(name.split()[0])}:
                    names.setdefault(alias, {}).setdefault(actor, []).append(proof)
    return names


def named(reference, names):
    matches = names.get(norm(reference), {})
    if len(matches) != 1:
        fail('recipient name is missing or ambiguous: ' + reference)
    pid = next(iter(matches))
    return pid, deepcopy(matches[pid])


def corroborated_relationships(subject, sources):
    """Recognize one bounded, explicitly corroborated conversational reference.

    Subject speaks; relative introduces their relationship/name; a question
    explicitly asks about that relationship; the SAME authenticated subject
    authorizes that relative to receive their information. Neither the relative's
    claim nor a question/ordinary "yes" alone establishes the relationship.
    """
    result = {}
    ordered = list(sources.items())
    relation_pattern = '|'.join(RELATIONS)
    introduction = (r"^\s*(?:I'm|I am) (?:her|his|their) (" + relation_pattern
        + r'), (' + NAME + r')(?=[.!?]|$)')
    for i in range(3, len(ordered)):
        initial, relative, question, confirmation = ordered[i-3:i+1]
        before, claim, ask, answer = [item[1] for item in (initial, relative, question, confirmation)]
        if before['source']['participant'] != subject or answer['source']['participant'] != subject:
            continue
        if any(item['source'].get('subject') not in (None, subject)
                for item in (before, claim, ask, answer)):
            continue
        match = re.match(introduction, claim['text'].replace('\u2019', "'"))
        if not match:
            continue
        rel = RELATIONS[match.group(1)]
        grantee = claim['source']['participant']
        question_relations = {RELATIONS[norm(m.group(1))] for m in re.finditer(
            r'\byour (' + relation_pattern + r')\b', ask['text'], re.I)}
        if (grantee == subject or ask['source']['participant'] in {subject, grantee}
                or question_relations != {rel} or '?' not in ask['text']):
            continue
        text = answer['text']
        if (not re.match(r'^\s*(?:Yes\b|Only\b)', text, re.I)
                or not re.search(r'\b(?:she|he|they) (?:can|may) (?:hear|receive|read|know)\b', text, re.I)
                or not re.search(r'\bmy\b', text, re.I) or '?' in text):
            continue
        proofs = [receipt(sid, body['text'], 0, len(body['text']),
            subject=subject, participant=grantee, relationship=rel,
            basis='adjacent_subject_corroboration')
            for sid, body in (initial, relative, question, confirmation)]
        result.setdefault(rel, {}).setdefault(grantee, []).extend(proofs)
    return result


def relationships(subject, names, sources):
    result = corroborated_relationships(subject, sources)
    contradicted = set()
    for sid, body in sources.items():
        # A relative's own claim cannot create a relationship for this subject.
        if body['source']['participant'] != subject:
            continue
        if body['source'].get('subject') not in (None, subject):
            continue
        text = body['text'].replace('\u2019', "'")
        relation = '|'.join(RELATIONS)
        negative = (r"\b(?:not|isn't|never|no longer) my (" + relation + r")\b|"
            r"\bmy (" + relation + r") (?:is not|isn't|is no longer)\b")
        for match in re.finditer(negative, text, re.I):
            contradicted.add(RELATIONS[norm(next(v for v in match.groups() if v))])
        patterns = [r'\b[Mm]y (' + relation + r') is (' + NAME + r')(?=[,.!?]|$)',
            r'\b(' + NAME + r') is my (' + relation + r')(?=[,.!?]|$)',
            r'\b[Mm]y (' + relation + r'), (' + NAME + r'),']
        for i, pattern in enumerate(patterns):
            for match in re.finditer(pattern, text):
                rel, name = (match.group(2), match.group(1)) if i == 1 else match.groups()
                candidates = names.get(norm(name), {})
                for pid, proofs in candidates.items():
                    result.setdefault(RELATIONS[rel], {}).setdefault(pid, []).extend([
                        *deepcopy(proofs), receipt(sid, body['text'], *match.span(),
                            subject=subject, participant=pid, relationship=RELATIONS[rel])])
    return {rel: candidates for rel, candidates in result.items() if rel not in contradicted}


def resolve_reference(reference, actor, subject, names, sources):
    reference = reference.strip()
    key = norm(reference)
    if key in {'me', 'myself', 'my', 'my own'}:
        return actor, []
    match = re.fullmatch(r'my (' + '|'.join(RELATIONS) + r')', key)
    if match:
        # "my" is relative to the requesting actor, never a worker or an
        # arbitrary grantee of the information subject.
        candidates = relationships(actor, names, sources).get(RELATIONS[match.group(1)], {})
        if len(candidates) != 1:
            fail('relationship does not identify one authenticated recipient: ' + reference)
        pid = next(iter(candidates))
        return pid, deepcopy(candidates[pid])
    return named(re.sub(r'^the ', '', reference, flags=re.I), names)


def scope(value):
    key = norm(value or '')
    if key in {'', 'all', 'all information', 'own read', 'read'}:
        return {'kind': 'all', 'value': '*'}
    if key in SCOPES:
        return {'kind': 'field', 'value': SCOPES[key]}
    fail('unsupported information scope')


def model_driven(updater):
    """With information types, the model reads the request and code verifies it."""
    return bool(updater.bootstrap.information_types)


def model_requests(updater, operations, state, sources, source_id):
    """Verify each model-proposed withdrawal; no wording decides what it means.

    The speaker must be the grant's own subject and the recipient must appear in
    the conversation. Every active grant of that person on that subject is covered,
    so another grant cannot quietly preserve what was withdrawn.
    """
    from .attribution_v19 import present_in_window
    body = sources.get(source_id)
    if body is None:
        fail('authenticated current source is unavailable')
    bootstrap = updater._source_bootstrap(sources)
    actor = body['source']['participant']
    present = present_in_window(updater, bootstrap, sources, source_id)
    result = {}
    for op in [o for o in operations if o.get('op') == 'revoke_assignment']:
        grant = state['assignment'].get(op.get('assignment_id'))
        if not grant or grant.get('status') != 'active':
            fail('target grant is not active')
        if grant.get('ownership') or grant['participant'] == grant['subject']:
            fail('a person cannot withdraw their own access to their own information')
        if grant['subject'] != actor:
            fail('only the information subject can withdraw this access')
        if grant['participant'] not in present:
            fail('the withdrawal recipient does not appear in the conversation')
        types = op.get('withdraw_types') or []
        if not set(types) <= set(bootstrap.information_types):
            fail('unknown information type')
        key = (grant['subject'], grant['participant'])
        request = result.setdefault(key, {'requesting_actor': actor, 'subject': grant['subject'],
            'recipients': [grant['participant']], 'actions': ['*'], 'types': set(), 'all': False,
            'conditions': {'effective': 'now'}, 'reference': receipt(source_id, body['text'], 0, len(body['text'])),
            'identity_relationship_evidence': {}, 'template': op})
        request['types'].update(types)
        request['all'] = request['all'] or not types
    resolved = []
    for request in result.values():
        everything, types = request.pop('all'), sorted(request.pop('types'))
        template = request.pop('template')
        request['scope'] = {'kind': 'all', 'value': '*'} if everything else {'kind': 'types', 'value': types}
        named = {o.get('assignment_id') for o in operations if o.get('op') == 'revoke_assignment'}
        for aid, grant in state['assignment'].items():
            if (grant['status'] == 'active' and aid not in named and grant['subject'] == request['subject']
                    and grant['participant'] == request['recipients'][0]):
                operations.append({**deepcopy(template), 'assignment_id': aid})
        resolved.append(request)
    return resolved


def requests(updater, sources, source_id):
    """Resolve source targets BEFORE looking at any assignments.

    Every request has four distinct identities: requesting actor, information
    subject, recipient set, and (later) independently validated grant IDs.
    Only immediate unconditional requests are supported in this batch.
    """
    if model_driven(updater):
        return []
    body = sources.get(source_id)
    if body is None:
        fail('authenticated current source is unavailable')
    bootstrap = updater._source_bootstrap(sources)
    actor = body['source']['participant']
    subject = body['source'].get('subject') or actor
    names = identities(bootstrap, sources)
    text = body['text'].replace('\u2019', "'")
    result = []
    for sentence in re.finditer(r'[^.!?]+[.!?]?', text):
        if not re.search(r'\brevoke\b', sentence.group(), flags=re.I):
            continue
        # Do not execute reported/negated/conditional commands or questions.
        command = sentence.group().strip().rstrip('.')
        match = re.fullmatch(r'(?:Please )?Revoke (.+?)(?: too)?', command, flags=re.I)
        if not match:
            fail('unsupported or conditional revocation instruction')
        if actor not in bootstrap.authorities.get(subject, {}).get('restrict', []):
            fail('requesting actor lacks restriction authority for the information subject')
        phrase = match.group(1)
        actions = ['read', *bootstrap.read_pipeline_actions]
        # Unqualified access includes every configured action. Narrow action
        # requests use the closed read/search/derive vocabulary explicitly.
        action_match = re.fullmatch(r'(.+?) (read|search|derive) access(?: to (.+))?', phrase, flags=re.I)
        if action_match:
            reference, action, field = action_match.groups()
            action=norm(action)
            actions = [action, *(bootstrap.read_pipeline_actions if action == 'read' else ())]
        else:
            access = re.fullmatch(r'(.+?) access(?: to (.+))?', phrase, flags=re.I)
            if not access:
                fail('missing recipient and access scope')
            head, field = access.groups()
            actions = ['*']
            reference = head
            # Possessive recipient plus an optional information field.
            possessive = re.fullmatch(r"(.+)'s(?: (.+))?", head)
            if possessive:
                reference, leading = possessive.groups()
                if field and leading:
                    fail('conflicting information scopes')
                field = field or leading
            elif norm(head).startswith('my '):
                remainder = norm(head)[3:]
                if remainder in SCOPES or remainder in {'own read', 'read'}:
                    reference, field = 'myself', remainder
        reference = re.sub(r"'s$", '', reference)
        resolved = {}
        for part in re.split(r'\s+and\s+|,\s*', reference):
            part = re.sub(r"'s$", '', part.strip())
            pid, proofs = resolve_reference(part, actor, subject, names, sources)
            resolved.setdefault(pid, []).extend(proofs)
        if not resolved:
            fail('empty recipient set')
        if any(pid not in bootstrap.participants for pid in resolved):
            fail('recipient is outside the authenticated namespace')
        result.append({'requesting_actor': actor, 'subject': subject,
            'recipients': sorted(resolved), 'actions': sorted(set(actions)),
            'scope': scope(field), 'conditions': {'effective': 'now'},
            'reference': receipt(source_id, body['text'], *sentence.span()),
            'identity_relationship_evidence': resolved})
    return result


def field_set(text):
    return {key for key, pattern in FIELDS.items() if re.search(pattern, text, re.I)}


def overlaps(assignment, requested_scope):
    if requested_scope['kind'] in {'all', 'types'}:
        return True
    text = assignment['information_scope']
    if norm(text) in {'all', 'all information', '*'}:
        return True
    # A field mentioned only as an exclusion is not an authorized field.
    positive = re.split(r'\b(?:not|no|excludes?|excluding|except|but)\b', text, maxsplit=1, flags=re.I)[0]
    fields = field_set(positive)
    if not fields:
        fail('cannot establish the grant information scope for a partial withdrawal')
    return requested_scope['value'] in fields


def validate(updater, op, assignment, sources, source_id, resolved=None):
    if resolved is None:
        resolved = requests(updater, sources, source_id)
    matching = [r for r in resolved if assignment['subject'] == r['subject']
        and assignment['participant'] in r['recipients']
        and op['issuer'] == r['requesting_actor']
        and source_id in op['evidence'].get('issuer', [])
        and overlaps(assignment, r['scope'])
        and ('*' in r['actions'] or set(assignment['actions']) & set(r['actions']))]
    if len(matching) != 1:
        fail('EVERY affected grant must match one resolved subject, recipient, action and scope')
    if assignment.get('status') != 'active':
        fail('target grant is not active')
    request = deepcopy(matching[0])
    request['assignment_id'] = assignment['id']
    return request


def validate_batch(updater, operations, state, sources, source_id):
    """No mixed proposal, omission, or canonical-path bypass can publish."""
    if any(not isinstance(op, dict) for op in operations):
        fail('control operations must be objects')
    if model_driven(updater):
        return model_requests(updater, operations, state, sources, source_id)
    revokes = [op for op in operations if op.get('op') == 'revoke_assignment']
    resolved = requests(updater, sources, source_id)
    if resolved:
        # The closed compiler below is the only representation of a supported
        # withdrawal. Wider alternative effects cannot evade recipient/scope
        # validation. Compound controls outside this grammar stay on hold.
        if any(op.get('op') in {'suspend_assignment', 'schedule_expiry', 'end_activity',
                'record_restriction', 'resolve_restriction'} for op in operations):
            fail('mixed adverse controls require independent supported source resolution')
        if any(op.get('op') == 'propose_assignment' and any(
                op.get('subject') == r['subject'] and op.get('participant') in r['recipients']
                for r in resolved) for op in operations):
            fail('a withdrawal cannot regrant the targeted recipient in the same proposal')
    expected = set()
    for request in resolved:
        expected.update(aid for aid, a in state['assignment'].items()
            if a['status'] == 'active' and a['subject'] == request['subject']
            and a['participant'] in request['recipients'] and overlaps(a, request['scope'])
            and ('*' in request['actions'] or set(a['actions']) & set(request['actions']))
            and not any(old['reference']['source_id'] == source_id
                for old in a.get('recipient_revocations', [])))
    actual = [op.get('assignment_id') for op in revokes]
    if len(set(actual)) != len(actual) or set(actual) != expected:
        fail('proposed grants do not equal the complete independently resolved grant set')
    if revokes and not resolved:
        fail('no supported authenticated revocation request')
    return resolved


INSTRUCTION = '''
V19 recipient revocation contract: authenticated requesting actor, information
subject, target grantees and target grant IDs are separate identities. Consult
revocation_resolution, which the host derives from authenticated source text and
identity/relationship receipts BEFORE grant selection. Its resolved recipient set
and scope constrain EVERY revoke operation. A role, active worker, grant holder
or structurally valid ID cannot substitute for the actual source reference.
Emit one revoke for EVERY matching active grant, preserving all other recipients.
The host retains residual actions/fields for partial scope requests. Never turn
partial revocation into a full suspension or wider denial. An unresolved reference
requires intents=[unresolved] and no guessed revoke or hold resolution. An explicit
staff target is valid; me/myself can validly equal the requesting actor.
'''


def context(updater, sources, source_id):
    from .permissions import PermissionProposalRejected
    try:
        return {'status': 'resolved', 'requests': requests(updater, sources, source_id)}
    except PermissionProposalRejected as exc:
        return {'status': 'unresolved', 'reason': str(exc), 'requests': []}


def validate_hold_resolution(updater, hold_id, sources, source_id):
    """A later ordinary utterance cannot clear an unresolved target reference.

    Require new authenticated identity/relationship evidence to resolve the
    original request AND a current matching withdrawal. The canonical validator
    still requires every affected grant in that current withdrawal to publish.
    Other kinds of holds retain their original validation policy.
    """
    import json
    from .control_holds import rows
    hold = next((h for h in rows(updater.core, updater.compartment) if h['id'] == hold_id), None)
    if hold is None:
        fail('unknown control hold')
    old_id = json.loads(hold['body'])['source_id']
    body = sources.get(old_id)
    if not body:
        fail('original control evidence is unavailable')
    if model_driven(updater) or not re.search(r'\brevoke\b', body['text'], re.I):
        return
    old = requests(updater, sources, old_id)
    current = requests(updater, sources, source_id)
    if not old or not all(any(r['subject'] == previous['subject']
            and set(previous['recipients']) <= set(r['recipients'])
            and previous['actions'] == r['actions'] and previous['scope'] == r['scope']
            for r in current) for previous in old):
        fail('hold resolution needs a current withdrawal matching the resolved original request')


def apply(assignment, request):
    """Keep the grant and bindings intact for the unrevoked residual scope."""
    assignment.setdefault('recipient_revocations', []).append(deepcopy(request))
    all_actions = '*' in request['actions'] or set(assignment['actions']) <= set(request['actions'])
    if request['scope']['kind'] == 'all' and all_actions:
        assignment['status'] = 'revoked'
    evidence = {request['reference']['source_id']}
    for proofs in request['identity_relationship_evidence'].values():
        evidence.update(p['source_id'] for p in proofs)
    assignment['evidence'].setdefault('recipient_resolution', [])
    assignment['evidence']['recipient_resolution'] = sorted(
        set(assignment['evidence']['recipient_resolution']) | evidence)


def blocks(assignment, action, text, types=None):
    """Rechecked for every read/derive/search, including buffered release.

    Unknown fields in a partially revoked grant remain protected. A recognized
    disjoint field (e.g. billing after scheduling withdrawal) remains available.
    Compound facts containing a forbidden field cannot release as a whole.
    types are the fact's own tags; a fact without tags stays protected.
    """
    for request in assignment.get('recipient_revocations', []):
        if '*' not in request['actions'] and action not in request['actions']:
            continue
        selected = request['scope']
        if selected['kind'] == 'all':
            return True
        if selected['kind'] == 'types':
            if not types or set(types) & set(selected['value']):
                return True
            continue
        fields = field_set(text)
        if not fields or selected['value'] in fields:
            return True
    return False
