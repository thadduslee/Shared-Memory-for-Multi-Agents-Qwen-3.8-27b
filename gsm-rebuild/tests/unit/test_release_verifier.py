"""Deterministic release verification."""

from __future__ import annotations

from gsm.disclosure.planner import DisclosureItem, DisclosurePlan
from gsm.domain.reasons import Reason
from gsm.domain.types import QueryAction, View
from gsm.providers.base import DraftResponse, GeneratorEvidence
from gsm.release.generator import CANONICAL_TEXT, DeterministicGenerator
from gsm.release.verifier import ProtectedValues, ReleaseMetadata, ReleaseVerifier

MID = "mem_" + "a" * 32
OTHER = "mem_" + "b" * 32


def plan(action: QueryAction = QueryAction.ANSWER_REDACTED, view: View = View.REDACTED) -> DisclosurePlan:
    items = ()
    if action in (QueryAction.ANSWER, QueryAction.ANSWER_REDACTED):
        items = (
            DisclosureItem(
                memory_id=MID,
                version_id="ver_1",
                view=view,
                allowed_fields=("employee", "availability"),
                prohibited_fields=("reason",),
                prohibited_categories=("medical",),
                reason="PARTIAL_VIEW_ONLY",
                policy_version="p",
            ),
        )
    return DisclosurePlan(
        plan_id="plan_1",
        tenant_id="acme",
        workspace_id="default",
        principal_id="paula",
        action=action,
        items=items,
        reasons=(),
        policy_version="p",
    )


def meta(**kw: object) -> ReleaseMetadata:
    base: dict[str, object] = dict(
        tenant_id="acme",
        protected=ProtectedValues(["knee surgery recovery"]),
        still_valid=frozenset({MID}),
        evidence_text="Dana Reyes is unavailable until 2026-10-20.",
    )
    base.update(kw)
    return ReleaseMetadata(**base)  # type: ignore[arg-type]


def draft(
    text: str, cited: tuple[str, ...] = (MID,), action: QueryAction = QueryAction.ANSWER_REDACTED
) -> DraftResponse:
    return DraftResponse(action=action, text=text, cited_memory_ids=cited, generator="model")


V = ReleaseVerifier()


def test_clean_draft_passes() -> None:
    assert V.verify(draft(f"Dana Reyes is unavailable until 2026-10-20 [{MID}]"), plan(), meta()).ok


def test_protected_exact_value_is_blocked() -> None:
    res = V.verify(draft("Dana is out for knee   Surgery recovery!"), plan(), meta())
    assert not res.ok and Reason.PROTECTED_VALUE_DETECTED in res.reasons


def test_unauthorized_memory_id_is_blocked() -> None:
    res = V.verify(draft(f"see [{OTHER}]", cited=(MID, OTHER)), plan(), meta())
    assert not res.ok and Reason.UNAPPROVED_MEMORY_CITED in res.reasons and res.unapproved_cited == {OTHER}
    res2 = V.verify(draft(f"see {OTHER}", cited=()), plan(), meta())
    assert Reason.UNAPPROVED_MEMORY_CITED in res2.reasons


def test_prohibited_category_is_blocked_unless_in_approved_evidence() -> None:
    res = V.verify(draft("Dana is on medical leave"), plan(), meta())
    assert Reason.PROHIBITED_CATEGORY_DETECTED in res.reasons


def test_action_inconsistent_with_plan_is_blocked() -> None:
    res = V.verify(draft("Dana Reyes is unavailable", action=QueryAction.ANSWER), plan(), meta())
    assert Reason.ACTION_MISMATCH in res.reasons


def test_non_answer_plans_allow_only_canonical_text() -> None:
    for action in (QueryAction.REFUSE, QueryAction.NO_MEMORY, QueryAction.ESCALATE):
        p = plan(action)
        ok = DraftResponse(action=action, text=CANONICAL_TEXT[action], generator="model")
        assert V.verify(ok, p, meta()).ok
        bad = DraftResponse(action=action, text="I can't say, but it relates to Dana's surgery.", generator="model")
        assert Reason.CONTENT_IN_NON_ANSWER in V.verify(bad, p, meta()).reasons


def test_stale_or_tombstoned_evidence_is_blocked() -> None:
    res = V.verify(draft("Dana Reyes is unavailable"), plan(), meta(still_valid=frozenset()))
    assert Reason.STALE_EVIDENCE in res.reasons and res.stale_ids == {MID}


def test_tenant_mismatch_is_blocked() -> None:
    assert Reason.TENANT_MISMATCH in V.verify(draft("x"), plan(), meta(tenant_id="other")).reasons


def test_deterministic_generator_output_always_verifies() -> None:
    p = plan()
    ev = [
        GeneratorEvidence(
            memory_id=MID,
            view=View.REDACTED,
            semantic_type="availability",
            text="Dana Reyes is unavailable until 2026-10-20.",
            fields={},
            observed_at=__import__("datetime").datetime(2026, 9, 1, tzinfo=__import__("datetime").UTC),
            content_schema="availability",
        )
    ]
    out = DeterministicGenerator().generate("where is dana", p.for_generator(), ev)
    assert V.verify(out, p, meta()).ok


def test_protected_values_are_keyed_per_instance() -> None:
    a, b = ProtectedValues(["secret value"]), ProtectedValues(["secret value"])
    assert a._fps != b._fps  # fingerprints are not reusable across requests
    assert a.found_in("the SECRET value.") and b.found_in("secret   value")
    assert not ProtectedValues(["abc"]).found_in("abc")  # too short to fingerprint meaningfully


class TestAnswererPolicy:
    def test_declining_is_always_accepted(self) -> None:
        for action in (QueryAction.REFUSE, QueryAction.NO_MEMORY):
            d = DraftResponse(action=action, text="free text the service will replace", generator="model")
            assert V.verify(d, plan(QueryAction.ANSWER, View.FULL), meta(), action_policy="answerer").ok
            assert V.verify(d, plan(QueryAction.NO_MEMORY), meta(), action_policy="answerer").ok

    def test_content_needs_permitted_evidence(self) -> None:
        d = draft("Dana is out", action=QueryAction.ANSWER)
        res = V.verify(d, plan(QueryAction.NO_MEMORY), meta(), action_policy="answerer")
        assert Reason.ACTION_MISMATCH in res.reasons

    def test_label_is_the_answerers_but_content_is_still_checked(self) -> None:
        ok = draft("Dana Reyes is unavailable until 2026-10-20", action=QueryAction.ANSWER)
        assert V.verify(ok, plan(), meta(), action_policy="answerer").ok  # plan says answer_redacted
        leak = draft("Dana: knee surgery recovery", action=QueryAction.ANSWER)
        assert Reason.PROTECTED_VALUE_DETECTED in V.verify(leak, plan(), meta(), action_policy="answerer").reasons
