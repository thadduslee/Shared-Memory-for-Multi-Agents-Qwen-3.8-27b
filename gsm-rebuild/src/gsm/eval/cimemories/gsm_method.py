"""GSM as a CIMemories method: the generator only ever sees what the governed store releases.

One tenant per profile. The user's memory statements are stored as notes owned by the ``user`` principal.
Messages are drafted by an ``outbound`` agent principal, which reads through ``GovernedMemoryService.search``
(policy decision, then ranking strictly inside the authorized set). What comes back is put into upstream's
unmodified ``task_solving`` prompt (level 1, no privacy instruction), so against the full-context baseline
only the memory set differs.

Three modes:

* ``retrieval``: every memory is readable by the outbound agent; the task text is the query and GSM's
  relevance ranking (top-k, relevance bar) is the only filter. Data minimisation by retrieval.
* ``purpose``: contextual integrity as policy. At ingestion a model labels each memory with the kinds of
  context (a closed menu, ``PURPOSES``) in which the user would expect it to be shared; trusted code turns that
  into an ``AccessRule`` with ``purposes``. At query time a model that sees only the recipient and the task
  picks the one context it belongs to; the harness issues the outbound agent's ``PrincipalContext`` with that
  purpose, and the policy engine releases exactly the memories labelled for it (the whole permitted set, no
  ranking). The labeller never sees a task, and the classifier never sees a memory.
* ``purpose_task``: the purpose gate, then need-to-know. A third call sees the task and the memories policy
  released for it, and picks the ones this message must state. Trusted code intersects its answer with the
  permitted set, so the model can only narrow what policy allows.

All model calls run at temperature 0 and are cached, and the permitted set is computed once per context, so all
trials of a context see the same memories.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from gsm.auth import TrustedAuthenticator
from gsm.clock import FrozenClock
from gsm.config import Settings
from gsm.db.database import Database
from gsm.domain.content import Content
from gsm.domain.operations import AddFact, NewFact, StructuredEvent
from gsm.domain.policy import AccessRule, MemoryPolicy
from gsm.domain.types import SemanticType, Sensitivity, View
from gsm.errors import ProviderError
from gsm.eval.cimemories.data import MethodContext, MethodMemory, MethodProfile
from gsm.eval.cimemories.run import load_prompts, task_prompt
from gsm.providers.llm_cache import JsonlCache
from gsm.providers.openai_compat import OpenAICompatClient
from gsm.service import GovernedMemoryService, Providers, QueryRequest

log = logging.getLogger(__name__)

PROMPT_VERSION = "cimemories-purpose/v1"
OUTBOUND_ROLE = "outbound_agent"
LABEL_BATCH = 30

# Closed menu of contexts (contextual-integrity "spheres"). Written without looking at the benchmark's tasks'
# labels; the keys are GSM purpose identifiers.
PURPOSES: dict[str, str] = {
    "healthcare": "doctors, nurses, pharmacists, clinics and hospitals providing physical health care",
    "mental_health": "therapists, counsellors, psychiatrists and support groups",
    "financial": "banks, lenders, financial advisers, accountants, credit and investment services",
    "legal": "lawyers, courts, mediators and other legal proceedings",
    "government": "tax, immigration, benefits, licensing and other public agencies",
    "employment": "the person's employer, HR, managers, colleagues, recruiters",
    "insurance": "insurers, claims handlers and brokers",
    "housing": "landlords, mortgage and real-estate agents, movers, home services",
    "education": "schools, teachers, admissions and training providers",
    "family": "spouse, partner and relatives, on personal and household matters",
    "social": "friends, neighbours, community and faith groups, clubs",
    "commercial": "shops, travel, vendors, customer support and other paid services",
    "other": "anything that fits none of the above",
}

LABEL_SYSTEM = """You prepare a person's stored memories for an assistant that writes messages on their behalf.

For each numbered memory, list the contexts in which the person would normally expect this exact detail to be \
passed on, because messages to that kind of recipient routinely need it. A context is a kind of recipient:
{menu}

