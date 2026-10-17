"""The closed action vocabularies. FROZEN by design: the prompts choose among
these; they cannot add to them. A model reply that names anything else is
rejected and traced as such -- that rejection count is itself evidence the
outer loop sees.

Four groups (DESIGN.md §2.3): record, policy, structure, control. Plus the
chunk kinds the front door may assign, the integrity signals the librarian
emits, and the SearchSpec the retriever composes. Everything here is parsed
from the model's JSON with `parse_ops` and validated with `validate_op`; the
identifier and predicate grammars are what keep the DDL path safe.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------- chunks


class ChunkKind(StrEnum):
    FACT = "fact"                 # a statement about the world (schedule, value, status)
    CORRECTION = "correction"     # a newer value for something already stated
    CONSENT = "consent"           # widens who may know something
    REVOKE = "revoke"             # narrows who may know something          (tightening)
    RESTRICT = "restrict"         # lowers granularity for someone          (tightening)
    DELETE = "delete"             # asks for content to be forgotten        (tightening)
    DOCUMENT = "document"         # a whole artefact to keep intact (note, lab result, summary)
    QUESTION = "question"         # not stored as a fact
    CHATTER = "chatter"           # nothing to keep


TIGHTENING_KINDS = frozenset({ChunkKind.REVOKE, ChunkKind.RESTRICT, ChunkKind.DELETE})


@dataclass
class Chunk:
    chunk_id: str
    turn_id: str
    timestamp: str
    speaker_id: str
    speaker_role: str
    text: str
    kind: str                                   # ChunkKind value
    about: list[str] = field(default_factory=list)
    suggested_audience: list[str] = field(default_factory=list)   # empty => {speaker}
    attributes: dict[str, str] = field(default_factory=dict)      # free key/values the processor may map to fields
    turn_kind: str = ""

    @property
    def is_tightening(self) -> bool:
        return self.kind in TIGHTENING_KINDS


# ---------------------------------------------------------------- ops


class RecordOp(StrEnum):
    ADD = "add"
    SPLIT = "split"
    MERGE = "merge"
    SUPERSEDE = "supersede"
    DERIVE = "derive"
    LINK = "link"
    DELETE = "delete"
    SHRED = "shred"


class PolicyOp(StrEnum):
    GRANT = "grant"
    REVOKE = "revoke"
    RESTRICT = "restrict"
    EXPIRE = "expire"


class StructureOp(StrEnum):
    ADD_FIELD = "add_field"
    ADD_COLLECTION = "add_collection"
    ADD_INDEX = "add_index"
    CREATE_VIEW = "create_view"
    REKEY = "rekey"


class ControlOp(StrEnum):
    HOLD = "hold"
    FLAG_CONFLICT = "flag_conflict"
    NOOP = "noop"


ALL_OPS: frozenset[str] = frozenset(
    {*RecordOp, *PolicyOp, *StructureOp, *ControlOp}
)

GRANULARITIES: tuple[str, ...] = ("full", "summary", "existence")
FIELD_TYPES: tuple[str, ...] = ("TEXT", "INTEGER", "REAL")

#: Required argument names per op. Optional ones are documented in DESIGN.md.
REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    RecordOp.ADD: ("chunk_id",),                                  # optional: collection, fields, audience, granularity
    RecordOp.SPLIT: ("chunk_id", "parts"),                        # parts: [{"text":..., "about":[...], "audience":[...]}]
    RecordOp.MERGE: ("record_ids", "text"),
    RecordOp.SUPERSEDE: ("old_record_id", "chunk_id"),            # the chunk becomes the new version
    RecordOp.DERIVE: ("source_record_ids", "text"),               # optional: granularity (default "summary")
    RecordOp.LINK: ("record_id", "other_record_id", "relation"),
    RecordOp.DELETE: ("record_ids",),
    RecordOp.SHRED: ("record_ids",),
    PolicyOp.GRANT: ("record_ids", "principal_ids", "authority"), # authority: the principal whose words authorise it
    PolicyOp.REVOKE: ("record_ids", "principal_ids"),
    PolicyOp.RESTRICT: ("record_ids", "principal_ids", "granularity"),
    PolicyOp.EXPIRE: ("record_ids", "principal_ids", "at"),
    StructureOp.ADD_FIELD: ("collection", "name", "type"),
    StructureOp.ADD_COLLECTION: ("name", "fields"),               # fields: [{"name":..., "type":...}]
    StructureOp.ADD_INDEX: ("collection", "fields"),
    StructureOp.CREATE_VIEW: ("name", "collection", "where"),     # optional: order_by
    StructureOp.REKEY: ("collection", "field", "assignments"),    # assignments: {record_id: value}
    ControlOp.HOLD: ("chunk_id",),
    ControlOp.FLAG_CONFLICT: ("record_ids", "note"),
    ControlOp.NOOP: ("chunk_id",),
}


@dataclass
class Op:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    chunk_id: str = ""              # provenance: which chunk motivated it
    signal_kind: str = ""           # instruction | integrity:<name> | ""

    @property
    def group(self) -> str:
        if self.name in set(RecordOp):
            return "record"
        if self.name in set(PolicyOp):
            return "policy"
        if self.name in set(StructureOp):
            return "structure"
        if self.name in set(ControlOp):
            return "control"
        return "unknown"


class OpError(ValueError):
    """An op the vocabulary does not admit. The message is the trace reason."""


# ---------------------------------------------------------------- grammars

_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_RESERVED = frozenset({
    # the envelope (DESIGN.md §2.4); a structure op may never name these
    "record_id", "collection", "body", "speaker_id", "speaker_role", "about", "source_turn",
    "created_at", "valid_from", "valid_to", "superseded_by", "derived_from", "granularity",
    "tombstoned_at",
    # keyring and trace tables
    "principal_keys", "record_keys", "trace", "sqlite_master", "sqlite_sequence",
})

ENVELOPE_COLUMNS: tuple[str, ...] = (
    "record_id", "collection", "body", "speaker_id", "speaker_role", "about", "source_turn",
    "created_at", "valid_from", "valid_to", "superseded_by", "derived_from", "granularity",
    "tombstoned_at",
)


def is_identifier(name: Any) -> bool:
    return isinstance(name, str) and bool(_IDENT.match(name)) and name not in _RESERVED


# A predicate is a conjunction of `field OP literal` clauses. No OR, no
# subqueries, no functions: enough to express "current records of this kind
# about this principal", which is what views and searches need.
_PRED_OPS = ("=", "!=", "<", "<=", ">", ">=", "LIKE", "IS NULL", "IS NOT NULL")


@dataclass
class Clause:
    field: str
    op: str
    value: str | int | float | None = None


def parse_predicate(where: Any) -> list[Clause]:
    """`where` is a list of [field, op, value] triples (value omitted for IS NULL
    forms). Fields may be envelope columns (read-only use is fine) or added
    fields; operators come from _PRED_OPS. Anything else raises OpError."""
    if where in (None, "", []):
        return []
    if not isinstance(where, list):
        raise OpError("where must be a list of [field, op, value] triples")
    clauses: list[Clause] = []
    for item in where:
        if not isinstance(item, (list, tuple)) or len(item) not in (2, 3):
            raise OpError(f"bad clause {item!r}")
        fld, op = item[0], item[1]
        if not isinstance(fld, str) or not (_IDENT.match(fld) or fld in ENVELOPE_COLUMNS):
            raise OpError(f"bad field {fld!r}")
        if op not in _PRED_OPS:
            raise OpError(f"bad operator {op!r}")
        if op.startswith("IS"):
            clauses.append(Clause(fld, op))
            continue
        if len(item) != 3 or not isinstance(item[2], (str, int, float)):
            raise OpError(f"clause {item!r} needs a scalar value")
        clauses.append(Clause(fld, op, item[2]))
    return clauses


def predicate_sql(clauses: list[Clause]) -> tuple[str, list[Any]]:
    """Parameterised SQL for a parsed predicate. Field names were validated by
    `parse_predicate`; values are always bound."""
    parts: list[str] = []
    params: list[Any] = []
    for c in clauses:
        if c.op.startswith("IS"):
            parts.append(f'"{c.field}" {c.op}')
        else:
            parts.append(f'"{c.field}" {c.op} ?')
            params.append(c.value)
    return " AND ".join(parts), params


# ---------------------------------------------------------------- search


@dataclass
class SearchSpec:
    """What the retriever's model composes; the scaffold runs it. `as_of` and
    the tombstone filter are added by the scaffold, never by the model."""

    collections: list[str] = field(default_factory=list)   # empty => all
    where: list[Clause] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)          # keyword terms, matched on opened text
    about: list[str] = field(default_factory=list)          # principal ids the records should concern
    current_only: bool = True
    limit: int = 40


def parse_search(payload: Any) -> SearchSpec:
    if not isinstance(payload, dict):
        raise OpError("search spec must be an object")
    cols = payload.get("collections") or []
    if not isinstance(cols, list) or not all(is_identifier(c) for c in cols):
        raise OpError("collections must be a list of identifiers")
    terms = [t for t in (payload.get("terms") or []) if isinstance(t, str) and t.strip()]
    about = [a for a in (payload.get("about") or []) if isinstance(a, str) and a.strip()]
    limit = payload.get("limit", 40)
    if not isinstance(limit, int) or not 1 <= limit <= 200:
        limit = 40
    return SearchSpec(
        collections=list(cols), where=parse_predicate(payload.get("where")),
        terms=terms[:20], about=about[:20], current_only=bool(payload.get("current_only", True)),
        limit=limit,
    )


# ---------------------------------------------------------------- integrity signals


class IntegrityKind(StrEnum):
    CONTRADICTION = "contradiction"          # two current records give different values for one thing
    DUPLICATE = "duplicate"                  # near-identical bodies
    SCHEMA_MISMATCH = "schema_mismatch"      # an attribute the processor emitted has no field to land in
    ORPHANED_DERIVATION = "orphaned_derivation"  # a derived record whose every source is gone
    DANGLING_LINK = "dangling_link"          # a link to a tombstoned record


@dataclass
class IntegritySignal:
    kind: str                       # IntegrityKind value
    record_ids: list[str]
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- parsing & validation


def validate_op(op: Op) -> None:
    """Raise OpError if `op` is outside the vocabulary or malformed. Does not
    check store state (that is the librarian's job) -- only shape."""
    if op.name not in ALL_OPS:
        raise OpError(f"unknown op {op.name!r}")
    missing = [a for a in REQUIRED_ARGS[op.name] if a not in op.args]
    if missing:
        raise OpError(f"{op.name} missing {missing}")
    a = op.args
    if op.name == PolicyOp.GRANT and not isinstance(a.get("authority"), str):
        raise OpError("grant needs a principal id in `authority`")
    if op.name == PolicyOp.RESTRICT and a.get("granularity") not in GRANULARITIES:
        raise OpError(f"granularity must be one of {GRANULARITIES}")
    if op.name in (StructureOp.ADD_FIELD,):
        if not is_identifier(a["collection"]) or not is_identifier(a["name"]):
            raise OpError("add_field: bad identifier")
        if a["type"] not in FIELD_TYPES:
            raise OpError(f"add_field: type must be one of {FIELD_TYPES}")
    if op.name == StructureOp.ADD_COLLECTION:
        if not is_identifier(a["name"]):
            raise OpError("add_collection: bad name")
        fields = a["fields"]
        if not isinstance(fields, list) or any(
            not isinstance(f, dict) or not is_identifier(f.get("name")) or f.get("type") not in FIELD_TYPES
            for f in fields
        ):
            raise OpError("add_collection: fields must be [{name, type}] with valid identifiers")
    if op.name == StructureOp.ADD_INDEX:
        if not is_identifier(a["collection"]) or not isinstance(a["fields"], list) or not a["fields"]:
            raise OpError("add_index: bad collection or fields")
        if any(not (is_identifier(f) or f in ENVELOPE_COLUMNS) for f in a["fields"]):
            raise OpError("add_index: bad field name")
    if op.name == StructureOp.CREATE_VIEW:
        if not is_identifier(a["name"]) or not is_identifier(a["collection"]):
            raise OpError("create_view: bad identifier")
        parse_predicate(a.get("where"))
        ob = a.get("order_by")
        if ob is not None and not (isinstance(ob, str) and (is_identifier(ob) or ob in ENVELOPE_COLUMNS)):
            raise OpError("create_view: bad order_by")
    if op.name == StructureOp.REKEY:
        if not is_identifier(a["collection"]) or not is_identifier(a["field"]):
            raise OpError("rekey: bad identifier")
        if not isinstance(a["assignments"], dict):
            raise OpError("rekey: assignments must be {record_id: value}")
    for key in ("record_ids", "principal_ids", "source_record_ids"):
        if key in a and (not isinstance(a[key], list) or not all(isinstance(x, str) for x in a[key])):
            raise OpError(f"{op.name}: {key} must be a list of strings")


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str | None) -> Any:
    """The first JSON value in a model reply: a fenced block if present, else
    the outermost {...} or [...]. None if nothing parses."""
    if not text:
        return None
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for cand in candidates:
        cand = cand.strip()
        for opener, closer in (("{", "}"), ("[", "]")):
            start, end = cand.find(opener), cand.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(cand[start:end + 1])
                except json.JSONDecodeError:
                    continue
    return None


