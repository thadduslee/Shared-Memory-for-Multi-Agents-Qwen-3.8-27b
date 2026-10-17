"""Deterministic disclosure projections expose exactly their allowed fields."""

from __future__ import annotations

import itertools

import pytest

from gsm.disclosure.schemas import (
    SCHEMAS,
    ContentValidationError,
    get_schema,
    hidden_values,
    project,
    validate_content,
    validate_topic,
)
from gsm.domain.content import Content
from gsm.domain.types import View

LEAVE = Content(
    fields={
        "employee": "Dana Reyes",
        "availability": "unavailable",
        "return_date": "2026-10-20",
        "reason": "knee surgery recovery",
        "team": "apollo",
    }
)
SCHEMA = get_schema("availability")


def render(view: View) -> tuple[dict[str, str], str]:
    p = project(SCHEMA, view, LEAVE, topic="Dana availability update", semantic_type="availability")
    return p.fields, p.text


def test_full_view_has_everything() -> None:
    fields, text = render(View.FULL)
    assert set(fields) == {"employee", "availability", "return_date", "reason", "team"}
    assert "knee surgery" in text


def test_redacted_view_exact_fields_and_no_reason() -> None:
    fields, text = render(View.REDACTED)
    assert set(fields) == {"employee", "availability", "return_date", "team"}
    assert "surgery" not in text and "knee" not in text
    assert "Dana Reyes is unavailable until 2026-10-20" in text


def test_aggregate_view_does_not_identify_the_employee() -> None:
    fields, text = render(View.AGGREGATE)
    assert set(fields) == {"availability", "team"}
    assert "Dana" not in text and "2026-10-20" not in text and "surgery" not in text


def test_existence_only_reveals_only_existence() -> None:
    fields, text = render(View.EXISTENCE_ONLY)
    assert fields == {}
    assert text == "An availability update exists."


def test_prohibited_lists_name_fields_never_values() -> None:
    p = project(SCHEMA, View.REDACTED, LEAVE, topic="t", semantic_type="availability")
    assert p.prohibited_fields == ("reason",)
    assert p.prohibited_categories == ("medical",)
    assert "knee surgery recovery" not in repr(p.prohibited_fields) + repr(p.prohibited_categories)


def test_hidden_values_are_exactly_the_withheld_values() -> None:
    assert hidden_values(SCHEMA, View.REDACTED, LEAVE) == ["knee surgery recovery"]
    assert hidden_values(SCHEMA, View.FULL, LEAVE) == []


def test_free_text_only_in_full_view_for_notes() -> None:
    note = get_schema("note")
    c = Content(text="The Apollo budget is 2.1M")
    for view in (View.REDACTED, View.AGGREGATE, View.EXISTENCE_ONLY):
        p = project(note, view, c, topic="Apollo budget", semantic_type="fact")
        assert "2.1M" not in p.text and "text" in p.prohibited_fields


def test_every_schema_nests_its_views() -> None:
    order = [View.FULL, View.REDACTED, View.AGGREGATE, View.EXISTENCE_ONLY]
    for schema in SCHEMAS.values():
        for more, less in itertools.pairwise(order):
            assert set(schema.views[less].fields) <= set(schema.views[more].fields)


def test_content_validation() -> None:
    with pytest.raises(ContentValidationError):
        validate_content(SCHEMA, Content(fields={"employee": "x"}))  # missing required
    with pytest.raises(ContentValidationError):
        validate_content(SCHEMA, Content(fields={**LEAVE.fields, "ssn": "123"}))  # unknown field
    with pytest.raises(ContentValidationError):
        validate_content(SCHEMA, Content(text="free text", fields=LEAVE.fields))


def test_topic_cannot_repeat_protected_value() -> None:
    with pytest.raises(ContentValidationError):
        validate_topic(SCHEMA, LEAVE, "Dana knee surgery recovery")
    validate_topic(SCHEMA, LEAVE, "Dana availability update")


def test_tiered_note_views_and_protected_values() -> None:
    tiered = get_schema("tiered_note")
    c = Content(
        fields={
            "detail": "For Maple the customer is Granite Mutual; keep that in the core room.",
            "summary": "Maple is the insurance pilot.",
            "protected": "Granite Mutual\nmp_stg_4R2N",
        }
    )
    full = project(tiered, View.FULL, c, topic="Maple customer", semantic_type="note")
    red = project(tiered, View.REDACTED, c, topic="Maple customer", semantic_type="note")
    assert full.text == c.fields["detail"] and red.text == "Maple is the insurance pilot."
    assert set(red.fields) == {"summary"} and red.prohibited_categories == ("sensitive",)
    assert hidden_values(tiered, View.REDACTED, c) == [c.fields["detail"], "Granite Mutual", "mp_stg_4R2N"]
    with pytest.raises(ContentValidationError):
        validate_topic(tiered, c, "Maple Granite Mutual mapping")
