"""Configuration: model endpoints, feature switches, budgets. Everything comes from env with defaults.

Three model roles (SPEC §14):
  memory   -- the interpreter and the organiser (any model; used as-is, never trained)
  answer   -- the benchmark answerer (Qwen3.8-27B, thinking ON)
  judge    -- the judge (Qwen3.8-27B)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ModelEndpoint:
    base_url: str
    api_key: str
    model: str
    thinking: bool = False
    max_tokens: int = 2048
    timeout_s: float = 300.0
    ca_bundle: str = ""
    max_concurrency: int = 6

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    @classmethod
    def from_env(cls, prefix: str, *, thinking: bool, max_tokens: int, concurrency: int) -> "ModelEndpoint":
        base = _env(f"{prefix}_BASE_URL") or _env("SOCLAAS_BASE_URL")
        key = _env(f"{prefix}_API_KEY") or _env("SOCLAAS_API_KEY")
        model = _env(f"{prefix}_MODEL") or _env("SOCLAAS_MODEL") or "qwen3.8:27b"
        return cls(
            base_url=base.rstrip("/"),
            api_key=key,
            model=model,
            thinking=_bool(f"{prefix}_THINKING", thinking),
            max_tokens=int(_env(f"{prefix}_MAX_TOKENS", str(max_tokens))),
            timeout_s=float(_env(f"{prefix}_TIMEOUT_S", "600")),
            ca_bundle=_env(f"{prefix}_CA_BUNDLE") or _env("SSL_CERT_FILE"),
            max_concurrency=int(_env(f"{prefix}_MAX_CONCURRENCY", str(concurrency))),
        )


@dataclass
class Features:
    """Every mechanism can be switched off; this is how ablations run (SPEC §12.6)."""

    model_interpretation: bool = True     # label + auxiliary calls; off -> deterministic fallback
    reference_resolution: bool = True
    completeness_guard: bool = True
    privacy_floor: bool = True
    keep_readers: bool = True
    denial_ledger: bool = True
    rescoping: bool = True
    widen_agreement: bool = True          # §8.5: widening needs a second independent check
    model_deletion: bool = True
    partial_delete: bool = True
    history_deletion: bool = True
    supersession: bool = True
    unqualified_relationship_access: bool = True
    organise: bool = True                 # §8 master switch
    enrich: bool = True
    link: bool = True
    evolve_neighbours: bool = True
    cluster: bool = True
    consolidate: bool = True
    link_expansion: bool = True
    relevance_before_policy: bool = True
    full_context_budget: bool = True
    query_split: bool = False
    read_time_signals: bool = True
    release_check: bool = True
    open_access: bool = False             # settings without access rules: every principal reads every record

    @classmethod
    def from_env(cls) -> "Features":
        out = cls()
        for f in fields(cls):
            setattr(out, f.name, _bool("GOVMEM_FEATURE_" + f.name.upper(), getattr(out, f.name)))
        return out

    def to_dict(self) -> dict[str, bool]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class Settings:
    memory: ModelEndpoint
    answer: ModelEndpoint
    judge: ModelEndpoint
    features: Features = field(default_factory=Features)
    memory_call_budget: int = 3000
    memory_token_budget: int = 12_000_000
    neighbours_k: int = 6
    cluster_every: int = 10
    budget_records: int = 400
    budget_chars: int = 60_000
    ranked_top_k: int = 20
    supersede_similarity: float = 0.45
    relevance_floor: float = 0.30
    roster_window: int = 0          # turns; 0 = whole declared cast. Continuous memories need a window.
    gatemem_repo: Path = REPO_ROOT

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            memory=ModelEndpoint.from_env("GOVMEM_MEMORY", thinking=False, max_tokens=3072, concurrency=8),
            answer=ModelEndpoint.from_env("GOVMEM_ANSWER", thinking=True, max_tokens=16384, concurrency=6),
            judge=ModelEndpoint.from_env("GOVMEM_JUDGE", thinking=False, max_tokens=2048, concurrency=6),
            features=Features.from_env(),
            memory_call_budget=int(_env("GOVMEM_MEMORY_CALL_BUDGET", "3000")),
            memory_token_budget=int(_env("GOVMEM_MEMORY_TOKEN_BUDGET", "12000000")),
            neighbours_k=int(_env("GOVMEM_NEIGHBOURS_K", "6")),
            cluster_every=int(_env("GOVMEM_CLUSTER_EVERY", "10")),
            budget_records=int(_env("GOVMEM_BUDGET_RECORDS", "400")),
            budget_chars=int(_env("GOVMEM_BUDGET_CHARS", "60000")),
            ranked_top_k=int(_env("GOVMEM_RANKED_TOP_K", "20")),
            supersede_similarity=float(_env("GOVMEM_SUPERSEDE_SIMILARITY", "0.45")),
            relevance_floor=float(_env("GOVMEM_RELEVANCE_FLOOR", "0.30")),
            roster_window=int(_env("GOVMEM_ROSTER_WINDOW", "0")),
            gatemem_repo=Path(_env("GATEMEM_REPO", str(REPO_ROOT))),
        )
