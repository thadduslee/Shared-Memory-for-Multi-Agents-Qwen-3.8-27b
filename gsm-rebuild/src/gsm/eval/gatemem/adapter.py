"""Run GSM as a GateMem method.

For each episode: a fresh tenant is bootstrapped with the episode's principals,
roles and relationships (GateMem's authenticated identities play the role of
GSM's trusted authenticator). Turns are ingested in order through the
governed service; at each checkpoint boundary the asker's query goes through
the full GSM pipeline and the released answer becomes the prediction.
Episodes are independent and can run in parallel (``workers``); turns within
an episode are always processed in order.

Interpretation of free-text turns is pluggable:

``DeterministicTurnPolicy`` (default) is a transparent heuristic baseline:
every turn is a note owned by its speaker; turns with privacy cues are
speaker-private; other turns are readable by relationship peers; turns with
deletion cues are lifecycle commands (``resolve_deletions="lexical"`` lets the
harness delete what a governed query surfaces for the requester).

``QwenTurnInterpreter`` (``gsm.eval.gatemem.qwen_interpreter``) labels each turn
with a model. ``LabelMapper`` turns the label into a GSM policy:

* audiences come only from a closed per-episode menu (relationship groups,
  roles, named principals, the people the fact is about, the speaker, everyone);
  unknown tokens are dropped;
* deterministic floor: when the turn has privacy cues or the model marked
  protected values, "everyone" is removed from the full-message audience;
* a shareable summary becomes a redacted view only if it contains none of the
  protected values; protected values must occur verbatim in the turn;
* anyone in a memory's full audience may update or delete it (GateMem's
  deletion requests often come from someone other than the author);
* existence is acknowledged to episode participants, so over-scoped requests
  get REFUSE rather than NO_MEMORY; deleted memories are never acknowledged;
* single-valued facts carry a semantic key; a later fact with the same key
  supersedes the earlier one when GSM authorizes it. When the supersession
  would widen access (a different speaker, a different audience), the speaker
  retires the outdated value instead, if GSM lets them update it, and the new
  value is stored under its own policy.

For deletion requests the harness runs governed queries *as the requester*,
the model picks targets among those results, and each deletion is submitted as
the requester and authorized by GSM. Deletion-request turns are never stored.

Adaptive re-scoping: a consent or revocation changes who may know memories
stored *earlier*. Targets are chosen the same way as deletion targets, then
the speaker restricts them (RESTRICT_ACCESS, authorized like an update) or
shares them (lift + GRANT_ACCESS, authorized for owners and subjects). Questions
are not stored.

Neither interpreter ever sees checkpoint queries or scoring annotations.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from gsm.auth import TrustedAuthenticator
from gsm.clock import FrozenClock
from gsm.config import Settings
from gsm.db.database import Database
from gsm.disclosure.schemas import ContentValidationError, get_schema, normalize, validate_topic
from gsm.domain.content import Content
from gsm.domain.context import PrincipalContext
from gsm.domain.operations import (
    AddFact,
    CandidateMemoryOperation,
    DeleteFact,
    GrantAccess,
    NewFact,
    RestrictAccess,
    RetireFact,
    StructuredEvent,
    SupersedeFact,
)
from gsm.domain.policy import AccessRule, MemoryPolicy, PrincipalPredicate, RelationRequirement
from gsm.domain.types import AuthStrength, LifecycleState, QueryAction, SemanticType, Sensitivity, VersionState, View
from gsm.errors import ContextError, GsmError
from gsm.eval.gatemem.baseline_answerer import BaselineAnswerer
from gsm.eval.gatemem.continuous import merge_domain
from gsm.eval.gatemem.qwen_interpreter import Group, QwenTurnInterpreter, RescopeIntent, Roster, TurnLabel
from gsm.eval.gatemem.repairs import REPAIRS
from gsm.eval.gatemem.sanitize import MethodCheckpoint, MethodEpisode, MethodTurn, load_for_method
from gsm.policy.engine import PolicyEngine
from gsm.policy.facts import AuthSnapshot, MemoryFacts, RelationFact, VersionFacts
from gsm.providers.base import DraftResponse, GeneratorEvidence, GeneratorPlan
from gsm.service import GovernedMemoryService, Providers, QueryRequest

log = logging.getLogger("gsm.gatemem")

ACTION_MAP = {
    QueryAction.ANSWER: "answer",
    QueryAction.ANSWER_REDACTED: "answer_redacted",
    QueryAction.REFUSE: "refuse",
    QueryAction.NO_MEMORY: "no_memory",
    QueryAction.ESCALATE: "refuse",  # GateMem has no escalate action
}
VERIFIER_BLOCK_REASONS = frozenset(
    {
        "PROTECTED_VALUE_DETECTED",
        "PROHIBITED_CATEGORY_DETECTED",
        "UNAPPROVED_MEMORY_CITED",
        "ACTION_MISMATCH",
        "CONTENT_IN_NON_ANSWER",
        "STALE_EVIDENCE",
        "SEMANTIC_VERIFIER_BLOCK",
    }
)
PRIVACY_CUES = re.compile(
    r"\b(private|privately|confidential|personal|do not share|don't share|dont share|between us|only for|"
    r"diagnos\w*|medical|medication|salary|compensation|password|credential|secret|ssn|home address|"
    r"disciplinary|performance plan|pip)\b",
    re.IGNORECASE,
)
DELETION_CUES = re.compile(r"\b(delete|forget|remove|erase|wipe|purge|scrub|redact)\b", re.IGNORECASE)
_SUBJECT_KEYS = ("principal_id", "clinician_id", "family_id")
_OBJECT_KEYS = (
    ("project_id", "project"),
    ("patient_id", "principal"),
    ("for_principal_id", "principal"),
    ("manages", "principal"),
    ("program_id", "program"),
    ("case_id", "case"),
    ("household_id", "household"),
    ("for_household_id", "household"),
    ("guest_id", "principal"),
)
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
_PROBE_TIME = datetime(2100, 1, 1, tzinfo=UTC)


def _ident(s: str) -> str:
    out = re.sub(r"[^a-z0-9_]", "_", s.lower()).strip("_") or "x"
    return ("r_" + out if not out[0].isalpha() else out)[:64]


def _obj(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", s)[:128] or "x"


def _key(raw: str | None) -> str | None:
    if not raw:
        return None
    k = re.sub(r"[^a-z0-9_.]+", "_", raw.strip().lower()).strip("._")
    if not k:
        return None
    return ("k_" + k if not k[0].isalpha() else k)[:128]


def tenant_for(episode_id: str) -> str:
    return "gm-" + hashlib.sha1(episode_id.encode(), usedforsecurity=False).hexdigest()[:20]


@dataclass
class EpisodeBinding:
    tenant_id: str
    principals: set[str]
    roles: dict[str, str] = field(default_factory=dict)
    relations: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    # speaker -> relation requirements that identify their relationship peers
    peers: dict[str, list[RelationRequirement]] = field(default_factory=dict)
    roster: Roster | None = None


def bootstrap_episode(service: GovernedMemoryService, ep: MethodEpisode) -> EpisodeBinding:
    tenant = tenant_for(ep.episode_id)
    # GateMem resets the agent at the start of every episode. Dropping any tenant
    # left by a previous run also makes re-runs on the same database work
    # (reset_tenant refuses in production).
    service.reset_tenant(tenant)
    service.create_tenant(tenant, f"GateMem {ep.domain}")
    principals: set[str] = set()
    roles: dict[str, str] = {}
    listing: list[tuple[str, str, str]] = []
    for pid, role, name in ep.principals:
        p = _obj(pid)
        service.create_principal(tenant, p, name[:200] or pid, roles=[_ident(role)] if role else [])
        principals.add(p)
        roles[p] = _ident(role) if role else ""
        listing.append((p, roles[p], name))
    by_object: dict[tuple[str, str], set[str]] = defaultdict(set)
    held: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    groups: dict[tuple[str, str, str], list[str]] = {}
    group_attrs: dict[tuple[str, str, str], dict[str, str]] = {}
    for rel in ep.relationships:
        subject = next((rel[k] for k in _SUBJECT_KEYS if k in rel), None)
        obj = next(((otype, rel[k]) for k, otype in _OBJECT_KEYS if k in rel), None)
        if subject is None or obj is None or _obj(subject) not in principals:
            continue
        relation, otype, oid = _ident(rel.get("type", "related_to")), obj[0], _obj(obj[1])
        s = _obj(subject)
        if (relation, otype, oid) in held[s]:
            continue
        service.add_relationship_admin(tenant, s, relation, otype, oid)
        by_object[(otype, oid)].add(relation)
        held[s].append((relation, otype, oid))
        groups.setdefault((relation, otype, oid), []).append(s)
        # Scope attributes GateMem attaches to a relationship (access_scope, scope, access_level, ...).
        used = {"type", *_SUBJECT_KEYS, *(k for k, _ in _OBJECT_KEYS)}
        for k, v in rel.items():
            if k not in used:
                group_attrs.setdefault((relation, otype, oid), {})[_ident(k)] = str(v)[:60]
    binding = EpisodeBinding(tenant, principals, roles=roles, relations=dict(held))
    for pid in principals:
        reqs: dict[tuple[str, str, str], RelationRequirement] = {}
        objects = {(o, i) for _r, o, i in held.get(pid, [])} | {("principal", pid)}
        for otype, oid in objects:
            for relation in sorted(by_object.get((otype, oid), set())):
                reqs[(relation, otype, oid)] = RelationRequirement(relation=relation, object_type=otype, object_id=oid)
        binding.peers[pid] = list(reqs.values())
    binding.roster = Roster(
        principals=tuple(listing),
        groups=tuple(
            Group(
                f"G{i + 1}",
                rel,
                otype,
                oid,
                tuple(members),
                tuple(sorted(group_attrs.get((rel, otype, oid), {}).items())),
            )
            for i, ((rel, otype, oid), members) in enumerate(groups.items())
        ),
        roles=tuple(sorted({r for r in roles.values() if r})),
    )
    return binding


class DeterministicTurnPolicy:
    name = "deterministic-turn-policy/v1"

    def policy_for(self, text: str, speaker: str, binding: EpisodeBinding) -> tuple[MemoryPolicy, Sensitivity]:
        if PRIVACY_CUES.search(text):
            return MemoryPolicy(), Sensitivity.CONFIDENTIAL
        rules = tuple(AccessRule(relation=req, view=View.FULL) for req in binding.peers.get(speaker, []))
        return MemoryPolicy(rules=rules, historical_access="same_as_current"), Sensitivity.INTERNAL


@dataclass
class MemRecord:
    policy: MemoryPolicy
    allowed_views: frozenset[View]
    audience: frozenset[str]
    key: str | None
    protected: tuple[str, ...] = ()
    text: str = ""
    fact: NewFact | None = None  # what was stored (lets a partial deletion re-store the rest, R6)


@dataclass
class MappedFact:
    fact: NewFact
    record: MemRecord


@dataclass
class TurnMapping:
    facts: list[MappedFact]
    excluded: frozenset[str] = frozenset()
    clamped: bool = False
    dropped_tokens: int = 0
    summaries_dropped: int = 0
    values_masked: int = 0
    statements_dropped: int = 0
    quotes_not_verbatim: int = 0
    questions_dropped: int = 0
    need_to_know_demoted: int = 0  # audience tokens that get the version without exact values
    scope_additions: int = 0
    quote_audiences: int = 0  # quotes stored with their own audience instead of the message's
    floor_additions: int = 0  # groups added because a scope-limited delegate of theirs was in the audience
    scope_records: int = 0  # extra records holding a scope-limited group's own in-scope version (R4)
    residual: bool = False  # the message was also kept whole (a quote was not verbatim or a value was lost)


_NUMBER = re.compile(r"(?<![A-Za-z0-9])\d(?:[\d,.:/-]*\d)?(?![A-Za-z0-9])")
# Identifier-like codes: letters and digits joined by - or _, containing a digit (bridge-63, INV-2291).
_CODE = re.compile(r"(?<![\w-])(?=[\w-]*\d)[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+(?![\w-])")
_MASK = "[removed]"
# Whole values a number match would split: "July 2, 2026" must retire as one value, never as "2" and "2026".
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_SPAN = re.compile(
    rf"(?<![A-Za-z]){_MONTH}\.? \d{{1,2}}(?:st|nd|rd|th)?(?:,? \d{{4}})?(?![\d])"
    r"|(?<![\d:])\d{1,2}(?::\d{2})? ?(?:[AaPp]\.[Mm]\.|[AaPp][Mm])(?![A-Za-z])"
)


def _contains(hay: str, needle: str) -> bool:
    n = normalize(needle)
    return bool(n) and f" {n} " in f" {normalize(hay)} "


def _values(text: str, protected: Sequence[str] = ()) -> list[str]:
    """Concrete values a statement must not lose or leak: protected values, identifiers and numbers.

    Dates, clock times and codes are whole values; a number inside one is not a value of its own.
    """
    dates = [(m.start(), m.end()) for m in _SPAN.finditer(text)]

    def free(m: re.Match[str], spans: list[tuple[int, int]]) -> bool:
        return not any(a <= m.start() and m.end() <= b for a, b in spans)

    codes = [(m.start(), m.end()) for m in _CODE.finditer(text) if free(m, dates)]
    numbers = [m.group(0) for m in _NUMBER.finditer(text) if free(m, dates + codes)]
    return list(dict.fromkeys([*protected, *(text[a:b] for a, b in codes + dates), *numbers]))


def _mask(text: str, values: Sequence[str]) -> tuple[str | None, int]:
    """Replace retired values; ``None`` if one survives in a form exact matching missed."""
    out, n = text, 0
    for v in sorted(values, key=len, reverse=True):
        # Whole tokens only: retiring "2" must not touch "12", nor "July 2" touch "July 21".
        out, k = re.subn(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])", _MASK, out, flags=re.IGNORECASE)
        n += k
    if any(_contains(out, v) for v in values):
        return None, n
    return out, n


def _withhold(text: str, protected: Sequence[str]) -> str:
    """The quote with every protected value replaced (a need-to-know summary)."""
    out = text
    for v in sorted(protected, key=len, reverse=True):
        out = re.sub(re.escape(v), "[withheld]", out, flags=re.IGNORECASE)
    return out


_SLOT_STOP = frozenset(
    {"a", "an", "the", "to", "of", "on", "in", "at", "and", "or", "for", "is", "are", "was", "be", "by", "with"}
    | {"from", "now", "still", "current", "currently", "new", "old"}
)


def _same_slot(part: str, new_text: str) -> bool:
    """Whether a part with a value names the same slot as the new statement ("room 4B" vs "room 7C").

    Such a part is the value the update changed, not a detail it left standing.
    """
    if not re.search(r"\d", part):
        return False
    words = {w for w in re.findall(r"[a-z]{2,}", part.lower())} - _SLOT_STOP
    return bool(words & set(re.findall(r"[a-z]{2,}", new_text.lower())))


_VALUE = "[value]"


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())} - _SLOT_STOP - {"please", "delete", "remove"}


def _mask_values(text: str) -> str:
    """Every value replaced by a placeholder: numbers, dates, times, codes, and any token holding a digit
    ("room 4B", "B7")."""
    for v in sorted(_values(text), key=len, reverse=True):
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])", _VALUE, text)
    return re.sub(r"(?<![\w\[-])(?=[\w-]*\d)[\w-]+(?![\w\]-])", _VALUE, text)


def _verbatim(values: Sequence[str], text: str) -> list[str]:
    """Model-reported values that really occur in the message, in the message's spelling."""
    lowered = text.lower()
    out: list[str] = []
    for v in values:
        v = v.strip()
        i = lowered.find(v.lower()) if v else -1
        if len(v) >= 3 and i >= 0:
            out.append(text[i : i + len(v)])
    return list(dict.fromkeys(out))[:12]


