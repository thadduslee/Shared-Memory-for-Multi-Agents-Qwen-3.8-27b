"""Controlled, annotation-free examples. Expectations are specified by the tests."""
import asyncio
import copy
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from gatemem_adapter import MedicalDataset, visible_checkpoint
from online.backend import Backend
from online.evidence import audit_history, build_packet, analyze_packet, evidence_assessment
from online.failure_feedback import sanitized_failure_feedback, classify_failure
from online.proposal import normalize_proposal, shape_errors
from online.runner import OnlineRunner, OnlineOptions
from online.validation import ValidationFailure, build_gates, copy_workspace
from templates.memory_system.agent import GateMemAgent, _answer_plan
from templates.memory_system.store import Decision, Evidence


def header(domain='generic'):
    return {'episode_id': 'e', 'domain': domain, 'entities': {'principals': [
        {'principal_id': 'owner', 'role': 'member', 'display_name': 'River'},
        {'principal_id': 'other', 'role': 'helper', 'display_name': 'Avery'}], 'relationships': []}}


def turn(tid, text, at='2026-01-01T00:00:00Z'):
    return {'turn_id': tid, 'text': text, 'speaker': {'principal_id': 'owner', 'role': 'member'},
            'turn_kind': 'message', 'timestamp': at}


def checkpoint(query='When is the project review?', requester='owner', tid='t1'):
    return {'checkpoint_id': 'q', 'episode_id': 'e', 'as_of_turn_id': tid,
            'query_text': query, 'asker': {'principal_id': requester, 'role': 'member'}}


@pytest.fixture
def memory(tmp_path):
    agent = GateMemAgent(str(tmp_path / 'memory.sqlite'))
    agent.open_episode(header())
    agent.ingest(turn('t1', 'The project review starts at 14:30 in room Birch.'))
    yield agent
    agent.store.close()


def blob(agent):
    agent.store.conn.commit()
    agent.store.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    return Path(agent.store.db_path).read_bytes()


def prediction(agent, cp=None):
    out = agent.query(cp or checkpoint())
    from renderer_contract import TrustedRenderDecision, enforce_render_contract
    released, contract = enforce_render_contract(TrustedRenderDecision.from_record(out), None)
    return {'output': {**out, **released, 'renderer_contract': contract,
                       'retrieved_memory': out['evidence']}}


async def test_sentences_only_model_output_has_no_synthetic_action(monkeypatch):
    import config
    import llm
    from nodes.medical_evaluator import _call_answerer
    class Client:
        async def chat(self, **kwargs):
            return SimpleNamespace(ok=True, text='{"sentences":[{"text":"Source fact.","claim_ids":["c"]}]}', usage={})
    monkeypatch.setattr(config, 'EVAL_TRANSPORT', 'http')
    monkeypatch.setattr(llm, 'get_llm_client', lambda: Client())
    parsed, _ = await _call_answerer({'action': 'answer'}, 'test', 'test')
    assert 'action' not in parsed
    assert parsed['action_origin'] == 'absent'
    from renderer_contract import TrustedRenderDecision, enforce_render_contract
    record = {'action': 'answer', 'answer_plan': {'schema_version': 2, 'claims': [{
        'claim_id': 'c', 'fact': 'Source fact.', 'required': True,
        'source_record_id': 'r', 'query_facets': ['source']}]},
        'evidence': [{'record_id': 'r', 'text': 'Source fact.', 'status': 'active'}]}
    out, result = enforce_render_contract(TrustedRenderDecision.from_record(record), parsed)
    assert out['action'] == 'answer' and not result['conflict']
    assert result['attempted_action_category'] is None
    assert not result['attempted_action_mismatch']


def test_cancelled_records_are_lifecycle_exclusions(memory):
    memory.ingest(turn('t2', 'The project review is cancelled.'))
    cp = checkpoint(tid='t2')
    out = memory.query(cp)
    assert not out['debug']['n_denied_scope'] and not out['debug']['n_denied_rbac']
    assert out['debug']['n_obsolete']
    assert out['action'] != 'answer_redacted'


def test_structured_heading_preserves_required_instruction_fragments(memory):
    memory.ingest(turn('t2', 'Deployment instructions:\n- Restart the cache;\n- Rotate the tokens;\n- Check the status.'))
    out = memory.query(checkpoint('What are the deployment instructions?', tid='t2'))
    required = [c['fact'] for c in out['answer_plan']['claims'] if c['required']]
    assert any('Rotate the tokens' in c for c in required)
    assert any('Check the status' in c for c in required)
    assert all('project review' not in c for c in required)


