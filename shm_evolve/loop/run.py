"""The outer loop, end to end.

    python -m loop.run --domain office --iterations 20 [--fake-llm] [--episodes N]
                       [--probe-only] [--run-dir runs/<id>] [--agent pkg.mod:Class]

One iteration (DESIGN.md §3):

    champion eval  (cached per release; the accepted candidate's full eval IS
                    the next champion's) -> judge -> attribution -> promote
    reflector      one component, one mechanism
    curator        a delta on that component's prompt, linted
    probe          champion and candidate on the probe set (champion results
                    reused from its full eval when the episodes overlap)
    gate           paired win/loss -> accept: full eval, release r<n+1>
                                   -> reject: rejected/<iter>, champion stays

Every stage writes its artefacts under `runs/<run>/iter_<n>/`, one log line
with its numbers, and the iteration appends one row to `history.jsonl`.

RESUMING. A run dir with a release chain is resumed: HEAD names the champion,
`history.jsonl` says which iterations finished, `run.json` pins the domains,
the evaluation episodes and the agent so a resumed run cannot silently change
what it measures, and `evals.json` maps each release to its cached full eval.
An iteration directory without a history row is moved aside and redone.
`--iterations` is the TOTAL for the run, so re-issuing the same command after
a crash finishes the job.

ONE EVENT LOOP for the whole run: the HTTP pool and the route semaphores
belong to it, and the answerer's fan-out across episodes is only bounded if
every render shares the same semaphore.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from contracts import AttributedFailure, Release
from loop import config as config_mod
from loop.attribution import write_attribution
from loop.config import Config
from loop.curator import curate
from loop.data import Dataset, list_domains, load_dataset, select_episodes, select_probe, select_split
from loop.evaluate import EvalResult, evaluate, load_eval
from loop.gate import ProbeSet, _mgs_over, paired_gate, promote_failures
from loop.jsonx import append_jsonl, read_json, read_jsonl, write_json
from loop.judge import judge, load_verdicts, scores_line
from loop.lint import dataset_vocabulary
from loop.llm_client import LLMClientProtocol, get_llm_client
from loop.reflector import reflect
from loop.releases import ReleaseChain, read_prompts, write_prompts

log = logging.getLogger("loop")


class Run:
    def __init__(self, run_dir: Path, cfg: Config, client: LLMClientProtocol, dataset: Dataset,
                 eval_episodes: list[str], probe: ProbeSet, agent_path: str, probe_only: bool,
                 heldout_episodes: list[str] | None = None) -> None:
        self.dir = run_dir
        self.cfg = cfg
        self.client = client
        self.dataset = dataset
        self.eval_episodes = eval_episodes
        # Scored for every head release, never attributed, probed or promoted.
        self.heldout_episodes = list(heldout_episodes or [])
        domain_by_episode = {ep["episode_id"]: str(ep.get("domain") or "") for ep in dataset.episodes}
        self.domain_of = {cid: domain_by_episode.get(cp.get("episode_id"), "")
                          for cid, cp in dataset.checkpoints_by_id.items()}
        self.probe = probe
        self.agent_path = agent_path
        self.probe_only = probe_only
        self.chain = ReleaseChain(run_dir)
        self.judge_cache = run_dir / "judge_cache.jsonl"
        # Lint against the benchmark's vocabulary AND the run's own data root
        # (identical for a real run; the test's synthetic data otherwise).
        vocab = dataset_vocabulary(cfg.gatemem_repo / "bench" / "data")
        if Path(cfg.data_root).resolve() != (cfg.gatemem_repo / "bench" / "data").resolve():
            vocab = vocab.merged(dataset_vocabulary(cfg.data_root))
        self.vocab = vocab

    # ------------------------------------------------------------ helpers

    def _evals_index(self) -> dict[str, str]:
        return read_json(self.dir / "evals.json", {}) or {}

    def _set_eval(self, release: int, path: Path) -> None:
        idx = self._evals_index()
        idx[str(release)] = str(path.relative_to(self.dir))
        write_json(self.dir / "evals.json", idx)

    def history(self) -> list[dict[str, Any]]:
        return read_jsonl(self.dir / "history.jsonl")

    async def _eval_and_judge(self, episodes: list[str], prompts_dir: Path, out_dir: Path, label: str,
                              reuse_from: list[Path] | None = None) -> tuple[EvalResult, list, Any]:
        res = await evaluate(self.dataset.subset(episodes), episodes, prompts_dir, out_dir, client=self.client,
                             cfg=self.cfg, agent_path=self.agent_path, reuse_from=reuse_from, label=label)
        verdicts, scores = await judge(res.predictions, self.dataset.subset(episodes), out_dir,
                                       client=self.client, cfg=self.cfg, cache_path=self.judge_cache)
        rep = res.report
        budget_refused = int((rep.get("memory_llm_total") or {}).get("refused_over_budget") or 0)
        log.info("%s: %d ep (%d reused), %d cp, %.1fs | %s | %d failed%s%s%s", label, rep["n_episodes"],
                 rep["n_reused_episodes"], rep["n_checkpoints"], rep["wall_s"], scores_line(scores),
                 sum(1 for v in verdicts if v.failed),
                 f" | render degraded {rep['render']['n_degraded']}/{rep['render']['n']}"
                 if rep["render"]["n_degraded"] else "",
                 f" | EPISODE ERRORS {len(res.episode_errors)}" if res.episode_errors else "",
                 f" | budget-refused={budget_refused}" if budget_refused else "")
        return res, verdicts, scores

    async def champion_eval(self, it: int, head: int, iter_dir: Path) -> tuple[Path, list, Any]:
        cached = self._evals_index().get(str(head))
        if cached and (self.dir / cached / "verdicts.jsonl").is_file() and (self.dir / cached / "attribution.json").is_file():
            path = self.dir / cached
            verdicts = load_verdicts(path / "verdicts.jsonl")
            scores = read_json(path / "scores.json", {})
            log.info("iter %d | champion r%d eval: cached at %s | %s", it, head, cached, scores_line(scores))
            return path, verdicts, scores
        path = iter_dir / "champion_eval"
        episodes = self.probe.episode_ids(self.dataset) if self.probe_only else self.eval_episodes
        # An earlier attempt at this head -- including one moved aside as
        # unfinished -- may hold finished episodes under the same prompts
        # hash; a resumed run reuses them instead of paying for them again.
        prior = sorted(p for p in self.dir.glob("iter_*/champion_eval") if p != path) + \
            sorted(self.dir.glob("iter_*.aborted.*/champion_eval"))
        res, verdicts, scores = await self._eval_and_judge(
            episodes, self.chain.prompts_dir(head), path, f"iter {it} | champion r{head} eval",
            reuse_from=prior)
        write_attribution(path, verdicts, res.predictions, res.traces)
        self._set_eval(head, path)
        return path, verdicts, scores.to_dict()

    def _promote(self, it: int, verdicts: list) -> None:
        new = promote_failures(self.probe, verdicts, self.dataset, cap=self.cfg.probe_promoted_max,
                               max_new_episodes=self.cfg.probe_promoted_episodes_max,
                               eligible_episodes=self.eval_episodes)
        if new:
            self.probe.save(self.dir / "probe_set.json")
            log.info("iter %d | probe: promoted %d champion-failed checkpoint(s) (%d promoted, %d episodes)", it,
                     len(new), len(self.probe.promoted), len(self.probe.episode_ids(self.dataset)))

    def _record(self, row: dict[str, Any]) -> None:
        append_jsonl(self.dir / "history.jsonl", row)

    def _reject(self, it: int, head: int, component: str | None, prompts: dict[str, str], delta: Any,
                probe: Any, reason: str, mechanism: str = "", scores: Any = None) -> None:
        rel = Release(number=it, parent=head, component_changed=component, prompts=prompts, delta=delta,
                      probe=probe, scores=scores, accepted=False, reason=reason)
        path = self.chain.reject(rel, iteration=it, extra={"mechanism": mechanism, "component": component})
        log.info("iter %d | REJECTED (%s) -> %s | champion stays r%d", it, reason, path.relative_to(self.dir), head)

    # ------------------------------------------------------------ one iteration

    async def iteration(self, it: int) -> dict[str, Any]:
        t0 = time.monotonic()
        iter_dir = self.dir / f"iter_{it}"
        if iter_dir.exists():
            aside = self.dir / f"iter_{it}.aborted.{int(time.time())}"
            iter_dir.rename(aside)
            log.warning("iter %d | an unfinished attempt was moved aside to %s", it, aside.name)
        iter_dir.mkdir(parents=True)
        head = self.chain.head()
        champion_prompts = self.chain.prompts(head)
        row: dict[str, Any] = {"iteration": it, "head_before": head, "head_after": head, "accepted": False,
                               "component": None, "mechanism": "", "reason": ""}

        # 1. champion: eval, judge, attribution, promotion
        champ_path, champ_verdicts, champ_scores = await self.champion_eval(it, head, iter_dir)
        row["champion"] = champ_scores
        row["champion_eval"] = str(champ_path.relative_to(self.dir))
        self._promote(it, champ_verdicts)
        row["champion_heldout"] = await self._ensure_heldout(it, head)
        attribution = read_json(champ_path / "attribution.json", {}) or {}
        failures_raw = read_jsonl(champ_path / "failures.jsonl")
        failures = [AttributedFailure(**f) for f in failures_raw]
        top = list((attribution.get("by_component_op_outcome") or {}).items())[:4]
        sat = (attribution.get("by_requester_role") or {}).get("saturated_groups") or []
        log.info("iter %d | attribution: %d failures | %s%s", it, len(failures),
                 ", ".join(f"{k}={v}" for k, v in top) or "-", f" | saturated groups {sat}" if sat else "")

        # 2. reflector
        rejections = self.chain.rejections(self.cfg.reflector_rejections_shown)
        diag, problem = await reflect(
            client=self.client, attribution=attribution, failures=failures,
            predictions=load_eval(champ_path).predictions, visible=self.dataset.visible_by_id(),
            prompts=champion_prompts, history=self.history(), rejections=rejections,
            out_dir=iter_dir / "reflector", cfg=self.cfg)
        if diag is None:
            row["reason"] = f"reflector: {problem}"
            self._reject(it, head, None, champion_prompts, None, None, row["reason"])
            return self._finish(row, t0)
        row["component"], row["mechanism"] = diag.component, diag.mechanism
        log.info("iter %d | reflector -> %s: %s", it, diag.component, diag.mechanism[:140])

        # 3. curator
        cur = await curate(client=self.client, diag=diag, prompt=champion_prompts[diag.component],
                           vocab=self.vocab, out_dir=iter_dir / "curator", cfg=self.cfg)
        row["edits_applied"] = len(cur.delta.edits)
        row["edits_rejected"] = len(cur.rejected_edits)
        log.info("iter %d | curator: %d applied, %d rejected | lint %s (%d chars)", it, len(cur.delta.edits),
                 len(cur.rejected_edits), "ok" if cur.lint and cur.lint.ok else
                 f"FAIL {cur.lint.problems if cur.lint else ''}", len(cur.new_prompt))
        candidate_prompts = dict(champion_prompts) | {diag.component: cur.new_prompt}
        if not cur.ok:
            row["reason"] = f"curator: {cur.reason}"
            self._reject(it, head, diag.component, candidate_prompts, cur.to_dict(), None, row["reason"],
                         diag.mechanism)
            return self._finish(row, t0)
        cand_dir = iter_dir / "candidate_prompts"
        write_prompts(cand_dir, candidate_prompts)

        # 4. probe, both sides
        probe_eps = self.probe.episode_ids(self.dataset)
        probe_ids = self.probe.checkpoint_ids(self.dataset)
        champ_probe, champ_pv, champ_ps = await self._eval_and_judge(
            probe_eps, self.chain.prompts_dir(head), iter_dir / "champion_probe",
            f"iter {it} | probe champion r{head}", reuse_from=[champ_path])
        cand_probe, cand_pv, cand_ps = await self._eval_and_judge(
            probe_eps, cand_dir, iter_dir / "candidate_probe", f"iter {it} | probe candidate")
        decision = paired_gate(champ_pv, cand_pv, probe_ids, min_margin=self.cfg.min_win_margin,
                               champion_errors=champ_probe.episode_errors, candidate_errors=cand_probe.episode_errors,
                               domain_of=self.domain_of if len(self.dataset.domains) > 1 else None,
                               domain_floor=self.cfg.gate_domain_floor)
        probe_doc = decision.to_dict() | {"probe_episodes": probe_eps, "n_probe_checkpoints": len(probe_ids),
                                          "champion": champ_ps.to_dict(), "candidate": cand_ps.to_dict()}
        write_json(iter_dir / "gate.json", probe_doc)
        row.update(wins=decision.wins, losses=decision.losses, ties=decision.ties,
                   champion_probe=champ_ps.to_dict(), candidate_probe=cand_ps.to_dict())
        log.info("iter %d | gate: %d paired | wins %d losses %d ties %d | %s (%s)", it, decision.n_pairs,
                 decision.wins, decision.losses, decision.ties, "ACCEPT" if decision.accepted else "REJECT",
                 decision.reason)
        probe_summary = {k: v for k, v in probe_doc.items() if k != "pairs"} | {
            "pairs": [p for p in decision.pairs if p["outcome"] != "tie"]}

        # 5. accept -> full eval + release; reject -> rejected/<it>
        if not decision.accepted:
            row["reason"] = f"gate: {decision.reason}"
            self._reject(it, head, diag.component, candidate_prompts, cur.delta.to_dict(), probe_summary,
                         row["reason"], diag.mechanism, cand_ps.to_dict())
            return self._finish(row, t0)

        if self.probe_only:
            full_path, full_scores = iter_dir / "candidate_probe", cand_ps
            write_attribution(full_path, cand_pv, cand_probe.predictions, cand_probe.traces)
            full_verdicts = cand_pv
        else:
            full_path = iter_dir / "candidate_full"
            res, full_verdicts, full_scores = await self._eval_and_judge(
                self.eval_episodes, cand_dir, full_path, f"iter {it} | candidate full", reuse_from=[iter_dir / "candidate_probe"])
            write_attribution(full_path, full_verdicts, res.predictions, res.traces)
        rel = Release(number=-1, parent=head, component_changed=diag.component, prompts=candidate_prompts,
                      delta=cur.delta.to_dict(), probe=probe_summary, scores=full_scores.to_dict(), accepted=True,
                      reason=decision.reason)
        rel = self.chain.append(rel, extra={"iteration": it, "mechanism": diag.mechanism,
                                            "eval_dir": str(full_path.relative_to(self.dir))})
        self._set_eval(rel.number, full_path)
        row.update(accepted=True, head_after=rel.number, reason=f"gate: {decision.reason}",
                   candidate_full=full_scores.to_dict())
        log.info("iter %d | release r%d <- r%d (%s) | full %s", it, rel.number, head, diag.component,
                 scores_line(full_scores))
        self._promote(it, full_verdicts)
        row["heldout"] = await self._ensure_heldout(it, rel.number)
        return self._finish(row, t0)

    # ------------------------------------------------------------ held-out

    async def _ensure_heldout(self, it: int, release: int) -> dict[str, Any] | None:
        """Score release `release` on the held-out episodes once, into
        releases/<n>/heldout_scores.json (pooled and per domain). Nothing in
        the loop reads it: it exists so the number next to the baselines is
        one the optimiser never saw."""
        if not self.heldout_episodes:
            return None
        out = self.chain.path(release) / "heldout_scores.json"
        cached = read_json(out, None)
        if isinstance(cached, dict):
            return cached
        path = self.dir / f"heldout_r{release}"
        # evaluate() redoes any episode already under its output directory
        # unless it comes from `reuse_from`: a held-out scoring interrupted
        # half-way is moved aside and its finished episodes reused.
        prior: list[Path] = []
        if path.exists():
            aside = path.with_name(f"{path.name}.partial.{int(time.time())}")
            path.rename(aside)
            prior.append(aside)
        _res, verdicts, scores = await self._eval_and_judge(
            self.heldout_episodes, self.chain.prompts_dir(release), path, f"iter {it} | held-out r{release}",
            reuse_from=prior)
        by_id = {v.checkpoint_id: v for v in verdicts}
        doc = scores.to_dict() | {"n_episodes": len(self.heldout_episodes), "eval_dir": str(path.relative_to(self.dir)),
                                  "by_domain": {}}
        for dom in sorted(set(self.domain_of.values()) - {""}):
            ids = [cid for cid in by_id if self.domain_of.get(cid) == dom]
            if ids:
                doc["by_domain"][dom] = {"mgs": round(_mgs_over(by_id, ids), 4), "n": len(ids)}
        write_json(out, doc)
        log.info("iter %d | held-out r%d: %s | by domain %s", it, release, scores_line(scores),
                 {d: v["mgs"] for d, v in doc["by_domain"].items()})
        return doc

    def _finish(self, row: dict[str, Any], t0: float) -> dict[str, Any]:
        row["elapsed_s"] = round(time.monotonic() - t0, 2)
        # Cumulative for the process (answerer, judge, reflector, curator): diff
        # consecutive rows for one iteration's spend.
        row["llm_usage_cumulative"] = dict(getattr(self.client, "usage_total", {}) or {})
        row["head_after"] = self.chain.head()
        self._record(row)
        return row


# ---------------------------------------------------------------- setup


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m loop.run", description=__doc__.split("\n\n")[0])
    p.add_argument("--domain", action="append", help="GateMem domain (repeatable, or 'all'); default office")
    p.add_argument("--iterations", type=int, default=1, help="TOTAL iterations for the run (resume-aware)")
    p.add_argument("--fake-llm", action="store_true", help="deterministic fakes for every model call")
    p.add_argument("--episodes", type=int, default=None, help="evaluate N episodes per domain (seeded)")
    p.add_argument("--probe-only", action="store_true", help="evaluate only the probe set; no full runs")
    p.add_argument("--run-dir", type=Path, default=None, help="run directory (resumed if it has a chain)")
    p.add_argument("--run-id", default=None, help="name under RUNS_DIR when --run-dir is not given")
    p.add_argument("--agent", default=None, help="agent class import path, pkg.module:Class")
    p.add_argument("--seed-prompts", type=Path, default=None, help="directory of the four seed prompts")
    p.add_argument("--data-root", type=Path, default=None, help="bench data root (default GATEMEM_REPO/bench/data)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S", force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _init_run(args: argparse.Namespace, cfg: Config) -> tuple[Path, dict[str, Any]]:
    run_dir = args.run_dir or (cfg.runs_dir / (args.run_id or time.strftime("run-%Y%m%d-%H%M%S")))
    run_dir = Path(run_dir).resolve()
    meta_path = run_dir / "run.json"
    meta = read_json(meta_path)
    if isinstance(meta, dict):
        return run_dir, meta
    domains = args.domain or ["office"]
    if domains == ["all"]:
        domains = list_domains(cfg.data_root)
    meta = {"domains": domains, "agent_path": args.agent or cfg.agent_path, "episodes_per_domain": args.episodes,
            "probe_only": bool(args.probe_only), "fake_llm": cfg.fake_llm, "data_root": str(cfg.data_root),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "probe_seed": cfg.probe_seed}
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(meta_path, meta)
    return run_dir, meta


async def run_loop(args: argparse.Namespace) -> Path:
    if args.fake_llm:
        os.environ["SHM_FAKE_LLM"] = "1"
    if args.data_root:
        os.environ["SHM_DATA_ROOT"] = str(Path(args.data_root).resolve())
    if args.seed_prompts:
        os.environ["SHM_SEED_PROMPTS_DIR"] = str(Path(args.seed_prompts).resolve())
    cfg = config_mod.reload()
    if cfg.fake_llm:
        from loop.fake_llm import install

        install()
        cfg = config_mod.get()
    client = get_llm_client()
    run_dir, meta = _init_run(args, cfg)
    if meta.get("data_root") and Path(meta["data_root"]) != Path(cfg.data_root):
        log.warning("run.json pins data_root=%s; using it over %s", meta["data_root"], cfg.data_root)
        os.environ["SHM_DATA_ROOT"] = meta["data_root"]
        cfg = config_mod.reload()
    dataset = load_dataset(cfg.data_root, list(meta["domains"]))
    # The split is decided once, when the run is created, and pinned in
    # run.json: a resumed run must not reshuffle which episodes the loop
    # was allowed to see. An explicit --episodes N (a smoke run) has no
    # held-out set.
    if meta.get("eval_episodes"):
        eval_episodes = list(meta["eval_episodes"])
        heldout_episodes = list(meta.get("heldout_episodes") or [])
    elif cfg.heldout_episodes_per_domain > 0 and not meta.get("episodes_per_domain"):
        eval_episodes, heldout_episodes = select_split(dataset.episodes, cfg.heldout_episodes_per_domain,
                                                       cfg.probe_seed)
    else:
        eval_episodes = select_episodes(dataset.episodes, meta.get("episodes_per_domain"), cfg.probe_seed)
        heldout_episodes = []
    if "heldout_episodes" not in meta:
        meta["heldout_episodes"] = heldout_episodes
        write_json(run_dir / "run.json", meta)
    probe = ProbeSet.load(run_dir / "probe_set.json")
    if probe is None:
        pool = [ep for ep in dataset.episodes if ep["episode_id"] in set(eval_episodes)]
        probe = ProbeSet(base_episodes=select_probe(pool, cfg.probe_episodes_per_domain, cfg.probe_seed),
                         seed=cfg.probe_seed)
        probe.save(run_dir / "probe_set.json")
    if "eval_episodes" not in meta:
        meta["eval_episodes"] = eval_episodes
        write_json(run_dir / "run.json", meta)

    chain = ReleaseChain(run_dir)
    if not chain.exists():
        seed_dir = cfg.seed_prompts_dir
        prompts = read_prompts(seed_dir)
        missing = [c for c, t in prompts.items() if not t.strip()]
        if missing:
            log.warning("seed prompts missing or empty under %s: %s", seed_dir, missing)
        chain.init_seed(prompts, extra={"seed_dir": str(seed_dir)})
        log.info("run %s | seed release r0 from %s", run_dir.name, seed_dir)
    run = Run(run_dir, cfg, client, dataset, list(eval_episodes), probe, meta["agent_path"],
              bool(meta.get("probe_only")), heldout_episodes=heldout_episodes)
    done = [int(r.get("iteration") or 0) for r in run.history()]
    start = (max(done) + 1) if done else 1
    log.info("run %s | domains %s | %d eval episodes, probe %d episodes | head r%d | iterations %d..%d%s",
             run_dir.name, meta["domains"], len(eval_episodes), len(probe.episode_ids(dataset)), chain.head(),
             start, args.iterations, " | FAKE LLM" if cfg.fake_llm else "")
    try:
        for it in range(start, args.iterations + 1):
            await run.iteration(it)
    finally:
        await client.aclose()
    hist = run.history()
    log.info("run %s | done: %d iterations, %d accepted, head r%d", run_dir.name, len(hist),
             sum(1 for r in hist if r.get("accepted")), chain.head())
    return run_dir


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging(args.verbose)
    asyncio.run(run_loop(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())

