"""Command-line entry point: ``gsm <command>``."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gsm.config import Settings
from gsm.eval.gatemem.repairs import REPAIRS

ROOT = Path(__file__).resolve().parents[2]


def _settings() -> Settings:
    s = Settings.from_env()
    if s.database_url is None:
        sys.exit("GSM_DATABASE_URL is not set (see .env.example)")
    return s


def cmd_migrate(_args: argparse.Namespace) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = _settings().database_url
    command.upgrade(cfg, "head")
    print("migrations applied (head)")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("gsm.api.app:create_app", factory=True, host=args.host, port=args.port)


def _service(settings: Settings, clock: object | None = None) -> object:
    from gsm.db.database import Database
    from gsm.providers.factory import build_providers
    from gsm.service import GovernedMemoryService

    assert settings.database_url is not None
    db = Database(settings.database_url, app_role=settings.db_app_role)
    return GovernedMemoryService(db, settings, providers=build_providers(settings), clock=clock)  # type: ignore[arg-type]


def cmd_demo(_args: argparse.Namespace) -> None:
    from gsm.auth import TrustedAuthenticator
    from gsm.clock import FrozenClock
    from gsm.demo import run_all
    from gsm.service import GovernedMemoryService

    settings = _settings()
    if settings.env == "production":
        sys.exit("refusing to run the demo against a production configuration")
    clock = FrozenClock(datetime.now(UTC))
    svc = _service(settings, clock)
    assert isinstance(svc, GovernedMemoryService)
    for tenant in ("demo-apollo", "demo-availability"):
        svc.reset_tenant(tenant)
    results = run_all(svc, TrustedAuthenticator(svc), clock)
    ok = True
    for res in results:
        print(f"\n== scenario: {res.name} ==")
        for step in res.steps:
            mark = "PASS" if step.ok else "FAIL"
            detail = json.dumps(step.detail, default=str) if step.detail else ""
            print(f"  [{mark}] {step.name} {detail}")
        ok &= res.ok
    print("\nall invariants held" if ok else "\nINVARIANT VIOLATION")
    sys.exit(0 if ok else 1)


def cmd_seed(args: argparse.Namespace) -> None:
    from gsm.service import GovernedMemoryService

    settings = _settings()
    svc = _service(settings)
    assert isinstance(svc, GovernedMemoryService)
    t = args.tenant
    svc.reset_tenant(t)
    svc.create_tenant(t, "Seeded demo tenant")
    for pid, roles in (
        ("alice", ["engineer"]),
        ("bob", ["engineer"]),
        ("carol", ["manager"]),
        ("dana", ["engineer"]),
        ("paula", ["project_manager"]),
        ("henry", ["hr"]),
    ):
        svc.create_principal(t, pid, pid.title(), roles=roles)
    svc.add_relationship_admin(t, "alice", "owner_of", "project", "apollo")
    for pid in ("alice", "bob", "dana", "paula"):
        svc.add_relationship_admin(t, pid, "member_of", "project", "apollo")
    print(f"seeded tenant {t!r}: alice (owner_of apollo), bob/dana/paula (members), carol (outside), henry (hr)")


def cmd_dev_token(args: argparse.Namespace) -> None:
    from gsm.auth import DevHmacAuthenticator

    settings = _settings()
    if not settings.dev_auth_enabled or settings.dev_auth_secret is None or settings.env == "production":
        sys.exit("development authenticator is not enabled (GSM_DEV_AUTH_ENABLED / GSM_DEV_AUTH_SECRET)")
    now = time.time()
    claims = {
        "tenant_id": args.tenant,
        "workspace_id": args.workspace,
        "principal_id": args.principal,
        "auth_strength": args.strength,
        "auth_time": now,
        "exp": now + args.ttl,
    }
    if args.purpose:
        claims["purpose"] = args.purpose
    print(DevHmacAuthenticator.mint(settings.dev_auth_secret.get_secret_value().encode(), claims))


def cmd_cascade(args: argparse.Namespace) -> None:
    from gsm.service import GovernedMemoryService

    svc = _service(_settings())
    assert isinstance(svc, GovernedMemoryService)
    for status in svc.run_cascades(args.tenant):
        print(json.dumps(status.__dict__, default=str))


def cmd_gatemem_run(args: argparse.Namespace) -> None:
    from gsm.db.database import Database
    from gsm.eval.gatemem.adapter import run_gatemem
    from gsm.providers.factory import build_providers

    settings = _settings()
    assert settings.database_url is not None
    db = Database(settings.database_url, app_role=settings.db_app_role, pool_size=max(10, args.workers + 2))
    from gsm.providers.llm_cache import JsonlCache
    from gsm.providers.openai_compat import OpenAICompatClient

    def llm(thinking: bool, flag: str) -> OpenAICompatClient:
        if not (settings.llm_base_url and settings.llm_model):
            sys.exit(f"{flag} needs GSM_LLM_BASE_URL and GSM_LLM_MODEL (and GSM_LLM_API_KEY if required)")
        extra = dict(settings.llm_extra_body)
        extra["chat_template_kwargs"] = {
            **dict(extra.get("chat_template_kwargs") or {}),  # type: ignore[call-overload]
            "enable_thinking": thinking,
        }
        return OpenAICompatClient(
            settings.llm_base_url,
            settings.llm_model,
            api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
            timeout_s=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
            extra_body=extra,
        )

    interpreter = None
    if args.interpreter == "qwen":
        from gsm.eval.gatemem.qwen_interpreter import QwenTurnInterpreter

        cache = JsonlCache(Path(args.interp_cache)) if args.interp_cache else None
        interpreter = QwenTurnInterpreter(
            llm(bool(args.interp_thinking), "--interpreter qwen"), cache=cache, label_prompt=args.label_prompt
        )
    providers = build_providers(settings)
    if args.planner == "decompose":
        from gsm.retrieval.decompose import DecomposingPlanner

        providers.planner = DecomposingPlanner(
            llm(False, "--planner decompose"),
            cache=JsonlCache(Path(args.planner_cache)) if args.planner_cache else None,
            max_parts=settings.planner_max_parts,
        )
    answerer_factory = None
    if args.answerer == "baseline":
        from gsm.eval.gatemem.baseline_answerer import BaselineAnswerer, BaselineAnswererConfig, make_router

        repo_root = Path(args.repo_root) if args.repo_root else Path(args.data_dir).resolve().parents[2]
        cfg = BaselineAnswererConfig(
            model=args.answerer_model, api_base=args.answerer_base_url, api_key_env=args.answerer_api_key_env
        )
        allowed = bool(args.allowed_by_default)
        answerer_factory = lambda: BaselineAnswerer(  # noqa: E731
            make_router(repo_root, cfg), repo_root, allowed_by_default=allowed
        )
    out = run_gatemem(
        db,
        settings,
        Path(args.data_dir),
        Path(args.out),
        max_episodes=args.max_episodes,
        resolve_deletions=args.resolve_deletions,
        providers=providers,
        interpreter=interpreter,
        answerer_factory=answerer_factory,
        top_k=args.top_k,
        workers=args.workers,
        diagnostics=args.diagnostics,
        need_to_know=args.need_to_know,
        oversight_withhold=args.oversight_withhold,
        revocation_override=args.revocation_override,
        scope_fit=args.scope_fit,
        snapshots=args.snapshot_memory,
        repairs=REPAIRS - set(args.disable_repair or []),
        continuous=args.continuous,
    )
    print(json.dumps({"predictions": len(out.predictions), "summary": out.summary}, indent=2, default=str))


def cmd_gatemem_calibrate(args: argparse.Namespace) -> None:
    from gsm.eval.gatemem.calibrate import calibrate
    from gsm.eval.gatemem.sanitize import load_for_method
    from gsm.providers.sentence_transformers import DEFAULT_MODEL, SentenceTransformerEmbedder

    episodes, _ = load_for_method(Path(args.data_dir))  # turns only; checkpoints are not used
    result = calibrate(episodes, SentenceTransformerEmbedder(args.model or DEFAULT_MODEL), high=args.high, low=args.low)
    print(json.dumps(result.as_dict(), indent=2))


def cmd_gatemem_score(args: argparse.Namespace) -> None:
    from gsm.eval.gatemem.scoring import score_run

    settings = Settings.from_env()
    report = score_run(
        Path(args.data_dir),
        Path(args.run_dir),
        repo_root=Path(args.repo_root) if args.repo_root else None,
        price_in_per_mtok=settings.llm_price_input_per_mtok,
        price_out_per_mtok=settings.llm_price_output_per_mtok,
    )
    print(json.dumps({"official": report["official"], "gsm": report["gsm"]}, indent=2, default=str))


def _endpoint(args: argparse.Namespace, prefix: str) -> object:
    """An OpenAI-compatible client from ``--<prefix>-base-url/-model/-api-key-env/-thinking`` (GSM_LLM_* default)."""
    import os

    from gsm.providers.openai_compat import OpenAICompatClient

    settings = Settings.from_env()  # benchmark endpoints need no database
    base = getattr(args, f"{prefix}_base_url") or settings.llm_base_url
    model = getattr(args, f"{prefix}_model") or settings.llm_model
    if not (base and model):
        sys.exit(f"--{prefix}-base-url/--{prefix}-model (or GSM_LLM_BASE_URL/GSM_LLM_MODEL) are required")
    key_env = getattr(args, f"{prefix}_api_key_env")
    key = (
        os.environ.get(key_env)
        if key_env
        else (settings.llm_api_key.get_secret_value() if settings.llm_api_key else None)
    )
    extra = dict(settings.llm_extra_body)
    extra["chat_template_kwargs"] = {
        **dict(extra.get("chat_template_kwargs") or {}),  # type: ignore[call-overload]
        "enable_thinking": bool(getattr(args, f"{prefix}_thinking")),
    }
    # Benchmark batches run for hours against a rate-limited endpoint: wait out bursts of 429s
    # (2, 4, 8, 16, 30, 30 s) instead of failing the job.
    return OpenAICompatClient(
        base,
        model,
        api_key=key,
        timeout_s=max(settings.llm_timeout_s, 300.0),
        max_retries=6,
        extra_body=extra,
        backoff_base_s=2.0,
        backoff_cap_s=30.0,
    )


def _endpoint_args(p: argparse.ArgumentParser, prefix: str, what: str) -> None:
    p.add_argument(f"--{prefix}-base-url", help=f"{what} endpoint (default GSM_LLM_BASE_URL)")
    p.add_argument(f"--{prefix}-model", help=f"{what} model (default GSM_LLM_MODEL)")
    p.add_argument(f"--{prefix}-api-key-env", help="environment variable holding the API key (default GSM_LLM_API_KEY)")
    p.add_argument(f"--{prefix}-thinking", action="store_true", help=f"let the {what} model think")


def cmd_cimemories_label(args: argparse.Namespace) -> None:
    import logging

    from gsm.eval.cimemories.labels import label_dataset

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    import signal

    def _term(_sig: int, _frame: object) -> None:  # save on `kill <pid>` like on Ctrl-C
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)

    client = _endpoint(args, "labeller")
    stats = label_dataset(
        client,  # type: ignore[arg-type]
        Path(args.data),
        Path(args.out),
        labeller=client.model,  # type: ignore[attr-defined]
        n=args.n,
        temperature=args.temperature,
        max_profiles=args.max_profiles,
        workers=args.workers,
        max_jobs=args.max_jobs,
    )
    print(json.dumps(stats, indent=2))
    if stats["stopped_early"]:
        sys.exit(f"stopped early with {stats['remaining']} persona-contexts left; run the same command to resume")


def cmd_cimemories_relabel(args: argparse.Namespace) -> None:
    from gsm.eval.cimemories.labels import relabel_ignoring_reworded

    print(json.dumps(relabel_ignoring_reworded(Path(args.labels), Path(args.out)), indent=2))


def _run_logging() -> None:
    import logging
    import signal

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    def _term(_sig: int, _frame: object) -> None:  # save on `kill <pid>` like on Ctrl-C
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)


def cmd_cimemories_run(args: argparse.Namespace) -> None:
    from gsm.eval.cimemories.data import load_for_method
    from gsm.eval.cimemories.run import FullContextMethod, run_method

    _run_logging()
    profiles = load_for_method(Path(args.data), max_profiles=args.max_profiles)
    client = _endpoint(args, "answerer")
    out = Path(args.out)
    if args.method == "full-context":
        method: Any = FullContextMethod(client, privacy_level=args.privacy_level)  # type: ignore[arg-type]
    else:
        from gsm.db.database import Database
        from gsm.eval.cimemories.gsm_method import GsmMethod, PurposeModel
        from gsm.providers.factory import build_providers
        from gsm.providers.llm_cache import JsonlCache

        settings = _settings()
        assert settings.database_url is not None
        db = Database(settings.database_url, app_role=settings.db_app_role, pool_size=max(10, args.workers + 2))
        mode = {"gsm-purpose": "purpose", "gsm-purpose-task": "purpose_task"}.get(args.method, "retrieval")
        purpose_model = None
        if mode != "retrieval":
            cache = JsonlCache(Path(args.purpose_cache) if args.purpose_cache else out / "purpose_cache.jsonl")
            purpose_model = PurposeModel(client, cache)  # type: ignore[arg-type]
        method = GsmMethod(
            db,
            settings,
            build_providers(settings),
            client,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            top_k=args.top_k,
            purpose_model=purpose_model,
            privacy_level=args.privacy_level,
            relevance_bar=not args.no_relevance_bar,
        )
    summary = run_method(method, profiles, out, trials=args.trials, workers=args.workers)
    if hasattr(method, "write_audit"):
        method.write_audit(out)
    print(json.dumps(summary, indent=2, default=str))
    if summary["stopped_early"]:
        sys.exit(f"stopped early with {summary['remaining']} answers left; run the same command to resume")


def cmd_cimemories_score(args: argparse.Namespace) -> None:
    from gsm.eval.cimemories.scoring import score_run

    _run_logging()
    judge = None if args.no_judge else _endpoint(args, "judge")
    report = score_run(
        Path(args.labels),
        Path(args.run_dir),
        judge,  # type: ignore[arg-type]
        workers=args.workers,
        tag=args.tag,
        verify_quotes=args.verify_quotes,
        judge_max_tokens=args.judge_max_tokens,
    )
    print(json.dumps({k: v for k, v in report.items() if k != "official_printout"}, indent=2, default=str))
    if report["official_printout"]:
        print(report["official_printout"])
    if not report["complete"]:
        sys.exit(f"{report['unjudged']} answers are not judged yet; run the same command to resume")


def cmd_mumbench_check(args: argparse.Namespace) -> None:
    from collections import Counter

    from gsm.eval.mumbench.data import load_for_method, load_raw

    method_view = load_for_method(Path(args.data_dir))
    raw = load_raw(Path(args.data_dir))
    out = {}
    for domain, recs in raw.items():
        ops = Counter(str(a.get("operation", "")).lower() for r in recs for a in r.get("memory_actions") or [])
        out[domain] = {
            "interactions": len(recs),
            "users": len({r.get("user_id") for r in recs}),
            "operations": dict(ops),
            "method_view_interactions": len(method_view.get(domain, [])),
        }
    print(json.dumps(out, indent=2))


def cmd_mumbench_score(args: argparse.Namespace) -> None:
    from gsm.eval.mumbench.scoring import score

    embed = None
    if args.embedder:
        from gsm.providers.sentence_transformers import SentenceTransformerEmbedder

        embed = SentenceTransformerEmbedder(args.embedder).embed
    print(json.dumps(score(Path(args.data_dir), Path(args.predictions), embed=embed), indent=2))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="gsm", description="Governed Shared Memory")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=cmd_migrate)
    s = sub.add_parser("serve", help="run the HTTP API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("demo", help="run the acceptance scenarios against the database").set_defaults(fn=cmd_demo)
    s = sub.add_parser("seed", help="create a demo tenant for API exploration")
    s.add_argument("--tenant", default="acme")
    s.set_defaults(fn=cmd_seed)
    s = sub.add_parser("dev-token", help="mint a development X-GSM-Context token")
    s.add_argument("--tenant", default="acme")
    s.add_argument("--workspace", default="default")
    s.add_argument("--principal", required=True)
    s.add_argument("--purpose")
    s.add_argument("--strength", default="password", choices=["none", "password", "mfa", "hardware"])
    s.add_argument("--ttl", type=int, default=3600)
    s.set_defaults(fn=cmd_dev_token)
    s = sub.add_parser("cascade", help="run pending deletion cascades for a tenant")
    s.add_argument("--tenant", required=True)
    s.set_defaults(fn=cmd_cascade)
    g = sub.add_parser("gatemem", help="GateMem evaluation adapter").add_subparsers(dest="gcmd", required=True)
    r = g.add_parser("run", help="run GSM as a GateMem method (writes predictions.jsonl)")
    r.add_argument("--data-dir", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--max-episodes", type=int)
    r.add_argument(
        "--resolve-deletions",
        choices=["none", "lexical"],
        default="none",
        help="heuristic interpreter only; the qwen interpreter resolves deletions itself",
    )
    r.add_argument(
        "--interpreter",
        choices=["heuristic", "qwen"],
        default="heuristic",
        help="qwen: label turns with the model at GSM_LLM_BASE_URL / GSM_LLM_MODEL",
    )
    r.add_argument("--interp-cache", help="JSONL cache of interpreter responses (re-runs make no model calls)")
    r.add_argument(
        "--label-prompt",
        choices=["v7", "v8", "v9"],
        default="v7",
        help="labelling prompt: v7 owners per quote (default, best measured); v8 audience by role function; "
        "v9 relationship members first",
    )
    r.add_argument("--interp-thinking", action="store_true", help="let the interpreter model think (slower)")
    r.add_argument(
        "--planner",
        choices=["settings", "decompose"],
        default="settings",
        help="decompose: split multi-part questions with the model (thinking off); default: GSM_PLANNER_PROVIDER",
    )
    r.add_argument("--planner-cache", help="JSONL cache of decompositions")
    r.add_argument(
        "--need-to-know",
        action="store_true",
        help="withhold exact protected values from audience entries a model call deems summary-only (off: it cost "
        "more utility than the leaks it removed on GateMem)",
    )
    r.add_argument(
        "--oversight-withhold",
        action="store_true",
        help="groups classified as oversight get quotes with protected values withheld (off: net loss on GateMem)",
    )
    r.add_argument(
        "--revocation-override",
        action="store_true",
        help="principals cut off by a revocation stop being added as owners/subjects (off: net loss on GateMem)",
    )
    r.add_argument(
        "--scope-fit",
        action="store_true",
        help="give left-out scope-limited groups an in-scope version of the quotes (off: no measured gain)",
    )
    r.add_argument(
        "--continuous",
        action="store_true",
        help="treat the whole domain as one memory, never reset between episodes (episodes in file order)",
    )
    r.add_argument(
        "--disable-repair",
        action="append",
        choices=sorted(REPAIRS),
        help="switch off one of the utility repairs (repeatable): delegate_floor, keep_readers (R3), "
        "scope_records (R4), carry_forward (R5), partial_delete (R6)",
    )
    r.add_argument(
        "--snapshot-memory",
        action="store_true",
        help="write memory_snapshots.jsonl: the whole memory store at every checkpoint (analysis only)",
    )
    r.add_argument("--workers", type=int, default=1, help="episodes processed in parallel")
    r.add_argument("--top-k", type=int, default=8, help="evidence items per query (the RAG baselines use 20)")
    r.add_argument(
        "--answerer",
        choices=["gsm", "baseline"],
        default="gsm",
        help="baseline: the GateMem baselines' own answerer (bench query_prompt + LLMRouter)",
    )
    r.add_argument("--answerer-base-url", default="http://localhost:8003/v1")
    r.add_argument("--answerer-model", default="Qwen/Qwen3.8-27B")
    r.add_argument("--answerer-api-key-env", default="LOCAL_API_KEY")
    r.add_argument(
        "--allowed-by-default",
        action="store_true",
        help="baseline answerer: start the evidence with GSM's access decision (the asker is authorized for every "
        "record shown), so roles the benchmark records no relationship for are not refused by default",
    )
    r.add_argument("--repo-root", help="GateMem repository root (default: three levels above --data-dir)")
    r.add_argument(
        "--diagnostics",
        action="store_true",
        help="also write ingest_log.jsonl and checkpoint_diag.jsonl (ids, labels, ranking; no annotations)",
    )
    r.set_defaults(fn=cmd_gatemem_run)
    r = g.add_parser("calibrate", help="label-free vector thresholds for a sentence embedder (turns only)")
    r.add_argument("--data-dir", required=True)
    r.add_argument("--model", help="sentence-transformers model (default all-MiniLM-L6-v2)")
    r.add_argument("--high", type=float, default=0.99, help="noise quantile for vector_min_similarity")
    r.add_argument("--low", type=float, default=0.25, help="overlap-noise quantile for the support floor")
    r.set_defaults(fn=cmd_gatemem_calibrate)
    r = g.add_parser("score", help="score a run with the official GateMem scorer plus GSM metrics")
    r.add_argument("--data-dir", required=True)
    r.add_argument("--run-dir", required=True)
    r.add_argument("--repo-root")
    r.set_defaults(fn=cmd_gatemem_score)

    from gsm.eval.cimemories.data import DEFAULT_DATA as CIM_DATA

    c = sub.add_parser("cimemories", help="CIMemories evaluation adapter").add_subparsers(dest="ccmd", required=True)
    r = c.add_parser("label", help="regenerate the (unreleased) gold labels with upstream's persona procedure")
    r.add_argument("--data", default=str(CIM_DATA))
    r.add_argument("--out", required=True, help="labelled JSON (upstream combined-labels layout); resumes if present")
    r.add_argument("--n", type=int, default=10, help="samples per persona and context (upstream: 10)")
    r.add_argument("--temperature", type=float, help="sampling temperature (default: server default, as upstream)")
    r.add_argument("--max-profiles", type=int)
    r.add_argument("--workers", type=int, default=8)
    r.add_argument("--max-jobs", type=int, help="label at most this many persona-contexts in this run")
    _endpoint_args(r, "labeller", "labeller")
    r.set_defaults(fn=cmd_cimemories_label)
    r = c.add_parser("relabel", help="rebuild labels from saved samples, ignoring reworded statements (no model calls)")
    r.add_argument("--labels", required=True, help="output of `gsm cimemories label` (its .samples.jsonl is read)")
    r.add_argument("--out", required=True)
    r.set_defaults(fn=cmd_cimemories_relabel)
    r = c.add_parser("run", help="write solutions.jsonl: one message per (context, trial)")
    r.add_argument("--data", default=str(CIM_DATA))
    r.add_argument("--out", required=True)
    r.add_argument(
        "--method",
        choices=["full-context", "gsm-retrieval", "gsm-purpose", "gsm-purpose-task"],
        default="full-context",
        help="gsm-retrieval: governed top-k retrieval is the only filter; gsm-purpose: memories carry purpose "
        "rules and the policy engine releases those labelled for the message's context; gsm-purpose-task: the "
        "same gate, then a need-to-know pick inside the permitted set (GSM methods need GSM_DATABASE_URL)",
    )
    r.add_argument("--top-k", type=int, default=20, help="gsm-retrieval: evidence items per message")
    r.add_argument(
        "--no-relevance-bar",
        action="store_true",
        help="gsm-retrieval: plain top-k ranking (GSM's relevance bar releases nothing for most task titles)",
    )
    r.add_argument("--purpose-cache", help="gsm-purpose: JSONL cache of purpose-model calls (default: in --out)")
    r.add_argument("--privacy-level", type=int, choices=[1, 2, 3, 4], default=1, help="upstream task_solving prompt")
    r.add_argument("--trials", type=int, default=5, help="upstream exp.sh: 5")
    r.add_argument("--max-profiles", type=int)
    r.add_argument("--workers", type=int, default=16)
    _endpoint_args(r, "answerer", "answerer")
    r.set_defaults(fn=cmd_cimemories_run)
    r = c.add_parser("score", help="reveal judge + upstream metrics.py (violation, coverage)")
    r.add_argument("--labels", required=True, help="output of `gsm cimemories label`")
    r.add_argument("--run-dir", required=True)
    r.add_argument("--workers", type=int, default=16)
    r.add_argument(
        "--tag", default="", help="suffix for results/score files (score one run against several label files)"
    )
    r.add_argument(
        "--judge-max-tokens",
        type=int,
        default=4096,
        help="output budget per verdict; with --judge-thinking the reasoning counts against it",
    )
    r.add_argument(
        "--verify-quotes",
        action="store_true",
        help="count a reveal only if the sentence the judge quotes for it occurs in the message (filters runaway "
        "verdicts; not part of upstream's metric, so report it next to the raw number)",
    )
    r.add_argument("--no-judge", action="store_true", help="score with the reveals already recorded; no model calls")
    _endpoint_args(r, "judge", "reveal judge")
    r.set_defaults(fn=cmd_cimemories_score)

    m = sub.add_parser("mumbench", help="MUMBench adapter (data not public; see docs/MUMBENCH.md)").add_subparsers(
        dest="mcmd", required=True
    )
    r = m.add_parser("check", help="load a MUMBench directory and print per-domain counts")
    r.add_argument("--data-dir", required=True)
    r.set_defaults(fn=cmd_mumbench_check)
    r = m.add_parser("score", help="score predictions.jsonl with the re-implemented metrics")
    r.add_argument("--data-dir", required=True)
    r.add_argument("--predictions", required=True)
    r.add_argument("--embedder", help="sentence-transformers model for content fidelity (else null)")
    r.set_defaults(fn=cmd_mumbench_score)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
