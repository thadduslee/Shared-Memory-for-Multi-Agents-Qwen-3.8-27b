"""Required end-to-end acceptance scenarios."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from gsm.auth import TrustedAuthenticator
from gsm.clock import FrozenClock
from gsm.demo import DECISION_V1, DECISION_V2, LEAVE_REASON, SUMMARY, run_apollo_lifecycle, run_partial_disclosure
from gsm.service import GovernedMemoryService


def test_apollo_lifecycle(
    service: GovernedMemoryService, auth: TrustedAuthenticator, clock: FrozenClock, owner_engine: Engine
) -> None:
    result = run_apollo_lifecycle(service, auth, clock)
    failed = [s.name for s in result.steps if not s.ok]
    assert not failed, failed
    assert len(result.steps) == 12
    # Independent check straight against storage: no plaintext of either version or the summary survives.
    with owner_engine.connect() as conn:
        for needle in (DECISION_V1, DECISION_V2, SUMMARY, "blue-green"):
            for table, col in (
                ("memory_versions", "content_text"),
                ("memory_projections", "text"),
                ("raw_events", "payload"),
                ("memory_atoms", "topic"),
            ):
                n = conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE CAST({col} AS text) ILIKE :n"),
                    {"n": f"%{needle}%"},
                ).scalar()
                assert n == 0, (table, needle)
        tomb = conn.execute(text("SELECT cause, count(*) FROM deletion_tombstones GROUP BY cause ORDER BY cause")).all()
        assert [tuple(r) for r in tomb] == [("cascade", 1), ("direct", 1)]


def test_partial_disclosure(
    service: GovernedMemoryService, auth: TrustedAuthenticator, clock: FrozenClock, owner_engine: Engine
) -> None:
    result = run_partial_disclosure(service, auth, clock)
    failed = [s.name for s in result.steps if not s.ok]
    assert not failed, failed
    with owner_engine.connect() as conn:
        audit = " ".join(str(r) for r in conn.execute(text("SELECT * FROM release_audits")).all())
    assert LEAVE_REASON not in audit
