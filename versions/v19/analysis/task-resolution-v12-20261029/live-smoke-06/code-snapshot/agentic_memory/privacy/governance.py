"""Domain-neutral capability candidates and host-issued approval history.

The learner supplies candidate data. Only an authenticated policy host can
activate it after checking the domain's evidence/authority contract. This module
never treats a model-authored `authenticated` or confidence field as authority.
"""
from dataclasses import asdict, dataclass

from .system import AccessDenied, digest, encoded


@dataclass(frozen=True)
class CapabilityCandidate:
    id: str
    principal: str
    resource_selector: dict
    actions: tuple
    purpose: str
    evidence: tuple
    issuer: str
    validity: dict


class PolicyGovernor:
    def __init__(self, core):
        self.core = core

    def record(self, ctx, candidate, *, state, receipt, source_job):
        c = self.core
        c._session(ctx, 'manage_policy')
        namespace = candidate.resource_selector.get('namespace')
        c._session(ctx, 'ingest', namespace)
        if candidate.issuer != ctx.principal_id:
            raise AccessDenied('A candidate cannot issue its own approval')
        if (not candidate.id or not candidate.principal or not candidate.purpose or
                set(candidate.resource_selector) != {'namespace','attributes','resource_ids'} or
                not isinstance(candidate.resource_selector['attributes'],dict) or
                not candidate.resource_selector['attributes'] or any(
                    not isinstance(k,str) or not k or v is None
                    for k,v in candidate.resource_selector['attributes'].items()) or
                not candidate.actions or not set(candidate.actions) <= {
                    'search','read','derive','write','link','export','forward','delegate','delete'} or
                not candidate.evidence or not receipt.get('authority') or not receipt.get('basis')):
            raise ValueError('A capability requires an exact selector, purpose, evidence, and authority receipt')
        if state in {'proposed','approved','approved_unbound'}:
            for sid in candidate.evidence:
                resource = c._resource(ctx, sid, 'derive')
                if resource['compartment'] != namespace:
                    raise AccessDenied('Capability evidence crosses its namespace')
        if state not in {'proposed','approved','approved_unbound','revoked','suspended','expired'}:
            raise ValueError('Unsupported capability decision')
        with c.transaction():
            # Candidate IDs are also their eventual runtime assignment IDs.
            # Reserve that same authority boundary even while unbound/proposed.
            c._claim_authority(ctx,'assignment',candidate.id)
            prior=c.db.execute('SELECT body FROM capability_candidates WHERE id=? ORDER BY version DESC LIMIT 1',
                               (candidate.id,)).fetchone()
            if prior:
                import json
                original=json.loads(prior[0])
                if (original['principal']!=candidate.principal or
                        original['resource_selector']['namespace']!=namespace or
                        original['resource_selector']['attributes']!=candidate.resource_selector['attributes']):
                    raise AccessDenied('Changed principal or selector scope requires a new capability ID')
            version = c.db.execute('SELECT COALESCE(MAX(version),0)+1 FROM capability_candidates WHERE id=?',
                                   (candidate.id,)).fetchone()[0]
            c.db.execute('INSERT INTO capability_candidates VALUES (?,?,?,?,?,?)',
                         (candidate.id,version,namespace,encoded(asdict(candidate)),state,source_job))
            decision_id = 'capability-decision:' + digest([candidate.id,version,source_job])
            c.db.execute('INSERT INTO capability_decisions VALUES (?,?,?,?,?,?)',
                         (decision_id,candidate.id,version,ctx.principal_id,state,encoded(receipt)))
        return decision_id

    def activate(self, ctx, candidate, *, receipt, source_job, evidence_relationships=None):
        c = self.core
        selector = candidate.resource_selector
        targets = selector.get('resource_ids', [])
        with c.transaction():
            decision = self.record(ctx,candidate,state='approved' if targets else 'approved_unbound',
                                   receipt=receipt,source_job=source_job)
            if targets:
                for rid in targets:
                    row = c._resource(ctx,rid)
                    if row['compartment'] != selector['namespace'] or any(
                            row['attributes'].get(k) != v for k,v in selector['attributes'].items()):
                        raise AccessDenied('Capability target exceeds the approved selector')
                c.install_assignment(ctx,candidate.id,principals=[candidate.principal],
                    compartment=selector['namespace'],task=candidate.purpose,
                    actions=candidate.actions,resource_ids=list(targets),
                    conditions=candidate.validity,
                    resource_selector={k:selector[k] for k in ('namespace','attributes')},
                    provenance={'authority':receipt['authority'],'evidence':list(candidate.evidence),
                                'decision_id':decision,'source_job':source_job},
                    evidence_relationships=evidence_relationships)
            return decision
