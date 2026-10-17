"""Environment-driven settings with fail-closed startup validation."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from gsm.domain.reasons import Reason
from gsm.errors import ConfigurationError


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    env: Literal["development", "test", "production"] = "development"
    database_url: str | None = None
    db_app_role: str | None = "gsm_app"

    dev_auth_enabled: bool = False
    dev_auth_secret: SecretStr | None = None
    admin_api_key: SecretStr | None = None
    audit_hmac_key: SecretStr | None = None

    cascade_mode: Literal["inline", "deferred"] = "inline"

    top_k: int = Field(default=8, ge=1, le=100)
    lexical_weight: float = Field(default=0.6, ge=0, le=1)
    vector_weight: float = Field(default=0.4, ge=0, le=1)
    vector_min_similarity: float = Field(default=0.35, ge=-1, le=1)
    # Semantic support a lexical match also needs (cosine similarity). -1 disables it;
    # meaningful only with a real sentence embedder (see docs/RETRIEVAL.md).
    vector_support_similarity: float = Field(default=-1.0, ge=-1, le=1)
    # Relevance bar (see gsm.retrieval.search): IDF-weighted query coverage an item
    # needs, the fraction of the best score it must reach, and duplicate collapsing.
    relevance_min_coverage: float = Field(default=0.2, ge=0, le=1)
    relevance_relative_cutoff: float = Field(default=0.5, ge=0, le=1)
    relevance_dedup: bool = True
    # Budgeted full context for answering: when everything a requester may see fits
    # these limits, the answer path receives all of it (oldest first) instead of a
    # ranked subset. 0 items disables it. Searches (e.g. deletion targets) stay ranked.
    full_context_max_items: int = Field(default=0, ge=0, le=5000)
    full_context_max_chars: int = Field(default=60000, ge=0, le=2_000_000)
    # How a model answerer's chosen action is reconciled with the disclosure plan:
    #   plan     - the draft must carry exactly the plan's action (default);
    #   answerer - the answerer may always decline (refuse / no_memory); it may give
    #              a content answer only when the plan has permitted evidence, and
    #              that content is still verified.
    release_action_policy: Literal["plan", "answerer"] = "plan"
    cache_ttl_seconds: int = Field(default=30, ge=0, le=3600)

    embeddings_provider: Literal["fake", "openai_compat", "sentence_transformers", "none"] = "fake"
    embedding_dim: int = Field(default=256, ge=8, le=4096)
    embeddings_model: str | None = None
    embeddings_device: str = "cpu"
    # Query planning: "deterministic" (default) or "decompose", which asks the LLM to
    # split a multi-part question into atomic sub-questions ranked separately.
    planner_provider: Literal["deterministic", "decompose"] = "deterministic"
    planner_cache_path: str | None = None
    planner_max_parts: int = Field(default=5, ge=1, le=8)
    generator_provider: Literal["deterministic", "openai_compat"] = "deterministic"
    interpreter_provider: Literal["rules", "openai_compat"] = "rules"
    semantic_verifier_provider: Literal["none", "openai_compat"] = "none"

    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_timeout_s: float = Field(default=30.0, gt=0, le=3600)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    # Output budget for the answer generator. Reasoning models count their hidden
    # thinking against it, so keep it generous when thinking is left on.
    llm_generator_max_tokens: int = Field(default=4096, ge=64, le=32768)
    llm_extra_body: dict[str, object] = Field(default_factory=dict)
    llm_price_input_per_mtok: float = Field(default=0.0, ge=0)
    llm_price_output_per_mtok: float = Field(default=0.0, ge=0)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = dict(os.environ if environ is None else environ)

        def get(name: str) -> str | None:
            value = env.get(f"GSM_{name}")
            return value if value not in (None, "") else None

        def flag(name: str) -> bool:
            return (get(name) or "false").strip().lower() in ("1", "true", "yes", "on")

        raw: dict[str, object] = {}
        mapping = {
            "ENV": "env",
            "DATABASE_URL": "database_url",
            "DEV_AUTH_SECRET": "dev_auth_secret",
            "ADMIN_API_KEY": "admin_api_key",
            "AUDIT_HMAC_KEY": "audit_hmac_key",
            "CASCADE_MODE": "cascade_mode",
            "TOP_K": "top_k",
            "LEXICAL_WEIGHT": "lexical_weight",
            "VECTOR_WEIGHT": "vector_weight",
            "VECTOR_MIN_SIMILARITY": "vector_min_similarity",
            "VECTOR_SUPPORT_SIMILARITY": "vector_support_similarity",
            "RELEVANCE_MIN_COVERAGE": "relevance_min_coverage",
            "RELEVANCE_RELATIVE_CUTOFF": "relevance_relative_cutoff",
            "FULL_CONTEXT_MAX_ITEMS": "full_context_max_items",
            "FULL_CONTEXT_MAX_CHARS": "full_context_max_chars",
            "RELEASE_ACTION_POLICY": "release_action_policy",
            "CACHE_TTL_SECONDS": "cache_ttl_seconds",
            "EMBEDDINGS_PROVIDER": "embeddings_provider",
            "EMBEDDING_DIM": "embedding_dim",
            "EMBEDDINGS_MODEL": "embeddings_model",
            "EMBEDDINGS_DEVICE": "embeddings_device",
            "PLANNER_PROVIDER": "planner_provider",
            "PLANNER_CACHE_PATH": "planner_cache_path",
            "PLANNER_MAX_PARTS": "planner_max_parts",
            "GENERATOR_PROVIDER": "generator_provider",
            "INTERPRETER_PROVIDER": "interpreter_provider",
            "SEMANTIC_VERIFIER_PROVIDER": "semantic_verifier_provider",
            "LLM_BASE_URL": "llm_base_url",
            "LLM_MODEL": "llm_model",
            "LLM_API_KEY": "llm_api_key",
            "LLM_TIMEOUT_S": "llm_timeout_s",
            "LLM_MAX_RETRIES": "llm_max_retries",
            "LLM_GENERATOR_MAX_TOKENS": "llm_generator_max_tokens",
            "LLM_PRICE_INPUT_PER_MTOK": "llm_price_input_per_mtok",
            "LLM_PRICE_OUTPUT_PER_MTOK": "llm_price_output_per_mtok",
        }
        for key, attr in mapping.items():
            value = get(key)
            if value is not None:
                raw[attr] = value
        if "GSM_DB_APP_ROLE" in env:
            raw["db_app_role"] = env["GSM_DB_APP_ROLE"] or None
        raw["dev_auth_enabled"] = flag("DEV_AUTH_ENABLED")
        extra = get("LLM_EXTRA_BODY")
        if extra is not None:
            try:
                parsed = json.loads(extra)
            except json.JSONDecodeError:
                raise ConfigurationError(Reason.UNSUPPORTED_OPERATION, "GSM_LLM_EXTRA_BODY is not JSON") from None
            if not isinstance(parsed, dict):
                raise ConfigurationError(Reason.UNSUPPORTED_OPERATION, "GSM_LLM_EXTRA_BODY must be an object")
            raw["llm_extra_body"] = parsed
        return cls.model_validate(raw)

    def validate_for_startup(self) -> None:
        """Fail closed on unsafe or incomplete configuration."""

        def fail(msg: str) -> None:
            raise ConfigurationError(Reason.UNSUPPORTED_OPERATION, msg)

        if self.database_url is None:
            fail("GSM_DATABASE_URL is required")
        if self.db_app_role is not None and not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", self.db_app_role):
            fail("GSM_DB_APP_ROLE is not a valid role name")
        if self.env == "production":
            if self.dev_auth_enabled:
                fail("the development authenticator cannot be enabled in production")
            if self.audit_hmac_key is None:
                fail("GSM_AUDIT_HMAC_KEY is required in production")
            if self.db_app_role is None:
                fail("row-level security role is required in production")
        if self.dev_auth_enabled and (
            self.dev_auth_secret is None or len(self.dev_auth_secret.get_secret_value()) < 16
        ):
            fail("GSM_DEV_AUTH_SECRET must be at least 16 characters")
        uses_llm = (
            "openai_compat"
            in (
                self.generator_provider,
                self.interpreter_provider,
                self.semantic_verifier_provider,
            )
            or self.planner_provider == "decompose"
        )
        if uses_llm and (self.llm_base_url is None or self.llm_model is None):
            fail("GSM_LLM_BASE_URL and GSM_LLM_MODEL are required for openai_compat providers and decomposition")
        if self.embeddings_provider == "openai_compat" and (self.llm_base_url is None or self.embeddings_model is None):
            fail("GSM_LLM_BASE_URL and GSM_EMBEDDINGS_MODEL are required for openai_compat embeddings")
        if abs(self.lexical_weight + self.vector_weight - 1.0) > 1e-6:
            fail("retrieval weights must sum to 1")
