"""Generic adaptive memory with provenance, scoped authorization, and forgetting.

Fixed code supplies mechanisms. Domain concepts are values learned from raw
observations. Relationships and semantic links aid organization and retrieval;
only explicit permission evidence authorizes a non-owner.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .policy import AuthorizationRequest, PolicyEvaluator, Principal, Resource

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _stable_id(prefix: str, *values: Any) -> str:
    return f"{prefix}_{hashlib.sha256(_json(values).encode()).hexdigest()[:24]}"


@dataclass
class Evidence:
    record_id: str
    turn_id: str
    author_role: str
    kind: str
    sensitivity: str
    body: str
    redacted: bool = False
    source_observation_id: str = ""
    status: str = "active"


@dataclass
class Decision:
    allowed: list[Evidence] = field(default_factory=list)
    denied_rbac: list[str] = field(default_factory=list)
    denied_scope: list[str] = field(default_factory=list)
    denied_tombstone: list[str] = field(default_factory=list)
    query_terms: list[str] = field(default_factory=list)
    denied_details: list[dict[str, Any]] = field(default_factory=list)
    obsolete: list[str] = field(default_factory=list)
    outside_top_k: list[str] = field(default_factory=list)
    relationship_labels: list[str] = field(default_factory=list)
    permission_facts: list[dict[str, Any]] = field(default_factory=list)
    candidates_considered: int = 0
    supported_query_facets: list[str] = field(default_factory=list)
    unsupported_query_facets: list[str] = field(default_factory=list)
    source_group_counts: dict[str, int] = field(default_factory=dict)
    decision_trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def touched_deleted(self) -> bool:
        return bool(self.denied_tombstone)

    @property
    def touched_unauthorized(self) -> bool:
        return bool(self.denied_rbac or self.denied_scope)


class MemoryStore:
    """A generic store. Empty initialization creates schema, not policy rows."""

    def __init__(self, db_path: str | Path = ":memory:", *,
                 interpreter: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                 policy: dict[str, Any] | None = None) -> None:
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.interpreter = interpreter
        self.policy = dict(policy or {})
        self.policy_evaluator = PolicyEvaluator(self.policy)
        self._seq = 0
        self._index_key_cache: bytes | None = None
        self.initialize()

    def initialize(self, schema_sql: str | None = None) -> None:
        self.conn.executescript(schema_sql or SCHEMA_PATH.read_text(encoding="utf-8"))
        self._seq = int(self.conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM observations").fetchone()[0])
        self.conn.commit()

    def apply_migration(self, migration_sql: str) -> None:
        if not migration_sql.strip():
            return
        try:
            self.conn.executescript("BEGIN;\n" + migration_sql + "\nCOMMIT;")
        except sqlite3.Error:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    # Generic entities and relationships ---------------------------------

    def upsert_principal(self, principal_id: str, role: str = "", display_name: str = "",
                         *, attributes: dict[str, Any] | None = None,
                         source_observation_id: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO principals VALUES (?,?,?,?,?,?)
               ON CONFLICT(principal_id) DO UPDATE SET
                 observed_role=excluded.observed_role,
                 display_name=excluded.display_name,
                 attributes_json=excluded.attributes_json""",
            (principal_id, role, display_name, _json(attributes or {}),
             source_observation_id, _now()),
        )

    def upsert_relationship(self, rel_type: str, subject_id: str, object_id: str,
                            scope: str = "", *, episode_id: str = "",
                            attributes: dict[str, Any] | None = None,
                            source_observation_id: str | None = None,
                            derived_from_id: str | None = None,
                            confidence: float | None = None) -> str:
        attrs = dict(attributes or {})
        if scope:
            attrs.setdefault("scope", scope)
        rel_id = _stable_id("rel", episode_id, subject_id, rel_type, object_id, attrs)
        self.conn.execute(
            """INSERT OR IGNORE INTO relationships
               (relationship_id,episode_id,subject_id,predicate,object_id,attributes_json,
                status,valid_from,valid_until,source_observation_id,derived_from_id,
                confidence,created_at,supersedes_id)
               VALUES (?,?,?,?,?,?,'active',NULL,NULL,?,?,?,?,NULL)""",
            (rel_id, episode_id, subject_id, rel_type, object_id, _json(attrs),
             source_observation_id, derived_from_id, confidence, _now()),
        )
        return rel_id

    def add_semantic_link(self, episode_id: str, original_value: str, related_value: str,
                          *, source_observation_id: str, relation: str = "related",
                          confidence: float = 1.0) -> str:
        """Persist an interpretation. It has no authorization effect."""
        self._require_observation(source_observation_id)
        link_id = _stable_id("sem", episode_id, original_value, related_value,
                             relation, source_observation_id)
        self.conn.execute(
            "INSERT OR IGNORE INTO semantic_links VALUES (?,?,?,?,?,'active',?,?,?)",
            (link_id, episode_id, original_value, related_value, relation,
             source_observation_id, float(confidence), _now()),
        )
        self.conn.commit()
        return link_id

    # Observations and derived memory -------------------------------------

    def ingest_turn(self, episode_id: str, turn: dict[str, Any], owner_id: str = "") -> str:
        turn_id = str(turn.get("turn_id") or f"seq-{self._seq + 1}")
        existing = self.conn.execute(
            "SELECT observation_id FROM observations WHERE episode_id=? AND turn_id=?",
            (episode_id, turn_id),
        ).fetchone()
        if existing:
            return str(existing[0])
        self._seq += 1
        speaker = turn.get("speaker") or {}
        text = str(turn.get("text") or "")
        observation_id = _stable_id("obs", episode_id, turn_id)
        observed_at = str(turn.get("timestamp") or _now())
        self.conn.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (observation_id, episode_id, turn_id,
             str(speaker.get("principal_id") or "unknown"),
             str(speaker.get("role") or ""), str(turn.get("turn_kind") or ""),
             text, _json(turn), observed_at, self._seq,
             hashlib.sha256(_json(turn).encode()).hexdigest()),
        )
        self._derive_clause_memories(episode_id, observation_id, text, owner_id, observed_at)
        self._derive_explicit_permissions(episode_id, observation_id, text, owner_id,
                                          str(speaker.get("principal_id") or ""), observed_at)
        self._run_interpreter(episode_id, observation_id, turn, owner_id)
        if _DELETE_REQUEST.search(text):
            self._honor_deletion_request(episode_id, text,
                                         str(speaker.get("principal_id") or "unknown"))
        self.conn.commit()
        return observation_id

    def add_fact(self, episode_id: str, *, subject_id: str, predicate: str, value: Any,
                 source_observation_id: str, text_value: str = "",
                 concepts: list[str] | None = None, status: str = "active",
                 valid_from: str | None = None, valid_until: str | None = None,
                 supersedes_id: str | None = None, confidence: float | None = None,
                 memory_type: str = "fact") -> str:
        self._require_observation(source_observation_id)
        seq = self.conn.execute(
            "SELECT seq FROM observations WHERE observation_id=?", (source_observation_id,)
        ).fetchone()[0]
        memory_id = _stable_id("mem", source_observation_id, memory_type, subject_id,
                               predicate, value, status)
        self.conn.execute(
            """INSERT OR IGNORE INTO memories
               (memory_id,episode_id,memory_type,subject_id,predicate,value_json,text_value,
                concepts_json,status,valid_from,valid_until,source_observation_id,
                derived_from_id,confidence,created_at,seq,supersedes_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (memory_id, episode_id, memory_type, subject_id, predicate, _json(value),
             text_value, _json(sorted(set(concepts or []))), status, valid_from,
             valid_until, source_observation_id, source_observation_id, confidence,
             _now(), seq, supersedes_id),
        )
        self._index_memory(memory_id, " ".join([predicate, text_value, *(concepts or [])]))
        if supersedes_id:
            self.conn.execute(
                "UPDATE memories SET status='superseded' WHERE memory_id=? AND status='active'",
                (supersedes_id,),
            )
        return memory_id

    def grant_permission(self, episode_id: str, *, grantee_id: str,
                         resource_owner_id: str, scope: list[str] | str,
                         source_observation_id: str, exclusions: list[str] | None = None,
                         purpose: str = "", valid_from: str | None = None,
                         valid_until: str | None = None, effect: str = "allow",
                         confidence: float = 1.0, supersedes_id: str | None = None) -> str:
        if effect not in {"allow", "deny"}:
            raise ValueError("permission effect must be allow or deny")
        self._require_observation(source_observation_id)
        scopes = [scope] if isinstance(scope, str) else list(scope)
        permission_id = _stable_id("perm", episode_id, grantee_id, resource_owner_id,
                                   effect, scopes, exclusions or [], source_observation_id)
        self.conn.execute(
            """INSERT OR IGNORE INTO permissions
               (permission_id,episode_id,grantee_id,resource_owner_id,effect,scope_json,
                purpose,exclusions_json,status,valid_from,valid_until,source_observation_id,
                derived_from_id,confidence,created_at,supersedes_id)
               VALUES (?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?)""",
            (permission_id, episode_id, grantee_id, resource_owner_id, effect,
             _json(scopes), purpose, _json(exclusions or []), valid_from, valid_until,
             source_observation_id, source_observation_id, confidence, _now(), supersedes_id),
        )
        if supersedes_id:
            self.conn.execute("UPDATE permissions SET status='superseded' WHERE permission_id=?",
                              (supersedes_id,))
        # Co-scoped concepts are a provenance-bearing semantic interpretation.
        # They improve retrieval, but authorization still requires this actual
        # permission row and exact scope membership.
        scope_concepts = sorted(_concepts(" ".join(scopes)) |
                                _structural_concepts(" ".join(scopes)))
        for index, left in enumerate(scope_concepts):
            for right in scope_concepts[index + 1:]:
                link_id = _stable_id("sem", episode_id, left, right,
                                     "co_scoped", source_observation_id)
                self.conn.execute(
                    "INSERT OR IGNORE INTO semantic_links VALUES (?,?,?,?,?,'active',?,?,?)",
                    (link_id, episode_id, left, right, "co_scoped",
                     source_observation_id, confidence, _now()))
        self.conn.commit()
        return permission_id

    def revoke_permission(self, permission_id: str, *, source_observation_id: str) -> None:
        self._require_observation(source_observation_id)
        self.conn.execute("UPDATE permissions SET status='revoked' WHERE permission_id=?",
                          (permission_id,))
        row = self.conn.execute(
            "SELECT episode_id,grantee_id,resource_owner_id FROM permissions WHERE permission_id=?",
            (permission_id,),
        ).fetchone()
        if row:
            self.add_fact(row["episode_id"], subject_id=row["grantee_id"],
                          predicate="permission_revocation",
                          value={"permission_id": permission_id,
                                 "resource_owner_id": row["resource_owner_id"]},
                          source_observation_id=source_observation_id,
                          memory_type="revocation")
        self.conn.commit()

    # Retrieval and authorization ----------------------------------------

    def retrieve(self, *, requester_id: str, requester_role: str = "",
                 owner_id: str = "",
                 query: str, episode_id: str | None = None,
                 as_of_seq: int | None = None, checkpoint_id: str | None = None,
                 top_k: int = 8) -> Decision:
        # A shard can contain observations about more than one principal. A
        # named query target therefore overrides the shard's default owner;
        # self-access never spills onto another named principal's facts.
        # A requester introducing themselves ("This is Dr. Lee ...") names who is
        # asking, not whose records are wanted.
        resource_owner_id = self._mentioned_principal(query, exclude=requester_id) or owner_id
        query_terms = _query_concepts(query)
        # Entity names identify the shard but are poor relevance signals: if a
        # query names its owner, that must not make every fact about that owner
        # responsive.
        for principal in self.conn.execute("SELECT principal_id,display_name FROM principals"):
            query_terms -= _concepts(f"{principal['principal_id']} {principal['display_name']}")
        decision = Decision(query_terms=sorted(query_terms))
        if episode_id is None:
            row = self.conn.execute(
                "SELECT episode_id FROM observations ORDER BY seq DESC LIMIT 1").fetchone()
            episode_id = str(row[0]) if row else ""
        as_of_time = self._time_at_seq(episode_id, as_of_seq)
        decision.relationship_labels = [r[0] for r in self.conn.execute(
            """SELECT DISTINCT predicate FROM relationships
               WHERE episode_id=? AND subject_id=? AND status='active' ORDER BY predicate""",
            (episode_id, requester_id),
        )]
        permission_candidates = list(self.conn.execute(
            """SELECT * FROM permissions WHERE episode_id=? AND grantee_id=?
               AND resource_owner_id=? ORDER BY created_at DESC""",
            (episode_id, requester_id, resource_owner_id),
        ))
        permissions = [row for row in permission_candidates
                       if row["status"] == "active" and
                       _is_current(row["valid_from"], row["valid_until"], as_of_time)]
        excluded_query_terms: set[str] = set()
        permitted_query_terms: set[str] = set()
        for permission in permissions:
            if permission["effect"] == "allow":
                scope_text = " ".join(_loads(permission["scope_json"], []))
                permitted_query_terms |= _scope_concepts(scope_text)
                excluded_query_terms |= _concepts(
                    " ".join(_loads(permission["exclusions_json"], [])))
        decision.permission_facts = [
            {**self._permission_summary(row),
             "valid_at_query": bool(row["status"] == "active" and
                                    _is_current(row["valid_from"], row["valid_until"], as_of_time)),
             "query_time": as_of_time}
            for row in permission_candidates
        ]
        permission_ids = [row["permission_id"] for row in permission_candidates]
        active_permission_ids = [row["permission_id"] for row in permissions]
        trace: dict[str, dict[str, Any]] = {}

        def record_trace(row: sqlite3.Row, concepts: set[str], outcome: str,
                         stage: str, rule: str) -> None:
            trace[row["memory_id"]] = {
                "trace_id": f"{checkpoint_id or 'query'}:record:{row['memory_id']}",
                "record_id": row["memory_id"],
                "source_observation_id": row["source_observation_id"],
                "status": row["status"],
                "record_valid_from": row["valid_from"],
                "record_valid_until": row["valid_until"],
                "classification": row["memory_type"],
                "structural_concepts": sorted(
                    value for value in concepts if value.endswith("_expression")),
                "event_time_features": _temporal_features(
                    row["text_value"], row["valid_from"] or row["created_at"]),
                "outcome": outcome,
                "decision_stage": stage,
                "decision_rule": rule,
                "permission_ids": permission_ids,
                "permission_validity_at_query": {
                    "query_time": as_of_time,
                    "active_permission_ids": active_permission_ids,
                },
            }
        query_topic_terms = query_terms - permitted_query_terms
        rows = self.conn.execute(
            """SELECT m.*,o.turn_id,o.observed_role,o.kind,o.raw_text AS source_text,
                      CASE WHEN t.target_id IS NULL THEN 0 ELSE 1 END AS deleted
               FROM memories m JOIN observations o ON o.observation_id=m.source_observation_id
               LEFT JOIN tombstones t ON
                 (t.target_id=m.memory_id AND t.target_type='memory') OR
                 (t.target_id=m.source_observation_id AND t.target_type='observation')
               WHERE m.episode_id=? AND m.subject_id=? AND (? IS NULL OR m.seq<=?)
               ORDER BY m.seq DESC,m.memory_id""",
            (episode_id, resource_owner_id, as_of_seq, as_of_seq),
        ).fetchall()

        ranked: list[tuple[float, sqlite3.Row, set[str]]] = []
        expanded = self._expand_terms(episode_id, query_terms)
        prepared: list[tuple[float, bool, sqlite3.Row, set[str], set[str]]] = []
        for row in rows:
            if row["memory_type"] in {"utterance", "permission_evidence"}:
                continue
            concepts = (set(_loads(row["concepts_json"], [])) |
                        _concepts(row["text_value"]) | _structural_concepts(row["text_value"]))
            # Tombstoning intentionally purges content and its term index. Its
            # prior relevance is therefore unknowable; fail closed before
            # semantic filtering so extraction or reopening cannot resurrect it.
            if row["deleted"]:
                decision.candidates_considered += 1
                decision.denied_tombstone.append(row["memory_id"])
                decision.denied_details.append({"memory_id": row["memory_id"],
                                                "reason": "tombstoned",
                                                "concepts": []})
                self._log(checkpoint_id, requester_id, row["memory_id"],
                          "deny_tombstone", "tombstoned")
                record_trace(row, set(), "denied", "deletion_gate", "tombstoned")
                continue
            score = _semantic_score(expanded, concepts)
            topic_ok = (not query_topic_terms or
                        _semantic_score(query_topic_terms, concepts) >= 0.75)
            direct = topic_ok and (not query_terms or score >= 0.75)
            context = _compound_context_concepts(row["source_text"])
            prepared.append((score, direct, row, concepts, context))

        # A structured source observation is one provenance group. A matching
        # heading (for example "deployment plan:" or "packing checklist:")
        # lets its fragments compete as one source without treating an
        # arbitrary co-located sentence as relevant.
        relevant_groups = {
            row["source_observation_id"]
            for _, direct, row, _, context in prepared
            if direct and context and bool(context & expanded)
        }
        direct_count = sibling_count = 0
        for score, direct, row, concepts, context in prepared:
            sibling = (not direct and row["source_observation_id"] in relevant_groups
                       and bool(context & expanded))
            if not direct and not sibling:
                continue
            decision.candidates_considered += 1
            direct_count += int(direct)
            sibling_count += int(sibling)
            if row["status"] in {"superseded", "revoked", "deleted"}:
                decision.obsolete.append(row["memory_id"])
                record_trace(row, concepts | context, "obsolete", "status_gate",
                             f"record_status_{row['status']}")
                continue
            if not _is_current(row["valid_from"], row["valid_until"], as_of_time):
                decision.obsolete.append(row["memory_id"])
                record_trace(row, concepts | context, "obsolete", "record_time_gate",
                             "record_not_valid_at_query_time")
                continue
            ranked.append((score if direct else 0.74, row, concepts | context))

        ranked.sort(key=lambda item: (item[1]["status"] == "active", item[0], item[1]["seq"]),
                    reverse=True)
        allowed: list[tuple[float, Evidence]] = []
        for score, row, concepts in ranked:
            if (requester_id != resource_owner_id and permissions and
                    not (query_terms & permitted_query_terms)):
                ok, reason = False, "outside_permission_scope"
            else:
                ok, reason = self._authorized(
                    requester_id, requester_role, resource_owner_id, episode_id,
                    row, concepts, row["text_value"], permissions, query_terms)
            if not ok:
                # A denied row that matched only the already-permitted part of
                # the question is unrelated private context, not a partial
                # denial. It is neither released nor allowed to change the
                # action label.
                matched = expanded & concepts
                if permissions and matched and matched <= permitted_query_terms:
                    record_trace(row, concepts, "suppressed", "authorization_gate", reason)
                    continue
                target = (decision.denied_rbac if reason == "no_explicit_permission"
                          else decision.denied_scope)
                target.append(row["memory_id"])
                decision.denied_details.append({"memory_id": row["memory_id"],
                                                "reason": reason,
                                                "concepts": sorted(concepts)})
                self._log(checkpoint_id, requester_id, row["memory_id"],
                          "deny_permission" if reason == "no_explicit_permission" else "deny_scope",
                          reason)
                record_trace(row, concepts, "denied", "authorization_gate", reason)
                continue
            released_text = (
                row["text_value"]
                if requester_id == resource_owner_id or reason.startswith("policy:")
                else self._scoped_projection(row["text_value"], permissions)
            )
            if not released_text:
                record_trace(row, concepts, "suppressed", "projection_gate",
                             "empty_authorized_projection")
                continue
            allowed.append((score, Evidence(
                row["memory_id"], row["turn_id"], row["observed_role"], row["kind"],
                "scoped", released_text, source_observation_id=row["source_observation_id"],
                status=row["status"])))

        selected = self._coverage_select(allowed, query_terms, top_k)
        decision.allowed = [item[1] for item in selected]
        if decision.allowed and permissions and not (query_terms & excluded_query_terms):
            # Private rows that merely share the subject/topic with a wholly
            # in-scope request stay invisible and do not turn a complete
            # scoped answer into a misleading partial-denial label.
            decision.denied_scope.clear()
            decision.denied_details = [item for item in decision.denied_details
                                       if item["reason"] != "outside_permission_scope"]
        selected_ids = {item[1].record_id for item in selected}
        decision.outside_top_k = [e.record_id for _, e in allowed
                                  if e.record_id not in selected_ids]
        ranked_by_id = {item[1]["memory_id"]: item[1] for item in ranked}
        for _, evidence in allowed:
            row = ranked_by_id[evidence.record_id]
            concepts = (set(_loads(row["concepts_json"], [])) |
                        _structural_concepts(row["text_value"]))
            record_trace(
                row, concepts,
                "selected" if evidence.record_id in selected_ids else "outside_result_limit",
                "result_selection",
                "coverage_selected" if evidence.record_id in selected_ids else "source_group_limit",
            )
        selected_groups = {item[1].source_observation_id for item in selected}
        allowed_groups = {item[1].source_observation_id for item in allowed}
        supported = set().union(*(
            (_concepts(item[1].body) | _structural_concepts(item[1].body)) & query_terms
            for item in selected), set())
        decision.supported_query_facets = sorted(supported)
        decision.unsupported_query_facets = sorted(query_terms - supported)
        decision.source_group_counts = {
            "relevant_groups": len({row[1]["source_observation_id"] for row in ranked}),
            "authorized_groups": len(allowed_groups),
            "selected_groups": len(selected_groups),
            "direct_fragments": direct_count,
            "sibling_fragments_considered": sibling_count,
            "authorized_fragments": len(allowed),
            "selected_fragments": len(selected),
            "fragments_outside_limit": len(decision.outside_top_k),
        }
        for evidence in decision.allowed:
            self._log(checkpoint_id, requester_id, evidence.record_id, "allow", "explicit_scope")
        decision.decision_trace = sorted(trace.values(), key=lambda item: item["trace_id"])
        self.conn.commit()
        return decision

    def _authorized(self, requester_id: str, requester_role: str, owner_id: str,
                    episode_id: str, record: sqlite3.Row, concepts: set[str], text: str,
                    permissions: list[sqlite3.Row],
                    requested_concepts: set[str]) -> tuple[bool, str]:
        denies = [p for p in permissions if p["effect"] == "deny"]
        allows = [p for p in permissions if p["effect"] == "allow"]
        for row in denies:
            if self._scope_match(row, concepts, text, requested_concepts)[0]:
                return False, "explicit_restriction"
        policy_decision = None
        if self.policy:
            principal_row = self.conn.execute(
                "SELECT attributes_json FROM principals WHERE principal_id=?", (requester_id,)
            ).fetchone()
            relationships = tuple(dict(row) for row in self.conn.execute(
                "SELECT predicate,subject_id,object_id,status,attributes_json "
                "FROM relationships WHERE episode_id=?", (episode_id,)))
            resource_type = "general_record"
            for classifier in self.policy.get("resource_classification", []):
                expected = set(classifier.get("concepts") or [])
                if not expected or expected & concepts:
                    resource_type = str(classifier.get("type") or resource_type)
                    break
            principal = Principal(
                requester_id, frozenset({requester_role} if requester_role else set()),
                attributes=_loads(principal_row[0], {}) if principal_row else {})
            resource = Resource(
                str(record["memory_id"]), resource_type, owner_id,
                {"episode_id": episode_id}, str(record["status"]),
                {"concepts": sorted(concepts), "predicate": record["predicate"]})
            policy_decision = self.policy_evaluator.evaluate(AuthorizationRequest(
                principal, "retrieve", resource, {"episode_id": episode_id}, relationships))
            if policy_decision.reason in {"explicit_denial", "explicit_revocation",
                                          "policy_denial", "isolation_boundary",
                                          "inactive_resource"}:
                return False, policy_decision.reason
        # Ownership is a configured generic relationship, but every explicit
        # restriction/revocation above still wins.
        if requester_id and requester_id == owner_id:
            return True, "owner"
        for row in allows:
            if self._scope_match(row, concepts, text, requested_concepts)[0]:
                return True, "explicit_permission"
        if policy_decision and policy_decision.allowed:
            return True, f"policy:{policy_decision.rule_id or policy_decision.reason}"
        return False, "no_explicit_permission" if not allows else "outside_permission_scope"

    @staticmethod
    def _scope_matches(row: sqlite3.Row, concepts: set[str], text: str,
                       requested_concepts: set[str] | None = None) -> bool:
        return MemoryStore._scope_match(row, concepts, text, requested_concepts)[0]

    @staticmethod
    def _scope_match(row: sqlite3.Row, concepts: set[str], text: str,
                     requested_concepts: set[str] | None = None) -> tuple[bool, str]:
        scope_text = " ".join(_loads(row["scope_json"], []))
        scope = _scope_concepts(scope_text)
        exclusions = _concepts(" ".join(_loads(row["exclusions_json"], [])))
        if requested_concepts and exclusions & requested_concepts:
            return False, "permission_exclusion"
        lowered = text.lower()
        scoped_structure = (scope & concepts) & {
            "time_expression", "date_expression", "location_expression"}
        if (exclusions & concepts or
                any(term in lowered for term in exclusions if len(term) > 3)) and not scoped_structure:
            return False, "permission_exclusion"
        if scope & concepts:
            return True, "scope_match"
        return False, "outside_permission_scope"

    @staticmethod
    def _coverage_select(values: list[tuple[float, Evidence]], query_terms: set[str],
                         top_k: int) -> list[tuple[float, Evidence]]:
        groups: dict[str, list[tuple[float, Evidence]]] = {}
        for item in values:
            groups.setdefault(item[1].source_observation_id, []).append(item)
        if top_k <= 0 or len(groups) <= top_k:
            return values
        chosen_groups: list[list[tuple[float, Evidence]]] = []
        remaining = list(groups.values())
        uncovered = set(query_terms)
        while remaining and len(chosen_groups) < top_k:
            def group_key(group, remaining_facets=uncovered):
                concepts = set().union(*(
                    _concepts(item[1].body) | _structural_concepts(item[1].body)
                    for item in group))
                return len(concepts & remaining_facets), max(item[0] for item in group)
            best = max(remaining, key=group_key)
            chosen_groups.append(best)
            uncovered -= set().union(*(
                _concepts(item[1].body) | _structural_concepts(item[1].body)
                for item in best))
            remaining.remove(best)
        selected_sources = {group[0][1].source_observation_id for group in chosen_groups}
        return [item for item in values if item[1].source_observation_id in selected_sources]

    @staticmethod
    def _scoped_projection(text: str, permissions: list[sqlite3.Row]) -> str:
        """Release only the structurally scoped fragment of a mixed clause."""
        scope_text = " ".join(
            value for row in permissions if row["effect"] == "allow"
            for value in _loads(row["scope_json"], []))
        structural = _scope_concepts(scope_text)
        if structural & {"time_expression", "date_expression", "location_expression"}:
            match = re.search(
                r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                r"january|february|march|april|may|june|july|august|september|october|november|december)\b",
                text, re.I)
            if match:
                start = match.start()
                # Keep a leading qualifier such as "prior" or "new": dropping it
                # turns "prior Monday ... canceled" into a different statement.
                lead = re.search(r"\b(?:" + "|".join(_TEMPORAL_QUALIFIERS) + r")\s+$",
                                 text[:start], re.I)
                if lead:
                    start = lead.start()
                return text[start:].strip()
        return text

    # Forgetting ---------------------------------------------------------

    def tombstone(self, target_id: str, requested_by: str, reason: str = "") -> None:
        obs = self.conn.execute(
            "SELECT 1 FROM observations WHERE observation_id=?", (target_id,)).fetchone()
        target_type = "observation" if obs else "memory"
        self.conn.execute("INSERT OR REPLACE INTO tombstones VALUES (?,?,?,?,?,1)",
                          (target_id, target_type, _now(), requested_by, reason))
        if target_type == "observation":
            memory_ids = [r[0] for r in self.conn.execute(
                "SELECT memory_id FROM memories WHERE source_observation_id=?", (target_id,))]
            for memory_id in memory_ids:
                self.conn.execute("DELETE FROM memory_terms WHERE memory_id=?", (memory_id,))
            self.conn.execute(
                "UPDATE observations SET raw_text='',raw_payload_json='{}' WHERE observation_id=?",
                (target_id,))
            self.conn.execute(
                """UPDATE memories SET text_value='',value_json='{}',concepts_json='[]',status='deleted'
                   WHERE source_observation_id=?""", (target_id,))
            self.conn.execute("UPDATE permissions SET status='revoked' WHERE source_observation_id=?",
                              (target_id,))
            self.conn.execute("UPDATE semantic_links SET status='revoked' WHERE source_observation_id=?",
                              (target_id,))
        else:
            self.conn.execute("DELETE FROM memory_terms WHERE memory_id=?", (target_id,))
            self.conn.execute(
                """UPDATE memories SET text_value='',value_json='{}',concepts_json='[]',status='deleted'
                   WHERE memory_id=?""", (target_id,))
        self.conn.commit()

    def is_deleted(self, target_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM tombstones WHERE target_id=?", (target_id,)).fetchone() is not None

    def _honor_deletion_request(self, episode_id: str, text: str, requested_by: str) -> int:
        terms = _concepts(_DELETE_REQUEST.sub(" ", text))
        rows = self.conn.execute(
            "SELECT observation_id,raw_text FROM observations WHERE episode_id=? ORDER BY seq",
            (episode_id,),
        ).fetchall()
        hits = [r["observation_id"] for r in rows
                if r["raw_text"] and not _DELETE_REQUEST.search(r["raw_text"])
                and len(terms & _concepts(r["raw_text"])) >= 2]
        for observation_id in hits:
            self.tombstone(observation_id, requested_by, "explicit deletion request")
        return len(hits)

    # Derivation helpers -------------------------------------------------

    def _derive_clause_memories(self, episode_id: str, observation_id: str, text: str,
                                owner_id: str, observed_at: str) -> None:
        clauses = [c.strip(" -") for c in _CLAUSE_BREAK.split(text) if c.strip(" -")]
        source_context = _compound_context_concepts(text)
        for clause in clauses or ([text] if text else []):
            concepts = sorted(_concepts(clause) | _structural_concepts(clause) |
                              source_context)
            status = "cancelled" if _CANCEL.search(clause) else "active"
            utterance = clause.rstrip().endswith("?") or bool(_REQUEST_CUE.search(clause))
            supersedes = None
            # A question or request proposes a change; it does not make one.
            if not utterance and (status == "cancelled" or _UPDATE.search(clause)):
                supersedes = self._find_prior_fact(
                    episode_id, clause, observation_id, cancelling=status == "cancelled")
            subject = self._mentioned_principal(clause, skip_participants=True) or owner_id
            self.add_fact(episode_id, subject_id=subject,
                          predicate=_predicate_from(concepts), value={"text": clause},
                          text_value=clause, concepts=concepts, status=status,
                          valid_from=observed_at, source_observation_id=observation_id,
                          supersedes_id=supersedes, confidence=1.0,
                          memory_type="utterance" if utterance else "derived_fact")

    def _derive_explicit_permissions(self, episode_id: str, observation_id: str, text: str,
                                     owner_id: str, speaker_id: str, observed_at: str) -> None:
        if not _AUTH_CUE.search(text) and not _PROHIBITION.search(text):
            return
        grantee = self._resolve_grantee(episode_id, text, speaker_id)
        if not grantee or not owner_id or grantee == owner_id:
            return
        allow_text, exclusions = _permission_phrases(text)
        prohibited = _prohibited_phrase(text)
        if prohibited:
            self.grant_permission(
                episode_id, grantee_id=grantee, resource_owner_id=owner_id,
                scope=[prohibited], source_observation_id=observation_id,
                valid_from=observed_at, effect="deny")
            self.conn.execute(
                """UPDATE memories SET memory_type='permission_evidence'
                   WHERE source_observation_id=? AND memory_type='derived_fact'""",
                (observation_id,))
            return
        if not allow_text:
            return
        previous = self.conn.execute(
            """SELECT permission_id FROM permissions WHERE episode_id=? AND grantee_id=?
               AND resource_owner_id=? AND status='active' ORDER BY created_at DESC LIMIT 1""",
            (episode_id, grantee, owner_id),
        ).fetchone()
        permission_id = self.grant_permission(
            episode_id, grantee_id=grantee, resource_owner_id=owner_id,
            scope=[allow_text], exclusions=exclusions,
            source_observation_id=observation_id, valid_from=observed_at,
            valid_until=_parse_valid_until(text, observed_at),
            supersedes_id=str(previous[0]) if previous else None)
        self.conn.execute(
            """UPDATE memories SET memory_type='permission_evidence'
               WHERE source_observation_id=? AND memory_type='derived_fact'""",
            (observation_id,))
        if _REVOKE.search(text):
            self.revoke_permission(permission_id, source_observation_id=observation_id)

    def derive_permission_from_relationship_metadata(self, relationship_id: str,
                                                     *, source_observation_id: str) -> str | None:
        """Use explicit access metadata, never relationship identity, as evidence."""
        row = self.conn.execute(
            "SELECT * FROM relationships WHERE relationship_id=?", (relationship_id,)).fetchone()
        if not row:
            return None
        attrs = _loads(row["attributes_json"], {})
        evidence = " ".join(str(attrs[k]) for k in attrs if any(
            cue in k.lower() for cue in ("access", "permission", "consent", "authorization")))
        if not evidence:
            return None
        allow, exclusions = _permission_phrases(evidence)
        return self.grant_permission(
            row["episode_id"], grantee_id=row["subject_id"],
            resource_owner_id=row["object_id"], scope=[allow or evidence],
            exclusions=exclusions, source_observation_id=source_observation_id)

    def _run_interpreter(self, episode_id: str, observation_id: str,
                         turn: dict[str, Any], owner_id: str) -> None:
        if self.interpreter is None:
            return
        try:
            output = self.interpreter({"observation_id": observation_id,
                                       "raw_text": str(turn.get("text") or ""),
                                       "owner_id": owner_id})
            if (not isinstance(output, dict) or
                    set(output) - {"facts", "semantic_links", "permissions"} or
                    not all(isinstance(output.get(key, []), list)
                            for key in ("facts", "semantic_links", "permissions"))):
                return
            for fact in output.get("facts", []):
                if not isinstance(fact, dict) or not {"predicate", "value"} <= set(fact):
                    continue
                self.add_fact(
                    episode_id, subject_id=str(fact.get("subject_id") or owner_id),
                    predicate=str(fact["predicate"]), value=fact["value"],
                    text_value=str(fact.get("text_value") or ""),
                    concepts=[str(x) for x in fact.get("concepts", [])],
                    source_observation_id=observation_id,
                    confidence=float(fact.get("confidence", 0.5)),
                    memory_type="model_interpretation")
            for link in output.get("semantic_links", []):
                if isinstance(link, dict) and {"original_value", "related_value"} <= set(link):
                    self.add_semantic_link(
                        episode_id, str(link["original_value"]), str(link["related_value"]),
                        source_observation_id=observation_id,
                        confidence=float(link.get("confidence", 0.5)))
            # Model output cannot invent authority: the source observation must
            # itself carry an authorization cue.
            if _AUTH_CUE.search(str(turn.get("text") or "")):
                for perm in output.get("permissions", []):
                    if isinstance(perm, dict) and {"grantee_id", "scope"} <= set(perm):
                        self.grant_permission(
                            episode_id, grantee_id=str(perm["grantee_id"]),
                            resource_owner_id=str(perm.get("resource_owner_id") or owner_id),
                            scope=perm["scope"], exclusions=perm.get("exclusions", []),
                            source_observation_id=observation_id,
                            valid_until=perm.get("valid_until"),
                            confidence=float(perm.get("confidence", 0.5)))
        except (TypeError, ValueError, KeyError):
            return  # malformed extraction fails closed; raw observation remains

    def _find_prior_fact(self, episode_id: str, text: str, observation_id: str = "",
                         *, cancelling: bool = False) -> str | None:
        terms = _concepts(text) - {"cancel", "cancell", "update", "replac", "mov", "chang"}
        best: tuple[int, str, set[str]] | None = None
        for row in self.conn.execute(
            """SELECT memory_id,text_value FROM memories
               WHERE episode_id=? AND status='active' AND source_observation_id<>?
               ORDER BY seq DESC""", (episode_id, observation_id)):
            old_terms = _concepts(row["text_value"])
            overlap = len(terms & old_terms)
            if overlap >= 2 and (best is None or overlap > best[0]):
                best = (overlap, row["memory_id"], old_terms)
        if best is None:
            return None
        # A cancellation names when the cancelled thing was; it must share a
        # weekday or clock time with the record it retires.
        when = _clock_and_weekdays(text)
        if cancelling and when and not when & _clock_and_weekdays(
                self.conn.execute("SELECT text_value FROM memories WHERE memory_id=?",
                                  (best[1],)).fetchone()[0]):
            return None
        # An update that repeats nearly everything the earlier record said is a
        # confirmation of the same fact, not a replacement for it.
        if not cancelling and best[0] >= 0.8 * len(best[2]):
            return None
        return best[1]

    def _resolve_grantee(self, episode_id: str, text: str, speaker_id: str) -> str:
        lowered = text.lower()
        matches: list[tuple[int, str]] = []
        for row in self.conn.execute("SELECT principal_id,display_name FROM principals"):
            names = [part for part in re.findall(r"[a-z0-9]+", row["display_name"].lower())
                     if len(part) > 2]
            positions = [lowered.find(name) for name in names if name in lowered]
            if positions:
                matches.append((min(positions), str(row["principal_id"])))
        if matches:
            return min(matches)[1]
        if re.search(r"\b(you|she|he|they)\b", lowered):
            row = self.conn.execute(
                """SELECT observer_id FROM observations WHERE episode_id=? AND observer_id<>?
                   ORDER BY seq DESC LIMIT 1""", (episode_id, speaker_id)).fetchone()
            return str(row[0]) if row else ""
        return ""

    def _mentioned_principal(self, text: str, *, skip_participants: bool = False,
                             exclude: str = "") -> str:
        """Return the earliest explicitly named principal, without using roles.

        With skip_participants, a name right after a preposition ("with Dr. Lee",
        "booked by Sam") is a participant in the fact, not its subject.
        """
        normalized = " ".join(re.findall(r"[a-z0-9]+", text.lower()))
        matches: list[tuple[int, int, str]] = []
        for row in self.conn.execute("SELECT principal_id,display_name FROM principals"):
            if exclude and row["principal_id"] == exclude:
                continue
            display = re.sub(r"\([^)]*\)", "", str(row["display_name"])).strip()
            aliases = {" ".join(re.findall(r"[a-z0-9]+", display.lower())),
                       " ".join(re.findall(r"[a-z0-9]+", str(row["principal_id"]).lower()))}
            for alias in aliases - {""}:
                if len(alias) < 3:
                    continue
                found = re.search(rf"(?:^| ){re.escape(alias)}(?: |$)", normalized)
                if not found:
                    continue
                before = normalized[:found.start()].split()
                if skip_participants and before and before[-1] in _PARTICIPANT_CUES:
                    continue
                matches.append((found.start(), -len(alias), str(row["principal_id"])))
        return min(matches)[2] if matches else ""

    # Internal lookup helpers -------------------------------------------

    def _require_observation(self, observation_id: str) -> None:
        if not self.conn.execute(
                "SELECT 1 FROM observations WHERE observation_id=?",
                (observation_id,)).fetchone():
            raise ValueError("derived memory requires an existing source observation")

    def _permissions_at(self, episode_id: str, grantee_id: str, owner_id: str,
                        as_of_time: str | None) -> list[sqlite3.Row]:
        return [row for row in self.conn.execute(
            """SELECT * FROM permissions WHERE episode_id=? AND grantee_id=?
               AND resource_owner_id=? AND status='active' ORDER BY created_at DESC""",
            (episode_id, grantee_id, owner_id),
        ) if _is_current(row["valid_from"], row["valid_until"], as_of_time)]

    @staticmethod
    def _permission_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {"permission_id": row["permission_id"], "effect": row["effect"],
                "scope": _loads(row["scope_json"], []),
                "exclusions": _loads(row["exclusions_json"], []),
                "valid_from": row["valid_from"], "valid_until": row["valid_until"],
                "status": row["status"],
                "source_observation_id": row["source_observation_id"]}

    def _expand_terms(self, episode_id: str, terms: set[str]) -> set[str]:
        expanded = set(terms)
        for row in self.conn.execute(
            """SELECT original_value,related_value FROM semantic_links
               WHERE episode_id=? AND status='active'""", (episode_id,)):
            left, right = _concepts(row[0]), _concepts(row[1])
            if left & expanded:
                expanded |= right
            if right & expanded:
                expanded |= left
        return expanded

    def _time_at_seq(self, episode_id: str, seq: int | None) -> str | None:
        if seq is None:
            row = self.conn.execute(
                "SELECT observed_at FROM observations WHERE episode_id=? ORDER BY seq DESC LIMIT 1",
                (episode_id,)).fetchone()
        else:
            row = self.conn.execute(
                """SELECT observed_at FROM observations WHERE episode_id=? AND seq<=?
                   ORDER BY seq DESC LIMIT 1""", (episode_id, seq)).fetchone()
        return str(row[0]) if row else None

    def seq_of_turn(self, episode_id: str, turn_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT seq FROM observations WHERE episode_id=? AND turn_id=?",
            (episode_id, turn_id)).fetchone()
        return int(row[0]) if row else None

    def _index_key(self) -> bytes:
        if self._index_key_cache is None:
            row = self.conn.execute(
                "SELECT value FROM store_meta WHERE key='term_index_key'").fetchone()
            if row:
                key = bytes(row[0])
            else:
                key = os.urandom(32)
                self.conn.execute("INSERT INTO store_meta VALUES ('term_index_key',?)", (key,))
            self._index_key_cache = key
        return self._index_key_cache

    def _index_memory(self, memory_id: str, text: str) -> None:
        key = self._index_key()
        hashes = [hmac.new(key, term.encode(), hashlib.sha256).hexdigest()[:16]
                  for term in _concepts(text)]
        self.conn.executemany("INSERT OR IGNORE INTO memory_terms VALUES (?,?)",
                              [(memory_id, value) for value in hashes])

    def _log(self, checkpoint_id: str | None, requester_id: str, memory_id: str,
             decision: str, reason: str) -> None:
        self.conn.execute("INSERT INTO access_log VALUES (NULL,?,?,?,?,?,?)",
                          (checkpoint_id, requester_id, memory_id, decision, reason, _now()))


_STOPWORDS = frozenset("""a an and are as at be been but by can could did do does for from had has have
her him his i if in into is it its me my not of on or our out over she should so that the their them then
there these they this to was we were what when which who will with would you your just now please tell give
may receive only else through current currently latest detail details authorized""".split())
_DELETE_REQUEST = re.compile(r"\b(delete|remove|erase|forget|purge|scrub)\b", re.I)
_AUTH_CUE = re.compile(
    r"\b(may receive|may access|can receive|can have|can hear|can access|authorized|"
    r"consent|permission|access\s+(?:is|was|remains|:))\b",
    re.I,
)
_REQUEST_CUE = re.compile(
    r"\b(?:tell|give|show|send)\s+(?:me|us)\b|\b(?:can|could|would)\s+you\b|"
    r"^\s*(?:what|when|where|who|why|how|is|are|do|does|did)\b",
    re.I,
)
_PROHIBITION = re.compile(
    r"\b(?:do not|don't|must not|cannot|can't|never)\s+(?:tell|share|disclose|give|send)\b",
    re.I,
)
_REVOKE = re.compile(r"\b(revoke|revoked|no longer|stop sharing|withdraw)\b", re.I)
_CANCEL = re.compile(r"\b(cancelled|canceled|voided|withdrawn)\b", re.I)
# Split clauses at sentence punctuation, but not after a title abbreviation.
_CLAUSE_BREAK = re.compile(
    r"(?<=[.!?;])(?<!\bDr\.)(?<!\bMr\.)(?<!\bMs\.)(?<!\bMrs\.)(?<!\bProf\.)(?<!\bSt\.)\s+")
_PARTICIPANT_CUES = frozenset({"with", "by", "from", "to", "via", "see", "seeing"})
_TEMPORAL_QUALIFIERS = ("prior", "previous", "original", "earlier", "new", "updated", "rescheduled")
_UPDATE = re.compile(r"\b(updated|replaced|instead of|moved|changed|supersedes?)\b", re.I)
_MONTHS = {name.lower(): i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1)}


def _stem(token: str) -> str:
    result = token
    # Two passes handle a plural wrapped around another ordinary suffix (for
    # example "reservations" and "instructions") without a vocabulary.
    for _ in range(2):
        for suffix in ("ization", "ation", "ments", "ment", "ingly", "ing", "ed", "es", "s"):
            minimum = 3 if suffix in {"s", "es"} else 4
            if result.endswith(suffix) and len(result) - len(suffix) >= minimum:
                result = result[:-len(suffix)]
                break
        else:
            break
    return result


def _concepts(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9_\-']{1,}", (text or "").lower())
    normalized = [t[:-2] if t.endswith("'s") else t.strip("'") for t in tokens]
    return {_stem(t) for t in normalized if t not in _STOPWORDS}


def _structural_concepts(text: str) -> set[str]:
    result: set[str] = set()
    if re.search(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b", text, re.I):
        result.add("time_expression")
    if re.search(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", text, re.I):
        result.add("date_expression")
    if re.search(r"\b(?:suite|room|building|address|venue|site)\b", text, re.I):
        result.add("location_expression")
    if re.search(r"\b\d+(?:\.\d+)?\s*[a-z%/]+\b", text, re.I):
        result.add("measurement_expression")
    return result


def _clock_and_weekdays(text: str) -> set[str]:
    return {match.group(0).lower() for match in re.finditer(
        r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|\b\d{1,2}:\d{2}\b",
        text or "", re.I)}


def _temporal_features(text: str, reference: str = "") -> list[str]:
    """Expose date/time tokens without copying their surrounding record text."""
    values = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text or ""))
    values.update(match.group(0).lower() for match in re.finditer(
        r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text or "", re.I))
    values.update(match.group(0).lower() for match in re.finditer(
        r"\b\d{1,2}:\d{2}(?:\s*(?:am|pm))?\b", text or "", re.I))
    event_date = _date_from_text(text, reference)
    if event_date:
        values.add(event_date)
    return sorted(values)


def _compound_context_concepts(text: str) -> set[str]:
    """Terms in a short heading that scopes a multi-fragment observation.

    The heading is provenance-backed raw text, not an inferred domain label.
    Requiring both a colon and list-like continuation prevents arbitrary
    neighboring sentences from inheriting one another's relevance.
    """
    prefix, marker, tail = (text or "").partition(":")
    if not marker or len(prefix) > 160:
        return set()
    if ";" not in tail and "\n-" not in tail and "\n*" not in tail:
        return set()
    return _concepts(prefix) | _structural_concepts(prefix)


def _query_concepts(text: str) -> set[str]:
    result = _concepts(text) | _structural_concepts(text)
    lowered = text.lower()
    if re.search(r"\b(?:time|hour|arrival)\b", lowered):
        result.add("time_expression")
    if re.search(r"\b(?:date|day|future|today|tomorrow)\b", lowered):
        result.add("date_expression")
    if re.search(r"\b(?:location|place|where)\b", lowered):
        result.add("location_expression")
    result -= {"time", "hour", "arrival", "date", "day", "future", "today",
               "tomorrow", "location", "place", "where"}
    return result


def _scope_concepts(text: str) -> set[str]:
    result = _query_concepts(text)
    # These words name authorization categories. Evidence must exhibit the
    # corresponding structural feature rather than merely repeat the noun.
    return result


def _semantic_score(query: set[str], memory: set[str]) -> float:
    if not query:
        return 1.0
    overlap = query & memory
    if overlap:
        return 1.0
    best = 0.0
    for left in query:
        left_grams = {left[i:i+3] for i in range(max(0, len(left)-2))}
        for right in memory:
            right_grams = {right[i:i+3] for i in range(max(0, len(right)-2))}
            if left_grams and right_grams:
                best = max(best, 2 * len(left_grams & right_grams) /
                           (len(left_grams) + len(right_grams)))
    return best


def _predicate_from(concepts: list[str]) -> str:
    structural = [c for c in concepts if c.endswith("_expression")]
    return structural[0] if structural else (concepts[0] if concepts else "statement")


def _is_current(valid_from: str | None, valid_until: str | None, at: str | None) -> bool:
    if at is None:
        return True
    return (not valid_from or valid_from <= at) and (not valid_until or at <= valid_until)


def _permission_phrases(text: str) -> tuple[str, list[str]]:
    normalized = re.sub(r"[_]+", " ", text)
    match = re.search(
        r"(?:may receive|may access|can receive|can have|can hear|can access|authorized(?: to)?|access(?: is|:))\s+(.+)",
        normalized, re.I)
    if not match:
        parts = re.split(r"\b(?:but|and)\s+no\b|\bno\b", normalized, maxsplit=1,
                         flags=re.I)
        allow = parts[0].strip(" .,:;")
        exclusions = ([parts[1].strip(" .,:;")] if len(parts) > 1 and parts[1].strip(" .,:;")
                      else [])
        return allow, exclusions
    tail = match.group(1).strip()
    parts = re.split(r"\b(?:but|except|nothing else|no)\b", tail, maxsplit=1, flags=re.I)
    allow = re.sub(r"\bonly\b.*$", "", parts[0], flags=re.I).strip(" .,:;") or parts[0]
    exclusions = []
    if len(parts) > 1:
        exclusions = [p.strip(" .,:;") for p in re.split(r",|\bor\b|\band\b", parts[1])
                      if p.strip(" .,:;")]
    return allow, exclusions


def _prohibited_phrase(text: str) -> str:
    match = _PROHIBITION.search(text)
    return text[match.end():].strip(" .,:;") if match else ""


def _parse_valid_until(text: str, observed_at: str) -> str | None:
    dated = re.search(
        r"\bthrough\s+(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+)?([A-Za-z]+)\s+(\d{1,2})\b",
        text, re.I)
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        observed = datetime.now(timezone.utc)
    if dated and dated.group(1).lower() in _MONTHS:
        return (f"{observed.year:04d}-{_MONTHS[dated.group(1).lower()]:02d}-"
                f"{int(dated.group(2)):02d}T23:59:59+00:00")
    weekday = re.search(
        r"\bthrough\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text, re.I)
    if not weekday:
        return None
    weekday_number = (
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ).index(weekday.group(1).lower())
    end = observed + timedelta(days=(weekday_number - observed.weekday()) % 7)
    return end.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()


def _date_from_text(text: str, reference: str) -> str | None:
    iso = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text, re.I)
    if iso:
        return iso.group(0)
    match = re.search(r"\b([A-Za-z]+)\s+(\d{1,2})\b", text, re.I)
    if not match or match.group(1).lower() not in _MONTHS:
        return None
    try:
        year = datetime.fromisoformat(reference.replace("Z", "+00:00")).year
    except ValueError:
        return None
    return f"{year:04d}-{_MONTHS[match.group(1).lower()]:02d}-{int(match.group(2)):02d}"