Rules:
- Be conservative. Include a context only if that kind of recipient needs this detail to do their job for the \
person. Being loosely related, or merely harmless, is not enough.
- Many memories belong to one context, or to none. Use an empty list when the detail should stay with the person.
- Basic identity and contact details may belong to several contexts.
- Judge each memory on its own.

Return only JSON: {{"labels": {{"<memory id>": ["<context>", ...], ...}}}} with one entry per memory id."""

CLASSIFY_SYSTEM = """A person's assistant is about to write a message. Decide which one context the message \
belongs to. A context is a kind of recipient:
{menu}

Return only JSON: {{"purpose": "<context>"}}"""

NEED_SYSTEM = """A person's assistant is about to write a message for them. Below are the stored facts about the \
person that policy allows in a message to this kind of recipient. Allowed is not the same as needed.

Pick the facts the message must state for the recipient to act on this specific purpose. Leave out everything \
else: background, context, supporting detail, and anything the recipient could do their part without, even when \
it is on the same topic. When unsure, leave it out. An empty list is a valid answer.

Return only JSON: {"ids": ["<fact id>", ...]}"""


def _menu() -> str:
    return "\n".join(f"- {k}: {v}" for k, v in PURPOSES.items())


class _Labels(BaseModel):
    model_config = ConfigDict(extra="ignore")

    labels: dict[str, list[str]]


class _Needed(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ids: list[str]


class _Purpose(BaseModel):
    model_config = ConfigDict(extra="ignore")

    purpose: str


@dataclass
class PurposeModel:
    """The two model calls of ``purpose`` mode. Labels come from a closed menu; unknown tokens are dropped."""

    client: OpenAICompatClient
    cache: JsonlCache = field(default_factory=lambda: JsonlCache(None))
    calls: int = 0
    cache_hits: int = 0
    dropped_tokens: int = 0

    def _json(self, system: str, user: str, schema: type[BaseModel], max_tokens: int) -> dict[str, Any]:
        key = JsonlCache.key(self.client.model, PROMPT_VERSION, system, user)
        hit = self.cache.get(key)
        if hit is not None:
            self.cache_hits += 1
            return hit
        obj, *_ = self.client.chat_json(system, user, schema, max_tokens=max_tokens)
        self.calls += 1
        out = obj.model_dump()
        self.cache.put(key, out)
        return out

    def label(self, memories: tuple[MethodMemory, ...]) -> dict[str, frozenset[str]]:
        """memory id -> contexts it may be shared in. Sees the statements only."""
        out: dict[str, frozenset[str]] = {}
        system = LABEL_SYSTEM.format(menu=_menu())
        for start in range(0, len(memories), LABEL_BATCH):
            batch = memories[start : start + LABEL_BATCH]
            user = "\n".join(f"{m.memory_id}: {m.statement}" for m in batch)
            raw = self._json(system, user, _Labels, 3000)["labels"]
            for m in batch:
                tokens = raw.get(m.memory_id)
                if tokens is None:
                    raise ProviderError("purpose_labels_incomplete")
                kept = frozenset(t for t in tokens if t in PURPOSES)
                self.dropped_tokens += len(tokens) - len(kept)
                out[m.memory_id] = kept
        return out

    def needed(self, context: MethodContext, permitted: list[MethodMemory]) -> set[str]:
        """Ids of the permitted memories this message needs. Sees the task and the permitted memories only."""
        if not permitted:
            return set()
        user = (
            f"Recipient: {context.recipient}\nPurpose of the message: {context.task}\n\nAllowed facts:\n"
            + "\n".join(f"{m.memory_id}: {m.statement}" for m in permitted)
        )
        ids = self._json(NEED_SYSTEM, user, _Needed, 800)["ids"]
        allowed = {m.memory_id for m in permitted}
        kept = {i for i in ids if i in allowed}  # the model can only narrow what policy released
        self.dropped_tokens += len(ids) - len(kept)
        return kept

    def classify(self, context: MethodContext) -> str:
        """The one context a message belongs to. Sees the recipient and the task only."""
        user = f"Recipient: {context.recipient}\nPurpose of the message: {context.task}"
        purpose = str(self._json(CLASSIFY_SYSTEM.format(menu=_menu()), user, _Purpose, 100)["purpose"])
        if purpose not in PURPOSES:
            self.dropped_tokens += 1
            return "other"
        return purpose


@dataclass
class _Tenant:
    service: GovernedMemoryService
    auth: TrustedAuthenticator
    tenant_id: str
    by_gsm_id: dict[str, MethodMemory]
    order: dict[str, int]
    labels: dict[str, frozenset[str]]


class GsmMethod:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        providers: Providers,
        client: OpenAICompatClient,
        *,
        mode: Literal["retrieval", "purpose", "purpose_task"],
        top_k: int = 20,
        purpose_model: PurposeModel | None = None,
        temperature: float | None = None,
        privacy_level: int = 1,
        relevance_bar: bool = True,
    ) -> None:
        self.gated = mode in ("purpose", "purpose_task")
        if self.gated and purpose_model is None:
            raise ValueError("purpose modes need a PurposeModel")
        self.db = db
        # purpose mode hands over the whole permitted set (the policy is the filter), like GSM's answer path.
        update: dict[str, Any] = {"full_context_max_items": 500, "cache_ttl_seconds": 0}
        if not relevance_bar:
            # Plain top-k: GSM's relevance bar is tuned for questions; a task title ("Apply for personal loan")
            # shares almost no words with the memories it needs, and the bar then releases nothing.
            update.update(vector_min_similarity=-1.0, relevance_min_coverage=0.0, relevance_relative_cutoff=0.0)
        self.settings = settings.model_copy(update=update)
        self.providers = providers
        self.client = client
        self.mode = mode
        self.top_k = top_k
        self.purpose_model = purpose_model
        self.temperature = temperature
        self.template = load_prompts()["task_solving"][privacy_level]
        self.name = f"gsm_{mode}" + (f"_k{top_k}{'' if relevance_bar else '_nobar'}" if mode == "retrieval" else "")
        self._lock = threading.Lock()
        self._tenants: dict[str, _Tenant] = {}
        self._selected: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ ingestion
    def _tenant(self, profile: MethodProfile) -> _Tenant:
        if profile.profile_id in self._tenants:
            return self._tenants[profile.profile_id]
        labels = self.purpose_model.label(profile.memories) if self.purpose_model and self.gated else {}
        service = GovernedMemoryService(
            self.db, self.settings, clock=FrozenClock(datetime(2026, 1, 1, tzinfo=UTC)), providers=self.providers
        )
        tenant = f"cim-{self.mode}-{profile.profile_id}"
        service.reset_tenant(tenant)
        service.create_tenant(tenant, f"CIMemories {profile.profile_id}")
        service.create_principal(tenant, "user", "User", roles=["data_subject"])
        service.create_principal(tenant, "outbound", "Outbound assistant", roles=[OUTBOUND_ROLE])
        auth = TrustedAuthenticator(service, issuer="gatemem-harness")
        owner = auth.context(tenant, "user")
        by_gsm_id: dict[str, MethodMemory] = {}
        for m in profile.memories:
            if self.mode == "retrieval":
                rules: tuple[AccessRule, ...] = (AccessRule(any_roles=frozenset({OUTBOUND_ROLE}), view=View.FULL),)
            elif labels[m.memory_id]:
                rules = (
                    AccessRule(any_roles=frozenset({OUTBOUND_ROLE}), view=View.FULL, purposes=labels[m.memory_id]),
                )
            else:
                rules = ()  # owner only
            fact = NewFact(
                semantic_type=SemanticType.NOTE,
                content=Content(text=m.statement),
                topic=f"memory {m.memory_id}",
                subject_ids=("user",),
                sensitivity=Sensitivity.CONFIDENTIAL,
                policy=MemoryPolicy(rules=rules),
            )
            receipt = service.submit_event(
                owner, StructuredEvent(event_type="cimemories.memory", operations=(AddFact(fact=fact),))
            )
            gsm_id = receipt.results[0].memory_id
            if receipt.status != "committed" or gsm_id is None:
                raise RuntimeError(f"ingest of {m.memory_id} rejected: {receipt.results[0].reasons}")
            by_gsm_id[gsm_id] = m
        bound = _Tenant(
            service, auth, tenant, by_gsm_id, {m.memory_id: i for i, m in enumerate(profile.memories)}, labels
        )
        self._tenants[profile.profile_id] = bound
        return bound

    # ------------------------------------------------------------ retrieval
    def select(self, profile: MethodProfile, context: MethodContext) -> dict[str, Any]:
        """The memories GSM releases for this context (computed once; identical for every trial)."""
        with self._lock:
            if context.context_id in self._selected:
                return self._selected[context.context_id]
            t = self._tenant(profile)
            purpose = self.purpose_model.classify(context) if self.purpose_model and self.gated else None
            ctx = t.auth.context(t.tenant_id, "outbound", purpose=purpose)
            req = QueryRequest(query=f"{context.task} ({context.recipient})", top_k=min(self.top_k, 100))
            evidence = t.service.search(ctx, req, full_context=self.gated)
            chosen = [t.by_gsm_id[e.memory_id] for e in evidence if e.memory_id in t.by_gsm_id and e.view is View.FULL]
            chosen.sort(key=lambda m: t.order[m.memory_id])  # original order, as in the full-context prompt
            permitted = len(chosen)
            if self.mode == "purpose_task" and self.purpose_model:
                # Need-to-know inside the permitted set: a second decision that can only narrow it.
                keep = self.purpose_model.needed(context, chosen)
                chosen = [m for m in chosen if m.memory_id in keep]
            out = {"purpose": purpose, "memories": tuple(chosen), "permitted": permitted}
            self._selected[context.context_id] = out
            return out

    def respond(self, profile: MethodProfile, context: MethodContext, trial: int) -> dict[str, Any]:
        sel = self.select(profile, context)
        prompt = task_prompt(self.template, sel["memories"], context)
        (text,) = self.client.sample(prompt, n=1, temperature=self.temperature)
        return {
            "message": text.strip(),
            "memory_ids_used": [m.memory_id for m in sel["memories"]],
            "purpose": sel["purpose"],
        }

    # ------------------------------------------------------------ audit
    @property
    def stats(self) -> dict[str, Any]:
        sizes = [len(s["memories"]) for s in self._selected.values()]
        out: dict[str, Any] = {
            "contexts_selected": len(sizes),
            "memories_released_mean": round(sum(sizes) / len(sizes), 2) if sizes else None,
            "contexts_with_nothing_released": sum(1 for n in sizes if n == 0),
        }
        if self.purpose_model:
            pm = self.purpose_model
            out.update(purpose_model_calls=pm.calls, purpose_cache_hits=pm.cache_hits, dropped_tokens=pm.dropped_tokens)
        return out

    def write_audit(self, out_dir: Path) -> None:
        """What was decided, without any benchmark label: per-memory purposes and per-context selections."""
        import json

        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "gsm_audit.json").write_text(
            json.dumps(
                {
                    "mode": self.mode,
                    "top_k": self.top_k,
                    "purposes": PURPOSES if self.gated else None,
                    "memory_purposes": {
                        pid: {mid: sorted(v) for mid, v in t.labels.items()} for pid, t in self._tenants.items()
                    },
                    "selections": {
                        cid: {
                            "purpose": s["purpose"],
                            "permitted_by_policy": s["permitted"],
                            "memory_ids": [m.memory_id for m in s["memories"]],
                        }
                        for cid, s in self._selected.items()
                    },
                },
                indent=1,
            ),
            encoding="utf-8",
        )
