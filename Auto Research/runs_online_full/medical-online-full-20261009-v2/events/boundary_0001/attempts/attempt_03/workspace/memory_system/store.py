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

    @property
    def touched_deleted(self) -> bool:
        return bool(self.denied_tombstone)

    @property
    def touched_unauthorized(self) -> bool:
        return bool(self.denied_rbac or self.denied_scope)


class MemoryStore:
    """A generic store. Empty initialization creates schema, not policy rows."""

    def __init__(self, db_path: str | Path = ":memory:", *,
                 interpreter: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.interpreter = interpreter
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
        resource_owner_id = self._mentioned_principal(query) or owner_id
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
        permissions = self._permissions_at(episode_id, requester_id,
                                           resource_owner_id, as_of_time)
        excluded_query_terms: set[str] = set()
        permitted_query_terms: set[str] = set()
        for permission in permissions:
            if permission["effect"] == "allow":
                scope_text = " ".join(_loads(permission["scope_json"], []))
                permitted_query_terms |= _scope_concepts(scope_text)
                excluded_query_terms |= _concepts(
                    " ".join(_loads(permission["exclusions_json"], [])))
        decision.permission_facts = [self._permission_summary(row) for row in permissions]
        query_topic_terms = query_terms - permitted_query_terms
        rows = self.conn.execute(
            """SELECT m.*,o.turn_id,o.observed_role,o.kind,
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
                continue
            if query_topic_terms and _semantic_score(query_topic_terms, concepts) < 0.75:
                continue
            score = _semantic_score(expanded, concepts)
            if score < 0.75 and query_terms:
                continue
            decision.candidates_considered += 1
            if row["status"] in {"revoked", "deleted"}:
                decision.obsolete.append(row["memory_id"])
                continue
            if not _is_current(row["valid_from"], row["valid_until"], as_of_time):
                decision.obsolete.append(row["memory_id"])
                continue
            ranked.append((score, row, concepts))

        ranked.sort(key=lambda item: (item[1]["status"] == "active", item[0], item[1]["seq"]),
                    reverse=True)
        allowed: list[tuple[float, Evidence]] = []
        for score, row, concepts in ranked:
            if (requester_id != resource_owner_id and permissions and
                    not (query_terms & permitted_query_terms)):
                ok, reason = False, "outside_permission_scope"
            else:
                ok, reason = self._authorized(
                    requester_id, resource_owner_id, concepts,
                    row["text_value"], permissions, query_terms)
            if not ok:
                # A denied row that matched only the already-permitted part of
                # the question is unrelated private context, not a partial
                # denial. It is neither released nor allowed to change the
                # action label.
                matched = expanded & concepts
                if permissions and matched and matched <= permitted_query_terms:
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
                continue
            released_text = (row["text_value"] if requester_id == resource_owner_id else
                             self._scoped_projection(row["text_value"], permissions))
            if not released_text:
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
        for evidence in decision.allowed:
            self._log(checkpoint_id, requester_id, evidence.record_id, "allow", "explicit_scope")
        self.conn.commit()
        return decision

    def _authorized(self, requester_id: str, owner_id: str, concepts: set[str], text: str,
                    permissions: list[sqlite3.Row],
                    requested_concepts: set[str]) -> tuple[bool, str]:
        # Generic ownership is independent of role labels and creates no seed rows.
        if requester_id and requester_id == owner_id:
            return True, "owner"
        denies = [p for p in permissions if p["effect"] == "deny"]
        allows = [p for p in permissions if p["effect"] == "allow"]
        for row in denies:
            if self._scope_matches(row, concepts, text, requested_concepts):
                return False, "explicit_restriction"
        for row in allows:
            if self._scope_matches(row, concepts, text, requested_concepts):
                return True, "explicit_permission"
        return False, "no_explicit_permission" if not allows else "outside_permission_scope"

    @staticmethod
    def _scope_matches(row: sqlite3.Row, concepts: set[str], text: str,
                       requested_concepts: set[str] | None = None) -> bool:
        scope_text = " ".join(_loads(row["scope_json"], []))
        scope = _scope_concepts(scope_text)
        exclusions = _concepts(" ".join(_loads(row["exclusions_json"], [])))
        if requested_concepts and exclusions & requested_concepts:
            return False
        lowered = text.lower()
        scoped_structure = (scope & concepts) & {
            "time_expression", "date_expression", "location_expression"}
        if (exclusions & concepts or
                any(term in lowered for term in exclusions if len(term) > 3)) and not scoped_structure:
            return False
        event_date = _date_from_text(text, row["valid_from"] or "")
        if event_date and row["valid_until"] and event_date > row["valid_until"][:10]:
            return False
        return bool(scope & concepts)

    @staticmethod
    def _coverage_select(values: list[tuple[float, Evidence]], query_terms: set[str],
                         top_k: int) -> list[tuple[float, Evidence]]:
        if top_k <= 0 or len(values) <= top_k:
            return values
        chosen: list[tuple[float, Evidence]] = []
        remaining = list(values)
        uncovered = set(query_terms)
        while remaining and len(chosen) < top_k:
            best = max(remaining, key=lambda item: (
                len((_concepts(item[1].body) | _structural_concepts(item[1].body)) & uncovered),
                item[0]))
            chosen.append(best)
            uncovered -= _concepts(best[1].body) | _structural_concepts(best[1].body)
            remaining.remove(best)
        return chosen

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
                return text[match.start():].strip()
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
        clauses = [c.strip(" -") for c in re.split(r"(?<=[.!?;])\s+", text)
                   if c.strip(" -")]
        for clause in clauses or ([text] if text else []):
            concepts = sorted(_concepts(clause) | _structural_concepts(clause))
            status = "cancelled" if _CANCEL.search(clause) else "active"
            supersedes = None
            if status == "cancelled" or _UPDATE.search(clause):
                supersedes = self._find_prior_fact(episode_id, clause)
            self.add_fact(episode_id, subject_id=self._mentioned_principal(clause) or owner_id,
                          predicate=_predicate_from(concepts), value={"text": clause},
                          text_value=clause, concepts=concepts, status=status,
                          valid_from=observed_at, source_observation_id=observation_id,
                          supersedes_id=supersedes, confidence=1.0,
                          memory_type=("utterance" if clause.rstrip().endswith("?") or
                                       _REQUEST_CUE.search(clause) else "derived_fact"))

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

    def _find_prior_fact(self, episode_id: str, text: str) -> str | None:
        terms = _concepts(text) - {"cancel", "cancell", "update", "replac", "mov", "chang"}
        best: tuple[int, str] | None = None
        for row in self.conn.execute(
            """SELECT memory_id,text_value FROM memories
               WHERE episode_id=? AND status='active' ORDER BY seq DESC""", (episode_id,)):
            overlap = len(terms & _concepts(row["text_value"]))
            if overlap >= 2 and (best is None or overlap > best[0]):
                best = (overlap, row["memory_id"])
        return best[1] if best else None

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

    def _mentioned_principal(self, text: str) -> str:
        """Return the earliest explicitly named principal, without using roles."""
        normalized = " ".join(re.findall(r"[a-z0-9]+", text.lower()))
        matches: list[tuple[int, int, str]] = []
        for row in self.conn.execute("SELECT principal_id,display_name FROM principals"):
            display = re.sub(r"\([^)]*\)", "", str(row["display_name"])).strip()
            aliases = {" ".join(re.findall(r"[a-z0-9]+", display.lower())),
                       " ".join(re.findall(r"[a-z0-9]+", str(row["principal_id"]).lower()))}
            for alias in aliases - {""}:
                if len(alias) < 3:
                    continue
                found = re.search(rf"(?:^| ){re.escape(alias)}(?: |$)", normalized)
                if found:
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
_UPDATE = re.compile(r"\b(updated|replaced|instead of|moved|changed|supersedes?)\b", re.I)
_MONTHS = {name.lower(): i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1)}


def _stem(token: str) -> str:
    for suffix in ("ization", "ation", "ments", "ment", "ingly", "ing", "ed", "es", "s"):
        minimum = 3 if suffix in {"s", "es"} else 4
        if token.endswith(suffix) and len(token) - len(suffix) >= minimum:
            return token[:-len(suffix)]
    return token


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
    match = re.search(r"\b([A-Za-z]+)\s+(\d{1,2})\b", text, re.I)
    if not match or match.group(1).lower() not in _MONTHS:
        return None
    try:
        year = datetime.fromisoformat(reference.replace("Z", "+00:00")).year
    except ValueError:
        return None
    return f"{year:04d}-{_MONTHS[match.group(1).lower()]:02d}-{int(match.group(2)):02d}"
