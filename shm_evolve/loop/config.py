"""Environment-driven configuration for the outer loop.

WHY THE OLD NAMES. A real run is configured by sourcing
`scripts/serve/env_local_eval.sh`, which was written for `Auto Research/`'s
config.py. Every knob that file sets (VLLM_BASE_URL, EVALUATOR_MODEL,
JUDGE_ROUTE, OPENAI_BASE_URL, OPENROUTER_*, VLLM_MAX_CONCURRENCY, ...) keeps its
exact name here, so the same shell session drives both loops and a stale export
cannot silently mean two different things.

WHY A DATACLASS AND NOT MODULE CONSTANTS. The old config froze everything at
import time; tests then had to reload modules to flip a value. `get()` builds a
frozen `Config` from the environment once and `reload()` rebuilds it, so a test
(or `run.py --fake-llm`) changes the environment and calls `reload()` instead of
fighting the import cache.

Nothing here talks to the network or imports `memory_system`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: shm_evolve/ -- the package root (loop/ and memory_system/ live under it).
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
#: The repository root two levels up from loop/: it holds bench/.
REPO_ROOT = PACKAGE_ROOT.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader, same semantics as the old config: `KEY=value`
    lines, `#` comments, no interpolation, and the shell always wins
    (setdefault), so an operator can override a committed .env."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if os.environ.get("SHM_NO_DOTENV", "").strip().lower() not in {"1", "true", "yes", "on"}:
    _load_dotenv(REPO_ROOT / ".env")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_opt_int(key: str, default: int | None) -> int | None:
    """`int` or the explicit absence of one. "", "none", "0", "-1" mean
    UNCAPPED: the caller then omits `max_tokens` entirely, because every
    provider reads a zero as "generate nothing" and vLLM rejects a null."""
    raw = os.environ.get(key)
    if raw is None:
        return default
    raw = raw.strip()
    if raw.lower() in {"", "none", "null", "unlimited", "off"}:
        return None
    try:
        value = int(raw)
    except ValueError:
        return default
    return None if value <= 0 else value


def _env_tuple(key: str, default: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in _env(key, default).split(",") if p.strip())


@dataclass(frozen=True)
class RouteConfig:
    """One serving route. `is_vllm` is DECLARED, not inferred from the name:
    the openai route is a local vLLM in env_local_eval.sh, and only a vLLM
    understands `chat_template_kwargs` (OpenAI/OpenRouter reject the key)."""

    name: str
    base_url: str
    api_key_env: str
    max_concurrency: int
    extra_headers: dict[str, str] = field(default_factory=dict)
    is_vllm: bool = False


@dataclass(frozen=True)
class Config:
    # ---- mode
    fake_llm: bool

    # ---- endpoints (old names)
    openrouter_base_url: str
    vllm_base_url: str
    openai_base_url: str
    openrouter_referer: str
    openrouter_title: str
    openai_route_is_vllm: bool

    # ---- models and routes
    evaluator_model: str
    evaluator_thinking: bool
    evaluator_max_tokens: int | None
    evaluator_timeout_s: float
    # Reflector and curator: one long thinking call over an ~11k-token prompt.
    # The transport default (HTTP_TIMEOUT_S, 120s) is sized for the short
    # memory-model calls and timed the reflector out on the first live run.
    optimizer_timeout_s: float
    eval_render_attempts: int
    eval_retry_base_s: float
    judge_model: str
    judge_route: str
    judge_max_tokens: int | None
    judge_llm_attempts: int
    judge_retry_base_s: float
    use_llm_judge: bool
    reflector_model: str
    reflector_route: str
    reflector_max_tokens: int | None
    curator_model: str
    curator_route: str
    curator_max_tokens: int | None

    # ---- OpenRouter pinning / reasoning (old names)
    openrouter_provider_order: tuple[str, ...]
    openrouter_allow_fallbacks: bool
    openrouter_pin_model_prefixes: tuple[str, ...]
    openrouter_reasoning_effort: str
    openrouter_reasoning_model_prefixes: tuple[str, ...]

    # ---- vLLM thinking toggle (old names)
    vllm_disable_thinking: bool
    vllm_thinking_model_prefixes: tuple[str, ...]

    # ---- concurrency / HTTP (old names)
    vllm_max_concurrency: int
    openrouter_max_concurrency: int
    judge_max_concurrency: int
    http_max_connections: int
    http_max_keepalive: int
    http_timeout_s: float
    http_connect_timeout_s: float
    http_max_retries: int
    http_backoff_base_s: float
    http_backoff_max_s: float
    http_retry_after_max_s: float

    # ---- memory-side model (handed to each episode process)
    memory_llm_base_url: str
    memory_llm_model: str
    memory_llm_token_budget: int
    memory_llm_call_budget: int
    memory_llm_max_tokens: int

    # ---- benchmark
    gatemem_repo: Path
    data_root: Path
    evaluator_prompt_file: str
    judge_prompt_file: str

    # ---- the outer loop (new)
    probe_episodes_per_domain: int
    probe_promoted_max: int
    # Held out from every domain: scored per accepted release, never attributed,
    # probed or promoted. The loop evolves on the rest.
    heldout_episodes_per_domain: int
    # With several training domains, a candidate may not buy one domain with
    # another: no domain's paired probe MGS may fall by more than this.
    gate_domain_floor: float
    probe_promoted_episodes_max: int
    probe_seed: int
    min_win_margin: int
    max_edits: int
    max_prompt_chars: int
    queue_batch: int
    episode_workers: int
    episode_timeout_s: float
    runs_dir: Path
    seed_prompts_dir: Path
    agent_path: str
    reflector_failure_sample: int
    reflector_history_rows: int
    reflector_rejections_shown: int

    @classmethod
    def from_env(cls) -> Config:
        vllm_base = _env("VLLM_BASE_URL", "http://localhost:8000/v1")
        openai_base = _env("OPENAI_BASE_URL", "https://api.openai.com/v1")
        evaluator_model = _env("EVALUATOR_MODEL", "qwen3.8-27b")
        gatemem_repo = Path(_env("GATEMEM_REPO", str(REPO_ROOT))).expanduser()
        return cls(
            fake_llm=_env_bool("SHM_FAKE_LLM", False),
            openrouter_base_url=_env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            vllm_base_url=vllm_base,
            openai_base_url=openai_base,
            openrouter_referer=_env("OPENROUTER_REFERER", "https://github.com/rzhub/GateMem"),
            openrouter_title=_env("OPENROUTER_TITLE", "GateMem shm_evolve"),
            # Same inference as the old openai_route(): a base URL that is not
            # OpenAI or OpenRouter is a local vLLM serving the judge.
            openai_route_is_vllm=_env_bool(
                "OPENAI_ROUTE_IS_VLLM",
                not openai_base.startswith(("https://api.openai.com", "https://openrouter.ai")),
            ),
            evaluator_model=evaluator_model,
            # ON by default: every baseline's answerer reasoned (memory note
            # office-baselines-thinking-on); forcing it off handicapped ours.
            evaluator_thinking=_env_bool("EVALUATOR_THINKING", True),
            evaluator_max_tokens=_env_opt_int("EVALUATOR_MAX_TOKENS", None),
            evaluator_timeout_s=_env_float("EVALUATOR_TIMEOUT_S", 900.0),
            optimizer_timeout_s=_env_float("OPTIMIZER_TIMEOUT_S", 900.0),
            eval_render_attempts=_env_int("EVAL_RENDER_ATTEMPTS", 3),
            eval_retry_base_s=_env_float("EVAL_RETRY_BASE_S", 4.0),
            judge_model=_env("JUDGE_MODEL", "gpt-4.1"),
            judge_route=_env("JUDGE_ROUTE", "openai"),
            judge_max_tokens=_env_opt_int("JUDGE_MAX_TOKENS", None),
            judge_llm_attempts=_env_int("JUDGE_LLM_ATTEMPTS", 3),
            judge_retry_base_s=_env_float("JUDGE_RETRY_BASE_S", 2.0),
            use_llm_judge=_env_bool("USE_LLM_JUDGE", True),
            # The optimiser defaults to the model the old Architect used.
            reflector_model=_env("REFLECTOR_MODEL", "deepseek/deepseek-v4-flash-0731"),
            reflector_route=_env("REFLECTOR_ROUTE", "openrouter"),
            reflector_max_tokens=_env_opt_int("REFLECTOR_MAX_TOKENS", 32768),
            curator_model=_env("CURATOR_MODEL", "deepseek/deepseek-v4-flash-0731"),
            curator_route=_env("CURATOR_ROUTE", "openrouter"),
            # A reasoning curator spent all of 16384 thinking and returned no
            # edits (joint_v3 iteration 1); the reflector already runs at 32768.
            curator_max_tokens=_env_opt_int("CURATOR_MAX_TOKENS", 32768),
            openrouter_provider_order=_env_tuple("OPENROUTER_PROVIDER_ORDER", "DeepSeek,Fireworks,Novita"),
            openrouter_allow_fallbacks=_env_bool("OPENROUTER_ALLOW_FALLBACKS", False),
            openrouter_pin_model_prefixes=_env_tuple("OPENROUTER_PIN_MODEL_PREFIXES", "deepseek/"),
            openrouter_reasoning_effort=_env("OPENROUTER_REASONING_EFFORT", "low"),
            openrouter_reasoning_model_prefixes=_env_tuple("OPENROUTER_REASONING_MODEL_PREFIXES", "deepseek/"),
            vllm_disable_thinking=_env_bool("VLLM_DISABLE_THINKING", True),
            vllm_thinking_model_prefixes=_env_tuple("VLLM_THINKING_MODEL_PREFIXES", "Qwen/,qwen/"),
            vllm_max_concurrency=_env_int("VLLM_MAX_CONCURRENCY", 12),
            openrouter_max_concurrency=_env_int("OPENROUTER_MAX_CONCURRENCY", 4),
            judge_max_concurrency=_env_int("JUDGE_MAX_CONCURRENCY", 4),
            http_max_connections=_env_int("HTTP_MAX_CONNECTIONS", 64),
            http_max_keepalive=_env_int("HTTP_MAX_KEEPALIVE", 16),
            http_timeout_s=_env_float("HTTP_TIMEOUT_S", 120.0),
            http_connect_timeout_s=_env_float("HTTP_CONNECT_TIMEOUT_S", 10.0),
            http_max_retries=_env_int("HTTP_MAX_RETRIES", 4),
            http_backoff_base_s=_env_float("HTTP_BACKOFF_BASE_S", 0.75),
            http_backoff_max_s=_env_float("HTTP_BACKOFF_MAX_S", 30.0),
            http_retry_after_max_s=_env_float("HTTP_RETRY_AFTER_MAX_S", 180.0),
            # Same default chain as the old config: the memory model is the
            # evaluator's vLLM unless pointed elsewhere.
            memory_llm_base_url=_env("MEMORY_LLM_BASE_URL", vllm_base),
            memory_llm_model=_env("MEMORY_LLM_MODEL", evaluator_model),
            # DESIGN.md §3.1: measured on a real 240-turn office episode,
            # one-pass: ~550 memory-model calls and ~1.07M prompt tokens.
            # 400k/1200 (sized for the old replay-per-checkpoint evaluator
            # with a warm cache) throttled most of a real episode onto
            # fallbacks; these leave headroom above the measured cost.
            memory_llm_token_budget=_env_int("MEMORY_LLM_TOKEN_BUDGET", 1_500_000),
            memory_llm_call_budget=_env_int("MEMORY_LLM_CALL_BUDGET", 900),
            memory_llm_max_tokens=_env_int("MEMORY_LLM_MAX_TOKENS", 512),
            gatemem_repo=gatemem_repo,
            data_root=Path(_env("SHM_DATA_ROOT", str(gatemem_repo / "bench" / "data"))).expanduser(),
            evaluator_prompt_file=_env("EVALUATOR_PROMPT_FILE", ""),
            judge_prompt_file=_env("JUDGE_PROMPT_FILE", ""),
            probe_episodes_per_domain=_env_int("PROBE_EPISODES_PER_DOMAIN", 3),
            probe_promoted_max=_env_int("PROBE_PROMOTED_MAX", 40),
            heldout_episodes_per_domain=_env_int("HELDOUT_EPISODES_PER_DOMAIN", 0),
            gate_domain_floor=_env_float("GATE_DOMAIN_FLOOR", 0.02),
            # Promotion adds CHECKPOINTS but one-pass evaluation runs whole
            # EPISODES; without this cap 40 promoted checkpoints can drag most of
            # a domain into every probe. See gate.promote_failures.
            probe_promoted_episodes_max=_env_int("PROBE_PROMOTED_EPISODES_MAX", 3),
            probe_seed=_env_int("PROBE_SEED", 20261013),
            min_win_margin=_env_int("MIN_WIN_MARGIN", 0),
            max_edits=_env_int("MAX_EDITS", 3),
            max_prompt_chars=_env_int("MAX_PROMPT_CHARS", 12000),
            queue_batch=_env_int("QUEUE_BATCH", 1),
            episode_workers=_env_int("EPISODE_WORKERS", 8),
            # One episode is one process; a child that hangs (a wedged model
            # endpoint, a runaway retry loop) must not stall the whole
            # evaluation. Generous by default -- a real episode's own budget
            # (MEMORY_LLM_CALL_BUDGET) is what should end it first.
            # A medical episode with reasoning on two components ran 60 min
            # on the first joint run; the old 3600 s default timed it out.
            episode_timeout_s=_env_float("EPISODE_TIMEOUT_S", 7200.0),
            runs_dir=Path(_env("RUNS_DIR", str(PACKAGE_ROOT / "runs"))).expanduser().resolve(),
            seed_prompts_dir=Path(
                _env("SHM_SEED_PROMPTS_DIR", str(PACKAGE_ROOT / "memory_system" / "prompts"))
            ).expanduser(),
            agent_path=_env("SHM_AGENT", "memory_system.agent:GateMemAgent"),
            reflector_failure_sample=_env_int("REFLECTOR_FAILURE_SAMPLE", 12),
            reflector_history_rows=_env_int("REFLECTOR_HISTORY_ROWS", 10),
            reflector_rejections_shown=_env_int("REFLECTOR_REJECTIONS_SHOWN", 5),
        )

    # ------------------------------------------------------------ routes

    def routes(self) -> dict[str, RouteConfig]:
        return {
            "openrouter": RouteConfig(
                name="openrouter", base_url=self.openrouter_base_url,
                api_key_env="OPENROUTER_API_KEY", max_concurrency=self.openrouter_max_concurrency,
                extra_headers={"HTTP-Referer": self.openrouter_referer, "X-Title": self.openrouter_title},
            ),
            # The answerer's route. Its semaphore is the only backpressure on
            # the per-checkpoint fan-out, so it is sized to the GPU, not to
            # the checkpoint count.
            "vllm": RouteConfig(
                name="vllm", base_url=self.vllm_base_url, api_key_env="VLLM_API_KEY",
                max_concurrency=self.vllm_max_concurrency, is_vllm=True,
            ),
            "openai": RouteConfig(
                name="openai", base_url=self.openai_base_url, api_key_env="OPENAI_API_KEY",
                max_concurrency=self.judge_max_concurrency, is_vllm=self.openai_route_is_vllm,
            ),
        }

    # ------------------------------------------------------------ per-model payload rules

    def openrouter_provider_preferences(self, model: str) -> dict[str, object] | None:
        """The OpenRouter `provider` block, or None. Prefix-gated because a
        judge on the same route ("openai/gpt-4.1") is served by none of the
        pinned DeepSeek hosts; a route-wide pin would fail every judge call."""
        if not self.openrouter_provider_order:
            return None
        if not any(model.startswith(p) for p in self.openrouter_pin_model_prefixes):
            return None
        prefs: dict[str, object] = {
            "order": list(self.openrouter_provider_order),
            "allow_fallbacks": self.openrouter_allow_fallbacks,
        }
        if not self.openrouter_allow_fallbacks:
            # `only` makes the pin exclusive; `order` alone is a preference.
            prefs["only"] = list(self.openrouter_provider_order)
        return prefs

    def openrouter_reasoning(self, model: str) -> dict[str, object] | None:
        """Cap the reasoning effort on reasoning models: uncapped, DeepSeek
        spends all of `max_tokens` thinking and returns empty content."""
        if not self.openrouter_reasoning_effort:
            return None
        if not any(model.startswith(p) for p in self.openrouter_reasoning_model_prefixes):
            return None
        return {"effort": self.openrouter_reasoning_effort}

    def vllm_chat_template_kwargs(self, model: str, role: str | None = None) -> dict[str, object] | None:
        """vLLM `chat_template_kwargs`, or None for the template default.

        THE "Qwen/" PREFIX IS LOAD-BEARING: only a model id matching
        VLLM_THINKING_MODEL_PREFIXES gets `enable_thinking: False`; the bare
        slug "qwen3.8-27b" silently keeps thinking on. The answerer (role
        "evaluator") keeps the template default -- thinking ON -- when
        EVALUATOR_THINKING is set, because the baselines' answerer reasoned.
        """
        if not self.vllm_disable_thinking:
            return None
        if role == "evaluator" and self.evaluator_thinking:
            return None
        if not any(model.startswith(p) for p in self.vllm_thinking_model_prefixes):
            return None
        return {"enable_thinking": False}

    def memory_llm_env(self) -> dict[str, str]:
        """The MEMORY_LLM_* environment each episode process gets. The frozen
        `memory_system/llm.py` reads these and nothing else."""
        return {
            "MEMORY_LLM_BASE_URL": self.memory_llm_base_url,
            "MEMORY_LLM_MODEL": self.memory_llm_model,
            "MEMORY_LLM_API_KEY": os.environ.get(
                "MEMORY_LLM_API_KEY", os.environ.get("VLLM_API_KEY", "sk-local-dummy-key")),
            "MEMORY_LLM_TOKEN_BUDGET": str(self.memory_llm_token_budget),
            "MEMORY_LLM_CALL_BUDGET": str(self.memory_llm_call_budget),
            "MEMORY_LLM_MAX_TOKENS": str(self.memory_llm_max_tokens),
            # memory_system.queue reads MEMORY_QUEUE_BATCH; the loop's knob is
            # QUEUE_BATCH. Both are set so either name works in the child.
            "QUEUE_BATCH": str(self.queue_batch),
            "MEMORY_QUEUE_BATCH": str(self.queue_batch),
        }


_CONFIG: Config | None = None


def get() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config.from_env()
    return _CONFIG


def reload() -> Config:
    """Rebuild from the current environment (tests, `--fake-llm`)."""
    global _CONFIG
    _CONFIG = Config.from_env()
    return _CONFIG
