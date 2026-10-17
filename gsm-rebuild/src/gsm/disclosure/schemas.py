"""Schema-aware, deterministic disclosure projections.

Each content schema declares, per view, exactly which fields may appear and
how the view's text is rendered from *only* those fields. Redaction is
therefore a lookup, not a model call: a redacted view cannot contain a hidden
field because the renderer never receives it.

To add a view or schema: register a ``ContentSchema`` in ``SCHEMAS``. Every
schema must define all four views; ``validate_registry`` enforces that each
view's fields are a subset of the next more revealing view's fields.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from gsm.domain.content import Content
from gsm.domain.types import View


@dataclass(frozen=True)
class FieldSpec:
    name: str
    required: bool = True
    # Semantic category of the value (e.g. "medical"). Used for prohibited-
    # category checks at release time; never stored alongside the value.
    category: str | None = None
    # Newline-separated list of independent values (each checked on its own).
    multi: bool = False

    def values(self, raw: str) -> list[str]:
        if not raw:
            return []
        return [v.strip() for v in raw.split("\n") if v.strip()] if self.multi else [raw]


@dataclass(frozen=True)
class ViewSpec:
    fields: tuple[str, ...]
    template: str
    include_text: bool = False
    # Append in-view fields the template does not mention, as "(name: value)".
    append_unreferenced: bool = True


@dataclass(frozen=True)
class ContentSchema:
    name: str
    fields: tuple[FieldSpec, ...]
    views: Mapping[View, ViewSpec]
    text_allowed: bool = False
    text_category: str | None = None
    aggregate_label: str = "{semantic_type} record"

    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def spec(self, name: str) -> FieldSpec:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)


@dataclass(frozen=True)
class Projection:
    view: View
    fields: dict[str, str]
    text: str
    allowed_fields: tuple[str, ...]
    prohibited_fields: tuple[str, ...]
    prohibited_categories: tuple[str, ...] = field(default_factory=tuple)


class ContentValidationError(ValueError):
    """Raised with a fixed message; never echoes content."""


_GENERIC_VIEWS = {
    View.FULL: ViewSpec(fields=(), template="{text}", include_text=True),
    View.REDACTED: ViewSpec(fields=(), template="Restricted {semantic_type}: {topic}."),
    View.AGGREGATE: ViewSpec(fields=(), template="{semantic_type} record: {topic}."),
    View.EXISTENCE_ONLY: ViewSpec(fields=(), template="A {semantic_type} about {topic} exists."),
}

SCHEMAS: dict[str, ContentSchema] = {
    "note": ContentSchema(name="note", fields=(), views=_GENERIC_VIEWS, text_allowed=True),
    "decision": ContentSchema(
        name="decision",
        fields=(
            FieldSpec("project"),
            FieldSpec("decision"),
            FieldSpec("rationale", required=False),
            FieldSpec("decided_by", required=False),
        ),
        views={
            View.FULL: ViewSpec(
                fields=("project", "decision", "rationale", "decided_by"),
                template="Decision for {project}: {decision}.",
            ),
            View.REDACTED: ViewSpec(
                fields=("project", "decided_by"), template="A decision was recorded for {project}."
            ),
            View.AGGREGATE: ViewSpec(fields=("project",), template="Decision record for {project}."),
            View.EXISTENCE_ONLY: ViewSpec(fields=("project",), template="A decision record exists for {project}."),
        },
    ),
    "availability": ContentSchema(
        name="availability",
        fields=(
            FieldSpec("employee"),
            FieldSpec("availability"),
            FieldSpec("return_date"),
            FieldSpec("reason", category="medical"),
            FieldSpec("team", required=False),
        ),
        views={
            View.FULL: ViewSpec(
                fields=("employee", "availability", "return_date", "reason", "team"),
                template="{employee} is {availability} until {return_date}. Reason: {reason}.",
            ),
            View.REDACTED: ViewSpec(
                fields=("employee", "availability", "return_date", "team"),
                template="{employee} is {availability} until {return_date}.",
            ),
            View.AGGREGATE: ViewSpec(fields=("availability", "team"), template="team member {availability}"),
            # Views must nest: existence-only may not name the employee, because
            # the aggregate view (which it sits below) does not.
            View.EXISTENCE_ONLY: ViewSpec(fields=(), template="An availability update exists."),
        },
        aggregate_label="team member availability",
    ),
    # A conversational turn with an optional shareable tier. The full view is the
    # verbatim message; the redacted view is a summary written for a broader
    # audience; ``protected`` lists the exact sensitive values it must not carry.
    "tiered_note": ContentSchema(
        name="tiered_note",
        fields=(
            FieldSpec("detail", category="sensitive"),
            FieldSpec("summary", required=False),
            FieldSpec("protected", required=False, category="sensitive", multi=True),
        ),
        views={
            # The verbatim message already contains everything; do not restate
            # the summary or the protected values next to it.
            View.FULL: ViewSpec(
                fields=("detail", "summary", "protected"), template="{detail}", append_unreferenced=False
            ),
            View.REDACTED: ViewSpec(fields=("summary",), template="{summary}"),
            View.AGGREGATE: ViewSpec(fields=(), template="{semantic_type} record: {topic}."),
            View.EXISTENCE_ONLY: ViewSpec(fields=(), template="A {semantic_type} about {topic} exists."),
        },
    ),
    "credential": ContentSchema(
        name="credential",
        fields=(FieldSpec("system"), FieldSpec("username"), FieldSpec("secret", category="credential")),
        views={
            View.FULL: ViewSpec(
                fields=("system", "username", "secret"),
                template="Credential for {system}: user {username}, secret {secret}.",
            ),
            View.REDACTED: ViewSpec(
                fields=("system", "username"), template="Credential for {system} (user {username}); secret withheld."
            ),
            View.AGGREGATE: ViewSpec(fields=("system",), template="Credential record for {system}."),
            View.EXISTENCE_ONLY: ViewSpec(fields=("system",), template="A credential exists for {system}."),
        },
    ),
}


def get_schema(name: str) -> ContentSchema:
    try:
        return SCHEMAS[name]
    except KeyError:
        raise ContentValidationError("unknown content schema") from None


def validate_registry() -> None:
    order = [View.FULL, View.REDACTED, View.AGGREGATE, View.EXISTENCE_ONLY]
    for schema in SCHEMAS.values():
        names = set(schema.field_names())
        for view in order:
            if view not in schema.views:
                raise ContentValidationError("schema missing a view")
            if not set(schema.views[view].fields) <= names:
                raise ContentValidationError("view references unknown field")
        for more, less in itertools.pairwise(order):
            if not set(schema.views[less].fields) <= set(schema.views[more].fields):
                raise ContentValidationError("less revealing view exposes more fields")
            if schema.views[less].include_text and not schema.views[more].include_text:
                raise ContentValidationError("less revealing view exposes text")


def validate_content(schema: ContentSchema, content: Content) -> None:
    names = set(schema.field_names())
    for key in content.fields:
        if key not in names:
            raise ContentValidationError("unexpected field for schema")
    for spec in schema.fields:
        if spec.required and not content.fields.get(spec.name, "").strip():
            raise ContentValidationError("missing required field")
    if content.text is not None and not schema.text_allowed:
        raise ContentValidationError("schema does not accept free text")
    if schema.text_allowed and not (content.text or "").strip():
        raise ContentValidationError("text required")


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def validate_topic(schema: ContentSchema, content: Content, topic: str) -> None:
    """Topic labels are disclosed in existence-only views and refusal ranking.

    Reject a topic that repeats any value of a categorized (sensitive) field.
    """
    t = normalize(topic)
    for spec in schema.fields:
        if spec.category is None:
            continue
        for raw in spec.values(content.fields.get(spec.name, "")):
            value = normalize(raw)
            if value and value in t:
                raise ContentValidationError("topic repeats a protected value")


class _SafeDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def project(
    schema: ContentSchema,
    view: View,
    content: Content,
    *,
    topic: str,
    semantic_type: str,
) -> Projection:
    spec = schema.views[view]
    visible = {k: v for k, v in content.fields.items() if k in spec.fields and v}
    values = _SafeDict(visible)
    values["topic"] = topic
    values["semantic_type"] = semantic_type
    if spec.include_text and content.text:
        values["text"] = content.text
    text = spec.template.format_map(values).strip()
    # Optional fields the template does not mention are appended explicitly.
    for name in spec.fields if spec.append_unreferenced else ():
        if name in visible and "{" + name + "}" not in spec.template:
            text += f" ({name.replace('_', ' ')}: {visible[name]})"
    hidden = [n for n in schema.field_names() if n not in spec.fields]
    categories = sorted({c for n in hidden if (c := schema.spec(n).category) is not None})
    if schema.text_allowed and not spec.include_text:
        hidden.append("text")
        if schema.text_category:
            categories = sorted({*categories, schema.text_category})
    return Projection(
        view=view,
        fields=visible,
        text=text,
        allowed_fields=tuple(n for n in spec.fields) + (("text",) if spec.include_text else ()),
        prohibited_fields=tuple(hidden),
        prohibited_categories=tuple(categories),
    )


def hidden_values(schema: ContentSchema, view: View, content: Content) -> list[str]:
    """Values present in the full content but absent from ``view``.

    Used only inside the trusted release verifier (as keyed fingerprints),
    never shown to a model or written to an audit record.
    """
    spec = schema.views[view]
    names = set(schema.field_names())
    out = [
        piece
        for k, v in content.fields.items()
        if k not in spec.fields and v
        for piece in (schema.spec(k).values(v) if k in names else [v])
    ]
    visible_text = " ".join(v for k, v in content.fields.items() if k in spec.fields)
    out = [v for v in out if normalize(v) and normalize(v) not in normalize(visible_text)]
    if content.text and not spec.include_text:
        out.append(content.text)
    return out


validate_registry()