def test_generic_time_roles_preserve_location_and_time_facets():
    generic = Decision(query_terms=['appointment', 'time_expression', 'location_expression'], allowed=[
        Evidence('generic', 't', 'member', 'message', 'scoped', 'At 10:00 in room Cedar.',
                 context_terms=('appoint',))])
    unknown = _answer_plan(generic, 'What time and where is the appointment?')['claims'][0]
    assert next(v for v in unknown['critical_values'] if v['type'] == 'time')['role'] == 'time'
    assert {'time_expression', 'location_expression'} <= set(unknown['query_facets'])
    d = Decision(query_terms=['meeting', 'time_expression', 'location_expression'], allowed=[
        Evidence('r', 't', 'member', 'message', 'scoped', 'Meeting at 10:00 in room Cedar.')])
    plan = _answer_plan(d, 'What time and where is the meeting?')
    assert plan['claims'][0]['required']
    assert {'time_expression', 'location_expression'} <= set(plan['claims'][0]['query_facets'])
    d.allowed[0].body = 'Meeting arrival is 09:30 in room Cedar.'
    c = _answer_plan(d, 'What time and where is the meeting?')['claims'][0]
    assert c['required'] and 'location_expression' in c['query_facets']
    assert 'time_expression' not in c['query_facets']


def test_weak_overlap_is_insufficient_to_require_unrelated_claim():
    d = Decision(query_terms=['project', 'review', 'time_expression'], allowed=[
        Evidence('r', 't', 'member', 'message', 'scoped', 'Another project starts at 10:00.')])
    assert not _answer_plan(d, 'What time is the project review?')['claims'][0]['required']


def test_schema_normalization_preserves_named_checks():
    d = normalize_proposal({'safety_preservation_checks': {'privacy': 'Keep assignments', 'deletion': 'Keep tombstones'}})
    assert d['safety_preservation_checks'] == ['deletion: Keep tombstones', 'privacy: Keep assignments']
    bad = normalize_proposal({'safety_preservation_checks': {'privacy': True}})
    errors = shape_errors(bad)
    err = next(e for e in errors if e['field_path'] == '$.safety_preservation_checks')
    assert err['expected_type'] == 'array' and err['received_type'] == 'object'
    assert 'Return' in err['guidance']
    feedback = sanitized_failure_feedback({}, {'proposal_schema_errors': [err],
        'diagnosis_review': {'feedback': ['Cite a source relationship.']}})
    assert feedback['proposal_schema_errors'] == [err]
    assert feedback['diagnosis_review_feedback'] == ['Cite a source relationship.']


def test_selection_defect_precedes_renderer_telemetry():
    c = {'authorized_evidence_count': 1, 'action': 'answer', 'answer_plan_valid': False,
         'required_claim_count': 0, 'renderer_contract': {'attempted_action_mismatch': True,
             'reasons': ['malformed_renderer_output']}}
    report = {'targeted_replay': {'pairs': {'q': {'candidate': c}}}}
    assert classify_failure(report)[1] == 'FACT_SELECTION'
    c['answer_plan_valid'] = True
    c['required_claim_count'] = 1
    c['renderer_contract']['reasons'] = []
    assert classify_failure(report)[1] != 'RENDERING'


def test_audit_finds_permitted_history_beyond_retrieved_snippets(memory):
    cp = checkpoint()
    pred = prediction(memory)
    pred['output']['retrieved_memory'] = []
    pred['output']['evidence'] = []
    pred['output']['answer_plan']['claims'] = []
    audit = audit_history(blob(memory), header(), cp)
    packet = build_packet(pred, cp, audit)
    report = analyze_packet(packet)
    assert audit['records'] and packet['retrieved_record_ids'] == []
    assert any(i['failure_stage'] == 'RETRIEVAL' for i in report['issues'])
    assert any(i['issue_id'] == 'empty_required_plan' and
               i['verification_status'] == 'verified_defect' for i in report['issues'])


