"""The utility repairs (R3-R6), each checked on and off with the scripted model fake."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from gsm.config import Settings
from gsm.db.database import Database
from gsm.eval.gatemem.adapter import run_gatemem
from gsm.eval.gatemem.qwen_interpreter import ACTIVITY_SYSTEM, CARRY_SYSTEM, SELECT_PARTS_SYSTEM
from gsm.eval.gatemem.repairs import REPAIRS

from .test_qwen_interpreter import FakeModel, _episode, _shown, interpreter

pytestmark = pytest.mark.gatemem


def _scoped_contractor(data: Path) -> Path:
    """Give the contractor relationship a scope limit (as GateMem's access_scope attributes do)."""
    ep = json.loads((data / "episodes.jsonl").read_text())
    for rel in ep["entities"]["relationships"]:
        if rel["type"] == "contractor_for_project":
            rel["access_scope"] = "logistics_only"
    (data / "episodes.jsonl").write_text(json.dumps(ep) + "\n")
    return data


def _run(
    app_db: Database, settings: Settings, data: Path, out: Path, fake: FakeModel, without: str = "", **kw: Any
) -> Any:
    full = settings.model_copy(update={"full_context_max_items": 50})
    repairs = REPAIRS - {without}
    return run_gatemem(app_db, full, data, out, interpreter=interpreter(fake), repairs=repairs, diagnostics=True, **kw)


# ---------------------------------------------------------------- R3: audience narrowed


@pytest.mark.parametrize("on", [True, False])
def test_people_a_delegate_acts_for_are_told_what_the_delegate_is_told(
    app_db: Database, settings: Settings, tmp_path: Path, on: bool
) -> None:
    labels = {
        "f001": {
            "kind": "fact",
            "full_audience": ["speaker", "G2"],  # the model keeps it with the delegate
            "facts": [{"quote": "Orion logging install window is now Friday 10:00 to 12:00."}],
        }
    }
    data = _scoped_contractor(
        _episode(
            tmp_path,
            [("f001", "con_dev", "Orion logging install window is now Friday 10:00 to 12:00.")],
            [
                {"checkpoint_id": "ava", "as_of_turn_id": "f001", "asker": "pm_ava", "query_text": "install window?"},
                {"checkpoint_id": "cara", "as_of_turn_id": "f001", "asker": "hr_cara", "query_text": "install window?"},
            ],
        )
    )
    fake = FakeModel(labels=labels, groups={"G1": "works", "G2": "scoped", "G3": "works"})
    out = _run(app_db, settings, data, tmp_path / "out", fake, without="" if on else "delegate_floor")
    shown = _shown(out)
    assert ("Friday 10:00" in shown["ava"]) is on  # G1 (project members) are who the contractor works for
    assert "Friday 10:00" not in shown["cara"]  # HR is not on the project
    assert (out.summary["counters"].get("floor_additions", 0) == 1) is on


@pytest.mark.parametrize("on", [True, False])
def test_an_update_keeps_the_readers_of_the_value_it_replaces(
    app_db: Database, settings: Settings, tmp_path: Path, on: bool
) -> None:
    labels = {
        "u001": {
            "kind": "fact",
            "full_audience": ["G1", "G2"],
            "exclude": ["eng_ben"],
            "facts": [{"quote": "Orion demo is in room 4B.", "key": "orion.demo_room"}],
        },
        # the update is given a narrower audience by the model: only the speaker
        "u002": {
            "kind": "fact",
            "full_audience": ["speaker"],
            "facts": [{"quote": "Orion demo moved to room 7C.", "key": "orion.demo_room"}],
        },
    }
    data = _episode(
        tmp_path,
        [
            ("u001", "pm_ava", "Orion demo is in room 4B. Do not tell Ben yet."),
            ("u002", "pm_ava", "Orion demo moved to room 7C."),
        ],
        [
            {"checkpoint_id": "dev", "as_of_turn_id": "u002", "asker": "con_dev", "query_text": "Orion demo room?"},
            {"checkpoint_id": "ben", "as_of_turn_id": "u002", "asker": "eng_ben", "query_text": "Orion demo room?"},
            {"checkpoint_id": "cara", "as_of_turn_id": "u002", "asker": "hr_cara", "query_text": "Orion demo room?"},
        ],
    )
    out = _run(app_db, settings, data, tmp_path / "out", FakeModel(labels=labels), without="" if on else "keep_readers")
    shown = _shown(out)
    assert ("7C" in shown["dev"]) is on  # Dev could read the old room, so he reads the new one
    assert "4B" not in shown["dev"]  # the old value is superseded either way
    assert "7C" not in shown["ben"] and "4B" not in shown["ben"]  # the exclusion by name is carried over
    assert "7C" not in shown["cara"]  # never a reader
    entry = {e["turn_id"]: e for e in out.ingest_log}["u002"]["facts"][0]
    assert entry["outcome"] == "superseded"
    assert ("readers_kept" in entry) is on


def test_whoever_could_update_the_old_value_can_update_the_new_one(
    app_db: Database, settings: Settings, tmp_path: Path
) -> None:
    labels = {
        "a001": {
            "kind": "fact",
            "full_audience": ["G1"],
            "facts": [{"quote": "Orion demo is in room 4B.", "key": "orion.demo_room"}],
        },
        # the owner updates it for herself only; Ben (G1) keeps reading it (keep_readers) ...
        "a002": {
            "kind": "fact",
            "full_audience": ["speaker"],
            "facts": [{"quote": "Orion demo moved to room 7C.", "key": "orion.demo_room"}],
        },
        # ... and, having been able to update the old value, can update the new one
        "a003": {
            "kind": "fact",
            "full_audience": ["G1"],
            "facts": [{"quote": "Orion demo moved again to room 9D.", "key": "orion.demo_room"}],
        },
    }
    data = _episode(
        tmp_path,
        [
            ("a001", "pm_ava", "Orion demo is in room 4B."),
            ("a002", "pm_ava", "Orion demo moved to room 7C."),
            ("a003", "eng_ben", "Orion demo moved again to room 9D."),
        ],
        [{"checkpoint_id": "ava", "as_of_turn_id": "a003", "asker": "pm_ava", "query_text": "Orion demo room?"}],
    )
    out = _run(app_db, settings, data, tmp_path / "out", FakeModel(labels=labels))
    shown = _shown(out)["ava"]
    assert "9D" in shown and "7C" not in shown and "4B" not in shown  # one current value, not two
    entry = {e["turn_id"]: e for e in out.ingest_log}["a003"]["facts"][0]
    assert "NOT_AUTHORIZED_TO_WRITE" not in entry.get("refused_reasons", [])
    assert entry["outcome"] == "superseded" or entry.get("retired")
    assert out.summary["counters"]["editors_kept"] >= 1


# ---------------------------------------------------------------- R4: scope-limited people get nothing


def _two_scoped_groups(data: Path) -> Path:
    ep = json.loads((data / "episodes.jsonl").read_text())
    for rel in ep["entities"]["relationships"]:
        if rel["type"] == "contractor_for_project":
            rel["access_scope"] = "logistics_only"
        if rel["type"] == "people_manager":
            rel["access_scope"] = "attendance_only"
    (data / "episodes.jsonl").write_text(json.dumps(ep) + "\n")
    return data


@pytest.mark.parametrize("on", [True, False])
def test_each_scope_limited_group_gets_its_own_in_scope_version(
    app_db: Database, settings: Settings, tmp_path: Path, on: bool
) -> None:
    labels = {
        "s001": {
            "kind": "fact",
            "full_audience": ["G1"],
            "facts": [
                {
                    "quote": "Ben presents the Orion design review in room 4B on Thursday 10:00; "
                    "it found a data leak bug.",
                    "key": "orion.design_review",
                }
            ],
            "scope_fit": {"0": {"G2": "Design review: room 4B, Thursday 10:00.", "G3": "Ben: busy Thursday 10:00."}},
        }
    }
    text = "Ben presents the Orion design review in room 4B on Thursday 10:00; it found a data leak bug."
    data = _two_scoped_groups(
        _episode(
            tmp_path,
            [("s001", "pm_ava", text)],
            [
                {"checkpoint_id": "dev", "as_of_turn_id": "s001", "asker": "con_dev", "query_text": "review?"},
                {"checkpoint_id": "eli", "as_of_turn_id": "s001", "asker": "mgr_eli", "query_text": "review?"},
                {"checkpoint_id": "cara", "as_of_turn_id": "s001", "asker": "hr_cara", "query_text": "review?"},
            ],
        )
    )
    fake = FakeModel(labels=labels, groups={"G1": "works", "G2": "scoped", "G3": "scoped"})
    out = _run(app_db, settings, data, tmp_path / "out", fake, without="" if on else "scope_records", scope_fit=True)
    shown = _shown(out)
    assert "room 4B" in shown["dev"] and "busy" not in shown["dev"] and "leak" not in shown["dev"]
    assert ("busy Thursday" in shown["eli"]) is on  # off: Eli is handed the contractor's version
    assert ("room 4B" not in shown["eli"]) is on
    assert "leak" not in shown["eli"]
    assert "Thursday" not in shown["cara"]
    assert (out.summary["counters"].get("scope_records", 0) == 1) is on


# ---------------------------------------------------------------- R5: partial update throws away details


class CarryModel(FakeModel):
    """Also answers the carry-forward call, from the NEW turn's "carry" entry."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if system != CARRY_SYSTEM:
            return super().__call__(request)
        turn = re.match(r"turn (\w+)\n", user)
        assert turn is not None
        content = json.dumps({"still_in_effect": self.labels[turn.group(1)].get("carry", [])})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {}})


@pytest.mark.parametrize("on", [True, False])
def test_an_update_keeps_the_parts_of_the_old_fact_it_does_not_change(
    app_db: Database, settings: Settings, tmp_path: Path, on: bool
) -> None:
    labels = {
        "c001": {
            "kind": "fact",
            "full_audience": ["G1"],
            "facts": [{"quote": "Orion demo is in room 4B on the big wall screen.", "key": "orion.demo_room"}],
        },
        "c002": {
            "kind": "fact",
            "full_audience": ["G1"],
            "facts": [{"quote": "Orion demo moved to room 7C.", "key": "orion.demo_room"}],
            # one valid part; one the old statement does not contain; one the new statement replaced
            "carry": ["on the big wall screen", "with the projector", "room 4B"],
        },
    }
    data = _episode(
        tmp_path,
        [
            ("c001", "pm_ava", "Orion demo is in room 4B on the big wall screen."),
            ("c002", "pm_ava", "Orion demo moved to room 7C."),
        ],
        [{"checkpoint_id": "ben", "as_of_turn_id": "c002", "asker": "eng_ben", "query_text": "Orion demo?"}],
    )
    out = _run(
        app_db, settings, data, tmp_path / "out", CarryModel(labels=labels), without="" if on else "carry_forward"
    )
    shown = _shown(out)["ben"]
    assert "7C" in shown
    assert ("big wall screen" in shown) is on
    assert "projector" not in shown  # not in the old statement: never carried
    entry = {e["turn_id"]: e for e in out.ingest_log}["c002"]["facts"][0]
    assert entry["outcome"] == "superseded"
    assert "4B" not in shown  # the changed value: same slot ("room") as the new statement
    assert entry.get("carried_forward", []) == (["on the big wall screen"] if on else [])


# ---------------------------------------------------------------- R6: deletion takes out neighbouring facts


def test_dates_times_and_codes_are_whole_values_and_masking_is_by_whole_token() -> None:
    from gsm.eval.gatemem.adapter import _mask, _values

    assert _values("Harbor moves from July 2, 2026 to July 14, 2026 at 10:30 AM.") == [
        "July 2, 2026",
        "July 14, 2026",
        "10:30 AM",
    ]
    assert _values("label bridge-88, replacing bridge-63; desk 2") == ["bridge-88", "bridge-63", "2"]
    assert _mask("Harbor moves from July 2, 2026 to July 14, 2026", ["July 2, 2026"]) == (
        "Harbor moves from [removed] to July 14, 2026",
        1,
    )
    assert _mask("Room 12, desk 2, July 21", ["2", "July 2"]) == ("Room 12, desk [removed], July 21", 1)


class PartModel(FakeModel):
    """Picks items containing ``target`` for deletion, removing only ``remove`` from them."""

    def __init__(self, remove: str | None, **kw: Any) -> None:
        super().__init__(**kw)
        self.remove = remove

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if system != SELECT_PARTS_SYSTEM:
            return super().__call__(request)
        items = re.findall(r"^\[(\d+)\] (.*)$", user, flags=re.MULTILINE)
        targets = [{"index": int(i), "remove": self.remove} for i, text in items if self.target in text]
        content = json.dumps({"targets": targets})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {}})


@pytest.mark.parametrize("on", [True, False])
def test_deleting_an_old_value_keeps_the_current_value_stored_next_to_it(
    app_db: Database, settings: Settings, tmp_path: Path, on: bool
) -> None:
    text = "Approved budget for Horizon is now 231,000 USD. That replaces the earlier working number of 252,000 USD."
    labels = {
        "d001": {
            "kind": "fact",
            "full_audience": ["G1"],
            "facts": [{"quote": text, "key": "horizon.budget"}],
        },
        "d002": {
            "kind": "deletion",
            "delete": {"what": "the earlier working number 252,000 USD"},
            "facts": [],
        },
    }
    data = _episode(
        tmp_path,
        [("d001", "pm_ava", text), ("d002", "pm_ava", "Please delete the earlier working number 252,000 USD.")],
        [{"checkpoint_id": "ben", "as_of_turn_id": "d002", "asker": "eng_ben", "query_text": "Horizon budget?"}],
    )
    fake = PartModel("That replaces the earlier working number of 252,000 USD.", labels=labels, target="252,000")
    out = _run(app_db, settings, data, tmp_path / "out", fake, without="" if on else "partial_delete")
    shown = _shown(out)["ben"]
    assert "252,000" not in shown  # the targeted value is gone either way
    assert ("231,000" in shown) is on  # off: the whole record went
    c = out.summary["counters"]
    assert (c.get("deletions_partial", 0) == 1) is on
    log = {e["turn_id"]: e for e in out.ingest_log}["d002"]
    assert bool(log["rewritten"]) is on and bool(log["deleted"]) is not on


def test_a_correction_naming_an_old_date_masks_only_that_date(
    app_db: Database, settings: Settings, tmp_path: Path
) -> None:
    text = "Project Harbor moves from July 2, 2026 to July 14, 2026."
    labels = {
        "h001": {
            "kind": "fact",
            "full_audience": ["G1"],
            "delete": {"what": "the old launch date July 2, 2026"},
            "facts": [{"quote": text, "key": "harbor.launch"}],
        }
    }
    data = _episode(
        tmp_path,
        [("h001", "pm_ava", text)],
        [{"checkpoint_id": "ben", "as_of_turn_id": "h001", "asker": "eng_ben", "query_text": "Harbor launch?"}],
    )
    out = _run(app_db, settings, data, tmp_path / "out", FakeModel(labels=labels, target="no-such-item"))
    shown = _shown(out)["ben"]
    assert "July 14, 2026" in shown and "July 2," not in shown  # before: "July 14, [removed]"


# ---------------------------------------------------------------- U1: the asker's own record of their work


class ActivityModel(FakeModel):
    """Also answers the activity check, from the turn's "activity" entry."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if system != ACTIVITY_SYSTEM:
            return super().__call__(request)
        turn = re.match(r"turn (\w+),", user)
        assert turn is not None
        content = json.dumps({"activity": bool(self.labels[turn.group(1)].get("activity"))})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {}})


