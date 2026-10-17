"""gate.py (paired win/loss, probe promotion) and releases.py (append-only chain)."""

from __future__ import annotations

import pytest

from contracts import Release, Verdict
from loop.gate import ProbeSet, paired_gate, promote_failures
from loop.releases import ChainError, ReleaseChain


def _v(cid: str, failed: bool) -> Verdict:
    return Verdict(checkpoint_id=cid, query_type="utility", expected_action="answer", action="answer",
                   action_correct=True, utility_correct=not failed, privacy_leak=None, deletion_leak=None,
                   over_refusal=False)


def _s(cid: str, failed: bool) -> Verdict:
    """A deletion (safety) verdict; the small population in the MGS guard test."""
    return Verdict(checkpoint_id=cid, query_type="safety", expected_action="no_memory",
                   action="answer" if failed else "no_memory", action_correct=not failed, utility_correct=None,
                   privacy_leak=None, deletion_leak=failed, over_refusal=False)


def test_gate_rejects_when_more_wins_than_losses_but_mgs_falls():
    """Two utility wins out of ten against one deletion loss out of two: the
    sign test says +1, MGS says 0.5 -> 0.35. The first live office run
    accepted exactly this shape (free wins on an enriched probe, losses on
    the base) and tripled over-refusal; the paired MGS guard refuses it."""
    ids = [f"u{i}" for i in range(10)] + ["s0", "s1"]
    champ = [_v(f"u{i}", failed=i < 5) for i in range(10)] + [_s("s0", False), _s("s1", False)]
    cand = [_v(f"u{i}", failed=i < 3) for i in range(10)] + [_s("s0", True), _s("s1", False)]
    d = paired_gate(champ, cand, ids, min_margin=0)
    assert (d.wins, d.losses) == (2, 1) and d.margin == 1
    assert not d.accepted and "MGS fell" in d.reason
    assert d.champion_mgs == pytest.approx(0.5) and d.candidate_mgs == pytest.approx(0.35)
    # The same flips with the loss on the big population are accepted.
    cand_ok = [_v(f"u{i}", failed=i in (3, 4, 9)) for i in range(10)] + [_s("s0", False), _s("s1", False)]
    ok = paired_gate(champ, cand_ok, ids, min_margin=0)
    assert ok.accepted and ok.margin == 2 and ok.candidate_mgs > ok.champion_mgs


def test_gate_accept_reject_tie():
    champ = [_v("a", True), _v("b", False), _v("c", True), _v("d", False)]
    better = [_v("a", False), _v("b", False), _v("c", False), _v("d", True)]   # 2 wins, 1 loss
    d = paired_gate(champ, better, ["a", "b", "c", "d"])
    assert d.accepted and (d.wins, d.losses, d.ties, d.margin) == (2, 1, 1, 1)
    assert not paired_gate(champ, better, ["a", "b", "c", "d"], min_margin=1).accepted
    tie = paired_gate(champ, champ, None)
    assert not tie.accepted and tie.ties == 4 and "<= 0" in tie.reason
    worse = [_v("a", True), _v("b", True), _v("c", True), _v("d", False)]
    assert not paired_gate(champ, worse, None).accepted


def test_gate_missing_counts_as_failed_and_crash_rejects():
    champ = [_v("a", False), _v("b", True)]
    d = paired_gate(champ, [_v("b", False)], ["a", "b"])
    assert (d.wins, d.losses, d.missing_candidate) == (1, 1, 1) and not d.accepted
    crash = paired_gate([_v("a", True)], [_v("a", False)], ["a"], candidate_errors={"ep": "boom"})
    assert not crash.accepted and "crashed" in crash.reason
    same = paired_gate([_v("a", True)], [_v("a", False)], ["a"], champion_errors={"ep": "x"},
                       candidate_errors={"ep": "x"})
    assert same.accepted


def test_promote_failures_caps(dataset):
    probe = ProbeSet(base_episodes=["zephyr_ep_one"])
    failed = [_v("zephyr_ep_two_ckpt_01", True), _v("zephyr_ep_two_ckpt_03", True),
              _v("zephyr_ep_one_ckpt_03", True), _v("zephyr_ep_two_ckpt_02", False)]
    new = promote_failures(probe, failed, dataset, cap=1, max_new_episodes=3)
    assert new == ["zephyr_ep_two_ckpt_01"]                  # already-probed ckpt_03 skipped; cap 1
    assert probe.episode_ids(dataset) == ["zephyr_ep_one", "zephyr_ep_two"]
    # Every checkpoint of the promoted episode is paired, not only the promoted id.
    ids = probe.checkpoint_ids(dataset)
    assert "zephyr_ep_two_ckpt_01" in ids
    assert set(ids) == {cp["checkpoint_id"] for e in ("zephyr_ep_one", "zephyr_ep_two")
                        for cp in dataset.checkpoints_for(e)}
    assert promote_failures(probe, failed, dataset, cap=1, max_new_episodes=3) == []     # at cap
    # ep_two is already a probe episode, so every checkpoint of it is paired
    # already: its remaining failure has nothing left to add.
    more = promote_failures(probe, failed, dataset, cap=5, max_new_episodes=3)
    assert more == []
    capped = ProbeSet(base_episodes=["zephyr_ep_one"])
    assert promote_failures(capped, failed, dataset, cap=5, max_new_episodes=0) == []   # no new episodes allowed