class LabelMapper:
    """Trusted mapping from a model label to a GSM policy (see module docstring)."""

    def __init__(self, binding: EpisodeBinding) -> None:
        self.b = binding
        assert binding.roster is not None
        self.groups = {g.gid: g for g in binding.roster.groups}
        self.names = {pid: name for pid, _role, name in binding.roster.principals}
        self.engine = PolicyEngine()

    def rules(
        self, tokens: Sequence[str], view: View, speaker: str, about: Sequence[str]
    ) -> tuple[list[AccessRule], int]:
        rules: list[AccessRule] = []
        named: set[str] = set()
        dropped = 0
        for raw in tokens:
            tok = str(raw).strip()
            low = tok.lower()
            if low == "everyone":
                rules.append(AccessRule(tenant_wide=True, view=view))
            elif low == "speaker":
                named.add(speaker)
            elif low == "about":
                named |= set(about)
            elif low.startswith("principal:") and _obj(tok.split(":", 1)[1]) in self.b.principals:
                named.add(_obj(tok.split(":", 1)[1]))
            elif low.startswith("role:") and _ident(tok[5:]) in set(self.b.roles.values()):
                rules.append(AccessRule(any_roles=frozenset({_ident(tok[5:])}), view=view))
            elif tok.upper() in self.groups:
                g = self.groups[tok.upper()]
                req = RelationRequirement(relation=g.relation, object_type=g.object_type, object_id=g.object_id)
                rules.append(AccessRule(relation=req, view=view))
            else:
                dropped += 1
        if named:
            rules.append(AccessRule(principal_ids=frozenset(named), view=view))
        return list(dict.fromkeys(rules)), dropped

    def audience(
        self, policy: MemoryPolicy, views: frozenset[View], owner: str, subjects: Sequence[str]
    ) -> frozenset[str]:
        """Principals who would receive the FULL view (exact engine evaluation)."""
        facts = MemoryFacts(
            memory_id="probe",
            tenant_id=self.b.tenant_id,
            workspace_id="default",
            lifecycle_state=LifecycleState.ACTIVE,
            retain_until=None,
            owner_ids=frozenset({owner}),
            subject_ids=frozenset(subjects),
            sensitivity=Sensitivity.INTERNAL,
            compartments=frozenset(),
            policy=policy,
            policy_revision=1,
            allowed_views=views,
            version=VersionFacts("probe", VersionState.ACTIVE, _EPOCH, None),
        )
        out: set[str] = set()
        for pid in self.b.principals:
            role = self.b.roles.get(pid)
            ctx = PrincipalContext(
                tenant_id=self.b.tenant_id,
                workspace_id="default",
                principal_id=pid,
                roles=frozenset({role} if role else set()),
                auth_time=_PROBE_TIME,
                auth_strength=AuthStrength.MFA,
                request_time=_PROBE_TIME,
                correlation_id="corr_audience_probe",
                issued_by="gatemem-harness",
            )
            rels = tuple(RelationFact(r, o, i, _EPOCH) for r, o, i in self.b.relations.get(pid, []))
            snap = AuthSnapshot(self.b.tenant_id, pid, True, _PROBE_TIME, relations=rels)
            d = self.engine.evaluate_read(ctx, facts, snap)
            if d.allowed and d.view is View.FULL:
                out.add(pid)
        return frozenset(out)

    def principals(self, raw: Sequence[str]) -> list[str]:
        return [p for p in dict.fromkeys(_obj(a) for a in raw) if p in self.b.principals]

    def named_in(self, pid: str, text: str) -> bool:
        """Whether the message names this principal (by id or by a word of their display name)."""
        words = set(re.findall(r"[a-z0-9_]+", text.lower()))
        name_words = {w for w in re.findall(r"[a-z0-9]+", self.names.get(pid, "").lower()) if len(w) >= 3}
        return pid.lower() in words or bool(name_words & words)

    def build(
        self,
        label: TurnLabel,
        speaker: str,
        text: str,
        turn_id: str,
        *,
        retired: Sequence[str] = (),
        summary_only: Sequence[str] = (),
        quote_owners: Sequence[Sequence[str]] | None = None,
        quote_scope: Sequence[Mapping[str, str]] | None = None,
        revoked: AbstractSet[str] = frozenset(),
        cut_off: AbstractSet[str] = frozenset(),
        floor: Mapping[str, Sequence[str]] | None = None,
        scope_records: bool = False,
    ) -> TurnMapping:
        """Map one label to one GSM fact per quote.

        All quotes of a turn share the turn's audience. A quote must occur in the
        message (up to case, punctuation and spacing); otherwise the message is kept
        whole as well. ``retired`` are values of records this turn deleted; they are
        masked out of every quote. ``summary_only`` are audience tokens that, under
        need-to-know, get quotes carrying protected values only with those values
        withheld (a deterministic redacted view); other quotes they see in full.
        ``quote_owners`` (one list per label fact) narrows ownership to the quotes each
        owner's own information is in; without it every quote is owned by all owners.
        ``quote_scope`` gives, per quote, scope-limited groups an in-scope version of it
        (a redacted view; rejected if it carries a protected or retired value).
        ``revoked`` principals are no longer added automatically as owners or subjects;
        ``cut_off`` principals (revoked by this very message) are excluded from it.
        ``floor`` maps a scope-limited group to the groups it acts for (same object, no scope
        limit): whoever a delegate is told, the people it acts for are told too.
        ``scope_records``: groups whose in-scope versions differ each get their own record (keyed
        ``<key>.scope_<group>`` so it follows later updates); otherwise all groups share the first one.
        """
        about = self.principals(label.about)
        # Only someone the message itself names can be excluded by it.
        excluded = frozenset(p for p in self.principals(label.exclude) if p != speaker and self.named_in(p, text))
        excluded |= frozenset(cut_off) - {speaker}
        protected = _verbatim(label.protected_values, text)
        full_tokens = list(label.full_audience) or ["speaker"]
        sensitive = bool(PRIVACY_CUES.search(text)) or bool(protected)
        out = TurnMapping(facts=[], excluded=excluded)
        if sensitive and any(t.strip().lower() == "everyone" for t in full_tokens):
            full_tokens = [t for t in full_tokens if t.strip().lower() != "everyone"] or ["speaker"]
            out.clamped = True
        # Owners see their own information; people who are merely mentioned get nothing
        # automatically (only through the audience the model chose).
        visible_about = [p for p in about if p not in excluded and p not in revoked]
        owners = [p for p in self.principals(label.owners) if p not in excluded and p not in revoked]
        # Need-to-know: tokens that keep access, but not to the exact protected values.
        demote = {t.strip().lower() for t in summary_only} - {"speaker", "about"}
        demoted = [t for t in full_tokens if t.strip().lower() in demote] if protected else []
        out.need_to_know_demoted = len(demoted)
        full_tokens = [t for t in full_tokens if t not in demoted]
        full_tokens = self._floored(full_tokens, floor, out)
        full_rules, out.dropped_tokens = self.rules(full_tokens, View.FULL, speaker, visible_about)
        if not full_rules:
            full_rules, _ = self.rules(["speaker"], View.FULL, speaker, visible_about)
        demoted_full, _ = self.rules(demoted, View.FULL, speaker, visible_about) if demoted else ([], 0)
        demoted_red, _ = self.rules(demoted, View.REDACTED, speaker, visible_about) if demoted else ([], 0)
        summary_rules: list[AccessRule] = []
        if any(f.shareable for f in label.facts):
            summary_rules, d2 = self.rules(label.summary_audience, View.REDACTED, speaker, visible_about)
            out.dropped_tokens += d2
        authority = tuple(
            PrincipalPredicate(
                principal_ids=r.principal_ids, any_roles=r.any_roles, relation=r.relation, tenant_wide=r.tenant_wide
            )
            for r in full_rules
        )
        subjects = tuple(dict.fromkeys([speaker, *about, *owners]))
        grantors: tuple[PrincipalPredicate, ...] = ()
        full_views = frozenset({View.FULL, View.EXISTENCE_ONLY})
        audience = (
            self.audience(self._policy(full_rules, authority, grantors), full_views, speaker, subjects) - excluded
        )
        wide_audience = (
            self.audience(self._policy(full_rules + demoted_full, authority, grantors), full_views, speaker, subjects)
            - excluded
            if demoted_full
            else audience
        )
        topic = re.sub(r"\s+", " ", label.topic).strip()[:200] or f"{label.kind} from {speaker}"
        per_quote: dict[tuple[str, ...], tuple[list[AccessRule], tuple[PrincipalPredicate, ...], frozenset[str]]] = {}

        def quote_rules(
            tokens: Sequence[str],
        ) -> tuple[list[AccessRule], tuple[PrincipalPredicate, ...], frozenset[str]]:
            """Rules for a quote with its own audience, under the same floor as the message's."""
            key = tuple(tokens)
            if key not in per_quote:
                toks = self._floored(list(tokens), floor, out)
                if sensitive and any(t.strip().lower() == "everyone" for t in toks):
                    toks = [t for t in toks if t.strip().lower() != "everyone"] or ["speaker"]
                    out.clamped = True
                rules, dropped = self.rules(toks, View.FULL, speaker, visible_about)
                out.dropped_tokens += dropped
                if not rules:
                    rules, _ = self.rules(["speaker"], View.FULL, speaker, visible_about)
                auth = tuple(
                    PrincipalPredicate(
                        principal_ids=r.principal_ids,
                        any_roles=r.any_roles,
                        relation=r.relation,
                        tenant_wide=r.tenant_wide,
                    )
                    for r in rules
                )
                aud = self.audience(self._policy(rules, auth, ()), full_views, speaker, subjects) - excluded
                per_quote[key] = (rules, auth, aud)
            return per_quote[key]

        items: list[tuple[str, str | None, str | None]] = []
        item_audience: list[list[str]] = []
        item_owners: list[list[str]] = []
        item_scope: list[dict[str, str]] = []
        for n, f in enumerate(label.facts):
            if f.quote.strip().endswith("?"):
                out.questions_dropped += 1  # a question states nothing to remember
            elif _contains(text, f.quote):
                items.append((f.quote, f.key, f.shareable))
                item_audience.append(list(f.audience))
                mine = owners if quote_owners is None else [p for p in owners if p in set(map(_obj, quote_owners[n]))]
                item_owners.append(mine)
                item_scope.append(dict(quote_scope[n]) if quote_scope is not None else {})
            else:
                out.quotes_not_verbatim += 1
        if label.facts and out.questions_dropped == len(label.facts):
            return out  # the message only asked something
        if not label.facts:
            items = [(text, None, None)]  # nothing split out: keep the message itself
            item_audience = [[]]
            item_owners = [owners]
            item_scope = [{}]
        elif label.delete is None and label.kind != "deletion":
            # Completeness guard: a quote the message does not contain, or a value no quote kept.
            kept = "\n".join(st for st, _k, _s in items)
            if out.quotes_not_verbatim or any(not _contains(kept, v) for v in _values(text, protected)):
                items.append((text, None, None))
                item_audience.append([])
                item_owners.append(owners)
                item_scope.append({})
                out.residual = True
        used_keys: set[str] = set()
        for (statement, raw_key, shareable), mine, scope, own_aud in zip(
            items, item_owners, item_scope, item_audience, strict=True
        ):
            if own_aud:
                q_rules, q_auth, q_aud = quote_rules(own_aud)
                out.quote_audiences += 1
            else:
                q_rules, q_auth, q_aud = full_rules, authority, audience
            scope_ok = {g: s for g, s in scope.items() if not any(_contains(s, v) for v in [*protected, *retired])}
            # Owners see their own information and may consent to sharing it (GSM grant authority).
            own_rules = [AccessRule(principal_ids=frozenset(mine), view=View.FULL)] if mine else []
            own_pred = (PrincipalPredicate(principal_ids=frozenset(mine)),) if mine else ()
            detail, n = _mask(statement.strip(), retired)
            out.values_masked += n
            if not detail or not normalize(detail.replace(_MASK, "")):
                out.statements_dropped += 1
                continue
            summary = (shareable or "").strip() or None
            leaks = any(_contains(summary, v) for v in [*protected, *retired]) if summary else False
            if summary is not None and (not summary_rules or leaks):
                out.summaries_dropped += 1
                summary = None
            carries_protected = any(_contains(detail, v) for v in protected)
            extra_full: list[AccessRule] = [] if own_aud else demoted_full
            extra_red: list[AccessRule] = []
            if demoted and carries_protected and not own_aud:
                extra_full, extra_red = [], demoted_red
                if summary is None:
                    summary = _withhold(detail, protected)
            key = _key(raw_key)
            if key in used_keys:
                key = None  # two statements of one message never supersede each other
            if key:
                used_keys.add(key)
            scope_rules: list[AccessRule] = []
            separate: dict[str, list[str]] = {}  # in-scope version -> groups that get it as their own record
            if scope_records and scope_ok:
                versions: dict[str, list[str]] = {}
                for g, version in scope_ok.items():
                    versions.setdefault(version.strip()[:1200], []).append(g)
                if summary is None:
                    first, gids = next(iter(versions.items()))
                    summary = first
                    scope_rules, _ = self.rules(gids, View.REDACTED, speaker, visible_about)
                    out.scope_additions += len(gids)
                    separate = {v: g for v, g in versions.items() if v != first}
                else:
                    separate = versions
            elif scope_ok and summary is None:
                # One redacted view per record: the in-scope version (all groups get the first one offered).
                summary = next(iter(scope_ok.values()))[:1200]
                scope_rules, _ = self.rules(list(scope_ok), View.REDACTED, speaker, visible_about)
                out.scope_additions += len(scope_ok)
            red_rules = list(dict.fromkeys((summary_rules if summary else []) + extra_red + scope_rules))
            rules = q_rules + own_rules + extra_full + red_rules
            policy = self._policy(rules, q_auth + own_pred, grantors + own_pred)
            views = full_views | ({View.REDACTED} if summary else set())
            fields = {"detail": detail[:8000]}
            if summary:
                fields["summary"] = summary
            if protected:
                fields["protected"] = "\n".join(protected)
            content = Content(fields=fields)
            fact_topic = topic
            try:
                validate_topic(get_schema("tiered_note"), content, fact_topic)
            except ContentValidationError:
                fact_topic = f"{label.kind} from {speaker} at {turn_id}"
            fact = NewFact(
                semantic_type=SemanticType.FACT if label.kind == "fact" else SemanticType.NOTE,
                content_schema="tiered_note",
                content=content,
                topic=fact_topic,
                attribute=key,
                subject_ids=subjects,
                sensitivity=Sensitivity.CONFIDENTIAL if sensitive else Sensitivity.INTERNAL,
                policy=policy,
                allowed_views=views,
            )
            who = (wide_audience if extra_full else q_aud) | frozenset(mine)
            out.facts.append(MappedFact(fact, MemRecord(policy, views, who, key, tuple(protected), detail)))
            for version, gids in separate.items():
                out.facts.append(self._scope_record(fact, version, gids, speaker, subjects, used_keys))
                out.scope_additions += len(gids)
                out.scope_records += 1
        return out

    def _scope_record(
        self, fact: NewFact, version: str, gids: Sequence[str], speaker: str, subjects: Sequence[str], used: set[str]
    ) -> MappedFact:
        """R4: a scope-limited group's own in-scope version of a fact, as a record of its own.

        Full view for those groups and the speaker (who may update or delete it), nothing else.
        """
        rules, _ = self.rules([*gids, "speaker"], View.FULL, speaker, ())
        auth = (PrincipalPredicate(principal_ids=frozenset({speaker})),)
        policy = self._policy(rules, auth)
        key = _key(f"{fact.attribute}.scope_{'_'.join(sorted(gids))}") if fact.attribute else None
        if key in used:
            key = None
        if key:
            used.add(key)
        views = frozenset({View.FULL, View.EXISTENCE_ONLY})
        scoped = fact.model_copy(
            update={
                "content": Content(fields={"detail": version}),
                "attribute": key,
                "policy": policy,
                "allowed_views": views,
            }
        )
        who = self.audience(policy, views, speaker, subjects)
        return MappedFact(scoped, MemRecord(policy, views, who, key, (), version))

    @staticmethod
    def _floored(tokens: list[str], floor: Mapping[str, Sequence[str]] | None, out: TurnMapping) -> list[str]:
        """Add the groups a scope-limited delegate in ``tokens`` acts for (R3, delegate_floor)."""
        if not floor:
            return tokens
        have = {t.strip().upper() for t in tokens}
        extra = [g for t in tokens for g in floor.get(t.strip().upper(), ()) if g not in have]
        extra = list(dict.fromkeys(extra))
        out.floor_additions += len(extra)
        return tokens + extra

    @staticmethod
    def _policy(
        rules: Sequence[AccessRule],
        authority: tuple[PrincipalPredicate, ...],
        grantors: tuple[PrincipalPredicate, ...] = (),
    ) -> MemoryPolicy:
        return MemoryPolicy(
            rules=tuple(rules),
            historical_access="same_as_current",
            update_authority=authority,
            delete_authority=authority,
            grant_authority=grantors,
            existence_visible_to=(PrincipalPredicate(tenant_wide=True),),
        )

    def resolve(self, tokens: Sequence[str], speaker: str, about: Sequence[str]) -> set[str]:
        """Principals an audience token list names (same closed menu as ``rules``)."""
        out: set[str] = set()
        for raw in tokens:
            tok = str(raw).strip()
            low = tok.lower()
            if low == "everyone":
                out |= self.b.principals
            elif low == "speaker":
                out.add(speaker)
            elif low == "about":
                out |= set(about)
            elif low.startswith("principal:") and _obj(tok.split(":", 1)[1]) in self.b.principals:
                out.add(_obj(tok.split(":", 1)[1]))
            elif _obj(tok) in self.b.principals:  # a bare roster id is as unambiguous as principal:<id>
                out.add(_obj(tok))
            elif low.startswith("role:"):
                role = _ident(tok[5:])
                out |= {p for p, r in self.b.roles.items() if r == role}
            elif tok.upper() in self.groups:
                out |= set(self.groups[tok.upper()].members)
        return out


