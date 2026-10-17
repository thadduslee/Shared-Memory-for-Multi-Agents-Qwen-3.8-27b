"""curator.py edit application and lint.py vocabulary checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts import PromptEdit
from loop.curator import apply_edits, curate_text
from loop.lint import Vocabulary, dataset_vocabulary, find_dataset_words, lint_prompt
from loop.reflector import Diagnosis, parse_diagnosis

PROMPT = "# Title\n\nIntro line.\n\n- first rule\n- second rule\n  - nested rule\n\nOutro.\n"


def test_add_replace_remove():
    new, applied, rejected = apply_edits(PROMPT, [
        PromptEdit("add", None, "appended rule"),                     # gets "- ", lands after the last bullet
        PromptEdit("add", "- first rule", "- after first"),
        PromptEdit("replace", "- second rule", "- second rule, sharper"),
    ], max_edits=3)
    assert not rejected and len(applied) == 3
    assert new.splitlines() == ["# Title", "", "Intro line.", "", "- first rule", "- after first",
                                "- second rule, sharper", "  - nested rule", "- appended rule", "", "Outro."]
    removed, applied, rejected = apply_edits(new, [PromptEdit("remove", "  - nested rule")], max_edits=3)
    assert "nested rule" not in removed and len(applied) == 1 and not rejected
    assert removed.endswith("\n")


def test_rejected_edits_and_cap():
    edits = [
        PromptEdit("replace", "- no such line", "- x"),           # anchor miss
        PromptEdit("replace", "Intro line.", "- x"),              # not a bullet
        PromptEdit("add", None, "- has {brace}"),                 # braces
        PromptEdit("add", None, "- first rule"),                  # duplicate
    ]
    new, applied, rejected = apply_edits(PROMPT, edits, max_edits=10)
    assert new == PROMPT and not applied
    assert [r["reason"] for r in rejected] == ["anchor not found", "replace anchor is not a bullet line",
                                               "text may not contain braces", "duplicate of an existing line"]
    capped, applied, rejected = apply_edits(PROMPT, [PromptEdit("add", None, f"- rule {i}") for i in range(5)],
                                            max_edits=3)
    assert len(applied) == 3 and [r["reason"] for r in rejected] == ["over MAX_EDITS=3"] * 2
    amb, applied, rejected = apply_edits("- a\n- a\n", [PromptEdit("remove", "- a")], max_edits=3)
    assert not applied and "ambiguous" in rejected[0]["reason"]
    # a line-number prefix copied from the numbered view is tolerated
    ok, applied, _ = apply_edits(PROMPT, [PromptEdit("remove", "  5: - first rule")], max_edits=3)
    assert len(applied) == 1 and "- first rule" not in ok


def test_curate_text_end_to_end(loop_env):
    cfg, _ = loop_env
    vocab = Vocabulary(roles=frozenset({"role_k"}))
    diag = Diagnosis(component="retriever", mechanism="m")
    reply = "Here you go:\n```json\n" + json.dumps(
        [{"op": "replace", "anchor": "- second rule", "text": "- second rule, but checked"},
         {"op": "add", "anchor": "- missing", "text": "- y"}]) + "\n```"
    cur = curate_text(diag, PROMPT, reply, vocab=vocab, cfg=cfg)
    assert cur.ok and len(cur.delta.edits) == 1 and cur.rejected_edits[0]["reason"] == "anchor not found"
    bad = curate_text(diag, PROMPT, json.dumps([{"op": "add", "anchor": None, "text": "- ask role_k first"}]),
                      vocab=vocab, cfg=cfg)
    assert not bad.ok and "dataset vocabulary" in bad.reason and bad.lint.new_dataset_words == ["role_k"]
    none = curate_text(diag, PROMPT, "no json here", vocab=vocab, cfg=cfg)
    assert not none.ok and "no JSON" in none.reason


def test_lint_size_structure_and_preexisting_words():
    vocab = Vocabulary(roles=frozenset({"product_lead"}), principal_ids=frozenset({"p_qq"}))
    assert find_dataset_words("Ask the product lead, or PRODUCT_LEAD, never p_qq.", vocab) == \
        ["p_qq", "product lead", "product_lead"]
    assert find_dataset_words("p_qqx and xproduct_lead", vocab) == []          # whole words only
    old = "- mentions p_qq already\n"
    res = lint_prompt(old + "- new rule\n", vocab=vocab, max_chars=1000, previous=old)
    assert res.ok and res.dataset_words == ["p_qq"] and res.new_dataset_words == []
    assert not lint_prompt("- " + "x" * 50 + "\n", vocab=vocab, max_chars=10).ok
    assert not lint_prompt("no bullets\n", vocab=vocab, max_chars=1000).ok
    assert not lint_prompt("- a {x}\n", vocab=vocab, max_chars=1000, previous="- a\n").ok


def test_lint_collects_synthetic_vocabulary(data_root):
    vocab = dataset_vocabulary(data_root)
    assert {"role_k", "role_l"} <= vocab.roles and "rel_watch" in vocab.relationship_types
    assert "Kilo Unit" in vocab.display_names and "zephyr" in vocab.domains
    role = sorted(vocab.roles)[0]
    res = lint_prompt(f"- Defer to the {role} when unsure.\n", vocab=vocab, max_chars=1000, previous="- a\n")
    assert not res.ok and res.new_dataset_words == [role]


def test_lint_rejects_real_benchmark_word_and_optimiser_prompts_are_clean(loop_env):
    cfg, _ = loop_env
    root = cfg.gatemem_repo / "bench" / "data"
    if not any(root.glob("*/episodes.jsonl")):
        pytest.skip("bench/data not present")
    vocab = dataset_vocabulary(root)
    for group in (vocab.roles, vocab.relationship_types, vocab.principal_ids):
        word = sorted(group)[0]          # picked at runtime; no dataset word is written in this file
        res = lint_prompt(f"- Treat {word} specially.\n", vocab=vocab, max_chars=12000, previous="- a\n")
        assert not res.ok and res.new_dataset_words
    loop_prompts = Path(__file__).resolve().parents[2] / "loop" / "prompts"
    for p in loop_prompts.glob("*.md"):
        assert find_dataset_words(p.read_text(encoding="utf-8"), vocab) == [], p.name


def test_parse_diagnosis():
    text = ("Example: ```json\n{\"component\": \"nope\"}\n```\nReal one:\n```json\n"
            '{"component": "processor", "mechanism": "emit grants", "evidence_ids": ["c1"], '
            '"expected_effect": {"U": "+", "Z": "+"}}\n```')
    diag, problem = parse_diagnosis(text)
    assert problem == "" and diag.component == "processor" and diag.expected_effect == {"U": "+"}
    bad, problem = parse_diagnosis('```json\n{"component": "everything", "mechanism": "x"}\n```')
    assert bad is None and "not one of" in problem
