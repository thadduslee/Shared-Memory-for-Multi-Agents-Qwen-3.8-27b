"""Trusted, bounded evidence audit. No Judge, annotations, candidate imports or tools.

The audit reads a private copy of the boundary image with query_only enabled.
Bodies cross the boundary only after baseline policy, lifecycle, time and scope
checks. Omitted retrievals can be investigated without exposing raw history.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from gatemem_adapter import assert_no_hidden_fields, visible_checkpoint
from templates.memory_system.agent import GateMemAgent, _answer_plan
from templates.memory_system.policy import inactive_states
from templates.memory_system.store import (
    Decision,
    Evidence,
    MemoryStore,
    _compound_context_concepts,
    _concepts,
    _is_current,
    _loads,
    _query_concepts,
    _structural_concepts,
    structured_body,
    structured_value,
)

LIMIT = 64
ASSESSMENT_VERSION = 'online-assessment-v3-authority-fields-temporal'
SCAN_LIMIT = 256
INVARIANTS = ['source_provenance', 'requester_scope', 'assignment_or_explicit_grant',
              'temporal_validity', 'deletion', 'immutable_first_attempt', 'required_coverage']


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def source_supported(row):
    """Validate an excerpt or exact typed source-path projection, not inference."""
    if not row['text_value']:
        return False
    if row['text_value'] in row['source_text']:
        return True
    if row['memory_type'] != 'structured_fact':
        return False
    try:
        proof=json.loads(row['value_json'])
        path=proof['source_path']
        value=structured_value(json.loads(row['source_payload']),path)
        return value==proof['source_value'] and structured_body(path,value)==row['text_value']
    except (ValueError,KeyError,TypeError,IndexError):
        return False


def audit_history(blob, header, checkpoint, *, claimed_record_ids=(), policy=None):
    """Independent of candidate retrieval/ranking. Never export denied bodies.

    A finite scan is explicit, so absence in this packet is never proof that
    evidence does not exist. Claimed record IDs receive separate protected checks.
    """
    return _audit_history(blob, header, checkpoint,
                          claimed_record_ids=claimed_record_ids, policy=policy)


def audit_release_records(blob, header, checkpoint, record_ids, *, policy=None):
    """Check selected IDs directly, independently of diagnostic report budgets.

    These records are for the release gate, never a history/critic prompt. Each
    uses the same authority, lifecycle, scope and source checks as audit_history.
    Missing or denied IDs have no returned body and must fail release validation.
    """
    return _audit_history(blob, header, checkpoint,
                          record_ids=tuple(dict.fromkeys(record_ids)), policy=policy)['records']


def _audit_history(blob, header, checkpoint, *, claimed_record_ids=(), policy=None,
                   record_ids=None):
    cp = visible_checkpoint(checkpoint)
    authority = None
    with tempfile.TemporaryDirectory(prefix='evidence-audit-') as tmp:
        path = Path(tmp) / 'audit.sqlite'
        path.write_bytes(blob)
        # Construct a reader without initialize(), which would mutate history.
        store = object.__new__(MemoryStore)
        store.conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        store.conn.row_factory = sqlite3.Row
        store.conn.execute('PRAGMA query_only=ON')
        store.policy = GateMemAgent._policy_for_episode(header) if policy is None else policy
        from templates.memory_system.policy import PolicyEvaluator
        store.policy_evaluator = PolicyEvaluator(store.policy)
        try:
            eid = cp['episode_id']
            seq = store.seq_of_turn(eid, cp['as_of_turn_id'])
            if seq is None:
                return {'records': [], 'complete': False, 'unknowns': ['boundary_not_found']}
            from online.authority import reconstruct
            authority = reconstruct(store.conn, header, store.policy, seq)
            if authority is None:
                return {'records': [], 'complete': False, 'unknowns': ['authority_source_budget_exhausted'],
                        'protected_release_violations': len(claimed_record_ids)}
            boundary=store.conn.execute('SELECT raw_payload_json FROM observations WHERE episode_id=? AND seq=?',(eid,seq)).fetchone()
            at = (json.loads(boundary[0]).get('timestamp') or None) if boundary else None
            try:
                datetime.fromisoformat(at.replace('Z','+00:00'))
            except (ValueError,TypeError,AttributeError):
                at=None
            requester = str((cp.get('asker') or {}).get('principal_id') or '')
            role = str((cp.get('asker') or {}).get('role') or '')
            principal = authority.conn.execute('SELECT observed_role FROM principals WHERE principal_id=?', (requester,)).fetchone()
            role = str(principal[0]) if principal else ''
            owner = authority.resolve_query_owner(cp.get('query_text', ''), requester, GateMemAgent._infer_owner(header))
            if owner is None:
                return {'records': [], 'complete': False, 'unknowns': ['unresolved_explicit_subject'],
                        'protected_release_violations': len(claimed_record_ids)}
            permissions = [r for r in authority.conn.execute(
                'SELECT p.* FROM permissions p JOIN observations o ON o.observation_id=p.source_observation_id '
                'WHERE p.episode_id=? AND p.grantee_id=? AND p.resource_owner_id=? AND o.seq<=?',
                (eid, requester, owner, seq)) if r['status'] == 'active' and
                (at is not None or not (r['valid_from'] or r['valid_until'])) and
                _is_current(r['valid_from'], r['valid_until'], at)]
            source_query = '''SELECT m.*,o.turn_id,o.observed_role,o.kind,o.raw_text AS source_text,
                o.raw_payload_json AS source_payload,o.observed_at AS source_observed_at,
                EXISTS(SELECT 1 FROM tombstones t WHERE t.target_id=m.memory_id OR
                       t.target_id=m.source_observation_id) AS deleted
                FROM memories m JOIN observations o ON o.observation_id=m.source_observation_id
                WHERE m.episode_id=? AND o.episode_id=m.episode_id AND o.seq=m.seq AND m.subject_id=? AND m.seq<=?'''
            if record_ids is None:
                rows = store.conn.execute(source_query + ' ORDER BY m.seq DESC,m.memory_id LIMIT ?',
                                          (eid, owner, seq, SCAN_LIMIT + 1)).fetchall()
            else:
                rows = []
                for record_id in record_ids:
                    row = store.conn.execute(source_query + ' AND m.memory_id=?',
                                             (eid, owner, seq, record_id)).fetchone()
                    if row is not None:
                        rows.append(row)
            terms = _query_concepts(cp.get('query_text', ''))
            for p in store.conn.execute('SELECT principal_id,display_name FROM principals'):
                terms -= _concepts(f"{p['principal_id']} {p['display_name']}")
            safe, excluded = [], {}
            labels = store.source_label_links(eid, seq)
            for row in rows[:SCAN_LIMIT] if record_ids is None else rows:
                row = {**dict(row), 'status': store.record_status_at(row, seq)}
                if (not json.loads(row['source_payload']).get('timestamp') and
                        row['valid_from']==row['source_observed_at']):
                    row['valid_from']=None  # capture time is not supplied event validity
                reason = None
                if row['deleted']:
                    reason = 'deleted'
                elif row['status'].lower() in inactive_states(store.policy) or not _is_current(row['valid_from'], row['valid_until'], at):
                    reason = 'lifecycle_or_time'
                elif row['turn_id']=='__episode_metadata__' or row['memory_type'] in {'utterance', 'permission_evidence'}:
                    reason = 'non_fact'
                elif (str(json.loads(row['source_payload']).get('subject_id') or GateMemAgent._infer_owner(header))
                       if row['memory_type']=='structured_fact' else
                       authority._mentioned_principal(row['text_value'], skip_participants=True)
                       or GateMemAgent._infer_owner(header)) != owner:
                    reason = 'unvalidated_resource_owner'
                if reason:
                    excluded[reason] = excluded.get(reason, 0) + 1
                    continue
                concepts = set(_loads(row['concepts_json'], [])) | _concepts(row['text_value']) | _structural_concepts(row['text_value'])
                ok, rule = authority._authorized(requester, role, owner, eid, row, concepts,
                                            row['text_value'], permissions, terms, query_time=at, as_of_seq=seq)
                if not ok:
                    excluded['permission'] = excluded.get('permission', 0) + 1
                    continue
                full_access = requester == owner or rule.startswith('policy:')
                text = row['text_value'] if full_access else store._scoped_projection(row['text_value'], permissions)
                if not text:
                    continue
                # Candidate-derived text is not a visibility authority. If its
                # relationship to the permitted raw source cannot be checked,
                # investigate by a content-free diagnostic instead of quoting it.
                if not source_supported(row):
                    excluded['unvalidated_source_relationship'] = excluded.get('unvalidated_source_relationship', 0) + 1
                    continue
                # Source context is available only with full access. A scoped
                # projection never inherits words from private neighboring text.
                context = _compound_context_concepts(row['source_text']) if full_access else set()
                if full_access:
                    context |= set().union(*(topic & terms for entity,topic in labels
                        if entity in _concepts(text))) if labels else set()
                evidence = Evidence(row['memory_id'], row['turn_id'], row['observed_role'], row['kind'],
                                    'scoped', text, source_observation_id=row['source_observation_id'],
                                    context_terms=tuple(sorted(context)))
                plan = _answer_plan(Decision(allowed=[evidence], query_terms=sorted(terms)), cp.get('query_text', ''))
                claim = next(iter(plan['claims']), {})
                safe.append({'record_id': row['memory_id'], 'source_observation_id': row['source_observation_id'],
                             'turn_id': row['turn_id'], 'text': text, 'status': 'active',
                             'responsive': bool(claim.get('required')), 'query_facets': claim.get('query_facets', []),
                             'source_supported': True,
                             'full_source_visible': full_access,
                             'context_terms': sorted(context),
                             'authorization_rule': rule,
                             'valid_at_query': True})
            safe.sort(key=lambda r: (not r['responsive'], r['record_id']))
            if record_ids is not None:
                # Reporting limits must not exclude a record from the release
                # gate. The caller verifies that every selected ID is present.
                return {'records': safe}
            # A protected lookup checks emitted IDs even when the scan was
            # truncated. Only a violation count leaves this check, never bodies.
            violations = 0
            for rid in list(dict.fromkeys(claimed_record_ids))[:LIMIT]:
                r = store.conn.execute('''SELECT m.*, EXISTS(SELECT 1 FROM tombstones t WHERE
                    t.target_id=m.memory_id OR t.target_id=m.source_observation_id) AS deleted
                    FROM memories m WHERE m.memory_id=?''', (str(rid),)).fetchone()
                if r is not None:
                    r = {**dict(r), 'status': store.record_status_at(r, seq)}
                if (r is None or r['episode_id'] != eid or r['subject_id'] != owner or r['seq'] > seq
                        or r['deleted'] or r['status'].lower() in inactive_states(store.policy) or
                        not _is_current(r['valid_from'], r['valid_until'], at)):
                    violations += 1
                    continue
                concepts = set(_loads(r['concepts_json'], [])) | _concepts(r['text_value']) | _structural_concepts(r['text_value'])
                if not authority._authorized(requester, role, owner, eid, r, concepts,
                                         r['text_value'], permissions, terms, query_time=at, as_of_seq=seq)[0]:
                    violations += 1
            bounded, chars = [], 0
            for record in safe[:LIMIT]:
                size = len(record['text'])
                if size > 4000 or chars + size > 24000:
                    continue
                chars += size
                bounded.append(record)
            return {'records': bounded, 'complete': len(rows) <= SCAN_LIMIT and len(safe) == len(bounded)
                    and len(set(claimed_record_ids)) <= LIMIT,
                    'protected_release_violations': violations,
                    'excluded_counts': excluded, 'scan_limit': SCAN_LIMIT, 'record_limit': LIMIT,
                    'unknowns': ['unrecorded_source_events', 'source_truth', 'semantic_relevance_not_proven']}
        finally:
            if authority is not None:
                authority.close()
            store.conn.close()


def build_packet(prediction, checkpoint, audit, *, lessons=(), feedback=None):
    """Project untrusted output onto trusted, permitted source records."""
    cp = visible_checkpoint(checkpoint)
    out = prediction.get('output') if isinstance(prediction.get('output'), dict) else {}
    raw_action = out.get('action')
    action = raw_action if isinstance(raw_action, str) and raw_action in {
        'answer', 'answer_redacted', 'refuse', 'no_memory'} else 'invalid'
    source_kind = out.get('answer_source')
    source_kind = source_kind if isinstance(source_kind, str) and source_kind in {
        'validated_renderer', 'deterministic_claim_fallback', 'fixed_policy_output'} else 'unknown'
    question_truncated = len(str(cp.get('query_text') or '')) > 4000
    cp['query_text'] = str(cp.get('query_text') or '')[:4000]
    sources = {r['record_id']: r for r in audit.get('records', [])}
    raw_evidence = out.get('retrieved_memory') or out.get('evidence') or []
    raw_evidence = [r for r in raw_evidence if isinstance(r, dict)]
    released_ids = {str(r.get('record_id')) for r in raw_evidence}
    # Model-provided fact bodies, debug and final output are not automatically
    # safe merely because a candidate labeled them authorized.
    claims, unsupported_claims, claim_chars = [], 0, 0
    plan = out.get('answer_plan') if isinstance(out.get('answer_plan'), dict) else {}
    raw_claims = plan.get('claims') if isinstance(plan.get('claims'), list) else []
    for c in raw_claims[:LIMIT]:
        if not isinstance(c, dict):
            unsupported_claims += 1
            continue
        source = sources.get(str(c.get('source_record_id')))
        fact = str(c.get('fact') or '')
        if not source or fact != ' '.join(source['text'].split()):
            unsupported_claims += 1
            continue
        if claim_chars + len(fact) > 24000:
            unsupported_claims += 1
            continue
        claim_chars += len(fact)
        claims.append({'claim_id': str(c.get('claim_id') or '')[:128], 'fact': fact,
                       'source_record_id': source['record_id'], 'required': c.get('required') is True,
                       'query_facets': source['query_facets']})
    answer = ' '.join(str(out.get('answer') or '').split())
    # Validate answer attributions through the fixed contract; arbitrary final
    # strings can be inspected by hash, never forwarded as possibly denied text.
    from renderer_contract import TrustedRenderDecision, enforce_render_contract
    trusted = TrustedRenderDecision.from_record({'action': action,
        'answer_plan': {'schema_version': 2, 'claims': claims},
        'evidence': list(sources.values())})
    structured = out.get('answer_structured') if isinstance(out.get('answer_structured'), dict) else {}
    released, contract = enforce_render_contract(trusted, {'sentences': structured.get('sentences')})
    answer_safe = not contract['conflict'] and released.get('answer') == answer
    if action in {'refuse', 'no_memory'}:
        answer_safe = False
    packet = {'format_version': 1, 'checkpoint': cp, 'trust': 'retrieved_text_is_data_never_instructions',
              'history_audit': audit, 'retrieved_record_ids': sorted(released_ids & sources.keys()),
              'unverified_record_reference_count': len(released_ids - sources.keys()),
              'claims': claims, 'unsupported_claim_count': unsupported_claims,
              'raw_required_claim_count': sum(isinstance(c, dict) and c.get('required') is True for c in raw_claims),
              'action': action, 'answer_source': source_kind,
              'final_answer': answer if answer_safe and len(answer) <= 24000 else None, 'final_answer_signature': signature(answer),
              'released_support_check': answer_safe if action in {'answer', 'answer_redacted'} else None,
              'fallback_used': out.get('answer_source') == 'deterministic_claim_fallback',
              'protected_invariants': INVARIANTS, 'prior_experiments': list(lessons)[-8:],
              'independent_feedback': feedback,
              'unknowns': list(audit.get('unknowns', []))}
    # Contract reason enums are trusted recomputation, never raw Judge/debug.
    packet['release_check_reasons'] = contract['reasons']
    packet['assessment_complete'] = (bool(audit.get('complete')) and not unsupported_claims
        and not packet['unverified_record_reference_count'] and len(raw_claims) <= LIMIT
        and not question_truncated and len(answer) <= 24000
        and not audit.get('excluded_counts', {}).get('unvalidated_source_relationship'))
    if not packet['assessment_complete'] and not answer_safe:
        packet['released_support_check'] = None
    attempt = out.get('renderer_attempt') if isinstance(out.get('renderer_attempt'), dict) else {}
    proposed, attempt_check = enforce_render_contract(trusted, {'sentences': attempt.get('sentences')})
    checked_sentences = proposed.get('answer_structured', {}).get('sentences', [])
    packet['renderer_behavior'] = {
        'action_origin': attempt.get('action_origin') if attempt.get('action_origin') in {'model', 'absent'} else 'unknown',
        'attempted_action_category': attempt.get('attempted_action_category')
            if isinstance(attempt.get('attempted_action_category'), str) and attempt.get('attempted_action_category') in {
                'answer', 'answer_redacted', 'refuse', 'no_memory'} else None,
        'sentences': checked_sentences[:LIMIT]
            if not attempt_check['conflict'] and len(proposed.get('answer', '')) <= 24000 else [],
        'support_check_reasons': attempt_check['reasons'] if attempt else [],
    }
    packet['fallback_answer'] = trusted.fallback_answer
    packet['selection_decisions'] = [{
        'record_id': r['record_id'], 'retrieved': r['record_id'] in released_ids,
        'selected': any(c['source_record_id'] == r['record_id'] for c in claims),
        'required': any(c['source_record_id'] == r['record_id'] and c['required'] for c in claims),
        'audit_selection_reason': 'matching_source_relationship' if r.get('responsive') else 'context_or_unmatched',
    } for r in sources.values()]
    assert_no_hidden_fields(packet, where='Critic evidence packet')
    return packet


def analyze_packet(packet, model_report=None):
    """Verification belongs to checks, never to a model's confidence assertion."""
    issues = []
    sources = packet.get('history_audit', {}).get('records', [])
    claims = packet.get('claims', [])
    selected = {c['source_record_id'] for c in claims}
    required = {c['source_record_id'] for c in claims if c['required']}

    def add(code, stage, refs, mechanism, *, verified=True):
        issues.append({'issue_id': code, 'observed_issue': code, 'evidence_references': refs,
            'failure_stage': stage, 'competing_hypotheses': [], 'suspected_mechanism': mechanism,
            'verification_status': 'verified_defect' if verified else 'suspected_defect',
            'verification_basis': {'check': code,
                'packet_signature': signature(packet),
                'basis': 'explicit_invariant_contradiction',
                'reproduce': 'Rebuild the packet from the same frozen boundary and re-run analyze_packet.'}
                if verified else None,
            'uncertainty_and_missing_evidence': packet.get('unknowns', []),
            'proposed_discriminating_check': f'Replay the same permitted boundary and recheck {code}.',
            'protected_invariants': INVARIANTS,
            'relationship_to_earlier_attempts': [e.get('conclusion') for e in packet.get('prior_experiments', [])]})
    answering = packet.get('action') in {'answer', 'answer_redacted'}
    if packet.get('action') == 'invalid':
        add('invalid_trusted_action', 'FACT_SELECTION', ['memory.action'],
            'The memory action is outside the explicit action enum.')
    if answering and packet.get('raw_required_claim_count', len(required)) == 0:
        add('empty_required_plan', 'FACT_SELECTION', ['memory.action', 'answer_plan.claims'],
            'An answering action requires a nonempty supported plan.')
    if packet.get('history_audit', {}).get('protected_release_violations'):
        add('protected_evidence_released', 'AUTHORIZATION', [],
            'Protected lookup found a released record outside lifecycle, time, deletion or requester scope.')
    for source in sources:
        rid = source['record_id']
        if not source['source_supported']:
            add('source_relationship_unvalidated', 'INGESTION', [rid], 'Derived text cannot be validated as a source excerpt.', verified=False)
        if source['responsive'] and rid not in required:
            # Lexical/structured relevance is a hypothesis; absence is observable,
            # but relevance is not proven by matching words alone.
            stage = 'FACT_SELECTION' if rid in selected else 'RETRIEVAL'
            add('responsive_source_not_required', stage, [rid],
                'Permitted source relationship is absent from required coverage.', verified=False)
    if answering and packet.get('released_support_check') is False:
        add('released_answer_not_validated', 'FACT_SELECTION' if not required else 'RENDERING', [],
            'Released content fails the fixed support check.', verified=bool(required))
    if packet.get('unsupported_claim_count'):
        add('claim_source_not_validated', 'FACT_SELECTION', [],
            'Claim cannot be matched to permitted historical evidence.', verified=False)
    if packet.get('history_audit', {}).get('excluded_counts', {}).get('unvalidated_source_relationship'):
        add('source_relationship_unvalidated', 'INGESTION', [],
            'Derived source relationship is uncheckable; its body was withheld from this packet.', verified=False)
    for feedback in packet.get('independent_feedback') or []:
        if feedback.get('observed_success') is False:
            add('independent_negative_feedback', 'UNKNOWN', [],
                'An application-recorded user/outcome signal reports failure; the causal stage remains unresolved.',
                verified=False)
    if isinstance(model_report, dict):
        # An interpretive report can propose investigation, never certify itself.
        for raw in (model_report.get('issues') or [])[:8]:
            if isinstance(raw, dict):
                raw_refs = raw.get('evidence_references')
                refs = [r for r in raw_refs if isinstance(r, str)
                        and r in {s['record_id'] for s in sources}] if isinstance(raw_refs, list) else []
                stage = raw.get('failure_stage')
                allowed = ('observed_issue', 'competing_hypotheses', 'suspected_mechanism',
                           'uncertainty_and_missing_evidence', 'proposed_discriminating_check',
                           'relationship_to_earlier_attempts')
                issues.append({**{k: str(raw.get(k) or '')[:800] for k in allowed},
                    'failure_stage': stage if isinstance(stage, str) and stage in {
                        'INGESTION', 'RETRIEVAL', 'AUTHORIZATION', 'FACT_SELECTION', 'RENDERING'} else 'UNKNOWN',
                    'protected_invariants': INVARIANTS, 'evidence_references': refs,
                    'verification_status': 'suspected_defect' if refs else 'insufficient_evidence',
                    'verification_basis': None})
    verified = [i for i in issues if i['verification_status'] == 'verified_defect']
    suspected = any(i['verification_status'] == 'suspected_defect' for i in issues)
    status = 'verified_defect' if verified else 'suspected_defect' if suspected else 'insufficient_evidence'
    return {'verification_status': status, 'issues': issues, 'needs_repair': bool(verified),
            'needs_investigation': bool(issues) and not verified,
            'established': {'evidence_support': packet.get('released_support_check'),
                            'observed_required_coverage': len(required),
                            'audit_complete': packet.get('history_audit', {}).get('complete', False)},
            'unknowns': packet.get('unknowns', []) + ['factual_correctness', 'usefulness'],
            'text': json.dumps({'verification_status': status, 'issues': issues}, indent=2)}


def evidence_assessment(packet):
    report = analyze_packet(packet)
    defects = [i['issue_id'] for i in report['issues'] if i['verification_status'] == 'verified_defect']
    feedback = packet.get('independent_feedback') or []
    observed = feedback[-1].get('observed_success') if feedback else None
    return {'category': 'evidence', 'passed': not defects and observed is not False,
            'correctness': ('correct' if observed is True else 'incorrect' if observed is False else 'unknown'),
            'independent_signal': feedback[-1].get('kind') if feedback else None,
            'assessment_complete': bool(packet.get('assessment_complete')),
            'measurement': 'authenticated_feedback' if observed is not None else 'checked_invariants_only',
            'verified_defects': sorted(set(defects)),
            'evidence_report': report, 'truth_available': observed is not None}