@pytest.mark.parametrize("on", [True, False])
def test_a_message_stating_the_speakers_own_task_is_kept_for_the_speaker(
    app_db: Database, settings: Settings, tmp_path: Path, on: bool
) -> None:
    labels = {
        "n001": {"kind": "chatter", "facts": [], "activity": True},
        "n002": {"kind": "chatter", "facts": [], "activity": False},
    }
    data = _episode(
        tmp_path,
        [
            ("n001", "eng_ben", "Hi Ava, I'm Ben. I'm running the Orion load test on cluster 7 this week."),
            ("n002", "eng_ben", "Thanks, sounds good."),
        ],
        [
            {"checkpoint_id": "ben", "as_of_turn_id": "n002", "asker": "eng_ben", "query_text": "Orion load test?"},
            {"checkpoint_id": "ava", "as_of_turn_id": "n002", "asker": "pm_ava", "query_text": "Orion load test?"},
        ],
    )
    out = _run(
        app_db, settings, data, tmp_path / "out", ActivityModel(labels=labels), without="" if on else "activity_notes"
    )
    shown = _shown(out)
    assert ("running the Orion load test on cluster [value]" in shown["ben"]) is on
    assert "cluster 7" not in shown["ben"]  # values never kept
    assert "load test" not in shown["ava"]  # the speaker's own note only
    assert "sounds good" not in shown["ben"]  # not an activity
    assert (out.summary["counters"].get("activity_notes", 0) == 1) is on


