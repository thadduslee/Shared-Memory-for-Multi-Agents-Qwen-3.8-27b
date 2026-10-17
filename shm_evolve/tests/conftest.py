"""Test configuration for the inner loop.

Every test runs without an endpoint. A test runs in one of two model modes:

  * offline -- `llm.complete` returns None, so every component's
    deterministic fallback decides;
  * fake    -- `memory_system.fake_llm` answers as each component, so the
    model-driven paths (parse, validate, apply, trace) run.

`llm_mode` parametrises a test over both. Tests marked `fake_llm` run with
the fake only. The ported contract file (tests/test_contract.py, unchanged)
runs in BOTH modes by default: `pytest_generate_tests` below parametrises it
over `_contract_llm_mode`, an autouse fixture every test already has in its
closure, so the ported file itself needs no edit to pick up a second run.
Its consent, revocation and deletion turns are instructions; the fallback
front door reads them through memory_system/cues.py's generic-English cue
words (not dataset vocabulary -- see cues.py's docstring and
test_vocabulary_lint.py) when no model is available, so the contract passes
offline too. Set SHM_CONTRACT_MODE=offline or =fake to pin it to one mode
(e.g. to see what the OTHER mode alone would catch).

`live` tests need MEMORY_LLM_BASE_URL and are skipped without it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_system import fake_llm, llm  # noqa: E402

_LIVE_URL = os.environ.get("MEMORY_LLM_BASE_URL", "").strip()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "fake_llm: run with memory_system.fake_llm installed")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    contract_mode = os.environ.get("SHM_CONTRACT_MODE", "both")
    for item in items:
        if item.path.name == "test_contract.py" and contract_mode == "fake":
            item.add_marker(pytest.mark.fake_llm)
        if item.get_closest_marker("live") and not _LIVE_URL:
            item.add_marker(pytest.mark.skip(reason="no MEMORY_LLM_BASE_URL configured"))


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Double-run tests/test_contract.py over both model modes by default,
    without editing the ported file. `_contract_llm_mode` (below) is
    autouse, so it is already in every test's fixture closure; indirectly
    parametrising it here is enough to make pytest invoke the contract's
    tests twice, once per mode, each picked up by `_memory_model` through
    `request.getfixturevalue`. SHM_CONTRACT_MODE=offline/=fake (handled in
    pytest_collection_modifyitems above) pins a single-mode run instead."""
    if not metafunc.definition.nodeid.split("::", 1)[0].endswith("test_contract.py"):
        return
    if os.environ.get("SHM_CONTRACT_MODE", "both") == "both":
        metafunc.parametrize("_contract_llm_mode", ["offline", "fake"], indirect=True, ids=["offline", "fake"])


@pytest.fixture(autouse=True)
def _contract_llm_mode(request: pytest.FixtureRequest) -> str | None:
    """None for every test except tests/test_contract.py's, which
    `pytest_generate_tests` parametrises to "offline" or "fake" by default.
    `_memory_model` reads this ahead of the `fake_llm` marker / `llm_mode`
    fixture when it is set."""
    return getattr(request, "param", None)


