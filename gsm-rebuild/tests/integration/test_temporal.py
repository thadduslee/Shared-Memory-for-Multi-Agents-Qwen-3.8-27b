"""Immutable versions, supersession, as-of queries, historical authorization, expiry."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from gsm.db.database import Database
from gsm.domain.content import Content
from gsm.domain.operations import DeletionRequest, NewFact, PolicyTemplateRef, RetireFact, SupersedeFact, UpdateFact
from gsm.domain.types import QueryAction, SemanticType
from gsm.errors import NotFoundError
from gsm.service import QueryRequest
from tests.helpers import World, add, apollo_decision, note_fact

Q = "Apollo launch decision rollout"


def setup_history(world: World) -> tuple[str, str, str]:
    mid = add(world, "alice", apollo_decision("Launch Apollo on October 14 with blue-green rollout"))
    old_version_time = world.clock.now()
    world.tick(60)
    r = world.service.commit_operations(
        world.ctx("alice"),
        [UpdateFact(memory_id=mid, content=Content(text="Launch Apollo on October 21 with canary rollout"))],
    )
    assert r.status == "committed"
    world.tick(60)
    return mid, str(r.results[0].version_id), old_version_time.isoformat()


def test_update_creates_new_immutable_version(world: World, app_db: Database) -> None:
    mid, new_vid, _ = setup_history(world)
    with app_db.tenant_tx("acme") as tx:
        rows = tx.all(
            """SELECT version_id, state, truth_state, content_text, supersedes_version_id, valid_until
               FROM memory_versions WHERE tenant_id = :tenant_id AND memory_id = :m ORDER BY created_at""",
            m=mid,
        )
    assert [r["state"] for r in rows] == ["superseded", "active"]
    assert rows[0]["truth_state"] == "superseded" and rows[0]["valid_until"] is not None
    assert "October 14" in rows[0]["content_text"]  # history kept, not overwritten
    assert rows[1]["supersedes_version_id"] == rows[0]["version_id"] and rows[1]["version_id"] == new_vid


def test_version_content_cannot_be_rewritten_in_place(world: World, app_db: Database) -> None:
    mid, _, _ = setup_history(world)
    with pytest.raises(DBAPIError), app_db.tenant_tx("acme") as tx:
        tx.exec(
            "UPDATE memory_versions SET content_text = 'rewritten' WHERE tenant_id = :tenant_id AND memory_id = :m",
            m=mid,
        )


def test_current_query_returns_only_new_value(world: World) -> None:
    setup_history(world)
    r = world.service.query(world.ctx("bob"), QueryRequest(query=Q))
    assert r.action is QueryAction.ANSWER
    assert "October 21" in r.answer and "October 14" not in r.answer
    assert all(e.truth_state == "current" for e in r.evidence)


def test_authorized_historical_query_sees_superseded_value(world: World) -> None:
    mid, _, t_old = setup_history(world)
    from datetime import datetime

    as_of = datetime.fromisoformat(t_old) + timedelta(minutes=1)
    # project template: historical access same_as_current -> members may look back
    r = world.service.query(world.ctx("bob"), QueryRequest(query=Q, as_of=as_of))
    assert "October 14" in r.answer and "October 21" not in r.answer
    history = world.service.get_history(world.ctx("bob"), mid)
    assert [h.version_state for h in history] == ["superseded", "active"]


def test_historical_access_is_separately_authorized(world: World) -> None:
    mid = add(world, "alice", note_fact("Office wifi password is tulip-42", "Office wifi", template="tenant_public"))
    t_old = world.clock.now()
    world.tick(60)
    world.service.commit_operations(
        world.ctx("alice"), [UpdateFact(memory_id=mid, content=Content(text="Office wifi password is daisy-77"))]
    )
    world.tick(1)
    # tenant_public allows everyone now, but its history is same_as_current; build an owners_only one
    mid2 = add(
        world, "alice", note_fact("Standup moved to 10:00", "Standup time", template="project", project_id="apollo")
    )
    assert mid2
    r_now = world.service.query(world.ctx("carol"), QueryRequest(query="office wifi password"))
    assert "daisy-77" in r_now.answer
    r_then = world.service.query(
        world.ctx("carol"), QueryRequest(query="office wifi password", as_of=t_old + timedelta(minutes=1))
    )
    assert "tulip-42" in r_then.answer  # tenant_public: history same as current


def test_private_history_is_owners_only(world: World) -> None:
    op = note_fact("Budget draft 1.2M", "Apollo budget", template="project", project_id="apollo")
    fact = op.fact.model_copy(
        update={
            "policy_template": None,
            "policy": __import__("gsm.policy.templates", fromlist=["x"])
            .project("apollo")
            .model_copy(update={"historical_access": "owners_only"}),
        }
    )
    mid = add(world, "alice", op.model_copy(update={"fact": fact}))
    t_old = world.clock.now()
    world.tick(10)
    world.service.commit_operations(
        world.ctx("alice"), [UpdateFact(memory_id=mid, content=Content(text="Budget 1.5M"))]
    )
    world.tick(1)
    past = t_old + timedelta(minutes=1)
    bob_then = world.service.query(world.ctx("bob"), QueryRequest(query="Apollo budget", as_of=past))
    assert "1.2M" not in bob_then.answer and bob_then.action is QueryAction.NO_MEMORY
    alice_then = world.service.query(world.ctx("alice"), QueryRequest(query="Apollo budget", as_of=past))
    assert "1.2M" in alice_then.answer
    assert [h.version_state for h in world.service.get_history(world.ctx("bob"), mid)] == ["active"]


def test_tombstoned_versions_cannot_be_recovered_historically(world: World) -> None:
    mid, _, t_old = setup_history(world)
    from datetime import datetime

    world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    as_of = datetime.fromisoformat(t_old) + timedelta(minutes=1)
    for who in ("alice", "bob"):
        r = world.service.query(world.ctx(who), QueryRequest(query=Q, as_of=as_of))
        assert r.action is QueryAction.NO_MEMORY and "October" not in r.answer
        with pytest.raises(NotFoundError):
            world.service.get_history(world.ctx(who), mid)
        with pytest.raises(NotFoundError):
            world.service.get_memory(world.ctx(who), mid, as_of=as_of)


def test_semantic_identity_prevents_two_current_values(world: World) -> None:
    add(world, "alice", apollo_decision())
    r = world.service.commit_operations(world.ctx("alice"), [apollo_decision("A conflicting launch plan")])
    assert r.status == "rejected" and r.results[0].reasons == ("SEMANTIC_IDENTITY_CONFLICT",)
    # a different owner's claim is a separate identity and cannot reveal alice's record
    r2 = world.service.commit_operations(world.ctx("bob"), [apollo_decision("Bob thinks launch is Nov 1")])
    assert r2.status == "committed"


def test_expected_version_conflict(world: World) -> None:
    mid = add(world, "alice", apollo_decision())
    r = world.service.commit_operations(
        world.ctx("alice"),
        [UpdateFact(memory_id=mid, expected_version_id="ver_stale", content=Content(text="x"))],
    )
    assert r.results[0].reasons == ("VERSION_CONFLICT",)


def test_supersede_links_memories_and_keeps_old_auditable(world: World, app_db: Database) -> None:
    old = add(world, "alice", apollo_decision())
    world.tick(5)
    r = world.service.commit_operations(
        world.ctx("alice"),
        [
            SupersedeFact(
                memory_id=old,
                replacement=NewFact(
                    semantic_type=SemanticType.DECISION,
                    content=Content(text="Apollo launch replaced by Apollo-2 plan"),
                    topic="Apollo-2 launch decision",
                    attribute="launch_plan",
                    subject_ids=("project:apollo",),
                    policy_template=PolicyTemplateRef(name="project", project_id="apollo"),
                ),
            )
        ],
    )
    new = r.results[0].memory_id
    assert r.status == "committed" and new and new != old
    world.tick(1)
    now = world.service.query(world.ctx("bob"), QueryRequest(query="Apollo launch"))
    assert "Apollo-2" in now.answer and "blue-green" not in now.answer
    with app_db.tenant_tx("acme") as tx:
        edge = tx.one(
            "SELECT edge_type FROM memory_edges WHERE tenant_id = :tenant_id AND src_memory_id = :o AND "
            "dst_memory_id = :n",
            o=old,
            n=new,
        )
        assert edge is not None and edge["edge_type"] == "supersedes"
    assert world.service.get_history(world.ctx("alice"), old)[0].version_state == "superseded"


def test_contradiction_is_recorded_as_truth_state(world: World, app_db: Database) -> None:
    mid = add(world, "alice", apollo_decision())
    world.tick(1)
    world.service.commit_operations(
        world.ctx("alice"), [UpdateFact(memory_id=mid, content=Content(text="Launch is cancelled"), contradiction=True)]
    )
    with app_db.tenant_tx("acme") as tx:
        states = [
            r["truth_state"]
            for r in tx.all(
                "SELECT truth_state FROM memory_versions "
                "WHERE tenant_id = :tenant_id AND memory_id = :m ORDER BY created_at",
                m=mid,
            )
        ]
    assert states == ["contradicted", "current"]


def test_validity_interval_expiry(world: World) -> None:
    op = note_fact("Visitor badge code is 7788", "Visitor badge", template="project", project_id="apollo")
    fact = op.fact.model_copy(update={"valid_until": world.clock.now() + timedelta(hours=1)})
    add(world, "alice", op.model_copy(update={"fact": fact}))
    assert "7788" in world.service.query(world.ctx("bob"), QueryRequest(query="visitor badge code")).answer
    world.tick(61)
    r = world.service.query(world.ctx("bob"), QueryRequest(query="visitor badge code"))
    assert r.action is QueryAction.NO_MEMORY


def test_retention_expiry(world: World, app_db: Database) -> None:
    op = note_fact("Temporary door code 1234", "Door code", template="project", project_id="apollo")
    fact = op.fact.model_copy(update={"retain_until": world.clock.now() + timedelta(minutes=5)})
    add(world, "alice", op.model_copy(update={"fact": fact}))
    world.tick(6)
    assert world.service.query(world.ctx("bob"), QueryRequest(query="door code")).action is QueryAction.NO_MEMORY


def test_db_rejects_second_active_version(world: World, app_db: Database) -> None:
    mid = add(world, "alice", apollo_decision())
    with pytest.raises(DBAPIError), app_db.tenant_tx("acme") as tx:
        tx.conn.execute(
            text(
                """INSERT INTO memory_versions (tenant_id, workspace_id, version_id, memory_id, content_text,
                   valid_from, state, truth_state, extraction_confidence, origin, created_by, created_at)
               VALUES ('acme', 'default', 'ver_dup', :m, 'x', now(), 'active', 'current', 1, 'oracle', 'alice',
                   now())"""
            ),
            {"m": mid},
        )


def test_retire_ends_the_current_value_and_keeps_history(world: World) -> None:
    mid = add(world, "alice", apollo_decision("Launch Apollo on October 14 with blue-green rollout"))
    before = world.clock.now()
    world.tick(5)
    # someone outside the project cannot retire it (and learns nothing about it)
    r = world.service.commit_operations(world.ctx("henry"), [RetireFact(memory_id=mid)])
    assert r.status != "committed" and r.results[0].reasons == ("NOT_FOUND",)
    r = world.service.commit_operations(world.ctx("alice"), [RetireFact(memory_id=mid)])
    assert r.status == "committed" and r.results[0].memory_id == mid
    world.tick(1)
    now = world.service.query(world.ctx("bob"), QueryRequest(query=Q))
    assert now.action is QueryAction.NO_MEMORY and "blue-green" not in now.answer
    then = world.service.query(world.ctx("bob"), QueryRequest(query=Q, as_of=before))
    assert "blue-green" in then.answer  # the retired value stays historically visible
    assert world.service.get_history(world.ctx("alice"), mid)[0].version_state == "superseded"
    # a memory with no current value is outside the write path: retiring again is not found
    again = world.service.commit_operations(world.ctx("alice"), [RetireFact(memory_id=mid)])
    assert again.results[0].reasons == ("NOT_FOUND",)