def test_critic_model_confidence_never_verifies_a_defect(memory):
    audit = audit_history(blob(memory), header(), checkpoint())
    p = build_packet(prediction(memory), checkpoint(), audit)
    report = analyze_packet(p, {'issues': [{'observed_issue': 'I am absolutely certain',
        'verification_status': 'verified_defect', 'confidence': 1.0,
        'evidence_references': [audit['records'][0]['record_id']], 'expected_action': 'hidden'}]})
    assert report['issues'][-1]['verification_status'] == 'suspected_defect'
    assert 'expected_action' not in report['issues'][-1]
    empty = analyze_packet(build_packet({'output': {'action': 'no_memory'}}, checkpoint(), {'records': []}),
                           {'issues': [{'verification_status': 'verified_defect'}]})
    assert empty['verification_status'] == 'suspected_defect' or empty['verification_status'] == 'insufficient_evidence'
    assert not empty['needs_repair']


def test_packet_excludes_hidden_future_denied_and_arbitrary_output(memory):
    memory.ingest(turn('t2', 'Future launch secret violet.', '2026-01-02T00:00:00Z'))
    cp = {**checkpoint(), 'expected_action': 'HIDDEN_GOLD', 'query_type': 'utility',
          'future_events': [{'text': 'FUTURE_EVENT'}], 'judge_spec': {'include': ['ANNOTATION']}}
    pred = prediction(memory)
    pred['output']['answer'] = 'DENIED_CONTENT ANNOTATION'
    pred['output']['renderer_contract'] = {'reason': 'ANNOTATION'}
    pred['output']['debug'] = {'raw': 'DENIED_CONTENT', 'future_events': 'FUTURE_EVENT'}
    p = build_packet(pred, cp, audit_history(blob(memory), header(), cp))
    rendered = json.dumps(p)
    assert all(s not in rendered for s in ('HIDDEN_GOLD', 'ANNOTATION', 'FUTURE_EVENT', 'violet', 'DENIED_CONTENT'))
    denied = audit_history(blob(memory), header(), checkpoint(requester='other'))
    assert not denied['records']


def test_deleted_content_is_not_available_even_to_owner_audit(memory):
    record_id = memory.query(checkpoint())['evidence'][0]['record_id']
    memory.store.tombstone(record_id, 'owner')
    audit = audit_history(blob(memory), header(), checkpoint(), claimed_record_ids=[record_id])
    assert 'Birch' not in json.dumps(audit)
    assert audit['protected_release_violations'] == 1
    p = build_packet({'output': {'action': 'no_memory'}}, checkpoint(), audit)
    assert analyze_packet(p)['needs_repair']


def test_irrelevant_update_and_equivalent_questions_preserve_facts(memory):
    initial = memory.query(checkpoint())['fallback_answer']
    memory.ingest(turn('t2', 'The garden grows purple flowers.'))
    after = memory.query(checkpoint(tid='t2'))['fallback_answer']
    equivalent = memory.query(checkpoint('What time does the project review start?', tid='t2'))['fallback_answer']
    assert initial == after == equivalent


def test_explicit_replacement_updates_current_view(memory):
    memory.ingest(turn('t2', 'The project review is moved to 16:00 in room Maple.'))
    answer = memory.query(checkpoint(tid='t2'))['fallback_answer']
    assert '16:00' in answer and '14:30' not in answer