@pytest.fixture(autouse=True)
def _memory_model(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Offline by default; the fake when marked, when `llm_mode` is
    requested, or when `_contract_llm_mode` pins the ported contract's run.
    A live test keeps the env."""
    if not request.node.get_closest_marker("live"):
        monkeypatch.delenv("MEMORY_LLM_BASE_URL", raising=False)
    # A generous budget unless a test sets its own: the fake charges tokens
    # like a real call, and the frozen defaults run out inside one episode.
    if "MEMORY_LLM_TOKEN_BUDGET" not in os.environ:
        monkeypatch.setenv("MEMORY_LLM_TOKEN_BUDGET", "100000000")
    if "MEMORY_LLM_CALL_BUDGET" not in os.environ:
        monkeypatch.setenv("MEMORY_LLM_CALL_BUDGET", "1000000")
    llm._CACHE.clear()
    llm.new_episode()
    use_fake = bool(request.node.get_closest_marker("fake_llm"))
    if "llm_mode" in request.fixturenames:
        use_fake = request.getfixturevalue("llm_mode") == "fake"
    contract_mode = request.getfixturevalue("_contract_llm_mode")
    if contract_mode is not None:
        use_fake = contract_mode == "fake"
    if use_fake:
        fake_llm.install()
    try:
        yield
    finally:
        fake_llm.uninstall()


@pytest.fixture(params=["offline", "fake"])
def llm_mode(request: pytest.FixtureRequest) -> str:
    return request.param


# ---------------------------------------------------------------- a synthetic episode


def make_synthetic_episode() -> dict:
    """30 turns over meaningless names. u_ann and u_bo hold an unqualified
    fact over thing_alpha, u_cy over thing_beta, u_dee a QUALIFIED one over
    thing_alpha; u_eve holds nothing. Instructions are plain (the fake reads
    cue words); the rest is filler so the store has some size."""
    principals = [
        ("u_ann", "r_one", "Ann Avo"),
        ("u_bo", "r_two", "Bo Bex"),
        ("u_cy", "r_three", "Cy Cel"),
        ("u_dee", "r_four", "Dee Dun"),
        ("u_eve", "r_five", "Eve Ews"),
    ]
    roles = {p: r for p, r, _ in principals}
    lines = [
        ("u_ann", "dialogue", "The thing_alpha launch window opens on Monday at eight."),
        ("u_bo", "dialogue", "Noted, thanks."),
        ("u_cy", "dialogue", "The thing_beta fee is 4,200 per cycle."),
        ("u_ann", "dialogue", "The thing_alpha passphrase is velvet-9921."),
        ("u_bo", "dialogue", "Can someone confirm the thing_alpha launch window?"),
        ("u_ann", "dialogue", "u_eve is now authorized to see the thing_alpha launch window."),
        ("u_cy", "dialogue", "The thing_beta reviewer is the second shift."),
        ("u_ann", "dialogue", "Update: the thing_alpha launch window moved to Thursday at ten."),
        ("u_ann", "dialogue", "u_bo's access to everything about thing_alpha is revoked from now on."),
        ("u_cy", "dialogue", "Please delete the thing_beta fee 4,200 from memory."),
    ]
    for i in range(11, 31):
        if i % 5 == 0:
            lines.append(
                (
                    "u_cy",
                    "entry_note",
                    f"Entry {i}: thing_beta checklist item {i} closed by the second shift.",
                )
            )
        elif i % 2:
            lines.append(("u_ann", "dialogue", f"Status {i}: thing_alpha rehearsal step {i} is complete."))
        else:
            lines.append(("u_cy", "dialogue", f"Status {i}: thing_beta inventory count {i * 7} recorded."))
    turns = [
        {
            "turn_id": f"t{i + 1:03d}",
            "timestamp": f"2026-05-01T{8 + i // 6:02d}:{(i % 6) * 10:02d}",
            "turn_kind": kind,
            "speaker": {"principal_id": s, "role": roles[s]},
            "text": text,
        }
        for i, (s, kind, text) in enumerate(lines)
    ]
    return {
        "episode_id": "ep_synthetic",
        "entities": {
            "principals": [{"principal_id": p, "role": r, "display_name": n} for p, r, n in principals],
            "relationships": [
                {"type": "rel_k", "member_id": "u_ann", "target_id": "thing_alpha"},
                {"type": "rel_k", "member_id": "u_bo", "target_id": "thing_alpha"},
                {"type": "rel_k", "member_id": "u_cy", "target_id": "thing_beta"},
                {"type": "rel_m", "member_id": "u_dee", "target_id": "thing_alpha", "limit": "partial"},
            ],
        },
        "turns": turns,
    }


@pytest.fixture()
def synthetic_episode() -> dict:
    return make_synthetic_episode()


def checkpoint(asker: str, role: str, text: str, as_of: str, cid: str = "") -> dict:
    return {
        "checkpoint_id": cid or f"cp_{asker}_{as_of}",
        "episode_id": "ep_synthetic",
        "as_of_turn_id": as_of,
        "asker": {"principal_id": asker, "role": role},
        "query_text": text,
    }


@pytest.fixture()
def make_checkpoint():
    return checkpoint
