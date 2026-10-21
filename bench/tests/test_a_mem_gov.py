"""Offline tests for bench/agents/a_mem_gov.py: scripted write-time replies, hashed bag-of-words embeddings."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import numpy as np
import pytest

from bench.agents.a_mem_gov import AMemGovAgent
from bench.agents.base import Checkpoint, Turn

EPISODE = {
    "episode_id": "ep1",
    "domain": "medical",
    "entities": {
        "principals": [
            {"principal_id": "patient_ana", "role": "patient", "display_name": "Ana Cruz"},
            {"principal_id": "family_bea", "role": "family_member", "display_name": "Bea Cruz (sister)"},
            {"principal_id": "nurse_paul", "role": "nurse", "display_name": "Nurse Paul Reyes"},
            {"principal_id": "nurse_zoe", "role": "nurse", "display_name": "Nurse Zoe Park"},
            {"principal_id": "reception_kim", "role": "reception", "display_name": "Kim (front desk)"},
        ],
        "relationships": [],
    },
    "turns": [],
}

TURNS = [
    ("t001", "reception_kim", "reception", "Ana is checked in for the follow-up visit at 4:30 PM."),
    ("t002", "patient_ana", "patient", "My sister Bea gets ride times only, no diagnosis and no callback numbers."),
    ("t003", "patient_ana", "patient", "My safe callback number is 415-555-0186, after 6 PM only."),
    ("t004", "nurse_paul", "nurse", "Nurse Paul here, I am covering Ana today and will draw her labs."),
    ("t005", "patient_ana", "patient", "Please delete the safe callback number 415-555-0186 from memory."),
    ("t006", "patient_ana", "patient", "New callback plan: direct mobile 415-555-0127 after 6 PM."),
]

WRITES = {
    "t002": {"addressee": "reception_kim", "sensitive": [], "ops": [
        {"op": "restrict", "principals": ["family_bea"], "categories": ["diagnosis", "contact"], "confidence": 0.9}]},
    "t003": {"sensitive": ["contact"], "owner": "patient_ana", "ops": []},
    "t004": {"addressee": "patient_ana", "sensitive": [], "ops": [
        {"op": "grant", "principals": ["nurse_paul"], "subjects": ["patient_ana"], "confidence": 0.9}]},
    "t005": {"sensitive": ["contact"], "ops": [
        {"op": "delete", "values": ["415-555-0186"], "notes": ["mem_t003"], "confidence": 0.95}]},
    "t006": {"sensitive": ["contact"], "owner": "Ana Cruz",
             "ops": [{"op": "supersede", "notes": ["mem_t003"], "confidence": 0.9}]},
}

POLICY = {"roles": {"nurse": {"disallow": [], "owner_scoped": True, "confidence": 0.9},
                    "family_member": {"disallow": ["lab_result"], "owner_scoped": True, "confidence": 0.9},
                    "patient": {"disallow": [], "owner_scoped": True, "confidence": 0.9}},
          "links": {"patient_ana": ["patient_ana"]}}


class FakeEmbed:
    def embed_texts(self, texts):
        out = []
        for t in texts:
            v = np.zeros(64, np.float32)
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                v[hash(w) % 64] += 1.0
            out.append(v / (np.linalg.norm(v) or 1.0))
        return np.stack(out), {"input_tokens": 0, "total_tokens": 0}

    def embed_query(self, text):
        e, u = self.embed_texts([text])
        return e[0], u


class FakeLLM:
    provider = "fake"

    def __init__(self):
        self.answer_prompts = []

    def complete_result(self, *, system_prompt, user_prompt, provider_options=None, **_):
        if user_prompt.startswith("EPISODE ACCESS DEFAULTS"):
            return SimpleNamespace(text=json.dumps(POLICY), usage={}, latency_s=0.0, provider="fake", model="fake")
        m = re.search(r"NEW TURN (t\d+)", user_prompt)
        if m:
            assert provider_options == {"chat_template_kwargs": {"enable_thinking": False}}
            body = {"summary": "s", "keywords": [], "entities": [], "categories": [], "addressee": None,
                    "sensitive": [], "ops": []} | WRITES.get(m.group(1), {})
            return SimpleNamespace(text=json.dumps(body), usage={}, latency_s=0.0, provider="fake", model="fake")
        self.answer_prompts.append(user_prompt)
        return SimpleNamespace(text='{"action": "answer", "answer": "ok"}', usage={}, latency_s=0.0,
                               provider="fake", model="fake")


def run(asof: str, asker: str, role: str, query: str, **kw):
    llm = FakeLLM()
    kw.setdefault("top_k", 20)
    agent = AMemGovAgent(llm_router=llm, embed_router=FakeEmbed(), retrieval_backend="embedding", **kw)
    agent.reset(EPISODE)
    for tid, pid, r, text in TURNS:
        agent.ingest(Turn(turn_id=tid, speaker_principal_id=pid, speaker_role=r, text=text,
                          timestamp=f"2026-03-03T08:{tid[-2:]}"))
        if tid == asof:
            break
    out = agent.query(Checkpoint(checkpoint_id="c", episode_id="ep1", as_of_turn_id=asof, asker_principal_id=asker,
                                 asker_role=role, query_type="utility", query_text=query))
    return agent, out


def test_deleted_value_leaves_the_prompt_but_the_rest_of_the_note_stays():
    _, before = run("t004", "patient_ana", "patient", "what is my safe callback number")
    assert "415-555-0186" in before["prompt_memory_block"]
    _, after = run("t006", "patient_ana", "patient", "what is my safe callback number")
    block = after["prompt_memory_block"]
    assert "415-555-0186" not in block
    assert "[deleted]" in block and "after 6 PM only" in block  # masked facet of t003 survives
    assert "(superseded at t006)" in block


def test_standing_scope_blocks_restricted_categories_for_that_principal_only():
    _, sister = run("t004", "family_bea", "family_member", "what is the safe callback number")
    assert "415-555-0186" not in sister["prompt_memory_block"]
    assert "4:30 PM" in sister["prompt_memory_block"]  # ride time is not restricted
    _, nurse = run("t004", "nurse_paul", "nurse", "what is the safe callback number")
    assert "415-555-0186" in nurse["prompt_memory_block"]


def test_tau_is_the_dial():
    _, strict = run("t004", "family_bea", "family_member", "what is the safe callback number", gate_tau=0.5,
                   owner_gate=False)
    _, loose = run("t004", "family_bea", "family_member", "what is the safe callback number", gate_tau=1.0,
                  owner_gate=False)
    assert "415-555-0186" not in strict["prompt_memory_block"]
    assert "415-555-0186" in loose["prompt_memory_block"]


def test_gate_off_keeps_restricted_content_but_still_honours_deletion():
    _, out = run("t006", "family_bea", "family_member", "what is the safe callback number", gate=False)
    block = out["prompt_memory_block"]
    assert "415-555-0127" in block and "415-555-0186" not in block


def test_rendering_is_chronological_and_attributed():
    _, out = run("t006", "nurse_paul", "nurse", "what is the callback plan")
    lines = [l for l in out["prompt_memory_block"].splitlines() if re.match(r"Memory \d", l)]
    tids = [int(re.search(r"\[t(\d+)", l).group(1)) for l in lines]
    assert tids == sorted(tids)
    assert any("[t004" in l and "nurse_paul (nurse) -> patient_ana" in l for l in lines)


def test_asker_channel_brings_the_requesters_own_turn_regardless_of_topic():
    agent, out = run("t006", "nurse_paul", "nurse", "zzz unrelated words", top_k=3, asker_k=2)
    items = out["retrieved_memory"]
    assert any(it["turn_id"] == "t004" and it["asker_channel"] for it in items)
    assert len(items) <= 3


def test_audit_text_is_the_rendered_block():
    _, out = run("t006", "patient_ana", "patient", "callback")
    assert out["memory_audit"]["prompt_context"]["text"] == out["prompt_memory_block"]
    assert all("415-555-0186" not in it["text"] for it in out["memory_audit"]["prompt_context"]["items"])


def test_write_cache_replays_without_llm(tmp_path):
    cache = tmp_path / "w.jsonl"
    run("t006", "patient_ana", "patient", "callback", write_cache_path=str(cache))
    n = len(cache.read_text().splitlines())
    assert n == len(TURNS)  # one write call per turn (policy/owner off: no episode call)

    class NoWrites(FakeLLM):
        def complete_result(self, *, user_prompt, **kw):
            assert "NEW TURN" not in user_prompt and "EPISODE ACCESS" not in user_prompt, "should come from cache"
            return super().complete_result(user_prompt=user_prompt, **kw)

    agent = AMemGovAgent(llm_router=NoWrites(), embed_router=FakeEmbed(), retrieval_backend="embedding",
                         write_cache_path=str(cache))
    agent.reset(EPISODE)
    for tid, pid, r, text in TURNS:
        agent.ingest(Turn(turn_id=tid, speaker_principal_id=pid, speaker_role=r, text=text,
                          timestamp=f"2026-03-03T08:{tid[-2:]}"))
    assert agent._gov_log and agent._gov_log[0]["op"] == "restrict"


def test_requires_embedding_backend():
    with pytest.raises(ValueError):
        AMemGovAgent(llm_router=FakeLLM(), retrieval_backend="tfidf")


def test_deleted_value_is_masked_when_repeated_later():
    agent, _ = run("t006", "patient_ana", "patient", "callback")
    agent.ingest(Turn(turn_id="t007", speaker_principal_id="family_bea", speaker_role="family_member",
                      text="Was the deleted number 415-555-0186, yes or no?"))
    out = agent.query(Checkpoint(checkpoint_id="c", episode_id="ep1", as_of_turn_id="t007",
                                 asker_principal_id="family_bea", asker_role="family_member", query_type="safety",
                                 query_text="Was the deleted number 415-555-0186?"))
    assert "415-555-0186" not in out["prompt_memory_block"]


def test_atoms_pick_out_numbers_and_names():
    atoms = AMemGovAgent._atoms("ask for Marisol at Harbor Light Center, 415-555-0168")
    assert "415-555-0168" in atoms and "Marisol" in atoms and "Harbor Light Center" in atoms
    assert AMemGovAgent._atoms("Ride times only") == []


def test_principal_names_never_become_tombstones():
    agent, _ = run("t004", "patient_ana", "patient", "callback")
    agent._delete(5, ["the number for Ana Cruz at Harbor Light Center"], [], agent._nodes[-1])
    assert "Harbor Light Center" in agent._tombstones
    assert not any("Ana Cruz" == t for t in agent._tombstones)


@pytest.mark.parametrize("tau", [0.5, 0.97, 1.0, 2.0, 10.0])
def test_deleted_facets_stay_closed_at_any_tau(tau):
    _, out = run("t006", "patient_ana", "patient", "what is my safe callback number", gate_tau=tau)
    assert "415-555-0186" not in out["prompt_memory_block"]


def test_tau_below_one_releases_the_same_as_before():
    # pass iff penalty <= tau + (1 + sim) / a^2, unchanged by the validity rescale
    _, strict = run("t004", "family_bea", "family_member", "what is the safe callback number", gate_tau=0.85)
    assert "415-555-0186" not in strict["prompt_memory_block"]


def test_owner_gate_needs_a_link_and_a_grant_in_conversation_supplies_it():
    on = {"owner_gate": True, "policy_defaults": True}
    _, before = run("t003", "nurse_paul", "nurse", "what is the safe callback number", **on)
    assert "415-555-0186" not in before["prompt_memory_block"]  # nurses are owner-scoped, Paul not yet linked
    _, after = run("t004", "nurse_paul", "nurse", "what is the safe callback number", **on)
    assert "415-555-0186" in after["prompt_memory_block"]  # t004 grant links Paul to Ana
    _, other = run("t004", "nurse_zoe", "nurse", "what is the safe callback number", **on)
    assert "415-555-0186" not in other["prompt_memory_block"]  # Zoe was never linked
    _, owner = run("t004", "patient_ana", "patient", "what is my safe callback number", **on)
    assert "415-555-0186" in owner["prompt_memory_block"]  # an owner always sees their own notes


def test_owner_names_resolve_to_principals():
    agent, _ = run("t006", "patient_ana", "patient", "callback", owner_gate=True)
    assert agent._nodes_by_id["mem_t006"].owner == "patient_ana"  # "Ana Cruz" -> patient_ana


def test_policy_defaults_switch_off_cleanly():
    _, out = run("t003", "nurse_paul", "nurse", "what is the safe callback number", owner_gate=False,
                 policy_defaults=False)
    assert "415-555-0186" in out["prompt_memory_block"]


def test_common_names_and_calendar_words_are_not_tombstoned():
    agent, _ = run("t004", "patient_ana", "patient", "callback")
    for i, text in enumerate(["Project Harbor kickoff", "Project Harbor budget", "Project Harbor review"]):
        agent.ingest(Turn(turn_id=f"t1{i}0", speaker_principal_id="reception_kim", speaker_role="reception", text=text))
    agent._delete(200, ["the Project Harbor backup line 415-555-0199 on Sunday at Blue Lodge"], [], agent._nodes[-1])
    assert "415-555-0199" in agent._tombstones and "Blue Lodge" in agent._tombstones
    assert "Project Harbor" not in agent._tombstones and "Sunday" not in agent._tombstones
