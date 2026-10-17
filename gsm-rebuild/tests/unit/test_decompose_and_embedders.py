"""Query decomposition (model-backed planning) and the local sentence embedder."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from gsm.domain.context import PrincipalContext
from gsm.domain.types import AuthStrength
from gsm.providers.llm_cache import JsonlCache
from gsm.providers.openai_compat import OpenAICompatClient
from gsm.retrieval.decompose import DecomposingPlanner
from gsm.retrieval.planner import tokenize

NOW = datetime(2026, 3, 1, tzinfo=UTC)
CTX = PrincipalContext(
    tenant_id="t",
    workspace_id="w",
    principal_id="bob",
    roles=frozenset(),
    auth_time=NOW,
    auth_strength=AuthStrength.PASSWORD,
    request_time=NOW,
    correlation_id="corr_12345678",
    issued_by="test",
)


class FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.bodies: list[dict[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": self.content}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 12},
            },
        )


def planner(fake: FakeLLM, cache: Path | None = None, max_parts: int = 5) -> DecomposingPlanner:
    client = OpenAICompatClient("http://llm.invalid/v1", "m", max_retries=0, transport=httpx.MockTransport(fake))
    return DecomposingPlanner(client, cache=JsonlCache(cache) if cache else None, max_parts=max_parts)


QUESTION = "Who is the Apollo vendor, which kickoff room, and when is demo day?"


def test_a_multi_part_question_is_split_into_parts() -> None:
    fake = FakeLLM(
        json.dumps(
            {"parts": ["Who is the Apollo vendor?", "Which Apollo kickoff room?", "  When is Apollo demo day? "]}
        )
    )
    plan = planner(fake).plan(QUESTION, CTX, top_k=5, as_of=None, requested_view=None)
    assert plan.parts == ("Who is the Apollo vendor?", "Which Apollo kickoff room?", "When is Apollo demo day?")
    assert plan.tokens == tokenize(QUESTION) and plan.planner == "decompose"
    subplans = plan.subplans()
    assert [p.semantic_text for p in subplans] == [QUESTION, *plan.parts]
    assert subplans[1].tokens == ("apollo", "vendor")
    # the model sees only the question, with thinking left to the client's configuration
    (body,) = fake.bodies
    messages = body["messages"]
    assert isinstance(messages, list) and messages[1]["content"] == json.dumps(QUESTION)


def test_atomic_questions_and_failures_fall_back_to_the_whole_question() -> None:
    single = planner(FakeLLM(json.dumps({"parts": ["Who is the Apollo vendor?"]})))
    assert single.plan("Who is the Apollo hardware vendor?", CTX, top_k=5, as_of=None, requested_view=None).parts == ()
    garbage = planner(FakeLLM("I cannot do that"))
    assert garbage.plan(QUESTION, CTX, top_k=5, as_of=None, requested_view=None).parts == ()
    assert garbage.metrics.snapshot()["failures"] == 1
    short = FakeLLM(json.dumps({"parts": ["a", "b"]}))
    assert planner(short).plan("Apollo vendor?", CTX, top_k=5, as_of=None, requested_view=None).parts == ()
    assert short.bodies == []  # too short to ask for several things: no model call


def test_parts_are_capped_deduplicated_and_cached(tmp_path: Path) -> None:
    parts = ["Q one apollo?", "q ONE apollo?", "Q two apollo?", "Q three apollo?", "Q four apollo?"]
    first = FakeLLM(json.dumps({"parts": parts}))
    cache = tmp_path / "decompose.jsonl"
    p = planner(first, cache, max_parts=3)
    assert p.decompose(QUESTION) == ["Q one apollo?", "Q two apollo?", "Q three apollo?"]
    second = FakeLLM("unused")
    replay = planner(second, cache, max_parts=3)
    assert replay.decompose(QUESTION) == ["Q one apollo?", "Q two apollo?", "Q three apollo?"]
    assert second.bodies == [] and replay.cache_hits == 1


def test_sentence_transformer_embedder_is_normalized_and_semantic() -> None:
    pytest.importorskip("sentence_transformers")
    from gsm.providers.sentence_transformers import DEFAULT_MODEL, SentenceTransformerEmbedder

    if not os.environ.get("HF_HOME"):
        pytest.skip("HF_HOME not set; the model may not be available offline")
    try:
        emb = SentenceTransformerEmbedder(DEFAULT_MODEL)
    except Exception as exc:  # pragma: no cover - model not downloadable here
        pytest.skip(f"model unavailable: {type(exc).__name__}")
    a, b, c = emb.embed(
        ["When is the dentist appointment?", "The dental check-up is on Friday.", "The Q3 budget is approved."]
    )
    assert emb.dim == len(a) == 384 and emb.model_id == f"st:{DEFAULT_MODEL}"
    assert abs(sum(x * x for x in a) - 1.0) < 1e-4

    def cos(u: list[float], v: list[float]) -> float:
        return sum(x * y for x, y in zip(u, v, strict=True))

    assert cos(a, b) > cos(a, c) + 0.2