def _states(out: Any, turn: str) -> list[str]:
    last = out.memory_snapshots[-1]["records"]
    return sorted(r["state"] for r in last if r["turn_id"] == turn)


@pytest.mark.parametrize("on", [True, False])
def test_a_deletion_reaches_the_earlier_version_it_names(
    app_db: Database, settings: Settings, tmp_path: Path, on: bool
) -> None:
    labels = {
        "h001": {
            "kind": "fact",
            "full_audience": ["G1"],
            "facts": [{"quote": "Orion partner wording is alpha harbor review.", "key": "orion.wording"}],
        },
        "h002": {
            "kind": "fact",
            "full_audience": ["G1"],
            "facts": [{"quote": "Orion partner wording is now beta review.", "key": "orion.wording"}],
        },
        "h003": {"kind": "deletion", "delete": {"what": "the earlier alpha harbor wording"}, "facts": []},
    }
    data = _episode(
        tmp_path,
        [
            ("h001", "pm_ava", "Orion partner wording is alpha harbor review."),
            ("h002", "pm_ava", "Orion partner wording is now beta review."),
            ("h003", "pm_ava", "Please delete the earlier alpha harbor wording."),
        ],
        [{"checkpoint_id": "c", "as_of_turn_id": "h003", "asker": "eng_ben", "query_text": "Orion wording?"}],
    )
    fake = FakeModel(labels=labels, target="alpha harbor")
    out = _run(app_db, settings, data, tmp_path / "out", fake, without="" if on else "delete_history", snapshots=True)
    assert _states(out, "h001") == (["deleted"] if on else ["superseded"])  # off: the search never sees it
    assert _states(out, "h002") == ["current"]  # the current wording is not touched
    assert (out.summary["counters"].get("history_deleted", 0) == 1) is on