@dataclass
class RunOutputs:
    predictions: list[dict[str, Any]] = field(default_factory=list)
    method_metrics: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    ingest_log: list[dict[str, Any]] = field(default_factory=list)
    checkpoint_diag: list[dict[str, Any]] = field(default_factory=list)
    memory_snapshots: list[dict[str, Any]] = field(default_factory=list)


def _parse_ts(ts: str | None, fallback: datetime) -> datetime:
    if not ts:
        return fallback + timedelta(minutes=1)
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return fallback + timedelta(minutes=1)
    t = t if t.tzinfo else t.replace(tzinfo=UTC)
    return max(t, fallback)


class _MeteredGenerator:
    """Per-episode wrapper so token counts stay per checkpoint when episodes run in parallel."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = inner.name
        self.metrics = inner.metrics
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.extra: list[GeneratorEvidence] = []  # appended after the evidence (own activity, U1)
        self.last_evidence: list[GeneratorEvidence] = []

    def generate(self, query: str, plan: GeneratorPlan, evidence: Sequence[GeneratorEvidence]) -> DraftResponse:
        shown = {e.memory_id for e in evidence}
        evidence = [*evidence, *(e for e in self.extra if e.memory_id not in shown)]
        self.last_evidence = evidence
        draft: DraftResponse = self.inner.generate(query, plan, evidence)
        self.prompt_tokens += draft.prompt_tokens
        self.completion_tokens += draft.completion_tokens
        return draft


def delegate_floor(roster: Roster, roles: Mapping[str, str]) -> dict[str, list[str]]:
    """R3: for each scope-limited group, the groups it acts for.

    A group with scope attributes (``access_scope=...``) is a delegate of the people who hold an
    unrestricted relationship to the same object (e.g. a device-setup delegate of a household and
    the household members). Groups classified as oversight-only or scoped are not added.
    """
    out: dict[str, list[str]] = {}
    for g in roster.groups:
        if not g.attributes:
            continue
        acts_for = [
            h.gid
            for h in roster.groups
            if h.gid != g.gid
            and (h.object_type, h.object_id) == (g.object_type, g.object_id)
            and not h.attributes
            and roles.get(h.gid) not in ("oversight", "scoped")
        ]
        if acts_for:
            out[g.gid] = acts_for
    return out


class _EpisodeSession:
    def __init__(self, runner: GateMemRunner, ep: MethodEpisode) -> None:
        self.r = runner
        self.ep = ep
        self.clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        self.answerer: BaselineAnswerer | None = runner.answerer_factory() if runner.answerer_factory else None
        settings = runner.settings
        if self.answerer is not None:
            # The baselines' answerer decides the action; GSM bounds it (see
            # release_action_policy) and every checkpoint must reach the model.
            self.answerer.speaker_of = lambda mid: self.speaker_of.get(mid)
            settings = settings.model_copy(update={"release_action_policy": "answerer", "cache_ttl_seconds": 0})
        self.generator = _MeteredGenerator(self.answerer or runner.providers.generator)
        providers = dataclasses.replace(runner.providers, generator=self.generator)
        self.service = GovernedMemoryService(runner.db, settings, clock=self.clock, providers=providers)
        self.auth = TrustedAuthenticator(self.service, issuer="gatemem-harness")
        self.service.hooks.before_generate = self._capture
        self.counters: dict[str, int] = defaultdict(int)
        self.seen_evidence: list[GeneratorEvidence] = []
        self.instructions: deque[str] = deque(maxlen=12)
        self.recent: deque[str] = deque(maxlen=3)
        self.registry: dict[str, MemRecord] = {}
        self.key_index: dict[str, str] = {}
        self.binding: EpisodeBinding | None = None
        self.mapper: LabelMapper | None = None
        self._last_memory_id: str | None = None
        self._last_reasons: list[str] = []
        self.revoked: set[str] = set()  # principals a revocation cut off in this episode
        self.oversight: set[str] = set()  # group ids that only oversee (per-episode classification)
        self.scoped: list[str] = []  # group ids limited to a stated scope
        self.floor: dict[str, list[str]] = {}  # scope-limited group -> groups it acts for (R3)
        self.speaker_of: dict[str, str] = {}
        self._history_text: dict[str, str] = {}
        self.replaced_by: dict[str, str] = {}  # retired by an update -> the record that replaced it
        self.protected_of: dict[str, tuple[str, ...]] = {}  # sensitive values of every record ever stored
        # Diagnostics (ids, labels and scores only; never hidden annotations).
        self.memory_turn: dict[str, str] = {}
        self.ingest_log: list[dict[str, Any]] = []
        self.checkpoint_diag: list[dict[str, Any]] = []
        self.memory_snapshots: list[dict[str, Any]] = []
        self._turn_log: dict[str, Any] = {}

    def _capture(self, evidence: Sequence[GeneratorEvidence]) -> None:
        self.seen_evidence = list(evidence)

    def ctx(self, principal: str) -> PrincipalContext:
        assert self.binding is not None
        return self.auth.context(self.binding.tenant_id, principal)

    # ------------------------------------------------------------ ingestion
    def ingest(self, turn: MethodTurn, speaker: str) -> None:
        assert self.binding is not None
        if speaker not in self.binding.principals:
            self.counters["turns_skipped_unknown_speaker"] += 1
            return
        self._turn_log = {"episode_id": self.ep.episode_id, "turn_id": turn.turn_id, "speaker": speaker}
        if self.r.diagnostics:
            self.ingest_log.append(self._turn_log)
        if self.r.interpreter is None:
            self._ingest_heuristic(turn.text, turn.turn_id, speaker)
            return
        self._ingest_model(turn, speaker)

    def _ingest_heuristic(self, text: str, turn_id: str, speaker: str) -> None:
        assert self.binding is not None
        ctx = self.ctx(speaker)
        if DELETION_CUES.search(text):
            self.counters["deletion_requests"] += 1
            if self.r.resolve_deletions == "lexical":
                for ev in self.service.search(ctx, QueryRequest(query=text, top_k=3)):
                    if ev.memory_id is not None:
                        self._delete(speaker, ev.memory_id)
            return
        self._turn_log.setdefault("kind", "heuristic")
        policy, sensitivity = self.r.turn_policy.policy_for(text, speaker, self.binding)
        op = AddFact(
            fact=NewFact(
                semantic_type=SemanticType.NOTE,
                content=Content(text=text[:8000]),
                topic=f"turn {turn_id} by {speaker}",
                subject_ids=(speaker,),
                sensitivity=sensitivity,
                policy=policy,
            )
        )
        receipt = self._submit(speaker, turn_id, op)
        self.counters["turns_ingested" if receipt == "committed" else "turns_rejected"] += 1
        self._turn_log.update(outcome="added" if receipt == "committed" else "rejected", memory_id=self._last_memory_id)

    def _submit(self, speaker: str, turn_id: str, op: CandidateMemoryOperation) -> str:
        r = self.service.submit_event(
            self.ctx(speaker),
            StructuredEvent(event_type="gatemem.turn", payload={"turn_id": turn_id}, operations=(op,)),
        )
        self._last_memory_id = r.results[0].memory_id
        self._last_reasons = [getattr(x, "value", str(x)) for x in r.results[0].reasons]
        if r.status == "committed" and self._last_memory_id:
            self.speaker_of[self._last_memory_id] = speaker
            self.memory_turn[self._last_memory_id] = turn_id
        return r.status

    def _delete(self, speaker: str, memory_id: str) -> bool:
        r = self.service.commit_operations(self.ctx(speaker), [DeleteFact(memory_ids=(memory_id,))])
        ok = r.status == "committed"
        self.counters["deletions_committed" if ok else "deletions_refused"] += 1
        if ok:
            self.registry.pop(memory_id, None)
            for k, m in list(self.key_index.items()):
                if m == memory_id:
                    del self.key_index[k]
        return ok

    def _visible_keys(self, speaker: str) -> list[str]:
        return sorted(k for k, m in self.key_index.items() if speaker in self.registry.get(m, _NO_RECORD).audience)[:40]

    def _ingest_model(self, turn: MethodTurn, speaker: str) -> None:
        assert self.binding is not None and self.binding.roster is not None and self.r.interpreter is not None
        assert self.mapper is not None
        label = self.r.interpreter.label(
            roster=self.binding.roster,
            instructions=list(self.instructions),
            recent=list(self.recent),
            visible_keys=self._visible_keys(speaker),
            turn_id=turn.turn_id,
            speaker=speaker,
            speaker_role=self.binding.roles.get(speaker, ""),
            turn_kind=turn.turn_kind,
            text=turn.text,
        )
        if label is None:
            self.counters["interpreter_fallbacks"] += 1
            self._turn_log["kind"] = "fallback"
            self.recent.append(f"[{turn.turn_id} {speaker}] {turn.text[:300]}")
            self._ingest_heuristic(turn.text, turn.turn_id, speaker)
            return
        self.counters[f"label_{label.kind}"] += 1
        self._turn_log.update(
            kind=label.kind,
            full_audience=list(label.full_audience),
            summary_audience=list(label.summary_audience),
            delete=label.delete is not None,
            statements=len(label.facts),
        )
        # A question states nothing to remember (separate check; the label itself is unchanged).
        if (
            label.kind in ("fact", "instruction")
            and label.delete is None
            and self.r.interpreter.is_question(turn_id=turn.turn_id, speaker=speaker, text=turn.text)
        ):
            self.counters["questions_detected"] += 1
            self.recent.append(f"[{turn.turn_id} {speaker}] {turn.text[:300]}")
            self._turn_log.update(kind="question", outcome="not_stored")
            return
        removing = label.kind == "deletion" or label.delete is not None
        retired: list[str] = []
        if removing:
            # Deletion requests and corrections: remove what the request names first,
            # then store whatever current information the same message carries.
            retired = self._model_delete(label, turn, speaker)
            if label.delete is not None:
                # Values the message itself names as what to remove are retired even when no
                # stored memory held them (e.g. "now bridge-88, replacing bridge-63").
                named = [v for v in _values(label.delete.what) if _contains(turn.text, v)]
                retired = list(dict.fromkeys([*retired, *named]))
        elif label.kind == "chatter":
            self.recent.append(f"[{turn.turn_id} {speaker}] {turn.text[:300]}")
            self._turn_log["outcome"] = "not_stored"
            if "activity_notes" in self.r.repairs and self.r.interpreter.is_activity(
                turn_id=turn.turn_id, speaker=speaker, text=turn.text
            ):
                self._store_activity(turn, speaker)
            return
        cut_off: set[str] = set()
        if label.kind == "instruction" or (label.kind == "deletion" and self.r.revocation_override):
            # Adaptive step: a consent or revocation changes who may know EARLIER memories.
            # Revocations are often phrased as deletions ("his access has been revoked").
            intent = self.r.interpreter.rescope(
                roster=self.binding.roster,
                recent=list(self.recent),
                turn_id=turn.turn_id,
                speaker=speaker,
                speaker_role=self.binding.roles.get(speaker, ""),
                text=turn.text,
            )
            if intent is not None:
                withheld = self._model_rescope(intent, self.mapper.principals(label.about), turn, speaker)
                if self.r.revocation_override:
                    # Someone cut off no longer gets messages automatically as their owner or
                    # subject (the model may still name them), nor the revoking message itself.
                    cut_off = withheld
                    self.revoked |= withheld
        if label.kind == "instruction":
            self.instructions.append(f"[{turn.turn_id} {speaker}] {turn.text[:400]}")
        if removing and not label.facts:
            self.recent.append(f"[{turn.turn_id} {speaker}] (deletion request)")
            return
        # Oversight groups (sponsors, approvers) get quotes with protected values withheld.
        summary_only: list[str] = (
            [t for t in label.full_audience if t.strip().upper() in self.oversight] if self.r.oversight_withhold else []
        )
        narrowable = [t for t in label.full_audience if t.strip().lower() not in ("speaker", "about")]
        if self.r.need_to_know and label.protected_values and narrowable:
            summary_only += (
                self.r.interpreter.need_to_know(
                    roster=self.binding.roster,
                    instructions=list(self.instructions),
                    turn_id=turn.turn_id,
                    speaker=speaker,
                    speaker_role=self.binding.roles.get(speaker, ""),
                    text=turn.text,
                    protected=label.protected_values,
                    audience=narrowable,
                )
                or []
            )
        quote_owners = None
        owner_ids = self.mapper.principals(label.owners)
        if owner_ids and len(label.facts) > 1:
            quote_owners = self.r.interpreter.quote_owners(
                turn_id=turn.turn_id,
                candidates=[(p, self.mapper.names.get(p, p)) for p in owner_ids],
                quotes=[f.quote for f in label.facts],
            )
            self.counters["quote_owner_calls"] += 1
            self.counters["quote_owner_failures"] += quote_owners is None
        # Scope-limited groups left out of the audience still get quotes inside their stated scope.
        quote_scope = None
        given = {t.strip().upper() for t in label.full_audience}
        left_out = [g for g in self.scoped if g not in given]
        if self.r.scope_fit and left_out and label.facts:
            quote_scope = self.r.interpreter.scope_fit(
                roster=self.binding.roster,
                instructions=list(self.instructions),
                turn_id=turn.turn_id,
                groups=left_out,
                quotes=[f.quote for f in label.facts],
            )
            self.counters["scope_fit_calls"] += 1
            self.counters["scope_fit_failures"] += quote_scope is None
        mapping = self.mapper.build(
            label,
            speaker,
            turn.text,
            turn.turn_id,
            retired=retired,
            summary_only=summary_only,
            quote_owners=quote_owners,
            quote_scope=quote_scope,
            revoked=frozenset(self.revoked),
            cut_off=frozenset(cut_off),
            floor=self.floor,
            scope_records="scope_records" in self.r.repairs,
        )
        self.counters["floor_additions"] += mapping.floor_additions
        self.counters["scope_records"] += mapping.scope_records
        self.counters["scope_additions"] += mapping.scope_additions
        self.counters["quote_audiences"] += mapping.quote_audiences
        self.counters["need_to_know_demoted"] += mapping.need_to_know_demoted
        self.counters["questions_dropped"] += mapping.questions_dropped
        if not mapping.facts and mapping.questions_dropped:
            self.recent.append(f"[{turn.turn_id} {speaker}] {turn.text[:300]}")
            self._turn_log["outcome"] = "not_stored"
            return
        self.counters["audience_clamped"] += mapping.clamped
        self.counters["audience_tokens_dropped"] += mapping.dropped_tokens
        self.counters["summaries_dropped"] += mapping.summaries_dropped
        self.counters["retired_values_masked"] += mapping.values_masked
        self.counters["statements_dropped"] += mapping.statements_dropped
        self.counters["quotes_not_verbatim"] += mapping.quotes_not_verbatim
        self.counters["residual_messages_kept"] += mapping.residual
        self.counters["audience_exclusions"] += len(mapping.excluded)
        self.counters["redacted_tier"] += sum(View.REDACTED in m.record.allowed_views for m in mapping.facts)
        audience = mapping.facts[0].record.audience if mapping.facts else frozenset()
        self._turn_log.update(clamped=mapping.clamped, audience_size=len(audience), residual=mapping.residual)
        if removing:
            # Never replay retired content as context for later turns.
            shown = "; ".join(m.fact.content.fields["detail"] for m in mapping.facts)
            self.recent.append(f"[{turn.turn_id} {speaker}] (correction) {shown[:300]}")
        else:
            self.recent.append(f"[{turn.turn_id} {speaker}] {turn.text[:300]}")
        facts_log: list[dict[str, Any]] = []
        self._turn_log["facts"] = facts_log
        for mapped in mapping.facts:
            entry = self._store(mapped, turn.turn_id, speaker, retired)
            facts_log.append(entry)
            mid = entry.get("memory_id")
            if mid:
                for pid in sorted(mapping.excluded):
                    # The message said not to tell them: an explicit denial outranks every rule.
                    self.service.add_denial(self.binding.tenant_id, pid, "memory", mid)
        self.counters["statements_stored"] += sum(1 for e in facts_log if e.get("memory_id"))

    def _same_thing(self, old: str, new_text: str) -> tuple[bool, float | None]:
        """Whether a same-key statement is about the same thing as the memory it would replace.

        Keys are model-chosen and sometimes too coarse, so a replacement also has to be
        semantically close to the old statement: at least ``vector_min_similarity``, the
        (calibrated) similarity above which a match needs no lexical support.
        """
        rec = self.registry.get(old)
        embedder = self.r.providers.embedder
        if rec is None or not rec.text or embedder is None:
            return True, None
        try:
            a, b = embedder.embed([rec.text, new_text])
        except Exception:
            return True, None
        norm = (sum(x * x for x in a) * sum(y * y for y in b)) ** 0.5 or 1.0
        sim = sum(x * y for x, y in zip(a, b, strict=True)) / norm
        return sim >= self.r.settings.vector_min_similarity, round(sim, 4)

    def _store(self, mapped: MappedFact, turn_id: str, speaker: str, retired: Sequence[str] = ()) -> dict[str, Any]:
        key = mapped.fact.attribute
        entry: dict[str, Any] = {"key": key}
        old = self.key_index.get(key) if key else None
        same, sim = self._same_thing(old, mapped.record.text) if old else (False, None)
        carried_denials: set[str] = set()
        if old and not same:
            # Same key, different content: keep both current.
            self.counters["supersede_skipped_dissimilar"] += 1
            entry.update(kept_both=old, similarity=sim)
        elif old:
            entry["similarity"] = sim
            if "carry_forward" in self.r.repairs:
                mapped = self._carry_forward(old, mapped, turn_id, retired, entry)
            if "keep_readers" in self.r.repairs:
                mapped, carried_denials = self._keep_readers(old, mapped, entry)
            if self._submit(speaker, turn_id, SupersedeFact(memory_id=old, replacement=mapped.fact)) == "committed":
                self.counters["superseded"] += 1
                self.registry.pop(old, None)
                self._register(dataclasses.replace(mapped.record, fact=mapped.fact))
                self._deny(carried_denials, self._last_memory_id)
                entry.update(outcome="superseded", superseded=old, memory_id=self._last_memory_id)
                return entry
            self.counters["supersede_refused"] += 1
            entry.update(supersede_refused=old, refused_reasons=self._last_reasons)
            if "POLICY_BROADENING" in self._last_reasons:
                # The speaker may update the old value but shares the new one with a different
                # audience: retire the outdated value (narrowing only), then store the new one.
                r = self.service.commit_operations(self.ctx(speaker), [RetireFact(memory_id=old)])
                if r.status == "committed":
                    self.counters["retired"] += 1
                    self.registry.pop(old, None)
                    if key:
                        self.key_index.pop(key, None)
                    entry["retired"] = old
        status = self._submit(speaker, turn_id, AddFact(fact=mapped.fact))
        if status != "committed" and key:
            # e.g. the key is taken by a record this speaker may not update: store it unkeyed
            fact = mapped.fact.model_copy(update={"attribute": None})
            mapped = MappedFact(fact, dataclasses.replace(mapped.record, key=None))
            status = self._submit(speaker, turn_id, AddFact(fact=fact))
        ok = status == "committed"
        if ok and entry.get("retired") and self._last_memory_id:
            self.replaced_by[entry["retired"]] = self._last_memory_id
        self.counters["facts_added" if ok else "facts_rejected"] += 1
        entry.update(outcome="added" if ok else "rejected", memory_id=self._last_memory_id if ok else None)
        if ok:
            self._register(dataclasses.replace(mapped.record, fact=mapped.fact))
            self._deny(carried_denials, self._last_memory_id)
        return entry

    def _carry_forward(
        self, old: str, mapped: MappedFact, turn_id: str, retired: Sequence[str], entry: dict[str, Any]
    ) -> MappedFact:
        """R5 carry_forward: an update that restates only part of a fact keeps the parts still in effect.

        The model names the parts of the old statement the new one leaves standing; each must occur
        verbatim in the old record, must not hold a value this message retired, and must not hold an old
        protected value when the new record reaches someone the old one did not.
        """
        prev = self.registry.get(old)
        if prev is None or not prev.text or self.r.interpreter is None:
            return mapped
        new_text = mapped.record.text
        parts = self.r.interpreter.carry_forward(turn_id=turn_id, old=prev.text, new=new_text)
        self.counters["carry_forward_calls"] += 1
        if parts is None:
            self.counters["carry_forward_failures"] += 1
            return mapped
        wider = bool(mapped.record.audience - prev.audience)
        keep: list[str] = []
        for p in dict.fromkeys(x.strip() for x in parts):
            if (
                len(normalize(p)) < 3
                or _MASK in p
                or not _contains(prev.text, p)
                or _contains(new_text, p)
                or any(_contains(p, v) for v in retired)
                or (wider and any(_contains(p, v) for v in prev.protected))
                or _same_slot(p, new_text)
            ):
                continue
            keep.append(p)
        if not keep:
            return mapped
        text = f"{new_text} (still in effect from before: {'; '.join(keep)})"[:8000]
        fields = {**mapped.fact.content.fields, "detail": text}
        protected = tuple(dict.fromkeys([*mapped.record.protected, *(v for v in prev.protected if _contains(text, v))]))
        if protected:
            fields["protected"] = "\n".join(protected)
        fact = mapped.fact.model_copy(update={"content": Content(fields=fields)})
        self.counters["carried_forward"] += 1
        entry["carried_forward"] = keep
        return MappedFact(fact, dataclasses.replace(mapped.record, text=text, protected=protected))

    def _keep_readers(self, old: str, mapped: MappedFact, entry: dict[str, Any]) -> tuple[MappedFact, set[str]]:
        """R3 keep_readers: an update keeps the readers, and the editors, of the record it replaces.

        A new value of the same thing goes to everyone who could read the old value: the old
        record's full-view rules, its speaker, and its explicit grants. Whoever could update or
        delete the old value (its update/delete authority, and its speaker) keeps that right on the
        new one, so their next update replaces it instead of being stored beside it. Explicit
        denials (a restriction, or an exclusion by name) are carried over as well. Narrowing who
        knows something is the job of an instruction (re-scoping), not of a routine update.
        """
        prev = self.registry.get(old)
        if prev is None:
            return mapped, set()
        denied, granted = self._access_overrides(old)
        cur = mapped.fact.policy
        assert cur is not None
        keep = [r for r in prev.policy.rules if r.view is View.FULL]
        named = (set(granted) | {self.speaker_of.get(old, "")}) - {""} - denied
        if named:
            keep.append(AccessRule(principal_ids=frozenset(named), view=View.FULL))
        author = self.speaker_of.get(old)
        editors = [PrincipalPredicate(principal_ids=frozenset({author}))] if author and author not in denied else []
        update = {
            "rules": tuple(dict.fromkeys([*cur.rules, *keep])),
            "update_authority": tuple(dict.fromkeys([*cur.update_authority, *prev.policy.update_authority, *editors])),
            "delete_authority": tuple(dict.fromkeys([*cur.delete_authority, *prev.policy.delete_authority, *editors])),
        }
        if all(update[k] == getattr(cur, k) for k in update):
            return mapped, denied
        policy = cur.model_copy(update=update)
        fact = mapped.fact.model_copy(update={"policy": policy})
        audience = (mapped.record.audience | prev.audience | named) - denied
        self.counters["readers_kept"] += 1
        entry["readers_kept"] = sorted(audience - mapped.record.audience)
        if update["update_authority"] != cur.update_authority:
            self.counters["editors_kept"] += 1
        return MappedFact(fact, dataclasses.replace(mapped.record, policy=policy, audience=audience)), denied

    def _access_overrides(self, mid: str) -> tuple[set[str], set[str]]:
        """Active explicit denials and grants on one memory."""
        assert self.binding is not None
        with self.service.db.tenant_tx(self.binding.tenant_id) as tx:
            denied = tx.all(
                """SELECT principal_id FROM explicit_denials WHERE tenant_id = :tenant_id
                   AND resource_type = 'memory' AND resource_id = :mid AND lifted_at IS NULL""",
                mid=mid,
            )
            granted = tx.all(
                """SELECT grantee_principal_id FROM explicit_grants WHERE tenant_id = :tenant_id
                   AND resource_type = 'memory' AND memory_id = :mid AND revoked_at IS NULL""",
                mid=mid,
            )
        return {r["principal_id"] for r in denied}, {r["grantee_principal_id"] for r in granted}

    def _deny(self, principals: AbstractSet[str], mid: str | None) -> None:
        assert self.binding is not None
        if not mid:
            return
        for pid in sorted(principals):
            self.service.add_denial(self.binding.tenant_id, pid, "memory", mid)

    def _register(self, record: MemRecord) -> None:
        mid = self._last_memory_id
        if mid is None:
            return
        self.registry[mid] = record
        # The message's sensitive values that this record itself holds.
        self.protected_of[mid] = tuple(p for p in record.protected if _contains(record.text, p))
        if record.key:
            self.key_index[record.key] = mid

    def _model_delete(self, label: TurnLabel, turn: MethodTurn, speaker: str) -> list[str]:
        """Delete what the request names, as the requester; return the retired values.

        Retired values are the values recorded on the deleted memories (their
        protected values and numbers) that this message does not restate, plus
        those the model's description of what to remove names explicitly.
        """
        assert self.r.interpreter is not None
        self.counters["deletion_requests"] += 1
        what = label.delete.what if label.delete else turn.text
        candidates: dict[str, str] = {}
        for q in dict.fromkeys([what, turn.text]):
            for ev in self.service.search(self.ctx(speaker), QueryRequest(query=q, top_k=8)):
                if ev.memory_id and ev.memory_id not in candidates:
                    candidates[ev.memory_id] = ev.text
        history: dict[str, list[str]] = {}  # current record -> its earlier versions (R6 delete_history)
        if "delete_history" in self.r.repairs:
            # Earlier versions of everything the speaker may read now: the request may name only an old
            # value that no current record still holds.
            readable = [
                ev.memory_id
                for ev in self.service.search(self.ctx(speaker), QueryRequest(query=what, top_k=100), full_context=True)
                if ev.memory_id
            ]
            history = self._history(readable)
            asked = _content_words(f"{what} {turn.text}")
            scored = sorted(
                {m for ms in history.values() for m in ms},
                key=lambda m: -len(asked & _content_words(self._history_text.get(m, ""))),
            )
            for earlier in [m for m in scored if asked & _content_words(self._history_text.get(m, ""))][:8]:
                candidates.setdefault(earlier, "(earlier version) " + self._history_text.get(earlier, ""))
        self._turn_log.update(outcome="deletion", candidates=list(candidates), deleted=[])
        if not candidates:
            self.counters["deletion_no_candidates"] += 1
            return []
        ids = list(candidates)
        shown = [candidates[i][:600] for i in ids]
        parts: list[tuple[int, str | None]] | None
        if "partial_delete" in self.r.repairs:
            parts = self.r.interpreter.select_deletions(what, shown)
        else:
            picked_ids = self.r.interpreter.select_targets(what, shown)
            parts = None if picked_ids is None else [(i, None) for i in picked_ids]
        if parts is None:
            self.counters["deletion_select_failed"] += 1
            return []
        records = {ids[i]: self.registry.get(ids[i]) for i, _ in parts}
        old: list[str] = []
        deleted: list[str] = []
        rewritten: list[str] = []
        earlier_ids = {m for ms in history.values() for m in ms}
        for i, remove in parts:
            m = ids[i]
            rec = records.get(m)
            if m in earlier_ids:
                # An earlier version holds nothing current: it goes whole.
                if self._delete(speaker, m):
                    deleted.append(m)
                    old.extend(_values(self._history_text.get(m, ""), self.protected_of.get(m, ())))
            elif remove and rec is not None and self._rewrite(speaker, m, rec, remove):
                rewritten.append(m)
                old.extend(_values(remove, [p for p in rec.protected if _contains(remove, p)]))
            elif self._delete(speaker, m):
                deleted.append(m)
                old.extend(_values(candidates[m], rec.protected if rec else ()))
                # Deleting a record deletes its earlier versions too.
                for e in history.get(m, []):
                    if e not in deleted and self._delete(speaker, e):
                        deleted.append(e)
                        old.extend(_values(self._history_text.get(e, ""), self.protected_of.get(e, ())))
        self.counters["history_deleted"] += sum(1 for m in deleted if m in earlier_ids)
        self._turn_log.update(picked=[ids[i] for i, _ in parts], deleted=deleted, rewritten=rewritten)
        return [
            v
            for v in dict.fromkeys(old)
            if normalize(v) and (not _contains(turn.text, v) or (label.delete and _contains(label.delete.what, v)))
        ]

    def _history(self, current: Sequence[str]) -> dict[str, list[str]]:
        """R6 delete_history: each current record's earlier versions (superseded, or retired by an update).

        Deletion search only reaches current records, so without this an earlier version of deleted
        information would outlive the deletion. Sets ``_history_text`` for the earlier versions found.
        """
        records = self.snapshot()
        pred: dict[str, list[str]] = defaultdict(list)
        for r in records:
            nxt = r.get("superseded_by") or self.replaced_by.get(r["memory_id"])
            if nxt and r["state"] in ("superseded", "retired"):
                pred[nxt].append(r["memory_id"])
        self._history_text = {r["memory_id"]: r["text"] or "" for r in records}
        out: dict[str, list[str]] = {}
        for mid in current:
            chain, todo = [], list(pred.get(mid, []))
            while todo:
                e = todo.pop()
                if e not in chain:
                    chain.append(e)
                    todo.extend(pred.get(e, []))
            if chain:
                out[mid] = chain
        return out

    def _rewrite(self, speaker: str, mid: str, rec: MemRecord, remove: str) -> bool:
        """R6 partial_delete: delete a record and store it again without the part asked to be removed.

        Only when the part occurs in the record and something substantive is left; the new record
        keeps the old one's key, policy, readers, denials and grants, speaker and source turn.
        """
        if rec.fact is None or not _contains(rec.text, remove):
            return False
        rest, n = _mask(rec.text, [remove])
        if not n or rest is None or len(normalize(rest.replace(_MASK, " ")).split()) < 3:
            return False
        denied, granted = self._access_overrides(mid)
        source_turn, source_speaker = self.memory_turn.get(mid), self.speaker_of.get(mid)
        if not self._delete(speaker, mid):
            return False
        fields: dict[str, str] = {"detail": rest}
        summary = rec.fact.content.fields.get("summary") if rec.fact.content.fields else None
        if summary and not _contains(summary, remove) and not any(_contains(summary, v) for v in _values(remove)):
            fields["summary"] = summary
        protected = tuple(p for p in rec.protected if _contains(rest, p))
        if protected:
            fields["protected"] = "\n".join(protected)
        policy = rec.policy
        if granted - denied:
            extra = AccessRule(principal_ids=frozenset(granted - denied), view=View.FULL)
            policy = policy.model_copy(update={"rules": (*policy.rules, extra)})
        views = rec.allowed_views if "summary" in fields else rec.allowed_views - {View.REDACTED}
        fact = rec.fact.model_copy(update={"content": Content(fields=fields), "policy": policy, "allowed_views": views})
        if self._submit(speaker, source_turn or "", AddFact(fact=fact)) != "committed":
            self.counters["partial_delete_restore_failed"] += 1
            return True  # the targeted value is gone either way
        new = self._last_memory_id
        assert new is not None
        if source_turn:
            self.memory_turn[new] = source_turn
        if source_speaker:
            self.speaker_of[new] = source_speaker
        audience = (rec.audience | (granted - denied)) - denied
        self._register(
            dataclasses.replace(
                rec, policy=policy, allowed_views=views, audience=audience, protected=protected, text=rest, fact=fact
            )
        )
        self._deny(denied, new)
        self.counters["deletions_partial"] += 1
        return True

    def _store_activity(self, turn: MethodTurn, speaker: str) -> None:
        """U1 activity_notes: keep a chatter message that states the speaker's own role or task.

        Readable (and updatable) by the speaker only; every value masked, so it never carries a figure
        that a later deletion would have to find.
        """
        text = _mask_values(turn.text.strip())[:2000]
        if len(normalize(text.replace(_VALUE, " ")).split()) < 3:
            return
        me = frozenset({speaker})
        policy = LabelMapper._policy(
            [AccessRule(principal_ids=me, view=View.FULL)], (PrincipalPredicate(principal_ids=me),)
        )
        views = frozenset({View.FULL, View.EXISTENCE_ONLY})
        fact = NewFact(
            semantic_type=SemanticType.NOTE,
            content_schema="tiered_note",
            content=Content(fields={"detail": text}),
            topic=f"activity of {speaker} at {turn.turn_id}",
            subject_ids=(speaker,),
            sensitivity=Sensitivity.INTERNAL,
            policy=policy,
            allowed_views=views,
        )
        if self._submit(speaker, turn.turn_id, AddFact(fact=fact)) == "committed":
            self._register(MemRecord(policy, views, me, None, (), text, fact))
            self.counters["activity_notes"] += 1
            self._turn_log["outcome"] = "activity_note"

    def _own_activity(self, asker: str, limit: int = 40) -> list[GeneratorEvidence]:
        """U1 own_activity: the asker's own earlier records, values masked, oldest first.

        Only what retrieval never shows: their superseded or retired records (an update does not erase
        that the asker did the work), marked as no longer current, and only while the information is
        still held: the chain of replacements (supersedes, and retire-and-add updates) ends in a current
        record, never a deletion. Never a record the asker was restricted from; only for the baselines'
        answerer, which decides entitlement from context.
        """
        out: list[GeneratorEvidence] = []
        now = self.clock.now()
        records = self.snapshot()
        by_id = {r["memory_id"]: r for r in records}

        def still_held(r: dict[str, Any]) -> bool:
            """The information was updated, not removed: its chain of replacements ends in a current record."""
            seen: set[str] = set()
            while r["state"] in ("superseded", "retired") and r["memory_id"] not in seen:
                seen.add(r["memory_id"])
                nxt = by_id.get(r.get("superseded_by") or self.replaced_by.get(r["memory_id"], ""))
                if nxt is None:
                    return False
                r = nxt
            return bool(r["state"] == "current")

        for r in records:
            if r["speaker"] != asker or r["state"] not in ("superseded", "retired") or not r["text"]:
                continue
            if not still_held(r) or self.protected_of.get(r["memory_id"]):
                continue  # removed since, or it held values marked sensitive (names, exact wording, amounts)
            if asker in r["restricted_from"]:
                continue
            text = _mask_values(r["text"])
            if len(normalize(text.replace(_VALUE, " ").replace(_MASK, " ")).split()) < 3:
                continue
            text = f"[earlier, since updated] {text}"
            out.append(
                GeneratorEvidence(
                    memory_id=r["memory_id"],
                    view=View.FULL,
                    semantic_type="note",
                    text=text,
                    fields={"detail": text},
                    observed_at=now,
                    content_schema="tiered_note",
                )
            )
        self.counters["own_activity_items"] += len(out[-limit:])
        return out[-limit:]

    def _model_rescope(self, intent: RescopeIntent, about: list[str], turn: MethodTurn, speaker: str) -> set[str]:
        """Re-scope earlier memories after a consent or revocation (the adaptive step).

        Like deletions: governed queries run *as the speaker*, the model picks the
        memories the instruction applies to among what the speaker can see, and every
        change is a GSM operation submitted as the speaker. Withholding is a memory-level
        restriction (authorized like an update: anyone who may update the memory). Sharing
        lifts any earlier restriction and adds a grant (authorized like a grant: the owner,
        or the people the memory is about). GSM refuses whatever the speaker may not do.
        """
        assert self.r.interpreter is not None and self.mapper is not None
        self.counters["rescope_requests"] += 1
        share = self.mapper.resolve(intent.share_with, speaker, about) - {speaker}
        withhold = self.mapper.resolve(intent.withhold_from, speaker, about) - {speaker} - share
        log: dict[str, Any] = {"share": sorted(share), "withhold": sorted(withhold)}
        self._turn_log["rescope"] = log
        if not share and not withhold:
            self.counters["rescope_no_principals"] += 1
            return withhold
        candidates: dict[str, str] = {}
        for q in dict.fromkeys([intent.what, turn.text]):
            for ev in self.service.search(self.ctx(speaker), QueryRequest(query=q, top_k=20)):
                if ev.memory_id and ev.memory_id not in candidates:
                    candidates[ev.memory_id] = ev.text
        log["candidates"] = list(candidates)
        if not candidates:
            self.counters["rescope_no_candidates"] += 1
            return withhold
        ids = list(candidates)
        picked = self.r.interpreter.select_targets(intent.what, [candidates[i][:600] for i in ids], purpose="rescope")
        if picked is None:
            self.counters["rescope_select_failed"] += 1
            return withhold
        targets = [ids[i] for i in picked]
        log.update(targets=targets, restricted=[], granted=[])
        ctx = self.ctx(speaker)
        for mid in targets:
            if withhold:
                # One operation names at most 50 principals (RestrictAccess); a domain-wide roster can exceed it.
                names = sorted(withhold)
                batches = [
                    RestrictAccess(memory_id=mid, principal_ids=tuple(names[i : i + 50]))
                    for i in range(0, len(names), 50)
                ]
                ok = all(self.service.commit_operations(ctx, [b]).status == "committed" for b in batches)
                self.counters["restrictions_committed" if ok else "restrictions_refused"] += 1
                if ok:
                    log["restricted"].append(mid)
                    self._adjust_audience(mid, remove=withhold)
            for pid in sorted(share):
                self.service.commit_operations(ctx, [RestrictAccess(memory_id=mid, principal_ids=(pid,), lift=True)])
                grant = GrantAccess(memory_id=mid, grantee_principal_id=pid, view=View.FULL)
                r = self.service.commit_operations(ctx, [grant])
                ok = r.status == "committed"
                self.counters["grants_committed" if ok else "grants_refused"] += 1
                if ok:
                    log["granted"].append([mid, pid])
                    self._adjust_audience(mid, add={pid})
        return withhold

    def _adjust_audience(
        self, mid: str, *, add: AbstractSet[str] = frozenset(), remove: AbstractSet[str] = frozenset()
    ) -> None:
        rec = self.registry.get(mid)
        if rec is not None:
            self.registry[mid] = dataclasses.replace(rec, audience=(rec.audience | add) - remove)

    def snapshot(self) -> list[dict[str, Any]]:
        """The whole memory store of this episode, with each record's state and readers (analysis only).

        Content of deleted records is scrubbed by GSM, so only their ids and source turns remain.
        """
        assert self.binding is not None and self.binding.roster is not None
        groups = {(g.relation, g.object_type, g.object_id): g.gid for g in self.binding.roster.groups}
        with self.service.db.tenant_tx(self.binding.tenant_id) as tx:
            atoms = tx.all(
                """SELECT a.memory_id, a.lifecycle_state, a.attribute, a.policy, a.superseded_by_memory_id,
                          v.version_id, v.state AS version_state, v.valid_from, v.valid_until
                   FROM memory_atoms a
                   JOIN LATERAL (SELECT * FROM memory_versions v WHERE v.tenant_id = a.tenant_id
                                 AND v.memory_id = a.memory_id ORDER BY v.created_at DESC LIMIT 1) v ON true
                   WHERE a.tenant_id = :tenant_id ORDER BY a.observed_at, a.memory_id"""
            )
            proj = {
                (r["version_id"], r["view"]): r["text"]
                for r in tx.all("SELECT version_id, view, text FROM memory_projections WHERE tenant_id = :tenant_id")
            }
            denials: dict[str, list[str]] = defaultdict(list)
            for r in tx.all(
                """SELECT resource_id, principal_id FROM explicit_denials WHERE tenant_id = :tenant_id
                   AND resource_type = 'memory' AND lifted_at IS NULL"""
            ):
                denials[r["resource_id"]].append(r["principal_id"])
            grants: dict[str, list[str]] = defaultdict(list)
            for r in tx.all(
                """SELECT memory_id, grantee_principal_id FROM explicit_grants WHERE tenant_id = :tenant_id
                   AND resource_type = 'memory' AND revoked_at IS NULL"""
            ):
                grants[r["memory_id"]].append(r["grantee_principal_id"])

        def readers(policy: dict[str, Any], view: str) -> list[str]:
            out: list[str] = []
            for rule in policy.get("rules", []):
                if rule.get("view") != view:
                    continue
                if rule.get("tenant_wide"):
                    out.append("everyone")
                out += [f"role:{r}" for r in rule.get("any_roles") or []]
                rel = rule.get("relation")
                if rel:
                    out.append(groups.get((rel["relation"], rel["object_type"], rel["object_id"]), rel["relation"]))
                out += list(rule.get("principal_ids") or [])
            return sorted(set(out))

        records = []
        for a in atoms:
            if a["lifecycle_state"] == "tombstoned":
                state = "deleted"
            elif a["version_state"] == "active":
                state = "current"
            elif a["superseded_by_memory_id"]:
                state = "superseded"
            else:
                state = "retired"
            mid = a["memory_id"]
            records.append(
                {
                    "memory_id": mid,
                    "turn_id": self.memory_turn.get(mid),
                    "speaker": self.speaker_of.get(mid),
                    "state": state,
                    "key": a["attribute"],
                    "text": proj.get((a["version_id"], "full")),
                    "summary": proj.get((a["version_id"], "redacted")),
                    "full_readers": readers(a["policy"] or {}, "full"),
                    "summary_readers": readers(a["policy"] or {}, "redacted"),
                    "restricted_from": sorted(denials.get(mid, [])),
                    "granted_to": sorted(grants.get(mid, [])),
                    "superseded_by": a["superseded_by_memory_id"],
                }
            )
        return records

    # -------------------------------------------------------------- answers
    def answer(self, ckpt: MethodCheckpoint) -> tuple[dict[str, Any], dict[str, Any]]:
        assert self.binding is not None
        self.seen_evidence = []
        own = "own_activity" in self.r.repairs and self.answerer is not None
        self.generator.extra = self._own_activity(_obj(ckpt.asker_principal_id)) if own else []
        self.generator.last_evidence = []
        if self.r.snapshots:
            self.memory_snapshots.append(
                {"checkpoint_id": ckpt.checkpoint_id, "as_of_turn_id": ckpt.as_of_turn_id, "records": self.snapshot()}
            )
        self.generator.prompt_tokens = self.generator.completion_tokens = 0
        if self.answerer is not None:
            self.answerer.bind_checkpoint(ckpt)
        diag: dict[str, Any] = {}
        if self.r.diagnostics:
            self.service.hooks.after_candidates = lambda _ctx, c: diag.update(
                allowed=[{"memory_id": x.memory_id, "view": x.view.value} for x in c.allowed],
                refusable=[x.memory_id for x in c.refusable],
            )
            self.service.hooks.on_ranking = lambda notes: diag.update(ranking=notes)
        started = time.perf_counter()
        try:
            ctx = self.ctx(_obj(ckpt.asker_principal_id))
            resp = self.service.query(ctx, QueryRequest(query=ckpt.query_text, top_k=self.r.top_k))
            if self.generator.last_evidence:
                self.seen_evidence = list(self.generator.last_evidence)
            action, answer = ACTION_MAP[resp.action], resp.answer
            used = [e.memory_id for e in resp.evidence if e.memory_id]
            reasons = list(resp.reason_codes)
        except (ContextError, GsmError):
            action, answer, used, reasons = "no_memory", "", [], ["CONTEXT_MISSING"]
        latency = (time.perf_counter() - started) * 1000
        if self.r.diagnostics:
            self.service.hooks.after_candidates = None
            self.service.hooks.on_ranking = None
            for item in [*diag.get("allowed", []), *diag.get("ranking", [])]:
                item["turn_id"] = self.memory_turn.get(item["memory_id"])
            self.checkpoint_diag.append(
                {
                    "checkpoint_id": ckpt.checkpoint_id,
                    "episode_id": ckpt.episode_id,
                    "asker": ckpt.asker_principal_id,
                    "evidence": [e.memory_id for e in self.seen_evidence],
                    "evidence_turns": [self.memory_turn.get(e.memory_id) for e in self.seen_evidence],
                    **diag,
                }
            )
        pred = {
            "checkpoint_id": ckpt.checkpoint_id,
            "output": {
                "action": action,
                "answer": answer,
                "answer_structured": {},
                "used_record_ids": used,
                # What the answerer was actually shown, for GateMem's context-exposure metrics.
                "memory_audit": self._memory_audit(),
            },
        }
        metric = {
            "checkpoint_id": ckpt.checkpoint_id,
            "action": action,
            "latency_ms": round(latency, 3),
            "prompt_tokens": self.generator.prompt_tokens,
            "completion_tokens": self.generator.completion_tokens,
            "evidence_items_shown": len(self.seen_evidence),
            "reason_codes": reasons,
            "verifier_intervened": bool(VERIFIER_BLOCK_REASONS & set(reasons)),
        }
        return pred, metric

    def _memory_audit(self) -> dict[str, Any]:
        out = self.answerer.last_output if self.answerer is not None else None
        if out and isinstance(out.get("memory_audit"), dict):
            audit: dict[str, Any] = out["memory_audit"]  # the bench's own record of its prompt block
            return audit
        return {"prompt_context": {"text": "\n".join(e.text for e in self.seen_evidence)}}

    def run(
        self, pending: dict[str, list[MethodCheckpoint]], on_checkpoint: Callable[[dict[str, Any]], None] | None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        self.binding = bootstrap_episode(self.service, self.ep)
        if self.answerer is not None:
            self.answerer.bind_episode(self.ep)
        self.mapper = LabelMapper(self.binding)
        self._last_memory_id = None
        floor_on = "delegate_floor" in self.r.repairs
        if (
            self.r.interpreter is not None
            and self.binding.roster is not None
            and (self.r.scope_fit or self.r.oversight_withhold or floor_on)
        ):
            roles = self.r.interpreter.classify_groups(roster=self.binding.roster) or {}
            self.oversight = {g for g, r in roles.items() if r == "oversight"}
            attributed = {g.gid for g in self.binding.roster.groups if g.attributes}
            self.scoped = sorted({g for g, r in roles.items() if r == "scoped"} | attributed)
            self.counters["groups_oversight"] += len(self.oversight)
            self.counters["groups_scoped"] += len(self.scoped)
            if floor_on:
                self.floor = delegate_floor(self.binding.roster, roles)
                self.counters["groups_with_floor"] += len(self.floor)
        preds: list[dict[str, Any]] = []
        metrics: list[dict[str, Any]] = []
        now = self.clock.now()
        for turn in self.ep.turns:
            now = _parse_ts(turn.timestamp, now)
            self.clock.set(now)
            self.ingest(turn, _obj(turn.speaker_principal_id))
            for ckpt in pending.get(turn.turn_id, []):
                self.clock.set(now + timedelta(seconds=30))
                pred, metric = self.answer(ckpt)
                preds.append(pred)
                metrics.append(metric)
                if on_checkpoint:
                    on_checkpoint(metric)
        self.counters["episodes"] += 1
        return preds, metrics


_NO_RECORD = MemRecord(MemoryPolicy(), frozenset(), frozenset(), None)


class GateMemRunner:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        providers: Providers | None = None,
        resolve_deletions: Literal["none", "lexical"] = "none",
        turn_policy: DeterministicTurnPolicy | None = None,
        interpreter: QwenTurnInterpreter | None = None,
        answerer_factory: Callable[[], BaselineAnswerer] | None = None,
        top_k: int = 8,
        workers: int = 1,
        diagnostics: bool = False,
        need_to_know: bool = False,
        oversight_withhold: bool = False,
        revocation_override: bool = False,
        scope_fit: bool = False,
        snapshots: bool = False,
        repairs: AbstractSet[str] = REPAIRS,
    ) -> None:
        unknown = set(repairs) - REPAIRS
        if unknown:
            raise ValueError(f"unknown repairs: {sorted(unknown)}")
        self.repairs = frozenset(repairs)
        self.snapshots = snapshots
        self.diagnostics = diagnostics
        # Off by default: in-scope versions (v15) measured no gain over v13 beyond run-to-run noise.
        self.scope_fit = scope_fit
        # Both off by default: measured on GateMem (v14 vs v13) each cost more than it fixed.
        self.oversight_withhold = oversight_withhold
        self.revocation_override = revocation_override
        # Off by default: measured on GateMem it cost far more utility than the leaks it removed.
        self.need_to_know = need_to_know
        self.answerer_factory = answerer_factory
        self.db = db
        self.settings = settings
        self.providers = providers or Providers()
        self.resolve_deletions = resolve_deletions
        self.turn_policy = turn_policy or DeterministicTurnPolicy()
        self.interpreter = interpreter
        self.top_k = top_k
        self.workers = max(1, workers)

    def run(
        self,
        episodes: Sequence[MethodEpisode],
        checkpoints: Sequence[MethodCheckpoint],
        *,
        on_checkpoint: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunOutputs:
        by_episode: dict[str, dict[str, list[MethodCheckpoint]]] = defaultdict(lambda: defaultdict(list))
        for c in checkpoints:
            by_episode[c.episode_id][c.as_of_turn_id].append(c)
        errors: dict[str, str] = {}

        def work(ep: MethodEpisode) -> tuple[_EpisodeSession, list[dict[str, Any]], list[dict[str, Any]]]:
            session = _EpisodeSession(self, ep)
            try:
                preds, metrics = session.run(by_episode.get(ep.episode_id, {}), on_checkpoint)
            except Exception as exc:  # one broken episode must not sink the run; reported below
                log.exception("episode_failed", extra={"episode_id": ep.episode_id})
                errors[ep.episode_id] = type(exc).__name__
                return session, [], []
            return session, preds, metrics

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(work, episodes))
        out = RunOutputs()
        counters: dict[str, int] = defaultdict(int)
        tenants: list[str] = []
        answerer_totals: dict[str, float] = defaultdict(float)
        for session, preds, metrics in results:
            if session.answerer is not None:
                for k, v in session.answerer.metrics.snapshot().items():
                    answerer_totals[k] += v
            out.predictions.extend(preds)
            out.method_metrics.extend(metrics)
            out.ingest_log.extend(session.ingest_log)
            out.checkpoint_diag.extend(session.checkpoint_diag)
            out.memory_snapshots.extend(session.memory_snapshots)
            for k, v in session.counters.items():
                counters[k] += v
            if session.binding is not None:
                tenants.append(session.binding.tenant_id)
        interp = self.interpreter
        out.summary = {
            "method": "gsm",
            "interpreter": interp.name if interp else self.turn_policy.name,
            "label_prompt": interp.label_prompt if interp else None,
            "repairs": sorted(self.repairs),
            "resolve_deletions": "model" if interp else self.resolve_deletions,
            "generator": BaselineAnswerer.name if self.answerer_factory else self.providers.generator.name,
            "release_action_policy": "answerer" if self.answerer_factory else self.settings.release_action_policy,
            "allowed_by_default": any(
                bool(getattr(s.answerer, "allowed_by_default", False)) for s, _, _ in results if s.answerer is not None
            ),
            "top_k": self.top_k,
            "workers": self.workers,
            "counters": dict(sorted(counters.items())),
            "episode_errors": errors,
            "deletion_propagation": self.deletion_propagation(tenants),
            "provider_metrics": {
                "generator": dict(answerer_totals)
                if self.answerer_factory
                else self.providers.generator.metrics.snapshot(),
                "interpreter": interp.metrics.snapshot() if interp else None,
                "interpreter_cache_hits": interp.cache_hits if interp else 0,
                "interpreter_failure_reasons": dict(interp.failure_reasons) if interp else {},
                "embedder": self.providers.embedder.metrics.snapshot() if self.providers.embedder else None,
                "planner": self._planner_metrics(),
            },
        }
        return out

    def _planner_metrics(self) -> dict[str, Any]:
        planner = self.providers.planner
        metrics = getattr(planner, "metrics", None)
        out: dict[str, Any] = {"name": planner.name}
        if metrics is not None:
            out |= metrics.snapshot() | {"cache_hits": getattr(planner, "cache_hits", 0)}
        return out

    def deletion_propagation(self, tenants: Sequence[str]) -> dict[str, Any]:
        """Content-free: fraction of cascade work completed across the run's tenants."""
        service = GovernedMemoryService(self.db, self.settings)
        found = tombstoned = jobs = completed = 0
        for t in tenants:
            audit = service.audit_export(t, limit=100_000)
            for c in audit["deletion_cascades"]:
                jobs += 1
                completed += c["status"] == "completed"
                found += c["descendants_found"]
                tombstoned += c["descendants_tombstoned"]
        return {
            "cascade_jobs": jobs,
            "cascade_jobs_completed": completed,
            "descendants_found": found,
            "descendants_tombstoned": tombstoned,
            "completeness": (completed / jobs) if jobs else None,
        }


