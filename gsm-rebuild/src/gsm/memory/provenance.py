"""Derived memories (summaries, reports, derived facts) with conservative policy inheritance.

Rules (MVP):

* the creator must hold a FULL view of every parent right now;
* all parents must be in the creator's tenant and workspace;
* sensitivity = max over parents; compartments = union; allowed views =
  intersection (empty -> reject); mandatory purposes = intersection of the
  non-empty sets (conflict -> reject); mandatory auth strength = max;
* by default the derived memory *inherits live*: every read is decided as the
  minimum view over fresh decisions on all parents, so it can never be more
  visible than any parent, and revoking or deleting any parent hides it;
* an explicit ``declassification`` policy replaces inheritance only when the
  creator owns every parent. A model can never supply one.
"""

from __future__ import annotations

from dataclasses import dataclass

from gsm.disclosure.schemas import ContentValidationError, get_schema, validate_content
from gsm.domain.operations import DeriveRequest
from gsm.domain.policy import MandatoryConstraints, MemoryPolicy
from gsm.domain.reasons import Reason
from gsm.domain.types import AuthStrength, Origin, Sensitivity, View
from gsm.ids import new_id
from gsm.memory.errors import OpRejected
from gsm.memory.repo import build_index_rows, insert_index_rows
from gsm.memory.writes import WriteExecutor, raise_for_write
from gsm.policy.engine import WriteAction
from gsm.policy.lineage import write_decision
from gsm.policy.tags import access_tags, existence_tags

DERIVATION_RULE = "conservative-meet/v1"


@dataclass(frozen=True)
class InheritedPolicy:
    sensitivity: Sensitivity
    compartments: frozenset[str]
    allowed_views: frozenset[View]
    subject_ids: frozenset[str]
    owner_ids: frozenset[str]
    mandatory: MandatoryConstraints


def meet(parents: list[dict[str, object]]) -> InheritedPolicy:
    sens = max((Sensitivity(str(p["sensitivity"])) for p in parents), key=lambda s: s.rank)
    comps: set[str] = set()
    views: set[View] | None = None
    subjects: set[str] = set()
    owners: set[str] | None = None
    purposes: set[str] | None = None
    auth = AuthStrength.NONE
    for p in parents:
        comps |= set(p["compartments"])  # type: ignore[call-overload]
        pv = {View(v) for v in p["allowed_views"]}  # type: ignore[attr-defined]
        views = pv if views is None else views & pv
        subjects |= set(p["subject_ids"])  # type: ignore[call-overload]
        po = set(p["owner_ids"])  # type: ignore[call-overload]
        owners = po if owners is None else owners & po
        policy = MemoryPolicy.model_validate(p["policy"])
        if policy.mandatory.purposes:
            ps = set(policy.mandatory.purposes)
            purposes = ps if purposes is None else purposes & ps
            if not purposes:
                raise OpRejected(Reason.DERIVATION_POLICY_CONFLICT)
        if policy.mandatory.min_auth_strength.rank > auth.rank:
            auth = policy.mandatory.min_auth_strength
    if not views:
        raise OpRejected(Reason.DERIVATION_POLICY_CONFLICT)
    return InheritedPolicy(
        sensitivity=sens,
        compartments=frozenset(comps),
        allowed_views=frozenset(views),
        subject_ids=frozenset(subjects),
        owner_ids=frozenset(owners or set()),
        mandatory=MandatoryConstraints(purposes=frozenset(purposes or set()), min_auth_strength=auth),
    )