def test_deleting_a_record_deletes_its_earlier_versions(app_db: Database, settings: Settings, tmp_path: Path) -> None:
    labels = {
        "g001": {
            "kind": "fact",
            "full_audience": ["G1"],
            "facts": [{"quote": "Orion vendor is Kestrel Labs.", "key": "orion.vendor"}],
        },
        "g002": {
            "kind": "fact",
            "full_audience": ["G1"],
            "facts": [{"quote": "Orion vendor is now Osprey Labs.", "key": "orion.vendor"}],
        },
        "g003": {"kind": "deletion", "delete": {"what": "the Orion vendor"}, "facts": []},
    }
    data = _episode(
        tmp_path,
        [
            ("g001", "pm_ava", "Orion vendor is Kestrel Labs."),
            ("g002", "pm_ava", "Orion vendor is now Osprey Labs."),
            ("g003", "pm_ava", "Please delete the Orion vendor."),
        ],
        [{"checkpoint_id": "c", "as_of_turn_id": "g003", "asker": "eng_ben", "query_text": "Orion vendor?"}],
    )
    out = _run(app_db, settings, data, tmp_path / "out", FakeModel(labels=labels, target="Osprey"), snapshots=True)
    assert _states(out, "g002") == ["deleted"] and _states(out, "g001") == ["deleted"]
