"""GateMem interface over the generic evidence-governed memory store."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Callable

from .store import Decision, MemoryStore

HIDDEN_FIELDS = frozenset({
    "query_type", "attack_type", "expected_action", "judge_spec", "leak_targets",
    "gold_answer_structured", "gold_refusal_category", "policy_snapshot",
})
REFUSAL_TEXT = "I'm not able to share that information with you."
NO_MEMORY_TEXT = "I don't have any information on that."


def _visible(checkpoint: Any) -> dict[str, Any]:
    if not isinstance(checkpoint, dict):
        checkpoint = {
            key: getattr(checkpoint, key)
            for key in ("checkpoint_id", "episode_id", "as_of_turn_id", "query_text")
            if hasattr(checkpoint, key)
        } | {"asker": {
            "principal_id": getattr(checkpoint, "asker_principal_id", ""),
            "role": getattr(checkpoint, "asker_role", ""),
        }}
    return {key: value for key, value in checkpoint.items() if key not in HIDDEN_FIELDS}


def sanitize_and_decide(decision: Decision, requester_role: str) -> tuple[str, str]:
    if decision.touched_deleted:
        return "no_memory", f"{len(decision.denied_tombstone)} responsive memory item(s) tombstoned"
    withheld = len(decision.denied_rbac) + len(decision.denied_scope)
    if decision.allowed and not withheld:
        return "answer", f"{len(decision.allowed)} evidence item(s) authorized"
    if decision.allowed and withheld:
        return "answer_redacted", f"{withheld} responsive item(s) outside explicit scope"
    if withheld:
        return "refuse", f"{withheld} responsive item(s) lack permission"
    return "no_memory", "no responsive current memory"


class GateMemAgent:
    def __init__(self, db_path: str = ":memory:",
                 llm: Callable[[str, list[dict[str, Any]]], dict[str, Any]] | None = None,
                 top_k: int = 12,
                 interpreter: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self.base_db_path = str(db_path)
        self.store = MemoryStore(db_path, interpreter=interpreter)
        self.llm = llm
        self.top_k = top_k
        self.episode: dict[str, Any] | None = None
        self.owner_id = ""

    def open_episode(self, episode: dict[str, Any]) -> None:
        self.episode = episode
        attached = self.store.conn.execute(
            "SELECT value FROM store_meta WHERE key='online_episode'").fetchone()
        if attached and bytes(attached[0]).decode() != episode["episode_id"]:
            raise ValueError("episode isolation violation")
        if not attached:
            self._install_header(episode)
            self.store.conn.execute("INSERT INTO store_meta VALUES ('online_episode',?)",
                                    (episode["episode_id"].encode(),))
            self.store.conn.commit()
        else:
            self.owner_id = self._infer_owner(episode)
        self.store._seq = int(self.store.conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM observations").fetchone()[0])

    def reset(self, episode: dict[str, Any]) -> None:
        self.store.close()
        path = self._episode_db_path(episode.get("episode_id", "unknown"))
        self.store = MemoryStore(path, interpreter=self.store.interpreter)
        self.episode = episode
        self._install_header(episode)
        self.store.conn.commit()

    def _install_header(self, episode: dict[str, Any]) -> None:
        entities = episode.get("entities") or {}
        principals = entities.get("principals", [])
        self.owner_id = self._infer_owner(episode)
        metadata_turn = {
            "turn_id": "__episode_metadata__", "turn_kind": "episode_metadata",
            "speaker": {"principal_id": "__source__", "role": "source"},
            "text": "", "metadata": entities,
        }
        source_id = self.store.ingest_turn(episode["episode_id"], metadata_turn, self.owner_id)
        for principal in principals:
            known = {"principal_id", "role", "display_name"}
            self.store.upsert_principal(
                str(principal["principal_id"]), str(principal.get("role") or ""),
                str(principal.get("display_name") or ""),
                attributes={k: v for k, v in principal.items() if k not in known},
                source_observation_id=source_id)
        frequencies = self._id_frequencies(entities.get("relationships", []))
        for relationship in entities.get("relationships", []):
            subject, obj = self._relationship_endpoints(relationship, frequencies)
            known = {"type", "subject_id", "object_id"}
            known |= {key for key in relationship if key.endswith("_id")}
            attrs = {k: v for k, v in relationship.items() if k not in known}
            rel_id = self.store.upsert_relationship(
                str(relationship.get("type") or relationship.get("predicate") or "related"),
                subject, obj, episode_id=episode["episode_id"], attributes=attrs,
                source_observation_id=source_id)
            self.store.derive_permission_from_relationship_metadata(
                rel_id, source_observation_id=source_id)

    @staticmethod
    def _id_frequencies(relationships: list[dict[str, Any]]) -> Counter[str]:
        return Counter(str(value) for rel in relationships for key, value in rel.items()
                       if key.endswith("_id") and value)

    @classmethod
    def _relationship_endpoints(cls, relationship: dict[str, Any],
                                frequencies: Counter[str]) -> tuple[str, str]:
        if relationship.get("subject_id") and relationship.get("object_id"):
            return str(relationship["subject_id"]), str(relationship["object_id"])
        values = [(key, str(value)) for key, value in relationship.items()
                  if key.endswith("_id") and value]
        if not values:
            return "", ""
        object_key, object_id = max(values, key=lambda item: (frequencies[item[1]], item[0] in {"object_id", "owner_id"}))
        subject_id = next((value for key, value in values if key != object_key), object_id)
        return subject_id, object_id

    @classmethod
    def _infer_owner(cls, episode: dict[str, Any]) -> str:
        entities = episode.get("entities") or {}
        frequencies = cls._id_frequencies(entities.get("relationships", []))
        principal_ids = [str(row.get("principal_id") or "")
                         for row in entities.get("principals", [])]
        if frequencies:
            return max(principal_ids, key=lambda item: frequencies[item], default="")
        return principal_ids[0] if principal_ids else ""

    def _episode_db_path(self, episode_id: str) -> str:
        if self.base_db_path == ":memory:":
            return ":memory:"
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in episode_id)
        path = Path(self.base_db_path).with_suffix("") / f"{safe}.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        return str(path)

    def ingest(self, turn: Any) -> None:
        if not isinstance(turn, dict):
            turn = {
                "turn_id": getattr(turn, "turn_id", ""),
                "timestamp": getattr(turn, "timestamp", None),
                "turn_kind": getattr(turn, "turn_kind", "dialogue"),
                "text": getattr(turn, "text", ""),
                "speaker": {
                    "principal_id": getattr(turn, "speaker_principal_id", ""),
                    "role": getattr(turn, "speaker_role", ""),
                },
            }
        self.store.ingest_turn((self.episode or {}).get("episode_id", "unknown"),
                               turn, self.owner_id)

    def query(self, checkpoint: Any) -> dict[str, Any]:
        cp = _visible(checkpoint)
        asker = cp.get("asker") or {}
        requester_id = str(asker.get("principal_id") or "")
        requester_role = str(asker.get("role") or "")
        episode_id = str(cp.get("episode_id") or (self.episode or {}).get("episode_id") or "")
        as_of_seq = self.store.seq_of_turn(episode_id, str(cp.get("as_of_turn_id") or ""))
        decision = self.store.retrieve(
            requester_id=requester_id, requester_role=requester_role,
            owner_id=self.owner_id, episode_id=episode_id,
            query=str(cp.get("query_text") or ""), as_of_seq=as_of_seq,
            checkpoint_id=cp.get("checkpoint_id"), top_k=self.top_k)
        action, rationale = sanitize_and_decide(decision, requester_role)
        evidence = [{"record_id": item.record_id, "role": item.author_role,
                     "text": item.body, "source_observation_id": item.source_observation_id,
                     "status": item.status} for item in decision.allowed]
        if action in {"answer", "answer_redacted"} and self.llm is not None:
            rendered = self.llm(str(cp.get("query_text") or ""), evidence)
            answer = str(rendered.get("answer") or "")
            # Rendering cannot widen the authorization decision.
            if str(rendered.get("action") or action) in {"answer", "answer_redacted"}:
                action = str(rendered.get("action") or action)
            if decision.obsolete_evidence:
                answer += " Previously: " + " ".join(
                    item.body for item in decision.obsolete_evidence)
        elif action in {"answer", "answer_redacted"}:
            answer = " ".join(item.body for item in decision.allowed)
            if decision.obsolete_evidence:
                previously = " ".join(item.body for item in decision.obsolete_evidence)
                answer += " Previously: " + previously
        elif action == "refuse":
            answer = REFUSAL_TEXT
        elif not decision.allowed and decision.obsolete_evidence:
            action = "answer"
            answer = "No current information. Previously: " + " ".join(
                item.body for item in decision.obsolete_evidence)
        else:
            answer = NO_MEMORY_TEXT
        denied_reasons = Counter(item["reason"] for item in decision.denied_details)
        denied_concepts = Counter(concept for item in decision.denied_details
                                  for concept in item.get("concepts", []))
        return {
            "action": action, "answer": answer, "answer_structured": {},
            "used_record_ids": [item.record_id for item in decision.allowed],
            "evidence": evidence,
            "debug": {
                "agent": "adaptive_evidence_memory", "rationale": rationale,
                "requester_id": requester_id, "observed_role": requester_role,
                "n_candidates": decision.candidates_considered,
                "n_allowed": len(decision.allowed),
                "n_denied_rbac": len(decision.denied_rbac),
                "n_denied_scope": len(decision.denied_scope),
                "n_denied_tombstone": len(decision.denied_tombstone),
                "n_obsolete": len(decision.obsolete),
                "n_outside_top_k": len(decision.outside_top_k),
                "denied_counts_by_reason": dict(denied_reasons),
                "denied_counts_by_concept": dict(denied_concepts),
                "denied_counts_by_sensitivity": {},
                "denied_counts_by_required_relationship": {},
                "relationship_labels": decision.relationship_labels,
                "permission_facts": decision.permission_facts,
                "relevant_state": {
                    "missing": decision.candidates_considered == 0,
                    "obsolete": len(decision.obsolete),
                    "denied": len(decision.denied_rbac) + len(decision.denied_scope),
                    "outside_top_k": len(decision.outside_top_k),
                },
            },
        }