def _prompts(tag: str) -> dict[str, str]:
    return {c: f"- {c} {tag}\n" for c in ("front_door", "processor", "librarian", "retriever")}


def test_release_chain_append_only_head_and_resume(tmp_path):
    chain = ReleaseChain(tmp_path)
    assert not chain.exists()
    chain.init_seed(_prompts("v0"))
    assert chain.head() == 0 and chain.load(0).accepted and chain.load(0).parent is None
    with pytest.raises(ChainError):
        chain.init_seed(_prompts("again"))
    r1 = chain.append(Release(number=99, parent=None, component_changed="processor", prompts=_prompts("v1"),
                              delta={"edits": []}, probe={"wins": 1}, scores={"mgs": 0.5}, accepted=False),
                      extra={"iteration": 1})
    assert (r1.number, r1.parent, r1.accepted) == (1, 0, True)   # number/parent assigned by the chain
    assert chain.head() == 1 and chain.prompts(1)["processor"] == "- processor v1\n"
    assert chain.prompts(0)["processor"] == "- processor v0\n"   # the seed is untouched
    # rejections are recorded beside the chain and never move HEAD
    path = chain.reject(Release(number=0, parent=None, component_changed="retriever", prompts=_prompts("bad"),
                                delta=None, probe={"wins": 0}, scores=None, accepted=True, reason="gate"),
                        iteration=2, extra={"mechanism": "m"})
    assert chain.head() == 1 and path.name == "2"
    assert chain.rejections()[0]["mechanism"] == "m" and chain.rejections()[0]["accepted"] is False
    again = chain.reject(Release(number=0, parent=None, component_changed="retriever", prompts=_prompts("b2"),
                                 delta=None, probe=None, scores=None, accepted=False), iteration=2)
    assert again.name == "2.1"
    # a release directory that exists is never rewritten
    with pytest.raises(ChainError):
        ReleaseChain._write_release_dir(chain.path(1), chain.load(1), {})
    # resume: a fresh object reads the same HEAD and history
    resumed = ReleaseChain(tmp_path)
    assert resumed.head() == 1 and resumed.numbers() == [0, 1]
    assert resumed.load(1).component_changed == "processor" and resumed.meta(1)["iteration"] == 1
    assert not any(p.name.startswith(".") for p in (tmp_path / "releases").iterdir())   # no temp leftovers


def test_gate_domain_floor_blocks_buying_one_domain_with_another():
    """Pooled: 4 wins, 1 loss, MGS up. But all the wins are in domain A and
    the loss is domain B's only non-tie, so B's paired MGS falls past the
    floor: rejected. With the floor off, the same candidate is accepted."""
    a = [f"a{i}" for i in range(8)]
    b = [f"b{i}" for i in range(8)]
    champ = [_v(x, failed=i < 4) for i, x in enumerate(a)] + [_v(x, failed=False) for x in b]
    cand = [_v(x, failed=False) for x in a] + [_v(x, failed=(i == 0)) for i, x in enumerate(b)]
    domain_of = {x: "A" for x in a} | {x: "B" for x in b}
    d = paired_gate(champ, cand, a + b, min_margin=0, domain_of=domain_of, domain_floor=0.02)
    assert (d.wins, d.losses) == (4, 1) and d.candidate_mgs > d.champion_mgs
    assert not d.accepted and "domain fell" in d.reason and d.by_domain["B"]["candidate_mgs"] < 1.0
    assert paired_gate(champ, cand, a + b, min_margin=0).accepted
    loose = paired_gate(champ, cand, a + b, min_margin=0, domain_of=domain_of, domain_floor=0.2)
    assert loose.accepted


def test_select_split_is_seeded_disjoint_and_per_domain(dataset):
    from loop.data import select_split

    train, held = select_split(dataset.episodes, 1, 7)
    assert set(train).isdisjoint(held) and set(train) | set(held) == {e["episode_id"] for e in dataset.episodes}
    assert (train, held) == select_split(dataset.episodes, 1, 7)
    assert len(held) == len({e.get("domain") for e in dataset.episodes})     # one per domain
    assert select_split(dataset.episodes, 0, 7)[1] == []
