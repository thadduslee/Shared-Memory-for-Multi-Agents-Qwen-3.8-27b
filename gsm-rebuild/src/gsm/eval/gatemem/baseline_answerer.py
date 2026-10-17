"""The GateMem baselines' own answerer, fed by GSM.

This is not a re-implementation. It subclasses the benchmark's
``bench.agents.base.BaseMemoryAgent`` and calls its unmodified ``_run_llm``, so
the answer prompt (``bench/prompts/query_prompt.txt``), the prompt variables
(asker id and role, the asker's relationship facts, the retrieved-memory block,
the query, the domain label and access-policy block), the JSON parsing and its
fallbacks, and the LLM client (``bench.llm.router.LLMRouter``) are exactly what
the baselines used. The defaults below are the ``qwen38_27b`` model block of
``configs/sweeps/paper_matrix.yaml`` (thinking is left at the server default,
as the bench client never sends a thinking toggle).

Only the retrieved memory differs: it is GSM's permitted evidence for the asker.
With ``allowed_by_default`` the evidence starts with one record stating GSM's own
access decision (the asker is authorized for every record shown). The benchmark's
policy otherwise refuses roles it has no relationship facts for (it records no
assignment for nurses, pharmacists, social workers or schedulers), even when GSM
authorized them; this tells the answerer what GSM decided, without editing the
benchmark's prompt.
Hidden checkpoint fields are never passed; ``query_type`` and ``expected_action``
are empty (the bench uses them only in its offline stub mode).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gsm.domain.types import QueryAction, View
from gsm.eval.gatemem.sanitize import MethodCheckpoint, MethodEpisode
from gsm.providers.base import DraftResponse, GeneratorEvidence, GeneratorPlan, ProviderMetrics

_ACTIONS = {
    "answer": QueryAction.ANSWER,
    "answer_redacted": QueryAction.ANSWER_REDACTED,
    "refuse": QueryAction.REFUSE,
    "no_memory": QueryAction.NO_MEMORY,
}


def load_bench(repo_root: Path) -> tuple[Any, Any, Any, Any]:
    """Import the benchmark package read-only.

    The bench imports some modules lazily (inside ``_run_llm`` and
    ``LLMRouter.__init__``), so bytecode writing is switched off for the rest of
    the process rather than only around these imports: nothing may be written
    into the benchmark's tree.
    """
    sys.dont_write_bytecode = True
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from bench.agents import utils_llm  # type: ignore[import-not-found]  # noqa: F401 - imported lazily by _run_llm
    from bench.agents.base import BaseMemoryAgent, Checkpoint  # type: ignore[import-not-found]
    from bench.llm.router import LLMRouter  # type: ignore[import-not-found]
    from bench.llm.types import LLMConfig  # type: ignore[import-not-found]

    return BaseMemoryAgent, Checkpoint, LLMRouter, LLMConfig


@dataclass(frozen=True)
class BaselineAnswererConfig:
    """Defaults: configs/sweeps/paper_matrix.yaml, model block ``qwen38_27b``."""

    provider: str = "llama"
    model: str = "Qwen/Qwen3.8-27B"
    api_base: str = "http://localhost:8003/v1"
    api_key_env: str = "LOCAL_API_KEY"
    temperature: float = 0.2
    max_output_tokens: int = 16384
    timeout_s: float = 1800.0
    max_retries: int = 3


def make_router(repo_root: Path, cfg: BaselineAnswererConfig) -> Any:
    _, _, llm_router, llm_config = load_bench(repo_root)
    return llm_router(
        llm_config(
            provider=cfg.provider,
            model=cfg.model,
            temperature=cfg.temperature,
            max_output_tokens=cfg.max_output_tokens,
            timeout_s=cfg.timeout_s,
            max_retries=cfg.max_retries,
            api_base=cfg.api_base,
            api_key_env=cfg.api_key_env,
        )
    )


def episode_for_bench(ep: MethodEpisode) -> dict[str, Any]:
    """The episode metadata the bench answerer reads (no turns, no checkpoints)."""
    return {
        "episode_id": ep.episode_id,
        "domain": ep.domain,
        "entities": {
            "principals": [{"principal_id": p, "role": r, "display_name": n} for p, r, n in ep.principals],
            "relationships": [dict(r) for r in ep.relationships],
        },
    }


ACCESS_CHECK_ID = "gsm-access-check"
ACCESS_CHECK_SPEAKER = "gsm_access_control"


def access_check_text(asker: str, role: str) -> str:
    """GSM's access decision, stated to the answerer (``allowed_by_default``)."""
    return (
        f"Access check (GSM): requester {asker} ({role or 'no role'}) is authorized to read every record below. "
        "Records this requester is not authorized for were withheld."
    )


class BaselineAnswerer:
    """GSM ``AnswerGenerator`` that is the baselines' answerer (one instance per episode)."""

    name = "gatemem-baseline-answerer"

    def __init__(self, router: Any, repo_root: Path, *, allowed_by_default: bool = False) -> None:
        base_agent, checkpoint_cls, _, _ = load_bench(repo_root)
        self.allowed_by_default = allowed_by_default

        class _Agent(base_agent):  # type: ignore[misc, valid-type]
            def ingest(self, turn: Any) -> None:  # GSM owns memory
                return None

        self._agent = _Agent(llm_router=router)
        self._checkpoint_cls = checkpoint_cls
        self._checkpoint: Any = None
        self.metrics = ProviderMetrics()
        self.speaker_of: Callable[[str], str | None] = lambda _mid: None
        self.last_output: dict[str, Any] | None = None

    def bind_episode(self, ep: MethodEpisode) -> None:
        self._agent.reset(episode_for_bench(ep))

    def bind_checkpoint(self, c: MethodCheckpoint) -> None:
        self._checkpoint = self._checkpoint_cls(
            checkpoint_id=c.checkpoint_id,
            episode_id=c.episode_id,
            as_of_turn_id=c.as_of_turn_id,
            asker_principal_id=c.asker_principal_id,
            asker_role=c.asker_role,
            query_type="",
            query_text=c.query_text,
        )
        self.last_output = None

    def generate(self, query: str, plan: GeneratorPlan, evidence: Sequence[GeneratorEvidence]) -> DraftResponse:
        if self._checkpoint is None:
            raise RuntimeError("bind_checkpoint() must be called before generate()")
        retrieved = [
            {
                "memory_id": e.memory_id,
                "text": e.text,
                # Attribution only for full views; limited views stay anonymous.
                "principal_id": self.speaker_of(e.memory_id) if e.view is View.FULL else None,
            }
            for e in evidence
        ]
        if self.allowed_by_default and retrieved:
            c = self._checkpoint
            retrieved.insert(
                0,
                {
                    "memory_id": ACCESS_CHECK_ID,
                    "text": access_check_text(c.asker_principal_id, c.asker_role),
                    "principal_id": ACCESS_CHECK_SPEAKER,
                },
            )
        started = time.perf_counter()
        try:
            out = self._agent._run_llm(checkpoint=self._checkpoint, retrieved_memory=retrieved)
        except Exception:
            self.metrics.record(ok=False)
            raise
        latency = (time.perf_counter() - started) * 1000
        self.last_output = out
        usage = out.get("llm_usage") or {}
        pt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        ct = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        self.metrics.record(ok=True, prompt_tokens=pt, completion_tokens=ct, latency_ms=latency)
        return DraftResponse(
            action=_ACTIONS.get(str(out.get("action")), QueryAction.ANSWER),
            text=str(out.get("answer") or ""),
            generator=self.name,
            prompt_tokens=pt,
            completion_tokens=ct,
            latency_ms=latency,
        )