def parse_ops(payload: Any, *, default_chunk_id: str = "", signal_kind: str = "") -> tuple[list[Op], list[str]]:
    """Turn a model's reply (already JSON-decoded, or a raw string) into ops.
    Returns (valid ops, rejection reasons). Never raises: a malformed reply is
    zero ops plus reasons, and the caller traces both."""
    if isinstance(payload, str):
        payload = extract_json(payload)
    if isinstance(payload, dict) and "ops" in payload:
        payload = payload["ops"]
    if payload is None:
        return [], ["no JSON in reply"]
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return [], ["ops must be a list"]
    ops: list[Op] = []
    rejected: list[str] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("op"), str):
            rejected.append(f"not an op object: {str(item)[:80]}")
            continue
        args = {k: v for k, v in item.items() if k not in ("op", "signal_kind")}
        # `default_chunk_id` is provenance only. It does NOT stand in for a
        # required chunk_id: a bare op is rejected, and the processor's
        # coverage rule then gives that chunk its fallback.
        op = Op(
            name=item["op"], args=args,
            chunk_id=str(args.get("chunk_id") or default_chunk_id),
            signal_kind=str(item.get("signal_kind") or signal_kind),
        )
        try:
            validate_op(op)
        except OpError as exc:
            rejected.append(f"{op.name}: {exc}")
            continue
        ops.append(op)
    return ops, rejected
