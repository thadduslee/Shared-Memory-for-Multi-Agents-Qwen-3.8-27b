"""Deterministic release verification.

Checks a draft against the disclosure plan before anything leaves the
service. It never loosens a decision: failures lead either to a
predetermined safe rendering (the deterministic generator over the still-valid
evidence) or to the canonical non-answer for the plan's action.

Protected values are compared through keyed fingerprints generated fresh per
request (HMAC with a random key that is never persisted), so the verifier's
working set is not a reusable dictionary of protected plaintext hashes.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from gsm.disclosure.planner import DisclosurePlan
from gsm.disclosure.schemas import normalize
from gsm.domain.reasons import Reason
from gsm.domain.types import QueryAction
from gsm.providers.base import DraftResponse
from gsm.release.generator import CANONICAL_TEXT

_MEMORY_ID = re.compile(r"\bmem_[0-9a-f]{32}\b")
MIN_PROTECTED_CHARS = 4

_MEDICAL_TERMS = """
    medical medically diagnosis diagnosed surgery surgical operation hospital hospitalized illness sick sickness
    cancer tumor chemotherapy chemo pregnancy pregnant maternity therapy therapist depression anxiety disorder
    injury injured fracture treatment prescription medication condition chronic disease infection hiv diabetes
    stroke rehab rehabilitation psychiatric mental
"""
_CREDENTIAL_TERMS = """
    password passwd passphrase secret token apikey api_key credential credentials privatekey ssh key otp pin
"""
DEFAULT_LEXICONS: dict[str, frozenset[str]] = {
    "medical": frozenset(_MEDICAL_TERMS.split()),
    "credential": frozenset(_CREDENTIAL_TERMS.split()),
}


class ProtectedValues:
    def __init__(self, values: Iterable[str]) -> None:
        self._key = secrets.token_bytes(32)
        self._fps: set[bytes] = set()
        self._lengths: set[int] = set()
        for value in values:
            norm = normalize(value)
            if len(norm) < MIN_PROTECTED_CHARS:
                continue
            toks = norm.split()
            self._lengths.add(len(toks))
            self._fps.add(self._fp(" ".join(toks)))

    def _fp(self, s: str) -> bytes:
        return hmac.new(self._key, s.encode(), hashlib.sha256).digest()

    def __len__(self) -> int:
        return len(self._fps)

    def found_in(self, text: str) -> bool:
        toks = normalize(text).split()
        for n in self._lengths:
            for i in range(0, max(0, len(toks) - n + 1)):
                if self._fp(" ".join(toks[i : i + n])) in self._fps:
                    return True
        return False


@dataclass(frozen=True)
class ReleaseMetadata:
    tenant_id: str
    protected: ProtectedValues
    still_valid: frozenset[str]
    evidence_text: str


@dataclass
class VerificationResult:
    ok: bool
    reasons: list[Reason] = field(default_factory=list)
    stale_ids: set[str] = field(default_factory=set)
    unapproved_cited: set[str] = field(default_factory=set)


class ReleaseDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: QueryAction
    content: str
    approved_memory_ids: tuple[str, ...]
    blocked_memory_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    confidence: float | None
    policy_version: str
    generator: str


class ReleaseVerifier:
    def __init__(self, lexicons: Mapping[str, frozenset[str]] | None = None) -> None:
        self.lexicons = dict(DEFAULT_LEXICONS if lexicons is None else lexicons)

    def verify(
        self,
        draft: DraftResponse,
        plan: DisclosurePlan,
        meta: ReleaseMetadata,
        *,
        action_policy: str = "plan",
    ) -> VerificationResult:
        """``action_policy="plan"``: the draft must carry the plan's action.

        ``action_policy="answerer"``: the answerer saw only permitted evidence, so
        declining (refuse / no_memory / escalate) can never leak and is accepted;
        the caller releases the canonical text for that action. A content answer
        is accepted only if the plan has permitted evidence, and it must then pass
        every content check below. The answer / answer_redacted label is the
        answerer's; the content check is what bounds disclosure.
        """
        res = VerificationResult(ok=True)

        def fail(reason: Reason) -> None:
            res.ok = False
            if reason not in res.reasons:
                res.reasons.append(reason)

        content_actions = (QueryAction.ANSWER, QueryAction.ANSWER_REDACTED)
        if meta.tenant_id != plan.tenant_id:
            fail(Reason.TENANT_MISMATCH)
        if action_policy == "answerer":
            if draft.action not in content_actions:
                return res
            if plan.action not in content_actions:
                fail(Reason.ACTION_MISMATCH)
                return res
        elif draft.action is not plan.action:
            fail(Reason.ACTION_MISMATCH)
        if plan.action not in content_actions:
            if draft.text.strip() != CANONICAL_TEXT[plan.action] or draft.cited_memory_ids:
                fail(Reason.CONTENT_IN_NON_ANSWER)
            return res
        approved = plan.approved_ids
        cited = set(draft.cited_memory_ids) | set(_MEMORY_ID.findall(draft.text))
        unapproved = cited - approved
        if unapproved:
            res.unapproved_cited = unapproved
            fail(Reason.UNAPPROVED_MEMORY_CITED)
        stale = set(approved) - set(meta.still_valid)
        if stale:
            res.stale_ids = stale
            fail(Reason.STALE_EVIDENCE)
        if meta.protected.found_in(draft.text):
            fail(Reason.PROTECTED_VALUE_DETECTED)
        draft_tokens = set(normalize(draft.text).split())
        evidence_tokens = set(normalize(meta.evidence_text).split())
        for category in plan.prohibited_categories:
            terms = self.lexicons.get(category, frozenset())
            if (draft_tokens & terms) - evidence_tokens:
                fail(Reason.PROHIBITED_CATEGORY_DETECTED)
        return res