def derive_in_tx(w: WriteExecutor, req: DeriveRequest) -> tuple[str, str, list[Reason]]:
    tx, ctx = w.tx, w.ctx
    parents: list[dict[str, object]] = []
    for pid in sorted(set(req.parent_ids)):
        out = write_decision(tx, w.engine, ctx, w.snap, pid, WriteAction.DERIVE, lock=True)
        if out is None:
            raise OpRejected(Reason.NOT_FOUND)
        decision, _res = out
        w.decisions.append(decision)
        raise_for_write(decision)
        row = tx.one(
            """SELECT memory_id, workspace_id, sensitivity, compartments, allowed_views, subject_ids, owner_ids, policy
               FROM memory_atoms WHERE tenant_id = :tenant_id AND memory_id = :mid""",
            mid=pid,
        )
        assert row is not None
        if row["workspace_id"] != ctx.workspace_id:
            raise OpRejected(Reason.NOT_FOUND)
        parents.append(dict(row))
    inherited = meet(parents)
    schema = get_schema("note")
    try:
        validate_content(schema, req.content)
    except ContentValidationError:
        raise OpRejected(Reason.INVALID_OPERATION) from None
    reasons = [Reason.WRITE_AUTHORIZED, Reason.INHERITED_FROM_PARENTS]
    inherits = True
    policy = MemoryPolicy(mandatory=inherited.mandatory, historical_access="owners_only")
    if req.declassification is not None:
        if w.origin is not Origin.ORACLE:
            raise OpRejected(Reason.MODEL_PROPOSAL_QUARANTINED)
        if not all(ctx.principal_id in set(p["owner_ids"]) for p in parents):  # type: ignore[call-overload]
            raise OpRejected(Reason.NOT_AUTHORIZED_TO_WRITE)
        policy = req.declassification
        inherits = False
        reasons = [Reason.WRITE_AUTHORIZED, Reason.DECLASSIFIED]
    owners = sorted(inherited.owner_ids | {ctx.principal_id})
    subjects = sorted(inherited.subject_ids)
    memory_id, version_id = new_id("mem"), new_id("ver")
    now = ctx.request_time
    tx.exec(
        """INSERT INTO memory_atoms (tenant_id, workspace_id, memory_id, semantic_type, content_schema, topic,
               subject_ids, owner_ids, source_principal_id, sensitivity, compartments, policy, allowed_views,
               access_tags, existence_tags, lifecycle_state, is_derived, inherits_parent_policy, derivation_rule,
               observed_at, created_at)
           VALUES (:tenant_id, :ws, :mid, :stype, 'note', :topic, CAST(:subjects AS text[]), CAST(:owners AS text[]),
               :src, :sens, CAST(:comps AS text[]), CAST(:policy AS jsonb), CAST(:views AS text[]),
               CAST(:atags AS text[]), CAST(:etags AS text[]), 'active', true, :inherits, :rule, :now, :now)""",
        ws=ctx.workspace_id,
        mid=memory_id,
        stype=req.semantic_type.value,
        topic=req.topic,
        subjects=subjects,
        owners=owners,
        src=ctx.principal_id,
        sens=inherited.sensitivity.value,
        comps=sorted(inherited.compartments),
        policy=policy.model_dump_json(),
        views=sorted(v.value for v in inherited.allowed_views),
        atags=[] if inherits else access_tags(policy, subjects),
        etags=[] if inherits else existence_tags(policy, subjects),
        inherits=inherits,
        rule=DERIVATION_RULE,
        now=now,
    )
    tx.exec(
        """INSERT INTO memory_versions (tenant_id, workspace_id, version_id, memory_id, content_text, content_fields,
               valid_from, state, truth_state, extraction_confidence, origin, created_by, created_at)
           VALUES (:tenant_id, :ws, :vid, :mid, :text, CAST('{}' AS jsonb), :now, 'active', 'current', 1.0,
               'derived', :by, :now)""",
        ws=ctx.workspace_id,
        vid=version_id,
        mid=memory_id,
        text=req.content.text,
        by=ctx.principal_id,
        now=now,
    )
    rows = build_index_rows(
        schema,
        req.content,
        topic=req.topic,
        semantic_type=req.semantic_type.value,
        allowed_views=inherited.allowed_views,
        embedder=w.embedder,
    )
    insert_index_rows(tx, memory_id=memory_id, version_id=version_id, rows=rows, embedder=w.embedder, now=now)
    for p in parents:
        tx.exec(
            """INSERT INTO memory_edges (tenant_id, edge_id, edge_type, src_memory_id, dst_memory_id, created_at)
               VALUES (:tenant_id, :id, :etype, :src, :dst, :now)""",
            id=new_id("edge"),
            etype=req.edge_type.value,
            src=p["memory_id"],
            dst=memory_id,
            now=now,
        )
    return memory_id, version_id, reasons
