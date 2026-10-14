"""The seed workspace's measured floor, against the real benchmark data.

WHY A TEST AND NOT A NOTE IN THE README. `templates/` is what every run starts
from, so its score is the number every iteration is measured against. A silent
regression there does not look like a regression: it looks like the research
loop failing to improve, which is exactly the thing this repository is for
studying and exactly the thing it must not manufacture.

These run the same retrieval/decision path with no model. The adaptive baseline
is deliberately fail-closed and contains no preseeded medical role grants. Its
utility floor is therefore lower than the former benchmark-specific RBAC seed,
while deletion safety is strict. The exact measured adaptive baseline is:

    dev:  U=.2222 A=.0000 F=.0000 MGS=.2222
    full: U=.2238 A=.0208 F=.0000 MGS=.2191

The floors guard implementation regressions; the trusted generic tests separately
guard narrow consent utility and the absence of domain seed policy.
"""

from __future__ import annotations

import pytest

import config
from bench_template import evaluate

pytestmark = pytest.mark.skipif(
    not (config.GATEMEM_DATA_DIR / "checkpoints.jsonl").is_file(),
    reason=f"no GateMem checkout at {config.GATEMEM_DATA_DIR}",
)


@pytest.fixture(scope="module")
def dev():
    return evaluate(config.TEMPLATES_DIR, "dev")


def test_the_template_clears_its_utility_floor(dev) -> None:
    """The fail-closed generic baseline has measured dev utility of 0.2222.

    The stronger behavior contracts live in the domain-neutral trusted tests;
    this floor catches accidental benchmark-wide retrieval regressions.
    """
    assert dev["U"] >= 0.20, dev["confusions"]


def test_the_template_does_not_leak_more_than_its_floor(dev) -> None:
    """The measured generic dev baseline has zero privacy/deletion leakage.

    A and F are the terms a utility fix is most likely to buy itself with, so
    they are asserted alongside U rather than left implicit. MGS is a product:
    a change that lifts U by 0.5 and lifts A by 0.5 is a change that made the
    system worse.
    """
    assert dev["A"] == 0.0, dev["confusions"]
    assert dev["F"] == 0.0, dev["confusions"]


def test_the_template_clears_its_mgs_floor(dev) -> None:
    """The measured fail-closed adaptive baseline has dev MGS 0.2222."""
    assert dev["MGS"] >= 0.20, dev


def test_the_action_label_confusion_is_no_longer_dominant(dev) -> None:
    """A scoped answer must not be downgraded by unrelated denied evidence.

    This guards authorization-before-rendering and relevance filtering without
    requiring the empty store to contain benchmark-specific grants.
    """
    assert dev["confusions"][("answer", "answer_redacted")] <= 2, dev["confusions"]


def test_the_adaptive_floor_holds_on_the_full_five_hundred_and_seventy_nine() -> None:
    """The dev assertions must not overfit the fixed fifty-checkpoint slice.

    The full measured adaptive baseline is U=.2238, A=.0208, F=.0000,
    MGS=.2191 across 579 checkpoints.
    """
    full = evaluate(config.TEMPLATES_DIR, "full")
    assert full["n"] == 579
    assert full["U"] >= 0.22, full["confusions"]
    assert full["A"] <= 0.03, full["confusions"]
    assert full["F"] == 0.0, full["confusions"]
    assert full["MGS"] >= 0.21, full


def test_evaluating_two_workspaces_in_one_process_does_not_cross_contaminate(
    tmp_path,
) -> None:
    """The mistake this guard exists for produces two identical scores.

    `evaluate` imports `memory_system` out of the workspace it is given. Python
    caches modules by name, so without the eviction in `evaluate` the second
    call would score the FIRST workspace's code and report a confident, wrong
    "no change".
    """
    import shutil

    crippled = tmp_path / "crippled"
    shutil.copytree(config.TEMPLATES_DIR, crippled,
                    ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache", "tests"))
    agent = crippled / "memory_system" / "agent.py"
    source = agent.read_text(encoding="utf-8")
    # Refuse everything: an unmistakable score, impossible to reach by accident.
    source = source.replace(
        '    if decision.touched_deleted:',
        '    return "refuse", "crippled for the cross-contamination test"\n'
        '    if decision.touched_deleted:',
        1,
    )
    agent.write_text(source, encoding="utf-8")

    baseline = evaluate(config.TEMPLATES_DIR, "dev")
    broken = evaluate(crippled, "dev")
    assert baseline["U"] >= 0.20
    assert broken["U"] == 0.0, "the second workspace's own code was not the one scored"
    # ...and the first is still scored correctly afterwards, so the eviction
    # cleans up in both directions.
    assert evaluate(config.TEMPLATES_DIR, "dev")["U"] == baseline["U"]