def write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def run_gatemem(
    db: Database,
    settings: Settings,
    data_dir: Path,
    out_dir: Path,
    *,
    max_episodes: int | None = None,
    resolve_deletions: Literal["none", "lexical"] = "none",
    providers: Providers | None = None,
    interpreter: QwenTurnInterpreter | None = None,
    answerer_factory: Callable[[], BaselineAnswerer] | None = None,
    top_k: int = 8,
    workers: int = 1,
    diagnostics: bool = False,
    need_to_know: bool = False,
    oversight_withhold: bool = False,
    revocation_override: bool = False,
    scope_fit: bool = False,
    snapshots: bool = False,
    repairs: AbstractSet[str] = REPAIRS,
    continuous: bool = False,
) -> RunOutputs:
    episodes, checkpoints = load_for_method(data_dir)
    if max_episodes is not None:
        episodes = episodes[:max_episodes]
    keep = {e.episode_id for e in episodes}
    checkpoints = [c for c in checkpoints if c.episode_id in keep]
    merge_report = None
    if continuous:
        # The whole domain as one memory, never reset between episodes (see continuous.py).
        merged, checkpoints, merge_report = merge_domain(episodes, checkpoints)
        episodes = [merged]
    runner = GateMemRunner(
        db,
        settings,
        providers=providers,
        resolve_deletions=resolve_deletions,
        interpreter=interpreter,
        answerer_factory=answerer_factory,
        top_k=top_k,
        workers=workers,
        diagnostics=diagnostics,
        need_to_know=need_to_know,
        oversight_withhold=oversight_withhold,
        revocation_override=revocation_override,
        scope_fit=scope_fit,
        snapshots=snapshots,
        repairs=repairs,
    )
    started = time.perf_counter()
    out = runner.run(episodes, checkpoints)
    out.summary.update(
        {
            "data_dir": str(data_dir),
            "episodes": len(episodes),
            "checkpoints": len(checkpoints),
            "predictions": len(out.predictions),
            "wall_seconds": round(time.perf_counter() - started, 1),
            "continuous": dataclasses.asdict(merge_report) if merge_report else None,
        }
    )
    write_jsonl(out_dir / "predictions.jsonl", out.predictions)
    write_jsonl(out_dir / "method_metrics.jsonl", out.method_metrics)
    if diagnostics:
        write_jsonl(out_dir / "ingest_log.jsonl", out.ingest_log)
        write_jsonl(out_dir / "checkpoint_diag.jsonl", out.checkpoint_diag)
    if snapshots:
        write_jsonl(out_dir / "memory_snapshots.jsonl", out.memory_snapshots)
    (out_dir / "run_summary.json").write_text(json.dumps(out.summary, indent=2, default=str))
    return out