class AnnotationFreeBackend(Backend):
    """Deterministic loop fixture; candidate improvement here is simulated."""
    def __init__(self):
        super().__init__(mock=True)
        self.architect_contexts = []
        self.developer_count = 0
        self.judge_count = 0
        self.predict_count = 0

    async def predict(self, *args, **kwargs):
        self.predict_count += 1
        return await super().predict(*args, **kwargs)

    async def memory(self, workspace, image, hdr, *, delta=None, checkpoint=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'memory.sqlite'
            if image:
                path.write_bytes(image)
            a = GateMemAgent(str(path))
            try:
                a.open_episode(hdr)
                for t in delta or []:
                    a.ingest(t)
                out = a.query(checkpoint) if checkpoint else {}
                if checkpoint and 'resolved_plan_probe' not in (Path(workspace) / 'memory_system/schema.sql').read_text():
                    for c in out['answer_plan']['claims']:
                        c['required'] = False
                return blob(a), out
            finally:
                a.store.close()

    async def judge(self, *args):
        self.judge_count += 1
        raise AssertionError('Annotations/Judge must never be required')

    async def architect(self, context, workspace):
        self.architect_contexts.append(copy.deepcopy(context))
        dossier = context['architect_incident_dossier']
        trace = dossier['targets'][0]['decision_trace'][0]
        return {'decision': 'CODE_OR_SCHEMA_CHANGE', 'failure_classification': 'FACT_SELECTION',
            'repair_component': 'memory', 'diagnosis': 'No supported required claims.',
            'specific_hypothesis': 'The required-claim probe changes the demonstrated empty-plan defect.',
            'focused_goal': 'Restore required coverage', 'work_order': ['Add the required plan probe index.'],
            'repair_scope': {'subsystem': 'selection', 'file': 'memory_system/schema.sql', 'function': 'query'},
            'migration_sql': 'CREATE INDEX IF NOT EXISTS resolved_plan_probe ON observations(episode_id,turn_id);',
            'trace_citations': [trace['trace_id']], 'addressed_decision_rule': trace['decision_rule'],
            'predicted_replay_change': {'observable': 'required_claims', 'expected_change': 'nonempty required plan'},
            'intended_target_ids': ['q'], 'safety_preservation_checks': {'privacy': 'Preserve assignment and consent',
                                                                        'deletion': 'Preserve tombstones'}}

    async def develop(self, workspace, design):
        self.developer_count += 1
        assert design['specific_hypothesis'] and design['safety_preservation_checks']
        assert 'critic_report' in design and 'relevant_experiments' in design
        return await super().develop(workspace, design)


def dataset():
    ep = {**header(), 'turns': [turn('t1', 'The project review starts at 14:30 in room Birch.')]}
    return MedicalDataset([ep], [checkpoint()])


async def test_annotation_free_loop_promotes_demonstrated_defect_and_preserves_first_attempt(tmp_path):
    backend = AnnotationFreeBackend()
    runner = OnlineRunner(dataset(), tmp_path / 'run', backend=backend,
                          options=OnlineOptions(adaptation_mode='evidence_based'))
    try:
        result = await runner.run(max_boundaries=1)
        assert backend.judge_count == 0 and backend.developer_count == 1
        assert runner.committed != 'version_0001'
        first = runner.journal.official()[0]
        assert not first['score']['passed']
        assert not any(c['required'] for c in first['prediction']['output']['answer_plan']['claims'])
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            runner.journal.db.execute("UPDATE official SET prediction='{}'")
        runner.journal.db.rollback()
        lessons = runner.journal.get('experiment_memory')
        assert lessons[-1]['conclusion'] == 'promoted'
        assert lessons[-1]['hypothesis'] and lessons[-1]['implementation_change']
        manifest = runner.journal.get('manifest')
        assert manifest['evaluation_mode'] == 'evidence_invariants'
        assert result['scoreboard']['MGS'] is None
        assert result['scoreboard']['truth_available'] is False
    finally:
        runner.close()


async def test_annotation_free_disabled_adaptation_records_unresolved_defect(tmp_path):
    backend = AnnotationFreeBackend()
    runner = OnlineRunner(dataset(), tmp_path / 'disabled', backend=backend,
                          options=OnlineOptions(adaptation_mode='evidence_based', disable_redesign=True))
    try:
        await runner.run(max_boundaries=1)
        assert backend.developer_count == 0 and backend.judge_count == 0
        assert not runner.journal.official()[0]['score']['passed']
        assert runner.journal.get('pending_repairs')
    finally:
        runner.close()


def test_candidate_cannot_remove_authoritative_assignment_requirement(tmp_path):
    baseline, candidate = tmp_path / 'baseline', tmp_path / 'candidate'
    copy_workspace('templates', baseline)
    copy_workspace('templates', candidate)
    policy = candidate / 'memory_system/policies/medical.json'
    value = json.loads(policy.read_text())
    del value['rules'][0]['relationship']
    policy.write_text(json.dumps(value))
    with pytest.raises(ValidationFailure, match='authorization specifications are protected'):
        build_gates(candidate, baseline / 'tests')


async def test_schema_correction_returns_precise_errors_without_rebuilding_hypothesis(tmp_path):
    class Corrected(AnnotationFreeBackend):
        async def architect(self, context, workspace):
            design = await super().architect(context, workspace)
            if len(self.architect_contexts) == 1:
                design['safety_preservation_checks'] = {'privacy': True}
            else:
                assert context['schema_correction_only']
                error = next(e for e in context['proposal_schema_errors']
                             if e['field_path'] == '$.safety_preservation_checks')
                assert error['expected_type'] == 'array' and error['received_type'] == 'object'
                assert error['guidance']
            return design
    backend = Corrected()
    runner = OnlineRunner(dataset(), tmp_path / 'corrected', backend=backend,
                          options=OnlineOptions(adaptation_mode='evidence_based'))
    try:
        await runner.run(1)
        assert backend.developer_count == 1 and len(backend.architect_contexts) == 2
        assert runner.journal.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 1
    finally:
        runner.close()


async def test_equivalent_failed_proposals_explain_prior_result_and_skip_developer(tmp_path):
    class Repeated(AnnotationFreeBackend):
        async def architect(self, context, workspace):
            if len(self.architect_contexts) == 2:
                feedback = context['latest_complete_structured_feedback']
                assert feedback['rejection_reason'] == 'equivalent_previous_attempt'
                assert 'measured result=' in ' '.join(feedback['diagnosis_review_feedback'])
                return {'decision': 'ESCALATE', 'failure_classification': 'ESCALATE',
                        'diagnosis': 'The discriminating probe found no change.'}
            design = await super().architect(context, workspace)
            name = 'inert_probe_first' if len(self.architect_contexts) == 1 else 'inert_probe_renamed'
            design['migration_sql'] = f'CREATE INDEX IF NOT EXISTS {name} ON observations(episode_id,turn_id);'
            return design
    backend = Repeated()
    runner = OnlineRunner(dataset(), tmp_path / 'repeated', backend=backend,
                          options=OnlineOptions(adaptation_mode='evidence_based'))
    try:
        await runner.run(1)
        assert backend.developer_count == 1
        assert backend.predict_count == 1  # only the immutable first attempt
        attempts = [json.loads(r[0]) for r in runner.journal.db.execute('SELECT value FROM attempts ORDER BY idx')]
        assert attempts[0]['cheap_deterministic_probe']['rendering_and_judging_used'] is False
        assert attempts[1]['developer_skipped']
    finally:
        runner.close()


def test_evidence_promotion_rejects_regression_of_established_content(tmp_path):
    runner = OnlineRunner(dataset(), tmp_path / 'gates', backend=Backend(mock=True),
                          options=OnlineOptions(adaptation_mode='evidence_based'))
    try:
        pair = {'transition': 'unchanged_pass', 'answer_changed': True,
                'parent': {'passed': True, 'vote_summary': {'valid': True}},
                'candidate': {'passed': True, 'vote_summary': {'valid': True}}}
        gate = runner._broad_result({'pairs': {'q': pair}})
        assert gate['status'] == 'failed'
        pair['answer_changed'] = False
        assert runner._broad_result({'pairs': {'q': pair}})['status'] == 'passed'
    finally:
        runner.close()


async def test_independent_feedback_is_scoped_temporal_and_does_not_rewrite_history(tmp_path):
    runner = OnlineRunner(dataset(), tmp_path / 'feedback', backend=AnnotationFreeBackend(),
                          options=OnlineOptions(adaptation_mode='evidence_based', disable_redesign=True))
    try:
        await runner.run(1)
        initial = runner.journal.official()
        with pytest.raises(ValueError, match='scope'):
            runner.record_feedback('q', principal_id='other', kind='user', observed_success=False,
                                   available_at_boundary=1, feedback_id='f')
        with pytest.raises(ValueError, match='future'):
            runner.record_feedback('q', principal_id='owner', kind='outcome', observed_success=False,
                                   available_at_boundary=3, feedback_id='f')
        runner.record_feedback('q', principal_id='owner', kind='outcome', observed_success=False,
                               available_at_boundary=1, feedback_id='f')
        assert runner.journal.official() == initial
        _, _, image = runner.journal.event(1)
        packet = runner._packet(initial[0]['prediction'], checkpoint(), image, header())
        issue = next(i for i in analyze_packet(packet)['issues']
                     if i['observed_issue'] == 'independent_negative_feedback')
        assert issue['verification_status'] == 'suspected_defect'
    finally:
        runner.close()


def test_experiment_lessons_are_revalidated_after_deletion_and_isolated(memory, tmp_path):
    runner = OnlineRunner(dataset(), tmp_path / 'lessons', backend=Backend(mock=True),
                          options=OnlineOptions(adaptation_mode='evidence_based'))
    try:
        rid = memory.query(checkpoint())['evidence'][0]['record_id']
        with runner.journal.db:
            runner.journal.put('experiment_memory', [
                {'experiment_id': '1:1', 'boundary_index': 1, 'scope': ['e', 'owner'],
                 'visibility_record_ids': [rid], 'hypothesis': 'A permitted observation informed this.'},
                {'experiment_id': '1:2', 'boundary_index': 1, 'scope': ['e', 'other'],
                 'visibility_record_ids': [], 'hypothesis': 'OTHER_USER'}])
        p = build_packet(prediction(memory), checkpoint(), audit_history(blob(memory), header(), checkpoint()))
        assert len(runner._experiment_lessons(1, packet=p)) == 1
        memory.store.tombstone(rid, 'owner')
        p = build_packet({'output': {'action': 'no_memory'}}, checkpoint(),
                         audit_history(blob(memory), header(), checkpoint()))
        assert runner._experiment_lessons(1, packet=p) == []
    finally:
        runner.close()


def test_unvalidated_derived_body_cannot_be_exported_as_permitted_source(memory):
    source = memory.store.conn.execute("SELECT observation_id FROM observations WHERE turn_id='t1'").fetchone()[0]
    memory.store.add_fact('e', subject_id='owner', predicate='review', value={},
                         text_value='PRIVATE_BODY_FROM_ELSEWHERE', source_observation_id=source)
    audit = audit_history(blob(memory), header(), checkpoint())
    assert 'PRIVATE_BODY_FROM_ELSEWHERE' not in json.dumps(audit)
    assert audit['excluded_counts']['unvalidated_source_relationship'] == 1
    packet = build_packet(prediction(memory), checkpoint(), audit)
    assert not packet['assessment_complete']
    assert any(i['observed_issue'] == 'source_relationship_unvalidated' for i in analyze_packet(packet)['issues'])


def test_truncated_audit_does_not_certify_promotion_or_assert_omission_defects(memory, tmp_path):
    cp = checkpoint()
    audit = audit_history(blob(memory), header(), cp)
    audit['records'] = []
    audit['complete'] = False
    packet = build_packet(prediction(memory), cp, audit)
    report = analyze_packet(packet)
    assert not report['needs_repair']
    assert not packet['assessment_complete']
    runner = OnlineRunner(dataset(), tmp_path / 'incomplete', backend=Backend(mock=True),
                          options=OnlineOptions(adaptation_mode='evidence_based'))
    try:
        pair = {'transition': 'unchanged_pass', 'answer_changed': False,
                'parent': {'passed': True, 'assessment_complete': True, 'vote_summary': {'valid': True}},
                'candidate': {'passed': True, 'assessment_complete': False, 'vote_summary': {'valid': True}}}
        assert runner._broad_result({'pairs': {'q': pair}})['status'] == 'failed'
    finally:
        runner.close()


async def test_silence_cannot_resolve_an_empty_required_plan(tmp_path):
    class Silent(AnnotationFreeBackend):
        async def memory(self, workspace, image, hdr, *, delta=None, checkpoint=None):
            image, out = await super().memory(workspace, image, hdr, delta=delta, checkpoint=checkpoint)
            if checkpoint and 'resolved_plan_probe' in (Path(workspace) / 'memory_system/schema.sql').read_text():
                out['action'] = 'no_memory'
                out['answer_plan']['claims'] = []
            return image, out
        async def architect(self, context, workspace):
            if self.architect_contexts:
                return {'decision': 'ESCALATE', 'failure_classification': 'ESCALATE',
                        'diagnosis': 'Silence did not demonstrate coverage restoration.'}
            return await super().architect(context, workspace)
    backend = Silent()
    runner = OnlineRunner(dataset(), tmp_path / 'silent', backend=backend,
                          options=OnlineOptions(adaptation_mode='evidence_based'))
    try:
        await runner.run(1)
        assert runner.committed == 'version_0001'
        attempts = [json.loads(r[0]) for r in runner.journal.db.execute('SELECT value FROM attempts ORDER BY idx')]
        assert attempts[0]['targeted_replay']['status'] == 'no_improvement'
    finally:
        runner.close()
