"""Content-free audit receipts.

Rows hold identifiers, decisions, reason codes, policy versions, counts and
timestamps. They never hold memory content, deleted plaintext, query text,
prompts or model output. The only derived value is a keyed HMAC fingerprint of
the normalized query, which lets an operator holding the key correlate
repeated queries without the table being usable to recover or confirm text.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable, Sequence

from gsm.db.database import Tx
from gsm.domain.context import PrincipalContext
from gsm.domain.types import QueryAction
from gsm.ids import new_id
from gsm.policy.decision import PolicyDecision


class AuditRecorder:
    def __init__(self, hmac_key: bytes | None = None) -> None:
        self._key = hmac_key or secrets.token_bytes(32)

    def fingerprint(self, text: str) -> str:
        norm = " ".join(text.lower().split())
        return hmac.new(self._key, norm.encode(), hashlib.sha256).hexdigest()[:32]

    def record_decisions(
        self, tx: Tx, ctx: PrincipalContext, operation: str, decisions: Iterable[PolicyDecision]
    ) -> int:
        n = 0
        for d in decisions:
            tx.exec(
                """INSERT INTO policy_decisions (tenant_id, decision_id, correlation_id, principal_id, operation,
                                                 memory_id, decision, view, reason_codes, policy_version, decided_at)
                   VALUES (:tenant_id, :id, :cid, :pid, :op, :mid, :dec, :view, CAST(:reasons AS text[]),
                           :pv, :at)""",
                id=new_id("pdec"),
                cid=ctx.correlation_id,
                pid=ctx.principal_id,
                op=operation,
                mid=d.memory_id,
                dec=d.decision.value,
                view=d.view.value if d.view else None,
                reasons=[r.value for r in d.reasons],
                pv=d.policy_version,
                at=d.decided_at,
            )
            n += 1
        return n

    def record_release(
        self,
        tx: Tx,
        ctx: PrincipalContext,
        *,
        operation: str,
        action: QueryAction,
        approved: Sequence[str],
        blocked: Sequence[str],
        withheld_count: int,
        reasons: Sequence[str],
        policy_version: str,
        query_text: str,
        generator: str,
        confidence: float | None,
        latency_ms: int,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> str:
        release_id = new_id("rel")
        tx.exec(
            """INSERT INTO release_audits (tenant_id, release_id, correlation_id, principal_id, operation, action,
                                           approved_memory_ids, blocked_memory_ids, withheld_count, reason_codes,
                                           policy_version, query_fingerprint, generator, verifier_confidence,
                                           latency_ms, prompt_tokens, completion_tokens, created_at)
               VALUES (:tenant_id, :rid, :cid, :pid, :op, :action, CAST(:approved AS text[]),
                       CAST(:blocked AS text[]), :withheld, CAST(:reasons AS text[]), :pv, :qfp, :gen, :conf,
                       :lat, :pt, :ct, :at)""",
            rid=release_id,
            cid=ctx.correlation_id,
            pid=ctx.principal_id,
            op=operation,
            action=action.value,
            approved=list(approved),
            blocked=list(blocked),
            withheld=withheld_count,
            reasons=list(reasons),
            pv=policy_version,
            qfp=self.fingerprint(query_text),
            gen=generator,
            conf=confidence,
            lat=latency_ms,
            pt=prompt_tokens,
            ct=completion_tokens,
            at=ctx.request_time,
        )
        return release_id
