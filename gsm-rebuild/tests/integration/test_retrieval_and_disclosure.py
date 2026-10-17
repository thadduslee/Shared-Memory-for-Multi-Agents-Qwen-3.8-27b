"""Pre-retrieval enforcement and disclosure behaviour."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

import pytest

import gsm.service as service_module
from gsm.domain.policy import AccessRule, MemoryPolicy, PrincipalPredicate
from gsm.domain.types import AuthStrength, QueryAction, Sensitivity, View
from gsm.providers.base import DraftResponse, GeneratorEvidence, GeneratorPlan, ProviderMetrics
from gsm.release.generator import DeterministicGenerator
from gsm.retrieval.planner import DeterministicPlanner, QueryPlan
from gsm.service import Providers, QueryRequest
from tests.helpers import World, add, apollo_decision, availability_fact, note_fact


class SpyGenerator(DeterministicGenerator):
    name = "spy"

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[GeneratorEvidence] = []
        self.queries: list[str] = []

    def generate(self, query: str, plan: GeneratorPlan, evidence: Sequence[GeneratorEvidence]) -> DraftResponse:
        self.seen.extend(evidence)
        self.queries.append(query)
        out = super().generate(query, plan, evidence)
        return out.model_copy(update={"generator": self.name})


@pytest.fixture
def spy() -> SpyGenerator:
    return SpyGenerator()


@pytest.fixture
def providers(spy: SpyGenerator) -> Providers:
    return Providers(generator=spy)


def seed_distractors(world: World) -> dict[str, str]:
    """Many near-duplicate memories the requester (bob) must never see."""
    ids = {}
    ids["zeus"] = add(
        world,
        "henry",
        note_fact(
            "Zeus launch decision: launch Zeus on October 14 with blue-green",
            "Zeus launch decision",
            template="project",
            project_id="zeus",
        ),
    )
    ids["private"] = add(
        world,
        "alice",
        note_fact("Private note: Apollo launch decision might slip to December", "Alice private launch note"),
    )
    ids["hr"] = add(
        world,
        "henry",
        note_fact(
            "Apollo launch decision owner Bob is on a PIP",
            "Bob HR case",
            template="hr_restricted",
            subjects=("bob2",),
            compartments=frozenset({"hr"}),
        ),
    )
    return ids


class TestPreRetrievalEnforcement:
    def test_unauthorized_never_in_candidates_bundle_or_generator(
        self, world: World, spy: SpyGenerator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        allowed = add(world, "alice", apollo_decision())
        hidden = seed_distractors(world)
        captured: dict[str, Any] = {}
        real_rank = service_module.rank_within

        def spy_rank(tx: Any, pairs: Any, plan: Any, cfg: Any, embedder: Any, **kw: Any) -> Any:
            captured.setdefault("pairs", []).append(list(pairs))
            return real_rank(tx, pairs, plan, cfg, embedder, **kw)

        def after_candidates(_ctx: Any, cands: Any) -> None:
            captured["allowed"] = cands.allowed_ids
            captured["decided"] = set(cands.decisions)

        monkeypatch.setattr(service_module, "rank_within", spy_rank)
        world.service.hooks.after_candidates = after_candidates
        r = world.service.query(world.ctx("bob"), QueryRequest(query="Apollo launch decision October blue-green"))
        assert r.action is QueryAction.ANSWER
        assert captured["allowed"] == {allowed}
        assert not set(hidden.values()) & captured["allowed"]
        # SQL pre-filter did not even return memories bob has no tag, grant or ownership path to
        assert not {hidden["zeus"], hidden["hr"], hidden["private"]} & captured["decided"]
        ranked_versions = {v for pairs in captured["pairs"] for v, _ in pairs}
        assert len(ranked_versions) == 1
        assert {e.memory_id for e in spy.seen} == {allowed}
        assert {e.memory_id for e in r.evidence} == {allowed}
        assert "December" not in r.answer and "PIP" not in r.answer and "Zeus" not in r.answer

    def test_vector_ranking_is_confined_to_authorized_subset(self, world: World) -> None:
        # bob can see exactly one weakly related memory; many closer ones are hidden
        add(
            world,
            "alice",
            note_fact("Apollo retrospective notes", "Apollo retro", template="project", project_id="apollo"),
        )
        for i in range(15):
            add(
                world,
                "henry",
                note_fact(
                    f"Quarterly revenue forecast number {i} for Zeus",
                    f"Zeus forecast {i}",
                    template="project",
                    project_id="zeus",
                ),
            )
        r = world.service.query(world.ctx("bob"), QueryRequest(query="quarterly revenue forecast", top_k=5))
        assert all("Zeus" not in e.text for e in r.evidence)
        assert "forecast" not in r.answer

    def test_lexical_only_mode(self, world: World) -> None:
        world.service.providers.embedder = None
        add(world, "alice", apollo_decision())
        r = world.service.query(world.ctx("bob"), QueryRequest(query="Apollo launch decision"))
        assert r.action is QueryAction.ANSWER and all(e.view is View.FULL for e in r.evidence)

    def test_redacted_ranking_ignores_hidden_fields(self, world: World) -> None:
        add(world, "dana", availability_fact())
        # "surgery" only exists in the hidden field: it must not make the record rank for paula
        r = world.service.query(world.ctx("paula"), QueryRequest(query="knee surgery recovery"))
        assert r.action is QueryAction.NO_MEMORY


class TestDisclosure:
    def test_each_view_exposes_exactly_its_fields(self, world: World, spy: SpyGenerator) -> None:
        mid = add(world, "dana", availability_fact())
        expected = {
            "dana": (View.FULL, {"employee", "availability", "return_date", "reason", "team"}),
            "henry": (View.FULL, {"employee", "availability", "return_date", "reason", "team"}),
            "paula": (View.REDACTED, {"employee", "availability", "return_date", "team"}),
            "bob": (View.AGGREGATE, {"availability", "team"}),
        }
        for who, (view, fields) in expected.items():
            m = world.service.get_memory(world.ctx(who), mid)
            assert m.view is view and set(m.fields) == fields, who
        # existence-only via an explicit narrow request
        m = world.service.get_memory(world.ctx("dana"), mid, max_view=View.EXISTENCE_ONLY)
        assert m.fields == {} and m.text == "An availability update exists."

    def test_generator_cannot_reconstruct_hidden_field(self, world: World, spy: SpyGenerator) -> None:
        add(world, "dana", availability_fact())
        r = world.service.query(world.ctx("paula"), QueryRequest(query="When is Dana Reyes back? Is she unavailable?"))
        assert r.action is QueryAction.ANSWER_REDACTED
        assert spy.seen and all("reason" not in e.fields and "surgery" not in e.text for e in spy.seen)
        assert "surgery" not in r.answer

    def test_hidden_existence_is_indistinguishable_from_nothing(self, world: World) -> None:
        add(world, "dana", availability_fact())
        s = world.service
        protected = s.query(world.ctx("carol"), QueryRequest(query="Dana availability update return date"))
        nothing = s.query(world.ctx("carol"), QueryRequest(query="Zork availability update return date"))
        strip = {"correlation_id", "release_id"}
        assert protected.model_dump(exclude=strip) == nothing.model_dump(exclude=strip)
        assert protected.action is QueryAction.NO_MEMORY

    def test_refuse_when_policy_permits_acknowledging_existence(self, world: World) -> None:
        policy = MemoryPolicy(existence_visible_to=(PrincipalPredicate(any_roles=frozenset({"manager"})),))
        op = note_fact("Reorg plan: merge Apollo and Zeus teams", "Reorg plan")
        add(
            world,
            "alice",
            op.model_copy(update={"fact": op.fact.model_copy(update={"policy_template": None, "policy": policy})}),
        )
        carol = world.service.query(world.ctx("carol"), QueryRequest(query="reorg plan"))
        assert carol.action is QueryAction.REFUSE and "merge" not in carol.answer and carol.evidence == ()
        bob = world.service.query(world.ctx("bob"), QueryRequest(query="reorg plan"))
        assert bob.action is QueryAction.NO_MEMORY

    def test_escalate_when_step_up_would_help(self, world: World) -> None:
        add(
            world,
            "alice",
            note_fact(
                "Apollo signing key rotation is Friday",
                "Apollo key rotation",
                template="project",
                project_id="apollo",
                sensitivity=Sensitivity.RESTRICTED,
            ),
        )
        weak = world.service.query(world.ctx("bob"), QueryRequest(query="Apollo key rotation"))
        assert weak.action is QueryAction.ESCALATE and "Friday" not in weak.answer
        strong = world.service.query(
            world.ctx("bob", strength=AuthStrength.MFA), QueryRequest(query="Apollo key rotation")
        )
        assert strong.action is QueryAction.ANSWER and "Friday" in strong.answer
        outsider = world.service.query(world.ctx("carol"), QueryRequest(query="Apollo key rotation"))
        assert outsider.action is QueryAction.NO_MEMORY

    def test_mixed_views_produce_answer_redacted_and_aggregate_hides_ids(self, world: World) -> None:
        add(world, "dana", availability_fact())
        r = world.service.query(world.ctx("bob"), QueryRequest(query="team member unavailable"))
        assert r.action is QueryAction.ANSWER_REDACTED
        assert r.evidence and all(e.memory_id is None for e in r.evidence)
        assert "1 x team member unavailable" in r.answer

    def test_purpose_bound_rule(self, world: World) -> None:
        policy = MemoryPolicy(
            rules=(
                AccessRule(
                    any_roles=frozenset({"manager"}), view=View.FULL, purposes=frozenset({"performance_review"})
                ),
            )
        )
        op = note_fact("Mallory exceeded Q3 targets by 12%", "Mallory Q3 review")
        add(
            world,
            "henry",
            op.model_copy(update={"fact": op.fact.model_copy(update={"policy_template": None, "policy": policy})}),
        )
        no_purpose = world.service.query(world.ctx("carol"), QueryRequest(query="Mallory Q3 review"))
        assert no_purpose.action is QueryAction.NO_MEMORY
        ok = world.service.query(
            world.ctx("carol", purpose="performance_review"), QueryRequest(query="Mallory Q3 review")
        )
        assert ok.action is QueryAction.ANSWER and "12%" in ok.answer


def test_request_top_k_is_honored(world: World) -> None:
    for i in range(5):
        add(
            world,
            "alice",
            note_fact(f"Apollo checklist item {i}", f"Apollo checklist {i}", template="project", project_id="apollo"),
        )
    one = world.service.query(world.ctx("bob"), QueryRequest(query="Apollo checklist item", top_k=1))
    assert len(one.evidence) == 1
    many = world.service.query(world.ctx("bob"), QueryRequest(query="Apollo checklist item", top_k=4))
    assert len(many.evidence) == 4


def test_search_returns_only_permitted_projections_without_generation(world: World, spy: SpyGenerator) -> None:
    mid = add(world, "dana", availability_fact())
    paula = world.service.search(world.ctx("paula"), QueryRequest(query="Dana Reyes unavailable"))
    assert [e.memory_id for e in paula] == [mid] and paula[0].view is View.REDACTED
    assert "surgery" not in paula[0].text and "reason" not in paula[0].fields
    bob = world.service.search(world.ctx("bob"), QueryRequest(query="team member unavailable"))
    assert bob and all(e.memory_id is None and e.view is View.AGGREGATE for e in bob)
    assert world.service.search(world.ctx("carol"), QueryRequest(query="Dana Reyes unavailable")) == ()
    assert spy.seen == []  # nothing was generated


class TestRelevanceBar:
    def seed(self, world: World) -> str:
        specific = add(
            world,
            "alice",
            note_fact(
                "Apollo budget is 221,000 USD and the max discount is 9%",
                "Apollo budget and discount",
                template="project",
                project_id="apollo",
            ),
        )
        for i in range(3):  # generic notes sharing only the word "customer" with the question
            add(
                world,
                "alice",
                note_fact(
                    f"Customer feedback round {i} scheduled for next sprint",
                    f"feedback round {i}",
                    template="project",
                    project_id="apollo",
                ),
            )
        return specific

    def test_one_shared_generic_word_is_not_relevance(self, world: World) -> None:
        specific = self.seed(world)
        world.service.providers.embedder = None  # lexical bar only
        r = world.service.search(
            world.ctx("bob"), QueryRequest(query="What are Apollo's exact budget, customer and max discount?")
        )
        assert [e.memory_id for e in r] == [specific]
        none = world.service.search(world.ctx("bob"), QueryRequest(query="Which customer signed the Zeus contract?"))
        assert none == ()

    def test_duplicates_are_collapsed(self, world: World) -> None:
        for i in range(4):
            add(
                world,
                "alice",
                note_fact(
                    "Reminder: keep Apollo and Apollo-2 names separate",
                    f"reminder {i}",
                    template="project",
                    project_id="apollo",
                ),
            )
        r = world.service.search(world.ctx("bob"), QueryRequest(query="Apollo Apollo-2 names separate reminder"))
        assert len(r) == 1

    def test_hidden_records_do_not_change_the_requesters_ranking(self, world: World) -> None:
        self.seed(world)
        q = QueryRequest(query="Apollo budget customer max discount")
        before = [(e.memory_id, e.text) for e in world.service.search(world.ctx("bob"), q)]
        from gsm.retrieval import search as search_module

        seen: list[dict[str, float]] = []
        real = search_module.term_weights

        def spy(tx: Any, pairs: Any, terms: Any) -> dict[str, float]:
            out = real(tx, pairs, terms)
            seen.append(out)
            return out

        search_module.term_weights = spy  # type: ignore[assignment]
        try:
            world.service.search(world.ctx("bob"), q)
            for i in range(10):  # records bob cannot see, full of the query's words
                add(
                    world,
                    "henry",
                    note_fact(
                        f"Zeus budget customer discount memo {i}",
                        f"Zeus memo {i}",
                        template="project",
                        project_id="zeus",
                    ),
                )
            after = [(e.memory_id, e.text) for e in world.service.search(world.ctx("bob"), q)]
        finally:
            search_module.term_weights = real  # type: ignore[assignment]
        assert after == before and seen[0] == seen[1]


class _PartsPlanner(DeterministicPlanner):
    """Stands in for the model decomposer with fixed sub-questions."""

    name = "parts"

    def __init__(self, parts: Sequence[str]) -> None:
        self.parts = tuple(parts)

    def plan(self, query: str, ctx: Any, **kw: Any) -> QueryPlan:
        return super().plan(query, ctx, **kw).model_copy(update={"parts": self.parts})


class TestMultiPartQuestions:
    QUESTION = (
        "For Apollo, who is the hardware vendor, which kickoff room are we using, "
        "what did legal approve for the rollout, and when is demo day?"
    )
    PARTS = (
        "Who is Apollo's hardware vendor?",
        "Which kickoff room is Apollo using?",
        "What did legal approve for the Apollo rollout?",
        "When is Apollo demo day?",
    )

    def seed(self, world: World) -> tuple[str, str, str]:
        def note(text: str, topic: str) -> str:
            return add(world, "alice", note_fact(text, topic, template="project", project_id="apollo"))

        return (
            note("Apollo vendor is Heron Supply", "Apollo vendor"),
            note("Apollo kickoff room is 4B on floor three", "Apollo kickoff room"),
            note("Apollo demo day moved to the twelfth", "Apollo demo day"),
        )

    def test_a_long_question_dilutes_single_part_answers(self, world: World) -> None:
        vendor, room, demo = self.seed(world)
        world.service.providers.embedder = None
        ids = {e.memory_id for e in world.service.search(world.ctx("bob"), QueryRequest(query=self.QUESTION))}
        assert {room, demo} <= ids and vendor not in ids  # the vendor note covers too little of the question

    def test_each_part_is_ranked_against_its_own_bar(self, world: World) -> None:
        vendor, room, demo = self.seed(world)
        world.service.providers.embedder = None
        world.service.providers.planner = _PartsPlanner(self.PARTS)
        notes: list[dict[str, Any]] = []
        world.service.hooks.on_ranking = notes.extend
        r = world.service.search(world.ctx("bob"), QueryRequest(query=self.QUESTION))
        assert {e.memory_id for e in r} == {vendor, room, demo}
        assert {n["memory_id"]: n["status"] for n in notes}[vendor] == "kept"

    def test_parts_share_top_k_round_robin(self, world: World) -> None:
        vendor, room, demo = self.seed(world)
        world.service.providers.embedder = None
        world.service.providers.planner = _PartsPlanner(self.PARTS)
        r = world.service.search(world.ctx("bob"), QueryRequest(query=self.QUESTION, top_k=3))
        assert {e.memory_id for e in r} == {vendor, room, demo}

    def test_parts_only_rank_inside_the_authorized_set(self, world: World) -> None:
        self.seed(world)
        hidden = add(
            world,
            "henry",
            note_fact("Zeus hardware vendor is Quartz", "Zeus vendor", template="project", project_id="zeus"),
        )
        world.service.providers.planner = _PartsPlanner(("Who is the hardware vendor?", "Which kickoff room?"))
        r = world.service.search(world.ctx("bob"), QueryRequest(query="hardware vendor and kickoff room?"))
        assert hidden not in {e.memory_id for e in r}


class _TopicEmbedder:
    """Two orthogonal directions: texts about budgets, and everything else."""

    model_id = "test-topic-2"
    dim = 2

    def __init__(self) -> None:
        self.metrics = ProviderMetrics()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "budget" in t.lower() else [0.0, 1.0] for t in texts]


class TestSemanticSupport:
    def test_lexical_matches_need_semantic_support_when_configured(self, world: World) -> None:
        world.service.providers.embedder = _TopicEmbedder()
        budget = TestRelevanceBar().seed(world)
        world.service.ranking = dataclasses.replace(world.service.ranking, relative_cutoff=0.0)
        q = QueryRequest(query="Apollo budget and customer feedback")
        loose = {e.memory_id for e in world.service.search(world.ctx("bob"), q)}
        assert budget in loose and len(loose) == 4  # the feedback notes match "customer feedback" lexically
        world.service.ranking = dataclasses.replace(world.service.ranking, vector_support_similarity=0.3)
        strict = [e.memory_id for e in world.service.search(world.ctx("bob"), q)]
        assert strict == [budget]


class TestBudgetedFullContext:
    def seed(self, world: World) -> tuple[list[str], str]:
        ids = []
        for i, text in enumerate(
            ["Apollo kickoff is Monday", "Apollo vendor is Heron Supply", "Payroll export finished for March"]
        ):
            ids.append(add(world, "alice", note_fact(text, f"note {i}", template="project", project_id="apollo")))
            world.tick(60)
        hidden = add(
            world, "henry", note_fact("Zeus budget is 48,500 EUR", "Zeus", template="project", project_id="zeus")
        )
        return ids, hidden

    def enable(self, world: World, items: int, chars: int = 60000) -> None:
        world.service.settings = world.service.settings.model_copy(
            update={"full_context_max_items": items, "full_context_max_chars": chars}
        )

    def test_answering_gets_the_whole_permitted_set_oldest_first(self, world: World, spy: SpyGenerator) -> None:
        ids, hidden = self.seed(world)
        self.enable(world, items=50)
        world.service.query(world.ctx("bob"), QueryRequest(query="Who is the Apollo vendor?"))
        assert [e.memory_id for e in spy.seen] == ids  # unrelated but permitted notes included, in time order
        assert hidden not in {e.memory_id for e in spy.seen}

    def test_over_budget_falls_back_to_ranking(self, world: World, spy: SpyGenerator) -> None:
        ids, _ = self.seed(world)
        self.enable(world, items=2)  # three permitted notes: too many
        world.service.query(world.ctx("bob"), QueryRequest(query="Who is the Apollo vendor?"))
        assert [e.memory_id for e in spy.seen] == [ids[1]]
        spy.seen.clear()
        self.enable(world, items=50, chars=10)  # too long
        world.service.query(world.ctx("bob"), QueryRequest(query="Which vendor does Apollo use?"))
        assert [e.memory_id for e in spy.seen] == [ids[1]]

    def test_searches_stay_ranked(self, world: World) -> None:
        ids, _ = self.seed(world)
        self.enable(world, items=50)
        r = world.service.search(world.ctx("bob"), QueryRequest(query="Who is the Apollo vendor?"))
        assert [e.memory_id for e in r] == [ids[1]]
