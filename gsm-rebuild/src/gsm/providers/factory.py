"""Build providers from settings. Deterministic fakes unless configured otherwise."""

from __future__ import annotations

from pathlib import Path

from gsm.config import Settings
from gsm.providers.fake import HashingEmbedder, RuleBasedInterpreter
from gsm.providers.openai_compat import (
    OpenAICompatClient,
    OpenAICompatEmbedder,
    OpenAICompatGenerator,
    OpenAICompatInterpreter,
    OpenAICompatSemanticVerifier,
)
from gsm.release.generator import DeterministicGenerator
from gsm.retrieval.planner import DeterministicPlanner
from gsm.service import Providers


def build_providers(settings: Settings) -> Providers:
    client: OpenAICompatClient | None = None
    if settings.llm_base_url and (settings.llm_model or settings.embeddings_provider == "openai_compat"):
        client = OpenAICompatClient(
            settings.llm_base_url,
            settings.llm_model or "",
            api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
            timeout_s=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
            extra_body=settings.llm_extra_body,
        )
    providers = Providers(
        embedder=HashingEmbedder(settings.embedding_dim),
        interpreter=RuleBasedInterpreter(),
        generator=DeterministicGenerator(),
        semantic_verifier=None,
        planner=DeterministicPlanner(),
    )
    if settings.embeddings_provider == "none":
        providers.embedder = None
    elif settings.embeddings_provider == "openai_compat":
        assert client is not None and settings.embeddings_model is not None
        providers.embedder = OpenAICompatEmbedder(client, settings.embeddings_model, settings.embedding_dim)
    elif settings.embeddings_provider == "sentence_transformers":
        from gsm.providers.sentence_transformers import DEFAULT_MODEL, SentenceTransformerEmbedder

        providers.embedder = SentenceTransformerEmbedder(
            settings.embeddings_model or DEFAULT_MODEL, device=settings.embeddings_device
        )
    if settings.planner_provider == "decompose":
        from gsm.providers.llm_cache import JsonlCache
        from gsm.retrieval.decompose import DecomposingPlanner

        assert client is not None
        cache = JsonlCache(Path(settings.planner_cache_path)) if settings.planner_cache_path else None
        providers.planner = DecomposingPlanner(client, cache=cache, max_parts=settings.planner_max_parts)
    if settings.interpreter_provider == "openai_compat":
        assert client is not None
        providers.interpreter = OpenAICompatInterpreter(client)
    if settings.generator_provider == "openai_compat":
        assert client is not None
        providers.generator = OpenAICompatGenerator(client, max_tokens=settings.llm_generator_max_tokens)
    if settings.semantic_verifier_provider == "openai_compat":
        assert client is not None
        providers.semantic_verifier = OpenAICompatSemanticVerifier(client)
    return providers
