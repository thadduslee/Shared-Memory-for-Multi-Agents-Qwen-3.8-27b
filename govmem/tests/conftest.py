"""Shared fixtures: a scripted fake model and synthetic conversations whose names mean nothing."""

from __future__ import annotations

import json
import os
from typing import Any, Callable

import pytest

os.environ.setdefault("GOVMEM_EMBED_BACKEND", "hash")      # tests never load a real encoder

from govmem.api import Memory, Relationship  # noqa: E402
from govmem.config import Features, ModelEndpoint, Settings  # noqa: E402
from govmem.embed import Embedder  # noqa: E402
from govmem.llm import ChatResult  # noqa: E402


class FakeChat:
    """Answers by call tag. `handlers[tag](system, user) -> dict`; records every prompt it sees."""

    def __init__(self, handlers: dict[str, Callable[[str, str], dict]] | None = None) -> None:
        self.handlers = handlers or {}
        self.seen: list[tuple[str, str, str]] = []

    def chat(self, messages, *, max_tokens=None, temperature=0.0, thinking=None, tag=""):  # noqa: ANN001
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        user = "\n".join(m["content"] for m in messages if m["role"] == "user")
        self.seen.append((tag, system, user))
        h = self.handlers.get(tag)
        if h is None:
            return ChatResult(text="{}", usage={"total_tokens": 10})
        return ChatResult(text=json.dumps(h(system, user)), usage={"total_tokens": 10})


def settings(**feature_overrides: Any) -> Settings:
    ep = ModelEndpoint(base_url="", api_key="", model="fake")
    f = Features()
    for k, v in feature_overrides.items():
        setattr(f, k, v)
    return Settings(memory=ep, answer=ep, judge=ep, features=f)


PEOPLE = [
    {"id": "p_ash", "role": "r_alpha", "display_name": "Ash Quell"},
    {"id": "p_bex", "role": "r_beta", "display_name": "Bex Morrow"},
    {"id": "p_cyd", "role": "r_gamma", "display_name": "Cyd Varn"},
    {"id": "p_dov", "role": "r_gamma", "display_name": "Dov Pell"},
]


def make_memory(tmp_path=None, chat: Any = None, **features: Any) -> Memory:  # noqa: ANN001
    path = str(tmp_path / "m.db") if tmp_path is not None else ":memory:"
    m = Memory(path, settings(**features), chat=chat, embedder=Embedder())
    m.declare(PEOPLE, relationships=[Relationship("link_one", {"x_id": "p_bex", "y_id": "p_ash"}, {})])
    return m


@pytest.fixture
def fake() -> FakeChat:
    return FakeChat()
