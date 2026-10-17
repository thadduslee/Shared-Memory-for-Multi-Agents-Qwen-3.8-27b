"""MUMBench adapter: gold isolation and the re-implemented scorer's arithmetic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gsm.eval.mumbench.data import load_for_method, load_raw
from gsm.eval.mumbench.scoring import score

FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.mumbench


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    for name in ("travel.json", "coding.jsonl"):
        (tmp_path / name).write_text((FIXTURES / name).read_text())
    return tmp_path


def test_method_view_has_no_gold(data_dir: Path) -> None:
    view = load_for_method(data_dir)
    assert set(view) == {"travel", "coding"}
    assert [i.interaction_id for i in view["travel"]] == ["t1", "t2", "t3", "t4", "t5"]
    assert [i.position for i in view["travel"]] == [0, 1, 2, 3, 4]
    assert "CANARY" not in json.dumps({d: [i.model_dump() for i in v] for d, v in view.items()})


def test_raw_loader_keeps_gold_for_scoring(data_dir: Path) -> None:
    raw = load_raw(data_dir)
    assert raw["travel"][0]["memory_actions"][0]["created_memory"]["visibility"] == "private"


def test_scorer_arithmetic(data_dir: Path) -> None:
    report = score(data_dir, FIXTURES / "predictions.jsonl")
    travel = report["domains"]["travel"]
    # t4 predicts create where gold is update; everything else has the right operation.
    assert travel["strict_operation_accuracy"] == pytest.approx(0.8)
    assert travel["count_match"] == pytest.approx(1.0)
    # Paired writes: t1 private/private, t2 gold public vs predicted private.
    assert travel["visibility_match"] == pytest.approx(0.5)
    assert travel["tag_overlap"] == pytest.approx(0.75)
    assert travel["content_fidelity"] is None  # needs embeddings
    # t3: u2 retrieved u1's private gate code (from t1) and u2's own public note.
    assert travel["retrieved_items"] == 2
    assert travel["unauthorized_retrieval_rate"] == pytest.approx(0.5)
    assert travel["adversarial_leak_rate"] == pytest.approx(0.2)
    coding = report["domains"]["coding"]
    assert coding["missing_predictions"] == 1 and coding["strict_operation_accuracy"] == 0.0
    assert report["macro"]["strict_operation_accuracy"] == pytest.approx(0.4)


def test_content_fidelity_with_embedder(data_dir: Path) -> None:
    report = score(data_dir, FIXTURES / "predictions.jsonl", embed=lambda texts: [[1.0, 0.0] for _ in texts])
    assert report["domains"]["travel"]["content_fidelity"] == pytest.approx(1.0)
