"""Deletion remains stronger than extraction, update, and reopening."""
from memory_system.store import MemoryStore


def _turn(turn_id, text):
    return {"turn_id": turn_id, "text": text, "turn_kind": "message",
            "speaker": {"principal_id": "owner", "role": "owner"}}


def test_tombstone_purges_raw_and_all_derived_layers_after_reopen(tmp_path):
    path = tmp_path / "store.sqlite"
    store = MemoryStore(path)
    store.upsert_principal("owner", "owner")
    obs = store.ingest_turn("ep", _turn("t1", "Backup phrase is orange lantern."), "owner")
    store.add_semantic_link("ep", "orange lantern", "backup phrase",
                            source_observation_id=obs)
    store.tombstone(obs, requested_by="owner")
    assert store.is_deleted(obs)
    assert store.conn.execute("SELECT raw_text FROM observations WHERE observation_id=?", (obs,)).fetchone()[0] == ""
    assert store.conn.execute("SELECT COUNT(*) FROM semantic_links WHERE status='active'").fetchone()[0] == 0
    store.close()

    reopened = MemoryStore(path)
    decision = reopened.retrieve(requester_id="owner", owner_id="owner", episode_id="ep",
                                 query="orange lantern")
    assert not decision.allowed
    assert "orange lantern" not in str(decision)


def test_delete_request_spares_unrelated_observation():
    store = MemoryStore(":memory:")
    first = store.ingest_turn("ep", _turn("t1", "Backup phrase is orange lantern."), "owner")
    second = store.ingest_turn("ep", _turn("t2", "Deadline is next Thursday."), "owner")
    store.ingest_turn("ep", _turn("t3", "Delete the backup phrase orange lantern from memory."), "owner")
    assert store.is_deleted(first)
    assert not store.is_deleted(second)
    result = store.retrieve(requester_id="owner", owner_id="owner", episode_id="ep",
                            query="deadline Thursday")
    assert result.allowed


def test_repeated_tombstone_is_idempotent():
    store = MemoryStore(":memory:")
    obs = store.ingest_turn("ep", _turn("t1", "Backup phrase is orange lantern."), "owner")
    store.tombstone(obs, requested_by="owner")
    store.tombstone(obs, requested_by="owner")
    assert store.is_deleted(obs)
