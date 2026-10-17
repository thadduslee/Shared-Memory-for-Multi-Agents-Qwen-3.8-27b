"""Active forgetting: synchronous tombstones, cascade, and every retrieval path."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from gsm.config import Settings
from gsm.db.database import Database
from gsm.domain.content import Content
from gsm.domain.operations import DeleteFact, DeleteSelector, DeletionRequest, DeriveRequest, StructuredEvent
from gsm.domain.types import QueryAction, SemanticType
from gsm.errors import ForbiddenError, NotFoundError
from gsm.service import GovernedMemoryService, QueryRequest
from tests.helpers import World, add, apollo_decision, note_fact

SECRET = "Launch Apollo on October 14 with the blue-green rollout"
Q = "Apollo launch decision blue-green rollout"


def scan_database(owner_engine: Engine, needles: list[str]) -> list[str]:
    """Every text/jsonb/array column of every table, searched for plaintext."""
    hits: list[str] = []
    with owner_engine.connect() as conn:
        cols = conn.execute(
            text(
                """SELECT table_name, column_name FROM information_schema.columns
               WHERE table_schema = 'public' AND data_type IN ('text', 'jsonb', 'ARRAY', 'tsvector')"""
            )
        ).all()
        for table, column in cols:
            for needle in needles:
                n = conn.execute(
                    text(f'SELECT count(*) FROM "{table}" WHERE CAST("{column}" AS text) ILIKE :n'),
                    {"n": f"%{needle}%"},
                ).scalar()
                if n:
                    hits.append(f"{table}.{column}:{needle}")
    return hits


def build(world: World) -> tuple[str, str]:
    receipt = world.service.submit_event(
        world.ctx("alice"),
        StructuredEvent(
            event_type="meeting.minutes", payload={"minutes": SECRET}, operations=(apollo_decision(SECRET),)
        ),
    )
    mid = receipt.results[0].memory_id
    assert mid
    world.tick()
    d = world.service.derive(
        world.ctx("alice"),
        DeriveRequest(
            parent_ids=(mid,),
            content=Content(text="Weekly summary: blue-green launch on October 14"),
            topic="Apollo weekly summary",
        ),
    )
    world.tick()
    return mid, d.memory_id


def test_every_retrieval_path_fails_after_tombstoning(world: World) -> None:
    mid, summary = build(world)
    s = world.service
    typo_query = "Apolo lanch bluegreen rolout Octobr"  # no lexical overlap; vector sim ~0.38
    before = s.query(world.ctx("bob"), QueryRequest(query=typo_query))
    assert before.action is QueryAction.ANSWER  # found by vector similarity alone
    t_before = world.clock.now()
    world.tick()
    receipt = s.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    assert receipt.cascade_status == "completed" and receipt.descendants_quarantined == 1
    for who in ("alice", "bob"):
        ctx = world.ctx(who)
        with pytest.raises(NotFoundError):
            s.get_memory(ctx, mid)  # direct lookup
        with pytest.raises(NotFoundError):
            s.get_memory(ctx, summary)  # derived summary
        with pytest.raises(NotFoundError):
            s.get_history(ctx, mid)
        for q in (Q, typo_query, "weekly summary blue-green"):  # lexical, vector, derived
            r = s.query(ctx, QueryRequest(query=q))
            assert r.action is QueryAction.NO_MEMORY and "October" not in r.answer
        r = s.query(ctx, QueryRequest(query=Q, as_of=t_before))  # historical
        assert r.action is QueryAction.NO_MEMORY


def test_deleted_plaintext_is_gone_from_the_whole_database(world: World, owner_engine: Engine) -> None:
    mid, _ = build(world)
    assert scan_database(owner_engine, ["blue-green"])  # sanity: present before
    world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    assert scan_database(owner_engine, ["blue-green", "October 14", "weekly summary"]) == []


def test_cascade_is_idempotent_and_retry_safe(world: World) -> None:
    mid, _ = build(world)
    world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    first = world.service.run_cascades("acme", include_completed=True)
    second = world.service.run_cascades("acme", include_completed=True)
    assert [c.status for c in first] == [c.status for c in second] == ["completed"]
    assert first[0].descendants_tombstoned == second[0].descendants_tombstoned == 1
    assert second[0].versions_scrubbed == first[0].versions_scrubbed
    assert second[0].attempts == first[0].attempts + 1
    again = world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    assert again.versions_tombstoned == 0 and again.tombstone_ids  # repeat request is a safe no-op


def test_deferred_cascade_still_hides_immediately(
    world: World, app_db: Database, settings: Settings, owner_engine: Engine
) -> None:
    deferred = GovernedMemoryService(
        app_db, settings.model_copy(update={"cascade_mode": "deferred"}), clock=world.clock
    )
    world.service = deferred
    world.auth.service = deferred
    mid, summary = build(world)
    receipt = deferred.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    assert receipt.cascade_status == "pending"
    for q in (Q, "weekly summary blue-green"):
        assert deferred.query(world.ctx("bob"), QueryRequest(query=q)).action is QueryAction.NO_MEMORY
    with pytest.raises(NotFoundError):
        deferred.get_memory(world.ctx("alice"), summary)
    assert scan_database(owner_engine, ["blue-green"])  # physical cleanup not yet done
    statuses = deferred.run_cascades("acme")
    assert statuses and statuses[0].status == "completed"
    assert scan_database(owner_engine, ["blue-green"]) == []


def test_cached_results_are_invalidated(world: World, app_db: Database, settings: Settings) -> None:
    mid = add(world, "alice", apollo_decision())
    other = GovernedMemoryService(app_db, settings, clock=world.clock)  # a second process with its own cache
    ctx = world.ctx("bob")
    assert world.service.query(ctx, QueryRequest(query=Q)).action is QueryAction.ANSWER
    assert other.query(ctx, QueryRequest(query=Q)).action is QueryAction.ANSWER
    assert world.service.query(ctx, QueryRequest(query=Q)).cache_hit
    world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    assert world.service.query(ctx, QueryRequest(query=Q)).action is QueryAction.NO_MEMORY
    stale = other.query(ctx, QueryRequest(query=Q))  # other's cache was never told
    assert stale.action is QueryAction.NO_MEMORY and not stale.cache_hit


def test_reindex_cannot_restore_deleted_content(world: World, app_db: Database) -> None:
    mid, _ = build(world)
    keep = add(
        world,
        "alice",
        note_fact("Apollo standup is at 10:00", "Apollo standup", template="project", project_id="apollo"),
    )
    world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    world.service.reindex("acme")
    after = world.service.query(world.ctx("bob"), QueryRequest(query=Q))
    assert "blue-green" not in after.answer and all(e.memory_id != mid for e in after.evidence)
    assert "10:00" in world.service.get_memory(world.ctx("bob"), keep).text
    with app_db.tenant_tx("acme") as tx:
        vid = tx.scalar("SELECT version_id FROM memory_versions WHERE tenant_id = :tenant_id AND memory_id = :m", m=mid)
    with pytest.raises(DBAPIError), app_db.tenant_tx("acme") as tx:
        tx.exec(
            """INSERT INTO memory_projections (tenant_id, memory_id, version_id, view, fields, text, created_at)
                   VALUES (:tenant_id, :m, :v, 'full', '{}', 'resurrected', now())""",
            m=mid,
            v=vid,
        )
    with pytest.raises(DBAPIError), app_db.tenant_tx("acme") as tx:
        tx.exec(
            "UPDATE memory_atoms SET lifecycle_state = 'active' WHERE tenant_id = :tenant_id AND memory_id = :m", m=mid
        )


def test_deletion_authority(world: World) -> None:
    mid = add(world, "alice", apollo_decision())
    with pytest.raises(ForbiddenError):
        world.service.forget(world.ctx("bob"), DeletionRequest(memory_ids=(mid,)))  # can see, cannot delete
    with pytest.raises(NotFoundError):
        world.service.forget(world.ctx("carol"), DeletionRequest(memory_ids=(mid,)))  # cannot even see
    world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    with pytest.raises(NotFoundError):
        world.service.forget(world.ctx("bob"), DeletionRequest(memory_ids=(mid,)))


def test_batch_delete_is_all_or_nothing(world: World) -> None:
    mine = add(world, "alice", apollo_decision())
    theirs = add(world, "henry", note_fact("Henry private note", "Henry note"))
    with pytest.raises(NotFoundError):
        world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mine, theirs)))
    assert world.service.get_memory(world.ctx("alice"), mine)


def test_ambiguous_selector_is_rejected(world: World) -> None:
    for i in range(2):
        add(world, "alice", note_fact(f"Dana onboarding note {i}", f"Dana onboarding {i}", subjects=("dana",)))
    op = DeleteFact(selector=DeleteSelector(subject_id="dana", semantic_type=SemanticType.FACT))
    r = world.service.commit_operations(world.ctx("alice"), [op])
    assert r.status == "rejected" and r.results[0].reasons == ("AMBIGUOUS_TARGET",)
    ok = world.service.commit_operations(world.ctx("alice"), [op.model_copy(update={"expected_count": 2})])
    assert ok.status == "committed"


def test_supersession_is_not_deletion(world: World) -> None:
    from gsm.domain.operations import UpdateFact

    mid = add(world, "alice", apollo_decision())
    t0 = world.clock.now()
    world.tick(10)
    world.service.commit_operations(world.ctx("alice"), [UpdateFact(memory_id=mid, content=Content(text="New plan"))])
    world.tick(1)
    r = world.service.query(world.ctx("alice"), QueryRequest(query=Q, as_of=t0 + timedelta(minutes=1)))
    assert "blue-green" in r.answer  # superseded: still historically auditable
    world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    r = world.service.query(world.ctx("alice"), QueryRequest(query=Q, as_of=t0 + timedelta(minutes=1)))
    assert r.action is QueryAction.NO_MEMORY  # deleted: gone from history too


def test_unreferenced_event_payloads_are_not_retained(world: World, owner_engine: Engine) -> None:
    from gsm.domain.operations import NaturalLanguageEvent

    world.service.ingest_natural_language(
        world.ctx("bob"), NaturalLanguageEvent(text="Please forget mem_" + "a" * 32 + " about the zebra budget")
    )
    world.service.submit_event(
        world.ctx("bob"),
        StructuredEvent(
            event_type="chat",
            payload={"text": "zebra budget is 9M"},
            operations=(DeleteFact(memory_ids=("mem_" + "b" * 32,)),),
        ),
    )
    assert scan_database(owner_engine, ["zebra"]) == []


def test_db_refuses_new_versions_on_tombstoned_memory(world: World, app_db: Database) -> None:
    mid = add(world, "alice", apollo_decision())
    world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    with pytest.raises(DBAPIError), app_db.tenant_tx("acme") as tx:
        tx.exec(
            """INSERT INTO memory_versions (tenant_id, workspace_id, version_id, memory_id, content_text, valid_from,
                   state, truth_state, extraction_confidence, origin, created_by, created_at)
               VALUES (:tenant_id, 'default', 'ver_zombie', :m, 'revived', now(), 'active', 'current', 1,
                   'oracle', 'alice', now())""",
            m=mid,
        )
