"""Fixed trusted boundary between memory decisions and untrusted wording.

Natural-language entailment cannot be proven in general. This contract uses
conservative schema, provenance, lexical, value, and coverage checks and fails
closed to extractive required claims whenever grounding is uncertain.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

ANSWER_ACTIONS = frozenset({"answer", "answer_redacted"})
DENIAL_ACTIONS = frozenset({"refuse", "no_memory"})
VALID_ACTIONS = ANSWER_ACTIONS | DENIAL_ACTIONS
CONTRACT_VERSION = "fixed_contract_v0002"
_DENIAL_TEXT = {
    "refuse": "I'm not able to share that information with you.",
    "no_memory": "I don't have any information on that.",
}

_STATUS = frozenset({"active", "inactive", "pending", "confirmed", "scheduled",
                     "rescheduled", "cancelled", "canceled", "completed", "expired",
                     "revoked", "deleted", "superseded", "denied", "approved",
                     "accepted", "rejected", "open", "closed"})
_NEGATION = frozenset({"not", "no", "never", "none", "without", "cannot", "can't",
                       "don't", "doesn't", "didn't"})
_GLUE = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "for", "from",
    "has", "have", "i", "in", "is", "it", "its", "of", "on", "or", "that",
    "the", "their", "them", "there", "these", "this", "to", "was", "were",
    "will", "with", "you", "your", "according", "based", "record", "records",
    "information", "indicates", "indicate", "says", "said", "also", "please", "note",
])
_TOKEN = re.compile(r"[a-z0-9]+(?:['.-][a-z0-9]+)*", re.IGNORECASE)
_TIME = re.compile(
    r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?:\s*[ap]m)?\b|\b(?:1[0-2]|0?[1-9])\s*[ap]m\b",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?\b|"
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", re.IGNORECASE)
_PHONE = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[ .-]?(?:\(\d{2,4}\)[ .-]?)?\d{3,4}[ .-]\d{3,4}|"
    r"(?:\(\d{2,4}\)[ .-]?|\d{2,4}[ .-])\d{3,4}[ .-]\d{3,4}|"
    r"\d{3,4}[ .-]\d{3,4})(?!\w)")
_PERCENT = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?\s*%")
_DOSE = re.compile(
    r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?\s*(?:mcg|mg|g|kg|ml|l|iu|units?)\b",
    re.IGNORECASE,
)
_QUANTITY = re.compile(
    r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?\s*(?:minutes?|hours?|days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
_IDENTIFIER = re.compile(r"\b(?=[A-Z0-9_-]{4,}\b)(?=[A-Z0-9_-]*[A-Z])(?=[A-Z0-9_-]*\d)[A-Z0-9]+(?:[-_][A-Z0-9]+)+\b")
_NUMBER = re.compile(r"(?<![\w:.,])[-+]?\d[\d,]*(?:\.\d+)?(?![\w:.,])", re.IGNORECASE)
_PERSON = re.compile(
    r"\b(?:Dr|Mr|Ms|Mrs|Prof)\.\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b|"
    r"\b(?:with|for|by|from|to)\s+([A-Z][a-z]+\s+[A-Z][a-z]+)\b")


def _normal(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _stem(token: str) -> str:
    token = token.lower()
    for suffix in ("ing", "edly", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[:-len(suffix)]
    return token


def _tokens(text: str) -> set[str]:
    return {_stem(t) for t in _TOKEN.findall(text) if t.lower() not in _GLUE}


def _value_role(text: str, start: int, kind: str) -> str:
    context = text[max(0, start - 48):start].lower()
    if kind == "time":
        if re.search(r"\b(?:arrive|arrival|check[ -]?in)\b", context):
            return "arrival_time"
        if re.search(r"\b(?:appointment|meeting|visit|session|starts?|scheduled)\b", context):
            return "appointment_time"
    return kind


def extract_critical_values(text: str) -> list[dict[str, str]]:
    """Extract complete, non-overlapping values in specificity order."""
    found: list[tuple[int, int, dict[str, str]]] = []
    occupied: list[tuple[int, int]] = []

    def add(kind: str, match: re.Match[str], surface: str | None = None) -> None:
        start, end = match.span(1) if match.lastindex else match.span()
        if any(start < old_end and end > old_start for old_start, old_end in occupied):
            return
        raw = surface if surface is not None else text[start:end]
        canonical = _normal(raw)
        occupied.append((start, end))
        found.append((start, end, {
            "type": kind,
            "role": _value_role(text, start, kind),
            "canonical_value": canonical,
            "surface_value": raw,
        }))

    for kind, pattern in (
        ("phone_number", _PHONE), ("date", _DATE), ("time", _TIME),
        ("dose", _DOSE), ("percentage", _PERCENT), ("identifier", _IDENTIFIER),
        ("quantity", _QUANTITY),
    ):
        for match in pattern.finditer(text):
            add(kind, match)
    for value in sorted(_STATUS):
        for match in re.finditer(rf"\b{re.escape(value)}\b", text, re.IGNORECASE):
            add("status", match)
    for value in sorted(_NEGATION):
        for match in re.finditer(rf"\b{re.escape(value)}\b", text, re.IGNORECASE):
            add("negation", match)
    for match in _PERSON.finditer(text):
        add("person", match, match.group(1) or match.group(0))
    for match in _NUMBER.finditer(text):
        add("number", match)
    return [item for _, _, item in sorted(found, key=lambda row: row[:2])]


def _values(text: str) -> dict[str, set[str]]:
    values = {kind: set() for kind in (
        "phone_number", "date", "time", "dose", "percentage", "identifier",
        "quantity", "number", "person", "status", "negation")}
    for value in extract_critical_values(text):
        values[value["type"]].add(value["canonical_value"])
    return values


@dataclass(frozen=True)
class AnswerClaim:
    claim_id: str
    fact: str
    required: bool
    source_record_id: str
    source_observation_id: str | None
    query_facets: tuple[str, ...]
    critical_values: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class TrustedRenderDecision:
    action: str
    claims: tuple[AnswerClaim, ...]
    authorized_record_ids: frozenset[str]
    fallback_answer: str
    evidence: tuple[dict[str, Any], ...] = ()
    plan_errors: tuple[str, ...] = ()
    legacy_plan: bool = False

    @property
    def answer_plan(self) -> dict[str, Any]:
        return {"schema_version": 2, "claims": [{
            "claim_id": c.claim_id, "fact": c.fact, "required": c.required,
            "source_record_id": c.source_record_id,
            "source_observation_id": c.source_observation_id,
            "query_facets": list(c.query_facets),
            "critical_values": [dict(v) for v in c.critical_values],
        } for c in self.claims]}

    @property
    def required_claim_ids(self) -> frozenset[str]:
        return frozenset(c.claim_id for c in self.claims if c.required)

    @property
    def claim_map(self) -> dict[str, AnswerClaim]:
        return {c.claim_id: c for c in self.claims}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> TrustedRenderDecision:
        action = str(record.get("action") or "refuse")
        action = action if action in VALID_ACTIONS else "refuse"
        supplied = record.get("evidence") or record.get("retrieved_memory") or []
        evidence = tuple(dict(item) for item in supplied if isinstance(item, dict)
                         and str(item.get("status") or "active") == "active")
        ids = frozenset(str(item.get("record_id")) for item in evidence if item.get("record_id"))
        plan = record.get("answer_plan")
        raw_claims = plan.get("claims") if isinstance(plan, Mapping) else None
        claims: list[AnswerClaim] = []
        plan_version = plan.get("schema_version") if isinstance(plan, Mapping) else None
        # Absence of a plan is the historical format. Once a producer declares
        # a current plan version, malformed or empty claims fail validation and
        # are never reconstructed from every authorized evidence row.
        legacy_plan = plan is None or plan_version in {0, "legacy"} or bool(
            record.get("legacy_answer_plan"))
        plan_errors: list[str] = []
        if isinstance(raw_claims, Sequence) and not isinstance(raw_claims, (str, bytes)):
            for raw in raw_claims:
                if not isinstance(raw, Mapping):
                    continue
                claim_id, fact = str(raw.get("claim_id") or ""), " ".join(str(raw.get("fact") or "").split())
                source_id = str(raw.get("source_record_id") or "")
                if not claim_id or not fact or not source_id:
                    continue
                if source_id not in ids:
                    plan_errors.append("unauthorized_record_reference")
                    continue
                critical = tuple({
                    "type": str(v.get("type") or "other"),
                    "role": str(v.get("role") or v.get("type") or "other"),
                    "canonical_value": _normal(v.get("canonical_value")),
                    "surface_value": str(v.get("surface_value") or ""),
                } for v in (raw.get("critical_values") or [])
                    if isinstance(v, Mapping) and v.get("canonical_value"))
                claims.append(AnswerClaim(
                    claim_id, fact, bool(raw.get("required")), source_id,
                    str(raw.get("source_observation_id")) if raw.get("source_observation_id") is not None else None,
                    tuple(str(x) for x in (raw.get("query_facets") or [])), critical))
        # Explicit legacy migration from authorized evidence to an all-required
        # extractive plan. No denied/obsolete body can enter through this path.
        if not claims and action in ANSWER_ACTIONS and legacy_plan:
            for item in evidence:
                fact, source_id = " ".join(str(item.get("text") or "").split()), str(item.get("record_id") or "")
                if fact and source_id:
                    stable = hashlib.sha256(f"{source_id}\0{fact}".encode()).hexdigest()[:20]
                    claims.append(AnswerClaim(f"legacy_claim_{stable}", fact, True, source_id,
                                              str(item.get("source_observation_id")) if item.get("source_observation_id") else None,
                                              (), ()))
        if not claims and action in ANSWER_ACTIONS and ids and legacy_plan:
            # Old agents sometimes returned the authorized joined body only in
            # ``answer``. Treat that explicit compatibility field as one
            # extractive legacy claim so historical implementations remain
            # runnable; new agents must emit answer_plan/fallback_answer.
            fact = " ".join(str(record.get("fallback_answer") or
                                record.get("answer") or "").split())
            if fact:
                source_id = min(ids)
                stable = hashlib.sha256(f"{source_id}\0{fact}".encode()).hexdigest()[:20]
                claims.append(AnswerClaim(f"legacy_claim_{stable}", fact, True,
                                          source_id, None, (), ()))
        if action in ANSWER_ACTIONS and not legacy_plan and not claims:
            plan_errors.append("invalid_answer_plan")
        fallback = " ".join(c.fact for c in claims if c.required)
        if action in DENIAL_ACTIONS:
            fallback = _DENIAL_TEXT[action]
        return cls(action, tuple(claims), ids, fallback, evidence,
                   tuple(dict.fromkeys(plan_errors)), legacy_plan)


def _fallback(decision: TrustedRenderDecision, reasons: list[str]):
    required = [c for c in decision.claims if c.required]
    if decision.legacy_plan:
        output = {"action": decision.action,
                  "answer": decision.fallback_answer if decision.action in ANSWER_ACTIONS else "",
                  "used_record_ids": sorted({c.source_record_id for c in required}),
                  "renderer_fallback": True}
        result = {"contract_version": CONTRACT_VERSION, "conflict": True,
                  "reasons": list(dict.fromkeys(reasons)),
                  "fallback_reason": reasons[0] if reasons else "malformed_renderer_output",
                  "trusted_action": decision.action,
                  "authorized_record_count": len(decision.authorized_record_ids),
                  "required_claim_count": len(decision.required_claim_ids)}
        return output, result
    output = {
        "action": decision.action,
        "answer": decision.fallback_answer if decision.action in ANSWER_ACTIONS else "",
        "answer_structured": ({"sentences": [{"text": c.fact, "claim_ids": [c.claim_id]} for c in required],
                               "used_claim_ids": sorted(c.claim_id for c in required)}
                              if decision.action in ANSWER_ACTIONS else {}),
        "used_record_ids": sorted({c.source_record_id for c in required}),
        "answer_source": "deterministic_claim_fallback", "renderer_fallback": True,
    }
    result = {"contract_version": CONTRACT_VERSION, "conflict": True,
              "reasons": list(dict.fromkeys(reasons)),
              "fallback_reason": reasons[0] if reasons else "malformed_renderer_output",
              "trusted_action": decision.action,
              "authorized_record_count": len(decision.authorized_record_ids),
              "required_claim_count": len(decision.required_claim_ids)}
    return output, result


def _supported(text: str, claims: list[AnswerClaim]) -> tuple[bool, str | None]:
    facts = " ".join(c.fact for c in claims)
    proposed, source = _values(text), _values(facts)
    for kind, values in proposed.items():
        if values - source[kind]:
            return False, ("negation_or_status_changed" if kind in {"status", "negation"}
                           else "critical_value_changed")
    # Typed values are not interchangeable. Compare their semantic role and
    # order, not only their presence in a bag of tokens.
    proposed_values = extract_critical_values(text)
    for claim in claims:
        protected = extract_critical_values(claim.fact)
        known = {(item["type"], item["canonical_value"]) for item in protected}
        for value in claim.critical_values:
            item = dict(value)
            key = (str(item.get("type") or "other"),
                   _normal(item.get("canonical_value")))
            if key not in known and key[1] and key[1] in _normal(claim.fact):
                item["canonical_value"] = key[1]
                item.setdefault("role", key[0])
                protected.append(item)
                known.add(key)
        cursor = -1
        normalized_text = _normal(text)
        for value in protected:
            canonical = _normal(value.get("canonical_value"))
            role = str(value.get("role") or value.get("type") or "other")
            position = normalized_text.find(canonical, cursor + 1)
            if position < 0:
                return False, ("negation_or_status_changed" if value.get("type") in {
                    "status", "negation"} else "critical_value_changed")
            cursor = position
            actual_role = next((item.get("role") for item in proposed_values
                                if item["canonical_value"] == canonical), role)
            if role in {"appointment_time", "arrival_time"} and actual_role != role:
                return False, "value_role_changed"
    # A bag-of-values check cannot prove that a value from claim A was not
    # reassigned to the subject of claim B.  For a multi-claim sentence, stay
    # conservative: each cited extractive fact must remain intact. Renderers
    # can always emit one attributed sentence per claim, and uncertain output
    # safely falls back to those same source-preserving facts.
    if len(claims) > 1 and any(_normal(c.fact) not in _normal(text) for c in claims):
        return False, "critical_value_changed"
    if _tokens(text) - _tokens(facts):
        return False, "unsupported_sentence"
    return True, None


def _covered(claim: AnswerClaim, texts: list[str]) -> tuple[bool, str | None]:
    combined, normalized = " ".join(texts), _normal(" ".join(texts))
    for value in claim.critical_values:
        canonical = _normal(value.get("canonical_value"))
        if canonical and canonical not in normalized:
            kind = value.get("type")
            return False, ("negation_or_status_changed" if kind in {"status", "negation"}
                           else "critical_value_changed")
    required, present = _tokens(claim.fact), _tokens(combined)
    ratio = len(required & present) / max(1, len(required))
    ok = _normal(claim.fact) in normalized or ratio >= 0.6
    return ok, None if ok else "missing_required_claim"


def enforce_render_contract(decision: TrustedRenderDecision,
                            rendered: Mapping[str, Any] | None, *,
                            logger: logging.Logger | None = None):
    """Return ``(released_output, sanitized_contract_result)``."""
    if decision.action in DENIAL_ACTIONS:
        if rendered and (not isinstance(rendered, Mapping) or
                         rendered.get("answer") or rendered.get("sentences")):
            return _fallback(decision, ["action_conflict"])
        return ({"action": decision.action, "answer": decision.fallback_answer,
                 "answer_structured": {}, "used_record_ids": [],
                 "answer_source": "fixed_policy_output"},
                {"contract_version": CONTRACT_VERSION, "conflict": False, "reasons": [],
                 "fallback_reason": None, "trusted_action": decision.action,
                 "authorized_record_count": 0, "required_claim_count": 0})

    reasons: list[str] = []
    reasons.extend(decision.plan_errors)
    if not isinstance(rendered, Mapping):
        reasons.append("malformed_renderer_output")
        rendered = {}
    attempted_action = rendered.get("action")
    if not decision.claims or not decision.required_claim_ids:
        reasons.append("empty_authorized_answer")
    raw_sentences = rendered.get("sentences")
    if raw_sentences is None and isinstance(rendered.get("answer_structured"), Mapping):
        raw_sentences = rendered["answer_structured"].get("sentences")
    if not isinstance(raw_sentences, list) or not raw_sentences:
        reasons.append("malformed_renderer_output")
        raw_sentences = []

    cmap = decision.claim_map
    attributed = {claim_id: [] for claim_id in cmap}
    sentences, used = [], set()
    for raw in raw_sentences:
        if not isinstance(raw, Mapping):
            reasons.append("malformed_renderer_output"); continue
        text = " ".join(str(raw.get("text") or "").split())
        ids = [str(x) for x in raw.get("claim_ids")] if isinstance(raw.get("claim_ids"), list) else []
        if not text or not ids:
            reasons.append("malformed_renderer_output"); continue
        if set(ids) - set(cmap):
            reasons.append("unknown_claim"); continue
        cited = [cmap[x] for x in ids]
        if any(c.source_record_id not in decision.authorized_record_ids for c in cited):
            reasons.append("unauthorized_record_reference"); continue
        ok, reason = _supported(text, cited)
        if not ok:
            reasons.append(reason or "unsupported_sentence")
        for claim_id in ids:
            attributed[claim_id].append(text)
        used.update(ids)
        sentences.append({"text": text, "claim_ids": ids})

    # Renderer-supplied action and bookkeeping are telemetry only. Release
    # authority and used IDs are always calculated from trusted sentence
    # attributions below.
    for claim_id in decision.required_claim_ids:
        if not attributed.get(claim_id):
            reasons.append("missing_required_claim"); continue
        ok, reason = _covered(cmap[claim_id], attributed[claim_id])
        if not ok:
            reasons.append(reason or "missing_required_claim")
    if reasons:
        safe, result = _fallback(decision, reasons)
        if logger:
            logger.warning("renderer contract conflict action=%s claims=%d reasons=%s; using deterministic fallback",
                           decision.action, len(decision.claims), ",".join(result["reasons"]))
        result["attempted_action_category"] = (
            str(attempted_action) if attempted_action in VALID_ACTIONS else
            "other" if attempted_action is not None else None)
        result["attempted_action_mismatch"] = bool(
            attempted_action is not None and attempted_action != decision.action)
        return safe, result
    output = {"action": decision.action,
              "answer": " ".join(s["text"] for s in sentences),
              "answer_structured": {"sentences": sentences, "used_claim_ids": sorted(used)},
              "used_record_ids": sorted({cmap[x].source_record_id for x in used}),
              "answer_source": "validated_renderer"}
    return output, {"contract_version": CONTRACT_VERSION, "conflict": False, "reasons": [],
                    "fallback_reason": None, "trusted_action": decision.action,
                    "attempted_action_category": (
                        str(attempted_action) if attempted_action in VALID_ACTIONS else
                        "other" if attempted_action is not None else None),
                    "attempted_action_mismatch": bool(
                        attempted_action is not None and attempted_action != decision.action),
                    "authorized_record_count": len(decision.authorized_record_ids),
                    "required_claim_count": len(decision.required_claim_ids)}
