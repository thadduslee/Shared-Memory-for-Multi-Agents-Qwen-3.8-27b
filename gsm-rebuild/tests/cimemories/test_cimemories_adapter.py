"""CIMemories adapter: label isolation, label regeneration, and agreement with upstream metrics.py."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

from gsm.eval.cimemories import labels as lab
from gsm.eval.cimemories.data import DEFAULT_DATA, DEFAULT_METRICS, load_for_method, load_raw, statements
from gsm.eval.cimemories.run import FullContextMethod, run_method
from gsm.eval.cimemories.scoring import build_results, headline, judge_run, official_metrics

FIXTURE = Path(__file__).parent / "fixtures" / "profiles_labelled.json"
pytestmark = pytest.mark.cimemories


class FakeClient:
    """Records every prompt; answers with ``reply(prompt)``."""

    def __init__(self, reply: Any, model: str = "fake") -> None:
        self.reply = reply
        self.model = model
        self.prompts: list[str] = []

    def sample(self, user: str, *, n: int = 1, temperature: float | None = None, max_tokens: int = 4096) -> list[str]:
        self.prompts.append(user)
        return [self.reply(user) for _ in range(n)]


def test_method_view_has_no_labels_keys_or_values(tmp_path: Path) -> None:
    profiles = load_for_method(FIXTURE)
    assert "CANARY" not in json.dumps([p.model_dump() for p in profiles])
    client = FakeClient(lambda _p: "Dear recipient, ...")
    run_method(FullContextMethod(client), profiles, tmp_path, trials=2, workers=2)  # type: ignore[arg-type]
    assert client.prompts and not any("CANARY" in p for p in client.prompts)
    rows = [json.loads(x) for x in (tmp_path / "solutions.jsonl").read_text().splitlines()]
    assert len(rows) == 2 * 4 and {r["trial"] for r in rows} == {0, 1}


def test_run_resumes_without_repeating(tmp_path: Path) -> None:
    profiles = load_for_method(FIXTURE)
    client = FakeClient(lambda _p: "hi")
    run_method(FullContextMethod(client), profiles, tmp_path, trials=1)  # type: ignore[arg-type]
    calls = len(client.prompts)
    summary = run_method(FullContextMethod(client), profiles, tmp_path, trials=1)  # type: ignore[arg-type]
    assert summary["new_solutions"] == 0 and len(client.prompts) == calls


def test_task_prompt_matches_upstream_stage_a() -> None:
    (profile,) = load_for_method(FIXTURE, max_profiles=1)
    client = FakeClient(lambda _p: "x")
    FullContextMethod(client).respond(profile, profile.contexts[0], 0)  # type: ignore[arg-type]
    prompt = client.prompts[0]
    assert "Write a complete message to gastroenterologist to achieve the following purpose: Book a follow-up" in prompt
    assert "Mira was diagnosed with celiac disease in 2021.\nMira earns $58,000 a year." in prompt


def test_bio_sentence_shadows_colliding_attribute_like_upstream() -> None:
    raw = {"bio": {"name": "Ann Lee"}, "information_attributes": {"name": {"memory_statement": "My name is Ann."}}}
    assert statements(raw) == [("name", "Ann Lee is the person's name.")]


def test_real_data_loads_if_vendored() -> None:
    if not DEFAULT_DATA.exists():
        pytest.skip("third_party/CIMemories not present")
    profiles = load_for_method(DEFAULT_DATA)
    assert len(profiles) == 10 and all(len(p.contexts) == 49 for p in profiles)


def test_label_distribution_and_persona_mix(tmp_path: Path) -> None:
    raw = load_raw(FIXTURE)
    for prof in raw:  # strip the fixture's labels: regenerate them
        for ctx in prof["contexts"]:
            for k in [k for k in ctx if k.startswith("labels_")]:
                del ctx[k]
    data = tmp_path / "unlabelled.json"
    data.write_text(json.dumps(raw))

    def reply(prompt: str) -> str:
        # Fundamentalists keep the diagnosis private; the other personas call it necessary.
        diag = "Mira was diagnosed with celiac disease in 2021."
        if "Fundamentalist" in prompt:
            return json.dumps({"share": [], "private": [diag]})
        return json.dumps({"share": [diag], "private": []})

    client = FakeClient(reply)
    out = tmp_path / "labelled.json"
    stats = lab.label_dataset(client, data, out, labeller="fake", n=2, workers=2)  # type: ignore[arg-type]
    assert stats["labelled"] == 4 * 3 and stats["short"] == 0 and not stats["stopped_early"]
    assert stats["contexts_fully_labelled"] == 4
    labelled = json.loads(out.read_text())
    dist = labelled[0]["contexts"][0]["labels_combined"]["Mira was diagnosed with celiac disease in 2021."]
    assert dist["private"] == pytest.approx(0.26) and dist["necessary"] == pytest.approx(0.74)
    assert labelled[0]["label_meta"]["labeller"] == "fake"
    assert not any("CANARY-LABEL" in p for p in client.prompts)
    # Resume: nothing left to label.
    assert lab.label_dataset(client, data, out, labeller="fake", n=2)["jobs_pending_at_start"] == 0  # type: ignore[arg-type]


def _unlabelled(tmp_path: Path) -> Path:
    raw = load_raw(FIXTURE)
    for prof in raw:
        for ctx in prof["contexts"]:
            for k in [k for k in ctx if k.startswith("labels_")]:
                del ctx[k]
    data = tmp_path / "unlabelled.json"
    data.write_text(json.dumps(raw))
    return data


def test_quota_exhaustion_saves_progress_and_resumes(tmp_path: Path) -> None:
    """When the provider stops answering mid-run, complete jobs are kept, failed ones are redone later."""
    from gsm.errors import ProviderError

    data, out = _unlabelled(tmp_path), tmp_path / "labelled.json"
    budget = {"calls": 4}

    def reply(_prompt: str) -> str:
        if budget["calls"] <= 0:
            raise ProviderError("http_429")
        budget["calls"] -= 1
        return json.dumps({"share": [], "private": []})

    client = FakeClient(reply)
    first = lab.label_dataset(client, data, out, labeller="fake", n=1, workers=1)  # type: ignore[arg-type]
    assert first["labelled"] == 4 and first["stopped_early"] and first["remaining"] == 8
    saved = json.loads(out.read_text())
    stored = [k for p in saved for c in p["contexts"] for k in c if k.startswith("labels_") and k != "labels_combined"]
    assert len(stored) == 4  # nothing was stored for the jobs the provider refused
    assert first["contexts_fully_labelled"] == 1  # only complete contexts get labels_combined

    budget["calls"] = 100  # the next day
    second = lab.label_dataset(client, data, out, labeller="fake", n=1, workers=1)  # type: ignore[arg-type]
    assert second["jobs_pending_at_start"] == 8 and second["labelled"] == 8 and not second["stopped_early"]
    assert second["contexts_fully_labelled"] == 4


def test_max_jobs_caps_a_run(tmp_path: Path) -> None:
    data, out = _unlabelled(tmp_path), tmp_path / "labelled.json"
    client = FakeClient(lambda _p: json.dumps({"share": [], "private": []}))
    stats = lab.label_dataset(client, data, out, labeller="fake", n=1, max_jobs=5)  # type: ignore[arg-type]
    assert stats["labelled"] == 5 and stats["remaining"] == 7 and not stats["stopped_early"]


def _upstream_labelling() -> tuple[list[str], Any]:
    """Upstream ``personas`` and ``label_attributes``, lifted out of persona_gold_labels.py without running it."""
    import ast
    from textwrap import dedent

    src = (DEFAULT_DATA.parent / "persona_gold_labels.py").read_text()
    tree = ast.parse(src)
    keep = [
        n
        for n in tree.body
        if (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "personas" for t in n.targets))
        or (isinstance(n, ast.FunctionDef) and n.name == "label_attributes")
    ]
    ns: dict[str, Any] = {"dedent": dedent, "completion_cost": lambda _m: 0.0}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "upstream", "exec"), ns)  # noqa: S102
    return ns["personas"], ns["label_attributes"]


def test_labelling_prompt_is_upstreams() -> None:
    if not DEFAULT_DATA.exists():
        pytest.skip("third_party/CIMemories not present")
    personas, label_attributes = _upstream_labelling()
    assert [p.split("\n")[1].strip() for p in personas] == [p.split("\n")[1].strip() for p in lab.PERSONAS.values()]
    profile = load_raw(FIXTURE)[0]
    ctx = profile["contexts"][0]
    memories = [a["memory_statement"] for a in profile["information_attributes"].values()]
    seen: list[str] = []

    class Model:
        def raw(self, prompt: str, n: int = 1) -> list[Any]:
            seen.append(prompt)
            return []

    for upstream_persona, ours in zip(personas, lab.PERSONAS.values(), strict=True):
        label_attributes(Model(), upstream_persona, profile, ctx, n=1)
        assert seen[-1] == lab.labelling_prompt(ours, ctx["recipient"], ctx["task"], memories)


def _judge(prompt: str) -> str:
    # Reveal every attribute whose statement appears in the message.
    attrs = dict(re.findall(r"^(\S+) → (.+)$", prompt.split("SOLUTION:")[0], flags=re.M))
    message = prompt.split("SOLUTION:")[1]
    return json.dumps({k: s for k, s in attrs.items() if s in message})


def test_headline_agrees_with_upstream_metrics(tmp_path: Path) -> None:
    profiles = load_for_method(FIXTURE)
    # Message = the first two memory statements, so some necessary and some private ones are revealed.
    run_method(
        FullContextMethod(FakeClient(lambda p: "\n".join(p.split("\n\n")[2].split("\n")[:2]))),  # type: ignore[arg-type]
        profiles,
        tmp_path,
        trials=2,
    )
    judged = judge_run(FIXTURE, tmp_path, FakeClient(_judge))  # type: ignore[arg-type]
    assert judged["judged_now"] == 8 and judged["unjudged"] == 0
    results, counts = build_results(FIXTURE, tmp_path)
    assert {k: counts[k] for k in ("solutions", "judged", "unjudged", "judge_verdicts_unparsed")} == {
        "solutions": 8,
        "judged": 8,
        "unjudged": 0,
        "judge_verdicts_unparsed": 0,
    }
    mine = headline(results)
    assert mine["contexts_scored"] == 4 and 0 < mine["violation"] < 1 and 0 < mine["coverage"] < 1

    if importlib.util.find_spec("pandas") is None or not DEFAULT_METRICS.exists():
        pytest.skip("upstream metrics.py needs pandas and third_party/CIMemories")
    printout = official_metrics(results)
    assert printout is not None
    v = float(re.search(r"Overall violation.*?:([\d.]+)", printout).group(1))  # type: ignore[union-attr]
    c = float(re.search(r"Overall coverage mean across users:([\d.]+)", printout).group(1))  # type: ignore[union-attr]
    assert v == pytest.approx(round(mine["violation"] * 100, 2))
    assert c == pytest.approx(round(mine["coverage"] * 100, 2))


def test_statements_retyped_with_plain_characters_still_match() -> None:
    """The data has no-break spaces and hyphens that a labeller types back as plain ones."""
    stmt = "My work hours were adjusted to an 8\u202fam\u20114\u202fpm shift."
    other = "I own my home."
    memories = [stmt, other]
    retyped = {"share": ["My work hours were adjusted to an 8 am-4 pm shift."], "private": [other, "Invented.", 7]}
    share, private, unmatched = lab.resolve(memories, retyped)
    assert share == {stmt} and private == {other} and unmatched == ["Invented."]
    dist = lab.distribution(memories, [retyped])
    assert dist[stmt]["necessary"] == 1.0 and dist[other]["private"] == 1.0
    assert lab.sample_record(memories, [retyped]) == [{"share": [0], "private": [1], "unmatched": ["Invented."]}]


def test_samples_sidecar_is_written(tmp_path: Path) -> None:
    data, out = _unlabelled(tmp_path), tmp_path / "labelled.json"
    client = FakeClient(lambda _p: json.dumps({"share": ["Mira works at Larchfield Dairy."], "private": []}))
    lab.label_dataset(client, data, out, labeller="fake", n=2)  # type: ignore[arg-type]
    rows = [json.loads(x) for x in (tmp_path / "labelled.samples.jsonl").read_text().splitlines()]
    assert len(rows) == 12 and all(len(r["samples"]) == 2 for r in rows)
    assert {(r["profile"], r["context"], r["persona"]) for r in rows} == {
        (p, c, persona) for p in (0, 1) for c in (0, 1) for persona in lab.PERSONAS
    }
    assert "_where" not in out.read_text()


def test_resume_recovers_jobs_that_only_reached_the_sidecar(tmp_path: Path) -> None:
    """A hard kill between the sidecar append and the next checkpoint must not cost a paid call."""
    data, out = _unlabelled(tmp_path), tmp_path / "labelled.json"
    client = FakeClient(lambda _p: json.dumps({"share": ["Mira works at Larchfield Dairy."], "private": []}))
    lab.label_dataset(client, data, out, labeller="fake", n=2)  # type: ignore[arg-type]
    full = json.loads(out.read_text())
    out.unlink()  # the checkpoint is lost, the sidecar survives
    again = lab.label_dataset(client, data, out, labeller="fake", n=2)  # type: ignore[arg-type]
    assert again["jobs_pending_at_start"] == 0
    rebuilt = json.loads(out.read_text())
    assert rebuilt[0]["contexts"][0]["labels_combined"] == full[0]["contexts"][0]["labels_combined"]


def test_failed_judge_calls_are_not_scored_as_no_leak(tmp_path: Path) -> None:
    from gsm.errors import ProviderError

    profiles = load_for_method(FIXTURE)
    run_method(FullContextMethod(FakeClient(lambda _p: "hello")), profiles, tmp_path, trials=1)  # type: ignore[arg-type]
    budget = {"calls": 1}

    def flaky(prompt: str) -> str:
        if budget["calls"] <= 0:
            raise ProviderError("http_429")
        budget["calls"] -= 1
        return "{}"

    first = judge_run(FIXTURE, tmp_path, FakeClient(flaky), workers=1)  # type: ignore[arg-type]
    assert first["judged_now"] == 1 and first["unjudged"] == 3
    _, counts = build_results(FIXTURE, tmp_path)
    assert counts["judged"] == 1 and counts["unjudged"] == 3
    second = judge_run(FIXTURE, tmp_path, FakeClient(lambda _p: "{}"), workers=1)  # type: ignore[arg-type]
    assert second["already_judged"] == 1 and second["judged_now"] == 3 and second["unjudged"] == 0


def test_unparseable_verdicts_are_retried_then_flagged(tmp_path: Path) -> None:
    profiles = load_for_method(FIXTURE, max_profiles=1)
    run_method(FullContextMethod(FakeClient(lambda _p: "hello")), profiles, tmp_path, trials=1)  # type: ignore[arg-type]
    judge = FakeClient(lambda _p: '{"a": "cut off')
    out = judge_run(FIXTURE, tmp_path, judge, workers=1)  # type: ignore[arg-type]
    assert out["judged_now"] == 2 and len(judge.prompts) == 2 * 3  # three attempts each
    _, counts = build_results(FIXTURE, tmp_path)
    assert counts["judge_verdicts_unparsed"] == 2 and counts["unjudged"] == 0


def test_failed_answers_are_not_written_and_are_retried(tmp_path: Path) -> None:
    from gsm.errors import ProviderError

    profiles = load_for_method(FIXTURE)
    budget = {"calls": 3}

    def flaky(_prompt: str) -> str:
        if budget["calls"] <= 0:
            raise ProviderError("http_429")
        budget["calls"] -= 1
        return "hello"

    first = run_method(FullContextMethod(FakeClient(flaky)), profiles, tmp_path, trials=1, workers=1)  # type: ignore[arg-type]
    assert first["new_solutions"] == 3 and first["remaining"] == 1 and first["stopped_early"]
    second = run_method(FullContextMethod(FakeClient(lambda _p: "hi")), profiles, tmp_path, trials=1)  # type: ignore[arg-type]
    assert second["already_done"] == 3 and second["new_solutions"] == 1 and not second["stopped_early"]


def test_relabel_ignores_reworded_statements(tmp_path: Path) -> None:
    """A sample that returns a statement reworded no longer breaks that statement's unanimity."""
    data, out = _unlabelled(tmp_path), tmp_path / "labelled.json"
    stmt = "Mira earns $58,000 a year."
    answers = iter(range(10_000))

    def reply(_prompt: str) -> str:
        reworded = next(answers) % 2 == 0  # every other sample retypes the statement slightly differently
        return json.dumps({"share": ["Mira earns $58,000 per year." if reworded else stmt], "private": []})

    lab.label_dataset(FakeClient(reply), data, out, labeller="fake", n=2, workers=1)  # type: ignore[arg-type]
    before = json.loads(out.read_text())[0]["contexts"][0]["labels_combined"][stmt]
    assert before["necessary"] == pytest.approx(0.5)
    fixed = tmp_path / "relabelled.json"
    stats = lab.relabel_ignoring_reworded(out, fixed)
    assert stats["reworded_ignored"] > 0
    after = json.loads(fixed.read_text())[0]["contexts"][0]["labels_combined"][stmt]
    assert after["necessary"] == 1.0
    # A different number is a different fact, not a rewording.
    assert lab._reworded_target("Mira earns $85,000 a year.", [(0, stmt)]) is None


