"""Bounding the answerer (SPEC §9.3). Declining is always accepted with canonical text; content needs released
evidence and must not carry a protected value of any record that was not released."""

from __future__ import annotations

from typing import Any

from govmem.core.store import Store
from govmem.text import mask, ngrams

CANONICAL = {
    "refuse": "I can't share that information with you.",
    "no_memory": "I don't have that information in accessible memory.",
}
ANSWERING = ("answer", "answer_redacted")


def check_release(store: Store, output: dict[str, Any], evidence_ids: list[str],
                  evidence_texts: list[str]) -> tuple[dict[str, Any], str]:
    """Returns (output to release, reason). reason is '' when accepted unchanged."""
    action = str(output.get("action") or "")
    if action not in ANSWERING:
        a = action if action in CANONICAL else "refuse"
        return {**output, "action": a, "answer": CANONICAL[a], "answer_structured": {}}, \
            ("" if action in CANONICAL else "unknown_action")
    if not evidence_ids:
        return {**output, "action": "no_memory", "answer": CANONICAL["no_memory"], "answer_structured": {}}, \
            "answer_without_evidence"
    fps = store.protected_fingerprints(exclude=set(evidence_ids))
    if fps:
        text = str(output.get("answer") or "") + " " + str(output.get("answer_structured") or "")
        released = {store.fingerprint(g) for t in evidence_texts for g in ngrams(t, 8)}
        leaked = [g for g in ngrams(text, 8) if (fp := store.fingerprint(g)) in fps and fp not in released]
        if leaked:
            # Mask only the offending values; the rest of the answer was built from released evidence.
            masked = mask(str(output.get("answer") or ""), sorted(set(leaked), key=len, reverse=True), "[withheld]")
            return {**output, "answer": masked, "answer_structured": {}}, "protected_value_of_unreleased_record"
    cited = output.get("used_record_ids") or []
    bad = [c for c in cited if c not in evidence_ids]
    if bad:
        return {**output, "used_record_ids": [c for c in cited if c in evidence_ids]}, "cited_unreleased"
    return output, ""
