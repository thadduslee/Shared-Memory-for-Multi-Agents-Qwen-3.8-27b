"""Batch atomicity, concurrent writers, and deletion/revocation racing retrieval."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Any

from gsm.db.database import Database
from gsm.domain.content import Content
from gsm.domain.operations import DeletionRequest, GrantAccess, RevokeAccess, UpdateFact
from gsm.domain.types import QueryAction, View
from gsm.providers.base import GeneratorEvidence
from gsm.service import QueryRequest
from tests.helpers import World, add, apollo_decision, note_fact

Q = "Apollo launch decision blue-green rollout"


def count(app_db: Database, sql: str, **params: Any) -> int:
    with app_db.tenant_tx("acme") as tx:
        return int(tx.scalar(sql, **params) or 0)


def test_partial_batch_rolls_back_everything(world: World, app_db: Database) -> None:
    ops = [
        note_fact("Batch fact one", "batch one"),
        apollo_decision(),
        UpdateFact(memory_id="mem_" + "f" * 32, content=Content(text="x")),  # fails: not found
    ]
    r = world.service.commit_operations(world.ctx("alice"), ops)
    assert r.status == "rolled_back"
    assert [x.status for x in r.results] == ["rolled_back", "rolled_back", "rejected"]
    assert r.results[2].reasons == ("NOT_FOUND",)
    for table in ("memory_atoms", "memory_versions", "memory_projections", "embeddings"):
        assert count(app_db, f"SELECT count(*) FROM {table} WHERE tenant_id = :tenant_id") == 0
    assert (
        count(app_db, "SELECT count(*) FROM commit_receipts WHERE tenant_id = :tenant_id AND status = 'rolled_back'")
        == 1
    )


def run_concurrently(fns: Sequence[Callable[[], Any]]) -> list[Any]:
    barrier = threading.Barrier(len(fns))
    results: list[Any] = [None] * len(fns)

    def wrap(i: int, fn: Callable[[], Any]) -> None:
        barrier.wait()
        try:
            results[i] = fn()
        except Exception as exc:  # surfaced to the assertion below
            results[i] = exc

    threads = [threading.Thread(target=wrap, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def test_concurrent_updates_with_same_base_version_cannot_both_win(world: World, app_db: Database) -> None:
    mid = add(world, "alice", apollo_decision())
    with app_db.tenant_tx("acme") as tx:
        base = tx.scalar(
            "SELECT version_id FROM memory_versions WHERE tenant_id = :tenant_id AND memory_id = :m", m=mid
        )
    world.tick()
    ctxs = [world.ctx("alice") for _ in range(4)]
    results = run_concurrently(
        [
            (
                lambda c=c, i=i: world.service.commit_operations(
                    c, [UpdateFact(memory_id=mid, expected_version_id=base, content=Content(text=f"variant {i}"))]
                )
            )
            for i, c in enumerate(ctxs)
        ]
    )
    statuses = sorted(r.status for r in results)
    assert statuses.count("committed") == 1 and statuses.count("rejected") == 3
    assert all(r.results[0].reasons == ("VERSION_CONFLICT",) for r in results if r.status == "rejected")
    assert (
        count(
            app_db,
            "SELECT count(*) FROM memory_versions WHERE tenant_id = :tenant_id AND memory_id = :m AND state = 'active'",
            m=mid,
        )
        == 1
    )


def test_concurrent_blind_updates_serialize_into_one_chain(world: World, app_db: Database) -> None:
    mid = add(world, "alice", apollo_decision())
    world.tick()
    ctxs = [world.ctx("alice") for _ in range(5)]
    results = run_concurrently(
        [
            (
                lambda c=c, i=i: world.service.commit_operations(
                    c, [UpdateFact(memory_id=mid, content=Content(text=f"v{i}"))]
                )
            )
            for i, c in enumerate(ctxs)
        ]
    )
    assert all(r.status == "committed" for r in results)
    with app_db.tenant_tx("acme") as tx:
        rows = tx.all(
            "SELECT version_id, supersedes_version_id, state FROM memory_versions "
            "WHERE tenant_id = :tenant_id AND memory_id = :m",
            m=mid,
        )
    assert sum(1 for r in rows if r["state"] == "active") == 1
    parents = [r["supersedes_version_id"] for r in rows if r["supersedes_version_id"]]
    assert len(parents) == len(set(parents)) == 5  # a linear chain, no forks


def test_concurrent_adds_of_same_semantic_identity(world: World, app_db: Database) -> None:
    ctxs = [world.ctx("alice") for _ in range(4)]
    results = run_concurrently(
        [
            (lambda c=c, i=i: world.service.commit_operations(c, [apollo_decision(f"plan {i}")]))
            for i, c in enumerate(ctxs)
        ]
    )
    assert sorted(r.status for r in results).count("committed") == 1
    assert count(app_db, "SELECT count(*) FROM memory_versions WHERE tenant_id = :tenant_id AND state = 'active'") == 1


def test_deletion_racing_retrieval_before_ranking(world: World) -> None:
    mid = add(world, "alice", apollo_decision())
    world.tick()

    def delete_now(_ctx: Any, _cands: Any) -> None:
        world.service.hooks.after_candidates = None
        world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))

    world.service.hooks.after_candidates = delete_now
    r = world.service.query(world.ctx("bob"), QueryRequest(query=Q))
    assert r.action is QueryAction.NO_MEMORY and "blue-green" not in r.answer


def test_deletion_racing_retrieval_before_release(world: World) -> None:
    mid = add(world, "alice", apollo_decision())
    world.tick()

    def delete_now(_evidence: Sequence[GeneratorEvidence]) -> None:
        world.service.hooks.before_generate = None
        world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))

    world.service.hooks.before_generate = delete_now
    r = world.service.query(world.ctx("bob"), QueryRequest(query=Q))
    assert r.action is QueryAction.NO_MEMORY and "blue-green" not in r.answer
    assert r.evidence == ()


def test_grant_revocation_racing_retrieval_fails_safely(world: World) -> None:
    mid = add(world, "alice", apollo_decision())
    g = world.service.commit_operations(
        world.ctx("alice"), [GrantAccess(memory_id=mid, grantee_principal_id="carol", view=View.FULL)]
    )
    gid = g.results[0].grant_id
    assert gid
    world.tick()

    def revoke_now(_ctx: Any) -> None:
        world.service.hooks.before_release = None
        world.service.commit_operations(world.ctx("alice"), [RevokeAccess(grant_id=gid)])

    world.service.hooks.before_release = revoke_now
    r = world.service.query(world.ctx("carol"), QueryRequest(query=Q))
    assert r.action is QueryAction.NO_MEMORY and "blue-green" not in r.answer


def test_concurrent_queries_during_deletion_never_leak_afterwards(world: World) -> None:
    mid = add(world, "alice", apollo_decision())
    stop = threading.Event()
    deleted = threading.Event()
    leaks_after_delete: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            was_deleted = deleted.is_set()
            r = world.service.query(world.ctx("bob"), QueryRequest(query=Q))
            if was_deleted and "blue-green" in r.answer:
                leaks_after_delete.append(r.answer)

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    deleted.set()
    for _ in range(5):
        assert world.service.query(world.ctx("bob"), QueryRequest(query=Q)).action is QueryAction.NO_MEMORY
    stop.set()
    for t in threads:
        t.join(timeout=30)
    assert leaks_after_delete == []