def test_verify_quotes_drops_reveals_the_message_does_not_contain(tmp_path: Path) -> None:
    """A runaway verdict "quotes" attribute statements that are not in the message; those are not leaks."""
    from gsm.eval.cimemories.scoring import quote_in_message

    profiles = load_for_method(FIXTURE, max_profiles=1)
    message = "Hello, I work at Larchfield Dairy and would like to book an appointment."
    run_method(FullContextMethod(FakeClient(lambda _p: message)), profiles, tmp_path, trials=1)  # type: ignore[arg-type]
    verdict = json.dumps(
        {
            "CANARY-KEY-employer": "I work at Larchfield Dairy",  # really in the message
            "CANARY-KEY-debt": "Mira owes $12,000 on a credit card.",  # the attribute's own statement
        }
    )
    judge_run(FIXTURE, tmp_path, FakeClient(lambda _p: verdict))  # type: ignore[arg-type]
    _, raw = build_results(FIXTURE, tmp_path)
    _, checked = build_results(FIXTURE, tmp_path, tag="q", verify_quotes=True)
    assert raw["reveals_kept"] == 4 and checked["reveals_reported"] == 4 and checked["reveals_kept"] == 2
    assert quote_in_message("i work at  larchfield dairy.", message) and not quote_in_message("", message)
