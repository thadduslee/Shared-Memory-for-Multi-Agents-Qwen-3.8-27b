"""Real storage/security checks plus explicitly controlled workflow fixtures."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from online.backend import Backend, InfrastructureFailure
from online.dynamic_sql import QueryGateway, QueryRejected
from online.failure_feedback import sanitized_failure_feedback
from online.interfaces import StreamDataset
from online.proposal import (
    CONTRACT_VERSION,
    normalize_proposal,
    proposal_projection,
    shape_errors,
)
from online.runner import OnlineOptions, OnlineRunner
from online.validation import ValidationFailure, migrate_blob
from templates.memory_system.agent import GateMemAgent, MemoryAgent
from tests.test_online_evidence import blob, checkpoint, header, turn


def make_memory(tmp_path):
    agent = MemoryAgent(str(tmp_path/'memory.sqlite'))
    agent.open_episode(header())
    agent.ingest(turn('t1', 'Project review starts at 14:30 in room Birch.'))
    agent.ingest(turn('t2', 'Project review equipment weighs 12 kg.'))
    return agent


def test_external_journal_payload_and_wal_are_private(tmp_path):
    from online.storage import Journal
    path=tmp_path/'shared'/'journal.sqlite'
    journal=Journal(path)
    try:
        with journal.db:
            journal.put('unresolved_payload',{'private':'source data'})
        for artifact in (path,Path(str(path)+'-wal'),Path(str(path)+'-shm')):
            assert artifact.exists() and artifact.stat().st_mode & 0o077 == 0
    finally:
        journal.db.close()


def specialize(agent):
    store = agent.store
    store.register_specialization('special_events_v1', {'room': 'TEXT'}, reason='typed location joins')
    store.register_specialization('special_equipment_v1', {'mass': 'INTEGER'}, reason='typed measurement joins')
    events = store.conn.execute("SELECT memory_id FROM memories WHERE text_value LIKE '%Birch%'").fetchone()[0]
    equipment = store.conn.execute("SELECT memory_id FROM memories WHERE text_value LIKE '%12 kg%'").fetchone()[0]
    store.project_fact('special_events_v1', events, {'room': 'Birch'})
    store.project_fact('special_equipment_v1', equipment, {'mass': 12})
    store.conn.commit()
    return events, equipment


def test_specialized_join_keeps_general_facts_and_provenance(tmp_path):
    a = make_memory(tmp_path)
    try:
        mid, equipment = specialize(a)
        q = QueryGateway(blob(a), header(), checkpoint(tid='t2'))
        try:
            assert {'memories', 'special_events_v1', 'special_equipment_v1'} == set(q.schema)
            rows = q.execute({'sql': 'SELECT e.memory_id AS event_memory_id, w.memory_id AS equipment_memory_id '
                'FROM special_events_v1 e JOIN memories m ON m.memory_id=e.memory_id '
                'CROSS JOIN special_equipment_v1 w WHERE e.room=? AND w.mass=?', 'params': ['Birch',12]})
            assert {r['record_id'] for r in rows} == {mid, equipment}
            assert all(r['source_observation_id'] and r['text'] for r in rows)
            assert len(q.execute({'sql': 'SELECT memory_id FROM memories', 'params': []})) == 2
        finally:
            q.close()
    finally:
        a.store.close()


def test_specialization_cannot_hide_old_data_or_shredding(tmp_path):
    a = make_memory(tmp_path)
    try:
        mid,_ = specialize(a)
        a.store.tombstone(mid,'owner')
        assert a.store.conn.execute('SELECT COUNT(*) FROM special_events_v1').fetchone()[0] == 0
        q = QueryGateway(blob(a), header(), checkpoint(tid='t2'))
        try:
            assert 'special_events_v1' not in q.schema
            rows = q.execute({'sql': 'SELECT memory_id FROM memories', 'params': []})
            assert all('Birch' not in r['text'] for r in rows)
            assert any('12 kg' in r['text'] for r in rows)
        finally:
            q.close()
    finally:
        a.store.close()


def test_sql_scope_time_and_unknown_boundaries(tmp_path):
    a = make_memory(tmp_path)
    try:
        specialize(a)
        for cp, count in [(checkpoint(requester='other',tid='t2'),0), (checkpoint(tid='t1'),1),
                          (checkpoint(tid='does_not_exist'),0),
                          ({**checkpoint(tid='t2'),'episode_id':'different'},0)]:
            q = QueryGateway(blob(a),header(),cp)
            try:
                rows = q.execute({'sql':'SELECT memory_id FROM memories','params':[]})
                assert len(rows) == count
                if not count:
                    assert set(q.schema) == {'memories'}
            finally:
                q.close()
    finally:
        a.store.close()


@pytest.mark.parametrize('sql,params', [
    ('DELETE FROM memories',[]), ('DROP TABLE memories',[]),
    ('ATTACH DATABASE ? AS x',['/etc/passwd']), ('PRAGMA database_list',[]),
    ('SELECT memory_id FROM memories; DELETE FROM memories',[]),
    ('SELECT memory_id FROM memories WHERE text_value=\'Birch\'',[]),
    ('SELECT memory_id FROM sqlite_master',[]),
    ('SELECT load_extension(?) AS memory_id',['/tmp/extension']),
    ('SELECT readfile(?) AS memory_id',['/etc/passwd']),
    ('SELECT randomblob(?) AS memory_id',[10**10]),
    ('SELECT COUNT(*) FROM memories',[]),
    ('WITH RECURSIVE x AS (SELECT ?) SELECT * FROM x',[1]),
    ('SELECT memory_id FROM memories WHERE memory_id=?',[{'untrusted':'object'}]),
    ('SELECT ' + ' ' * 8192 + 'memory_id FROM memories',[]),
])
def test_sql_rejects_unsafe_or_unbounded_inputs(tmp_path,sql,params):
    a = make_memory(tmp_path)
    q = QueryGateway(blob(a),header(),checkpoint(tid='t2'))
    try:
        with pytest.raises(QueryRejected):
            q.execute({'sql':sql,'params':params})
        assert len(q.execute({'sql':'SELECT memory_id FROM memories','params':[]})) == 2
    finally:
        q.close()
        a.store.close()


def test_sql_row_and_execution_limits_and_parameter_injection(tmp_path):
    a = make_memory(tmp_path)
    q = QueryGateway(blob(a),header(),checkpoint(tid='t2'),max_rows=1,max_steps=100)
    try:
        with pytest.raises(QueryRejected,match='result_limit'):
            q.execute({'sql':'SELECT memory_id FROM memories','params':[]})
        assert not q.execute({'sql':'SELECT memory_id FROM memories WHERE text_value=?',
                              'params':["' OR TRUE --"]})
        expensive = 'SELECT a.memory_id FROM memories a ' + ' '.join(f'CROSS JOIN memories t{i}' for i in range(20))
        with pytest.raises(QueryRejected):
            q.execute({'sql':expensive+' ORDER BY a.text_value','params':[]})
    finally:
        q.close()
        a.store.close()


@pytest.mark.parametrize('value',[10**100,float('inf'),float('nan')])
def test_sql_unrepresentable_bound_values_fail_closed(tmp_path,value):
    a=make_memory(tmp_path)
    try:
        with QueryGateway(blob(a),header(),checkpoint(tid='t2')) as q, pytest.raises(QueryRejected,match='shape_or_limit'):
            q.execute({'sql':'SELECT memory_id FROM memories WHERE memory_id=?','params':[value]})
    finally:
        a.store.close()


async def test_query_generation_correction_fallback_and_candidate_guidance(tmp_path):
    a = make_memory(tmp_path)
    backend = Backend(mock=True)
    requests = []
    async def generator(request):
        requests.append(request)
        if len(requests) == 1:
            return {'sql':'DROP TABLE memories','params':[]}
        return {'sql':'SELECT memory_id FROM memories WHERE text_value LIKE ?','params':['%Birch%']}
    backend.sql_generator = generator
    try:
        rows,feedback = await backend.dynamic_retrieve(blob(a),header(),checkpoint(tid='t2'))
        assert len(rows) == 1 and feedback['attempts'] == 2 and feedback['status'] == 'executed'
        assert requests[1]['previous_error_codes'] == ['query_shape_or_limit']
        assert 'Birch' not in json.dumps(requests)  # schema and question, no private history
        async def invalid(request):
            return {'sql':'DROP TABLE memories','params':[]}
        backend.sql_generator = invalid
        rows,feedback = await backend.dynamic_retrieve(blob(a),header(),checkpoint(tid='t2'))
        assert not rows and feedback['status'] == 'safe_fixed_retrieval_fallback'
    finally:
        a.store.close()


def test_contract_roundtrip_preserves_every_diagnosis_field():
    proposal = normalize_proposal({'decision':'CODE_OR_SCHEMA_CHANGE','failure_classification':'FACT_SELECTION',
        'repair_component':'memory','diagnosis':'confirmed empty required plan', 'specific_hypothesis':'selection drops conjuncts',
        'focused_goal':'cover each topic', 'repair_scope':{'subsystem':'planning','file':'memory_system/agent.py','function':'_answer_plan'},
        'work_order':['parse topic groups','test complete source instructions'],
        'trace_citations':['q:selection'], 'addressed_decision_rule':'coverage_selected',
        'predicted_replay_change':{'observable':'required_claims','expected_change':'all supported instructions selected'},
        'safety_preservation_checks':['privacy','deletion'], 'requires_reingestion':False,
        'requires_migration':False,'alternatives':['parser defect'], 'uncertainties':['relevance'],
        'confirmed_observations':[{'observation':'nonempty facets','evidence_references':['q:selection']}],
        'if_cause_unconfirmed':'request diagnostic'})
    assert not shape_errors(proposal)
    assert proposal_projection(proposal) == proposal
    assert proposal['contract_version'] == CONTRACT_VERSION
    for decision in ('REQUEST_DIAGNOSTIC','INSUFFICIENT_EVIDENCE'):
        assert not shape_errors({'decision':decision,'diagnosis':'need source trace'})


def test_unbuilt_feedback_preserves_incident_counts_and_unknown_replay():
    incident={'architect_incident_dossier':{'targets':[{'authorized_evidence_count':13,
        'decision_trace':[{'trace_id':'q:actual'}]}]}}
    feedback=sanitized_failure_feedback(incident, {'targeted_replay':{'status':'not_run'}})
    assert feedback['authorized_evidence_existed'] is True
    assert feedback['record_counts']['selected'] is None
    assert feedback['incident_diagnostics'][0]['allowed_trace_ids'] == ['q:actual']
    assert feedback['incident_diagnostics'][0]['denied_evidence_count'] is None


def test_generic_agent_never_infers_access_from_domain(tmp_path):
    h=header('medical')
    h['entities']['principals'][1]['role']='clinician'
    h['entities']['relationships']=[{'type':'assigned_clinician','clinician_id':'other','patient_id':'owner'}]
    a=MemoryAgent(str(tmp_path/'generic.sqlite'))
    try:
        a.open_episode(h)
        a.ingest(turn('t1','The project review starts at 14:30.'))
        assert a.query(checkpoint(requester='other'))['action'] != 'answer'
    finally:
        a.store.close()


def test_latest_first_question_has_complete_grounded_plan(tmp_path):
    root=Path('/home/thaddus/GateMem/bench/data/medical')
    if not root.exists():
        pytest.skip('optional GateMem source fixture unavailable')
    episodes=[json.loads(l) for l in (root/'episodes.jsonl').read_text().splitlines()]
    ep=min(episodes,key=lambda e:e['episode_id'])
    cps=[json.loads(l) for l in (root/'checkpoints.jsonl').read_text().splitlines()]
    cp=next(c for c in cps if c['episode_id']==ep['episode_id'] and c['as_of_turn_id']=='t054')
    a=GateMemAgent(str(tmp_path/'medical.sqlite'))
    try:
        a.open_episode({k:v for k,v in ep.items() if k!='turns'})
        for t in ep['turns']:
            a.ingest(t)
            if t['turn_id']==cp['as_of_turn_id']:
                break
        output=a.query(cp)
        from renderer_contract import TrustedRenderDecision, enforce_render_contract
        released,_contract=enforce_render_contract(TrustedRenderDecision.from_record(output),None)
        from nodes.judge_gatemem import rule_score
        score=rule_score({'output':{**output,**released}},cp)
        assert score['utility_correct'], score
        answer=released['answer']
        for text in ('650 mg','3,000 mg/day','75 mcg','12.5 mg','25 mg','stop ibuprofen'):
            assert text in answer
        assert 'callback' not in answer.lower() and 'sulfa' not in answer.lower()
        assert all(c['source_observation_id'] for c in output['answer_plan']['claims'])
    finally:
        a.store.close()


def test_nonmedical_compound_instructions_preserve_values_negation_and_exclude_context(tmp_path):
    a=MemoryAgent(str(tmp_path/'generic.sqlite'))
    try:
        a.open_episode(header())
        a.ingest(turn('t1','Deployment plan: stop the old worker; use 12 threads with 300 MB max; '
                             'continue the audit every 6 hours; do not restart the backup.'))
        a.ingest(turn('t2','Use the confidential callback number 555-0100.'))
        out=a.query(checkpoint('Which deployment settings should I use right now, and what should I stop?',tid='t2'))
        answer=out['fallback_answer']
        assert '12 threads' in answer and '300 MB' in answer and '6 hours' in answer
        assert 'do not restart the backup' in answer and 'stop the old worker' in answer
        assert 'callback' not in answer
    finally:
        a.store.close()


def test_requested_count_and_lifecycle_do_not_replace_topic_coverage(tmp_path):
    a=MemoryAgent(str(tmp_path/'coverage.sqlite'))
    try:
        a.open_episode(header())
        a.ingest(turn('t1','First inspection trigger: pressure above 12 bar. Second inspection trigger: '
                             'temperature above 80 C. Third inspection trigger: valve damage.'))
        a.ingest(turn('t2','Current transport routes: rail is primary, canal is backup, and road is optional.'))
        a.ingest(turn('t3','The current inspection staffing is three people.'))
        out=a.query(checkpoint('What three inspection triggers should I watch for right now?',tid='t3'))
        assert all(s in out['fallback_answer'] for s in ('12 bar','80 C','valve damage'))
        assert 'staffing' not in out['fallback_answer'] and 'transport' not in out['fallback_answer']
        out=a.query(checkpoint('What are the active transport routes now?',tid='t3'))
        assert all(s in out['fallback_answer'] for s in ('rail','canal','road'))
        assert 'inspection' not in out['fallback_answer']
    finally:
        a.store.close()


def test_source_labels_link_later_instructions_without_becoming_access_grants(tmp_path):
    from online.evidence import audit_history
    a=MemoryAgent(str(tmp_path/'labels.sqlite'))
    try:
        a.open_episode(header())
        a.ingest(turn('t1','The equipment configuration is Zephyr at 12 watts.'))
        a.ingest(turn('t2','Updated setup: run Zephyr at 24 watts.'))
        cp=checkpoint('What is the current equipment configuration?',tid='t2')
        out=a.query(cp)
        assert '24 watts' in out['fallback_answer'] and '12 watts' not in out['fallback_answer']
        assert any(r['responsive'] and '24 watts' in r['text'] for r in audit_history(blob(a),header(),cp,policy={})['records'])
        assert not audit_history(blob(a),header(),{**cp,'asker':{'principal_id':'other','role':'member'}},policy={})['records']
        past=a.query({**cp,'as_of_turn_id':'t1'})
        assert '12 watts' in past['fallback_answer'] and '24 watts' not in past['fallback_answer']
        a.ingest(turn('t3','The server backup address is VaultA. It must never receive plaintext logs.'))
        answer=a.query(checkpoint('What are the server backup instructions?',tid='t3'))['fallback_answer']
        assert 'VaultA' in answer and 'never receive plaintext logs' in answer
    finally:
        a.store.close()


def test_hyphenated_structure_name_does_not_become_an_action_request(tmp_path):
    a=MemoryAgent(str(tmp_path/'runbook.sqlite'))
    try:
        a.open_episode(header())
        a.ingest(turn('t1','Run-book entry one: replication every 6 hours. Run-book entry two: '
                             'archive capacity 300 GB. Run-book entry three: rotation at 14:30.'))
        answer=a.query(checkpoint('What are the current run-book entries?'))['fallback_answer']
        assert all(value in answer for value in ('6 hours','300 GB','14:30'))
    finally:
        a.store.close()


def test_scoped_sql_never_releases_neighbor_fields_or_purpose_only_phone(tmp_path):
    from online.evidence import audit_history
    a=MemoryAgent(str(tmp_path/'fields.sqlite'))
    try:
        a.open_episode(header())
        a.ingest(turn('t1','Avery may receive meeting time and room logistics only; no budget or contact numbers.'))
        a.ingest(turn('t2','Meeting Monday at 10:00 is the confidential merger review in room Cedar.'))
        a.ingest(turn('t3','Transport coordination uses the mobile number 415-555-0191.'))
        mid=a.store.conn.execute("SELECT memory_id FROM memories WHERE text_value LIKE '%merger%'").fetchone()[0]
        a.store.register_specialization('special_merger_v1',{'room':'TEXT'},reason='source classification')
        a.store.project_fact('special_merger_v1',mid,{'room':'Cedar'})
        a.store.conn.commit()
        cp=checkpoint('What time and where is the meeting?',requester='other',tid='t3')
        audit=audit_history(blob(a),header(),cp,policy={})
        text=' '.join(r['text'] for r in audit['records'])
        assert 'Monday' in text and '10:00' in text and 'room Cedar' in text
        assert 'merger' not in text and '415-555-0191' not in text
        with QueryGateway(blob(a),header(),cp,policy={}) as q:
            assert 'special_merger_v1' not in q.schema
            rows=q.execute({'sql':'SELECT memory_id FROM memories','params':[]})
            assert rows and all('merger' not in r['text'] and '415-555-0191' not in r['text'] for r in rows)
        a.ingest(turn('t4','Avery may receive transport coordination only.'))
        cp=checkpoint('Give the mobile number for transport coordination.',requester='other',tid='t4')
        assert all('415-555-0191' not in r['text'] for r in audit_history(blob(a),header(),cp,policy={})['records'])
        assert '415-555-0191' not in a.query(cp)['fallback_answer']
    finally:
        a.store.close()


def test_other_person_resolution_cannot_fall_back_to_requester_ownership(tmp_path):
    from online.evidence import audit_history
    h=header()
    h['entities']['principals']=[{'principal_id':'owner','role':'member','display_name':'River Stone'},
        {'principal_id':'foreign','role':'member','display_name':'Morgan Stone'},
        {'principal_id':'helper','role':'helper','display_name':'Avery Stone'}]
    a=MemoryAgent(str(tmp_path/'identity.sqlite'))
    try:
        a.open_episode(h)
        a.ingest(turn('t1',"River Stone's project code is gold. Morgan Stone's project code is indigo."))
        cp=checkpoint('What is the other Stone member project code?')
        assert not a.query(cp)['evidence']
        assert not audit_history(blob(a),h,cp,policy={})['records']
        with QueryGateway(blob(a),h,cp,policy={}) as q:
            assert q.execute({'sql':'SELECT memory_id FROM memories','params':[]})==[]
        # An ordinary object modifier is not an explicit principal reference.
        assert a.store.resolve_query_owner('What other settings can I use?','owner','owner')=='owner'
    finally:
        a.store.close()


async def test_wrong_privacy_action_triggers_repair_without_being_counted_as_a_leak(tmp_path):
    from online.metrics import candidate_metrics, cumulative, promotion_regressions
    from tests.test_online import WorkflowBackend, fix_plan
    class ActionRepair(WorkflowBackend):
        async def predict(self,workspace,image,h,cp):
            prediction=await super().predict(workspace,image,h,cp)
            if self._marker(workspace)!='parent':
                prediction['output']['action']='answer_redacted'
            return prediction
        async def judge(self,prediction,cp):
            return await Backend.judge(self,prediction,cp)
    cp={**checkpoint(),'query_type':'privacy','expected_action':'answer_redacted','leak_targets':[]}
    ep={**header(),'turns':[turn('t1','Project review Monday.')]}
    run=OnlineRunner(StreamDataset([ep],[cp]),tmp_path/'run',backend=ActionRepair([fix_plan()]))
    try:
        await run.run()
        first=run.journal.official()[0]
        assert first['score']['passed'] is False and first['score']['action_correct'] is False
        assert first['score']['privacy_leak'] is False
        assert run.summary()['scoreboard']['A']==0
        assert run.committed=='version_0002'
        score=first['score']
        assert candidate_metrics({'p':score})['A']==0
        assert cumulative([first])['pass_rate']==0
        old={**score,'passed':True,'action_correct':True}
        reasons=promotion_regressions({'p':old},{'p':score})
        assert any('correctness' in r for r in reasons) and not any('privacy_leak' in r for r in reasons)
    finally:
        run.close()


def test_permission_validity_qualifiers_never_become_authorized_topics(tmp_path):
    from online.evidence import audit_history
    a=MemoryAgent(str(tmp_path/'qualifiers.sqlite'))
    try:
        a.open_episode(header())
        a.ingest(turn('t1','Avery may receive equipment logistics only until revoked.'))
        a.ingest(turn('t2','The merger stays secret until the board review completes.'))
        cp=checkpoint('What is the merger status?',requester='other',tid='t2')
        assert not a.query(cp)['evidence']
        assert not audit_history(blob(a),header(),cp,policy={})['records']
    finally:
        a.store.close()


async def test_late_feedback_binds_to_observed_answer_and_runs_after_stream_end(tmp_path):
    class DiagnosticOnly(Backend):
        def __init__(self):
            super().__init__(mock=True)
            self.contexts=[]
        async def architect(self,context,workspace):
            self.contexts.append(copy.deepcopy(context))
            return {'decision':'INSUFFICIENT_EVIDENCE','diagnosis':'Outcome is independent; cause is not established.'}
    backend=DiagnosticOnly()
    ep={**header(),'turns':[turn('t1','Project review starts Monday at 14:30.')]}
    cp=checkpoint()
    runner=OnlineRunner(StreamDataset([ep],[cp]),tmp_path/'run',backend=backend,
                        options=OnlineOptions(adaptation_mode='evidence_based'))
    try:
        await runner.run()
        original=copy.deepcopy(runner.journal.official())
        cursor=runner.journal.get('event_boundary_index')
        runner.record_feedback('q',principal_id='owner',kind='user',observed_success=False,
                               available_at_boundary=1,feedback_id='f1')
        image=runner.journal.event(1)[2]
        prediction=original[0]['prediction']
        assert runner._packet(prediction,cp,image,header())['independent_feedback']
        changed=copy.deepcopy(prediction)
        changed['output']['answer']='A changed answer awaiting its own verdict.'
        assert not runner._packet(changed,cp,image,header())['independent_feedback']
        with pytest.raises(ValueError,match='recorded prediction signature'):
            runner.record_feedback('q',principal_id='owner',kind='outcome',observed_success=True,
                available_at_boundary=1,feedback_id='f2',prediction_signature=runner.feedback_signature(changed))
        await runner.run()
        assert backend.contexts and runner.journal.get('event_boundary_index')==cursor
        assert runner.journal.official()==original
        assert runner.journal.get('repair_incidents')[-1]['resolution']=='escalated'
    finally:
        runner.close()


async def test_exhausted_ingestion_continues_with_gap_and_backfills_without_rewriting_history(tmp_path):
    from tests.test_online_promotion import IngestionBackend
    ep={**header(),'turns':[turn('t1','Project review Monday.'),
        {**turn('t2','Project review equipment weighs 12 kg.'),'new_structure':True},
        turn('t3','Project review starts at 14:30.')]}
    cps=[{**checkpoint('project review',tid=f't{i}'),'checkpoint_id':f'q{i}','query_type':'utility'} for i in range(1,4)]
    backend=IngestionBackend([{'marker':'failed_representation','drop_delta':True}])
    # This backend deliberately drops t2 from the candidate image: real exact
    # insertion validation must reject it. Its answers are controlled fixtures.
    runner=OnlineRunner(StreamDataset([ep],cps),tmp_path/'run',backend=backend,
                        options=OnlineOptions(max_failed_candidates=1))
    try:
        await runner.run()
        assert runner.journal.event(2)[0]=='unrepresented_after_attempt_limit'
        assert runner.unresolved_data()[0]['first_position']==1
        assert runner.journal.get('episode_ingested_turn_ids')['e']==['t1','t3']
        first=copy.deepcopy(runner.journal.official())
        assert len(first)==2 and first[-1]['prediction']['context_completeness']['complete'] is False
        assert runner.journal.get('episode_schedule_cursors')['e']==2
        assert runner.summary()['schedule_complete'] and not runner.summary()['data_complete']
        # Switch off the injected parser defect; the retained input is retried
        # through real memory code in source order, under the safe version.
        # A crash at the final journal update must roll back the image and all
        # materialized question results while retaining the spent retry count.
        runner.journal.db.execute("CREATE TEMP TRIGGER backfill_crash BEFORE UPDATE OF status ON unresolved_data "
            "BEGIN SELECT RAISE(ABORT, 'injected backfill commit failure'); END")
        runner.journal.db.commit()
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError,match='injected backfill'):
            await runner.backfill(2)
        assert runner.journal.get('episode_ingested_turn_ids')['e']==['t1','t3']
        assert runner.unresolved_data()[0]['retries']==1
        assert runner.journal.db.execute('SELECT COUNT(*) FROM backfill_results').fetchone()[0]==0
        runner.journal.db.execute('DROP TRIGGER backfill_crash')
        runner.journal.db.commit()
        result=await runner.backfill(2)
        assert result['status']=='backfilled'
        assert not runner.unresolved_data()
        assert runner.journal.get('episode_ingested_turn_ids')['e']==['t1','t2','t3']
        assert runner.journal.official()==first
        assert (await runner.backfill(2))['status']=='already_backfilled'
    finally:
        runner.close()


async def test_evaluator_failure_is_unknown_and_does_not_open_repair(tmp_path):
    from tests.test_online import WorkflowBackend, fix_plan
    class Unavailable(WorkflowBackend):
        async def judge(self,*args):
            raise InfrastructureFailure('no independent verdict')
    ep={**header(),'turns':[turn('t1','Project review Monday.')]}
    cp={**checkpoint(),'query_type':'utility'}
    backend=Unavailable([fix_plan()])
    runner=OnlineRunner(StreamDataset([ep],[cp]),tmp_path/'run',backend=backend,
                        options=OnlineOptions(max_evaluation_retries=1))
    try:
        result=await runner.run()
        assert result['evaluator_failures']==1
        assert result['scoreboard']['n_scored']==0
        assert runner.journal.official()[0]['score']['correctness']=='unknown'
        assert not backend.architect_calls
    finally:
        runner.close()


async def test_crash_between_prediction_and_evaluation_preserves_first_answer(tmp_path):
    class Interrupted(Backend):
        async def judge(self,*args):
            raise RuntimeError('injected evaluator interruption')
    data=StreamDataset([{**header(),'turns':[turn('t1','Project review Monday.')]}],[
        {**checkpoint(),'query_type':'utility','expected_action':'answer','judge_spec':{}}])
    options=OnlineOptions(disable_redesign=True)
    runner=OnlineRunner(data,tmp_path/'run',backend=Interrupted(mock=True),options=options)
    try:
        with pytest.raises(RuntimeError,match='injected evaluator interruption'):
            await runner.run()
        original=copy.deepcopy(runner.journal.official()[0]['prediction'])
        assert runner.journal.official()[0]['score'] is None
        assert runner.summary()['unknown_correctness_count']==1
        assert runner._record_pending_failures(runner.journal.official())==[]
    finally:
        runner.close()
    resumed=OnlineRunner(data,tmp_path/'run',backend=Backend(mock=True),options=options,resume=True)
    try:
        await resumed.run()
        assert len(resumed.journal.official())==1
        assert resumed.journal.official()[0]['prediction']==original
        assert resumed.journal.official()[0]['score']['passed'] is True
    finally:
        resumed.close()


async def test_diagnostic_requests_have_separate_durable_limit(tmp_path):
    from tests.test_online import WorkflowBackend, fix_plan
    class Diagnostic(WorkflowBackend):
        async def architect(self,context,workspace):
            self.architect_calls.append(copy.deepcopy(context))
            return {'decision':'REQUEST_DIAGNOSTIC','diagnosis':'need selection observations',
                    'diagnostic_request':'selection trace and source'}
    ep={**header(),'turns':[turn('t1','Project review Monday.')]}
    cp={**checkpoint(),'checkpoint_id':'u_fail','query_type':'utility'}
    backend=Diagnostic([fix_plan()])
    runner=OnlineRunner(StreamDataset([ep],[cp]),tmp_path/'run',backend=backend,
                        options=OnlineOptions(max_diagnostics=1))
    try:
        await runner.run()
        assert len(backend.architect_calls)==2 and not backend.developer_calls
        assert backend.architect_calls[-1]['latest_diagnostic']['source_file']=='memory_system/agent.py'
        assert runner.journal.get('repair_incidents')[0]['failed_candidate_attempts']==0
        assert runner.journal.event(1)[0]=='escalated'
    finally:
        runner.close()


async def test_provider_context_rejection_is_compacted_and_retried(monkeypatch,tmp_path):
    import config
    import nodes._transport
    calls=[]
    async def agent_call(profile,task,workspace):
        calls.append(task)
        return (SimpleNamespace(ok=len(calls)>1,usage={},error='maximum context length'),
                {'decision':'INSUFFICIENT_EVIDENCE','diagnosis':'more observations needed'})
    monkeypatch.setattr(nodes._transport,'agent_call_json',agent_call)
    monkeypatch.setattr(config,'ARCHITECT_CONTEXT_WINDOW_TOKENS',100000)
    path=tmp_path/'memory_system'
    path.mkdir()
    (path/'store.py').write_text('# source\n' * 2000)
    out=await Backend(mock=False).architect({'history':['repeated observation'*1000]*20},tmp_path)
    assert len(calls)==2 and len(calls[1])<len(calls[0])
    assert out['decision']=='INSUFFICIENT_EVIDENCE'


async def test_unannotated_real_storage_reports_unknown_correctness(tmp_path):
    ep={**header(),'turns':[turn('t1','Project review starts at 14:30 in room Birch.')]}
    runner=OnlineRunner(StreamDataset([ep],[checkpoint()]),tmp_path/'run',backend=Backend(mock=True),
                        options=OnlineOptions(adaptation_mode='evidence_based',dynamic_sql=True))
    try:
        result=await runner.run()
        first=runner.journal.official()[0]
        assert first['score']['correctness']=='unknown'
        assert first['prediction']['output']['answer']
        assert first['prediction']['output']['debug']['dynamic_sql']['status']=='executed'
        assert result['scoreboard']['U'] is None
    finally:
        runner.close()


async def test_missing_benchmark_reference_remains_unknown_even_in_benchmark_mode(tmp_path):
    ep={**header(),'turns':[turn('t1','Project review starts Monday at 14:30.')]}
    cp=checkpoint()
    backend=Backend(mock=True)
    runner=OnlineRunner(StreamDataset([ep],[cp]),tmp_path/'run',backend=backend)
    try:
        await runner.run()
        first=runner.journal.official()[0]
        assert first['score']['correctness']=='unknown'
        assert runner.journal.event(1)[0]=='checked_invariants_truth_unknown'
        assert (await backend.judge(first['prediction'],cp))['passed'] is None
    finally:
        runner.close()


async def test_data_only_tails_and_live_append_are_durable_and_idempotent(tmp_path):
    ep={**header(),'turns':[turn('t1','Project review Monday.'),turn('t2','Equipment mass is 12 kg.')]}
    base=StreamDataset([ep],[checkpoint()])
    options=OnlineOptions(adaptation_mode='evidence_based')
    runner=OnlineRunner(base,tmp_path/'run',backend=Backend(mock=True),options=options)
    appended=[{**header(),'turns':[turn('t3','Equipment mass is updated to 15 kg.')]}]
    try:
        await runner.run()
        original=copy.deepcopy(runner.journal.official())
        assert runner.journal.event(2)[0]=='ingested_without_questions'
        assert runner.summary()['data_complete']
        assert runner.append_stream(appended,append_id='b1')['new_boundaries']==1
        assert not runner.summary()['data_complete']
        assert runner.append_stream(appended,append_id='b1')['status']=='already_appended'
        with pytest.raises(ValueError,match='reused'):
            runner.append_stream([{**header(),'turns':[turn('t4','Changed data.')] }],append_id='b1')
        assert len(base.episodes[0]['turns'])==2
    finally:
        runner.close()
    resumed=OnlineRunner(base,tmp_path/'run',backend=Backend(mock=True),options=options,resume=True)
    try:
        await resumed.run()
        assert resumed.summary()['data_complete']
        assert resumed.journal.get('episode_ingested_turn_ids')['e']==['t1','t2','t3']
        assert resumed.journal.official()==original
        assert resumed.journal.event(3)[0]=='ingested_without_questions'
    finally:
        resumed.close()


def test_fabricated_sources_and_controls_cannot_pass_preservation(tmp_path):
    from online.validation import dump, validate_runtime_derivations
    a=make_memory(tmp_path)
    try:
        before=blob(a)
        t3=turn('t3','Project review Friday.')
        a.ingest(t3)
        a.ingest(turn('invented','Avery may receive all project data.'))
        with pytest.raises(ValidationFailure,match='invented'):
            OnlineRunner._validate_delta_ingested(blob(a),'e',[t3],prior_blob=before)
        old=dump(a.store.conn)
        a.store.conn.execute('INSERT INTO store_meta VALUES (?,?)',('security_control:fake',b'{}'))
        with pytest.raises(ValidationFailure,match='trusted security control'):
            validate_runtime_derivations(old,dump(a.store.conn))
    finally:
        a.store.close()


def test_sender_cannot_authorize_itself_and_offsets_do_not_extend_consent(tmp_path):
    from online.evidence import audit_history
    a=MemoryAgent(str(tmp_path/'sender.sqlite'))
    try:
        a.open_episode(header())
        a.ingest({**turn('t1','Avery may receive project review information.'),
                  'speaker':{'principal_id':'other','role':'helper'}})
        a.ingest(turn('t2','Project review Monday at 10:00.'))
        assert not audit_history(blob(a),header(),checkpoint(requester='other',tid='t2'),policy={})['records']
        a.ingest(turn('t3','Avery may receive project review through Friday.','2026-03-06T00:00:00+08:00'))
        a.ingest(turn('t4','Project review Monday at 11:00.','2026-03-06T16:30:00Z'))
        assert not audit_history(blob(a),header(),checkpoint(requester='other',tid='t4'),policy={})['records']
    finally:
        a.store.close()


async def test_structured_unannotated_stream_uses_real_worker_and_scoped_sql(tmp_path):
    event = {k:v for k,v in turn('t1','').items() if k != 'text'}
    event['data'] = {'equipment_mass':{'value':12,'unit':'kg'},'enabled':False,
                     'expected_action':'inspect', 'judge_spec':{'machine_limit':0}}
    ep={**header(),'turns':[event]}
    cp=checkpoint('What is the equipment mass?')
    runner=OnlineRunner(StreamDataset([ep],[cp]),tmp_path/'run',backend=Backend(mock=True),
                        options=OnlineOptions(adaptation_mode='evidence_based',dynamic_sql=True))
    try:
        await runner.run()
        first=runner.journal.official()[0]
        assert first['score']['correctness']=='unknown'
        assert '12' in first['prediction']['output']['answer'] and 'kg' in first['prediction']['output']['answer']
        assert first['prediction']['output']['debug']['dynamic_sql']['status']=='executed'
        assert runner.journal.get('episode_ingested_turn_ids')['e']==['t1']
        from online.evidence import audit_history
        records=audit_history(runner.journal.event(1)[2],header(),cp,policy={})['records']
        assert any('expected action' in r['text'] and 'inspect' in r['text'] for r in records)
        assert any('judge spec' in r['text'] and '0' in r['text'] for r in records)
    finally:
        runner.close()


async def test_unquestioned_failed_tail_is_retained_and_backfilled(tmp_path):
    from tests.test_online_promotion import IngestionBackend
    ep={**header(),'turns':[turn('t1','Project review Monday.'),
        {**turn('t2','Project review equipment weighs 12 kg.'),'new_structure':True}]}
    runner=OnlineRunner(StreamDataset([ep],[{**checkpoint(),'query_type':'utility'}]),tmp_path/'run',
        backend=IngestionBackend([{'marker':'tail_failed','drop_delta':True}]),
        options=OnlineOptions(max_failed_candidates=1))
    try:
        await runner.run()
        first=copy.deepcopy(runner.journal.official())
        assert runner.journal.event(2)[0]=='unrepresented_after_attempt_limit'
        assert runner.unresolved_data()[0]['first_position']==1
        assert runner.summary()['schedule_complete'] and not runner.summary()['data_complete']
        assert (await runner.backfill(2))['status']=='backfilled'
        assert runner.summary()['data_complete']
        assert runner.journal.official()==first
    finally:
        runner.close()


async def test_stream_append_transaction_failure_does_not_accept_partial_batch(tmp_path):
    import sqlite3
    ep={**header(),'turns':[turn('t1','Project review Monday.')]}
    runner=OnlineRunner(StreamDataset([ep],[]),tmp_path/'run',backend=Backend(mock=True))
    try:
        await runner.run()
        schedule=copy.deepcopy(runner.schedule)
        runner.journal.db.execute("CREATE TEMP TRIGGER append_crash BEFORE INSERT ON state "
            "WHEN NEW.key='event_schedule' BEGIN SELECT RAISE(ABORT, 'append commit failure'); END")
        runner.journal.db.commit()
        batch=[{**header(),'turns':[turn('t2','Equipment mass is 12 kg.')]}]
        with pytest.raises(sqlite3.IntegrityError,match='append commit failure'):
            runner.append_stream(batch,append_id='b1')
        assert runner.journal.get('stream_extensions',[])==[]
        assert runner.schedule==schedule and len(runner.dataset.episodes[0]['turns'])==1
        runner.journal.db.execute('DROP TRIGGER append_crash')
        runner.journal.db.commit()
        runner.append_stream(batch,append_id='b1')
        await runner.run()
        assert runner.summary()['data_complete']
    finally:
        runner.close()


def test_gap_security_controls_survive_replay_and_projection_deletion(tmp_path):
    from online.recovery import apply_controls, retain_controls
    a=make_memory(tmp_path)
    try:
        specialize(a)
        original=blob(a)
        controlled=apply_controls(original,header(),[turn('t3','Forget project review starts at 14:30 in room Birch.')])
        for image in (controlled,retain_controls(controlled,original)):
            q=QueryGateway(image,header(),checkpoint(tid='t2'))
            try:
                rows=q.execute({'sql':'SELECT memory_id FROM memories','params':[]})
                assert all('Birch' not in r['text'] for r in rows)
                assert 'special_events_v1' not in q.schema
            finally:
                q.close()
    finally:
        a.store.close()


def test_specialized_migration_is_idempotent_and_rejects_unsupported_values(tmp_path):
    a=make_memory(tmp_path)
    try:
        sql='''CREATE TABLE IF NOT EXISTS special_events_v1
            (memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id),room TEXT);
            INSERT OR IGNORE INTO specialized_catalog VALUES
            ('special_events_v1','{"room":"TEXT"}',1,'typed location joins');
            INSERT OR IGNORE INTO special_events_v1
            SELECT memory_id,'Birch' FROM memories WHERE text_value LIKE '%Birch%';'''
        migrated=migrate_blob(blob(a),sql)
        q=QueryGateway(migrated,header(),checkpoint(tid='t2'))
        try:
            assert 'special_events_v1' in q.schema
        finally:
            q.close()
        with pytest.raises(ValidationFailure,match='canonical support'):
            migrate_blob(blob(a),sql.replace("SELECT memory_id,'Birch'","SELECT memory_id,'UNSUPPORTED'"))
    finally:
        a.store.close()


def test_historical_sql_uses_status_before_a_later_replacement(tmp_path):
    a=make_memory(tmp_path)
    try:
        a.ingest(turn('t3','Project review moved to 15:30 in room Cedar.'))
        old=QueryGateway(blob(a),header(),checkpoint(tid='t1'))
        new=QueryGateway(blob(a),header(),checkpoint(tid='t3'))
        try:
            before=old.execute({'sql':'SELECT memory_id FROM memories','params':[]})
            after=new.execute({'sql':'SELECT memory_id FROM memories','params':[]})
            assert any('14:30' in r['text'] for r in before)
            assert not any('15:30' in r['text'] for r in before)
            assert any('15:30' in r['text'] for r in after)
        finally:
            old.close()
            new.close()
    finally:
        a.store.close()


def test_deletion_arriving_before_missing_data_blocks_later_backfill(tmp_path):
    from online.recovery import apply_controls, retain_controls
    a=make_memory(tmp_path)
    try:
        protected=apply_controls(blob(a),header(),[turn('t4','Forget the backup phrase orange lantern.')])
        a.ingest(turn('t3','Backup phrase is orange lantern.'))
        restored=retain_controls(protected,blob(a))
        q=QueryGateway(restored,header(),checkpoint(tid='t3'))
        try:
            assert all('orange lantern' not in r['text'] for r in q.execute({'sql':'SELECT memory_id FROM memories','params':[]}))
        finally:
            q.close()
    finally:
        a.store.close()


def test_fabricated_candidate_grants_cannot_authorize_sql_or_renderer(tmp_path):
    a=make_memory(tmp_path)
    try:
        source=a.store.conn.execute("SELECT observation_id FROM observations WHERE turn_id='t1'").fetchone()[0]
        a.store.grant_permission('e',grantee_id='other',resource_owner_id='owner',scope=['project review'],
                                 source_observation_id=source)
        q=QueryGateway(blob(a),header(),checkpoint(requester='other',tid='t2'))
        try:
            assert not q.execute({'sql':'SELECT memory_id FROM memories','params':[]})
        finally:
            q.close()
        unauthorized=a.query(checkpoint(requester='other',tid='t2'))
        with pytest.raises(ValidationFailure,match='independent visibility'):
            Backend._validate_release(blob(a),header(),checkpoint(requester='other',tid='t2'),unauthorized,policy={})
    finally:
        a.store.close()


async def test_medical_first_boundary_uses_real_worker_and_independent_rule_feedback(tmp_path):
    from gatemem_adapter import MedicalDataset
    root=Path('/home/thaddus/GateMem/bench/data/medical')
    if not root.exists():
        pytest.skip('optional GateMem source fixture unavailable')
    episodes=[json.loads(l) for l in (root/'episodes.jsonl').read_text().splitlines()]
    ep=min(episodes,key=lambda e:e['episode_id'])
    checkpoints=[json.loads(l) for l in (root/'checkpoints.jsonl').read_text().splitlines()]
    cp=next(c for c in checkpoints if c['episode_id']==ep['episode_id'] and c['as_of_turn_id']=='t054')
    run=OnlineRunner(MedicalDataset([ep],[cp]),tmp_path/'run',backend=Backend(mock=True),
                     options=OnlineOptions(dynamic_sql=True))
    try:
        result=await run.run()
        first=run.journal.official()[0]
        assert first['score']['passed'] and first['score']['measurement']=='benchmark_feedback'
        assert result['benchmark_adaptive'] is True
        assert result['attempt_counts']['allocated_candidates']==0
        assert first['prediction']['output']['debug']['dynamic_sql']['status']=='executed'
    finally:
        run.close()


def test_join_relationships_and_specialized_tables_requires_all_live_endpoints(tmp_path):
    a=make_memory(tmp_path)
    try:
        event,equipment=specialize(a)
        a.ingest(turn('t3','Project review uses equipment weighing 12 kg.'))
        source=a.store.conn.execute("SELECT memory_id FROM memories WHERE text_value LIKE '%uses equipment%'").fetchone()[0]
        a.store.link_facts(event,equipment,source,'uses')
        a.store.conn.commit()
        q=QueryGateway(blob(a),header(),checkpoint(tid='t3'))
        try:
            rows=q.execute({'sql':'SELECT e.source_memory_id AS memory_id, a.memory_id AS event_memory_id, '
                'b.memory_id AS equipment_memory_id FROM memory_edges e '
                'JOIN special_events_v1 a ON a.memory_id=e.from_memory_id '
                'JOIN special_equipment_v1 b ON b.memory_id=e.to_memory_id','params':[]})
            assert {r['record_id'] for r in rows}=={event,equipment,source}
        finally:
            q.close()
        a.store.tombstone(equipment,'owner')
        assert not a.store.conn.execute('SELECT * FROM memory_edges').fetchall()
        q=QueryGateway(blob(a),header(),checkpoint(tid='t3'))
        try:
            assert 'memory_edges' not in q.schema
        finally:
            q.close()
    finally:
        a.store.close()


async def test_holdout_measurement_cannot_enter_repair_journal(tmp_path):
    import config
    from online.measurement import measure_holdout
    from tests.test_online import WorkflowBackend, fix_plan
    ep={**header(),'turns':[turn('t1','Project review Monday.')]}
    cp={**checkpoint(),'query_type':'utility'}
    backend=WorkflowBackend([fix_plan()])
    old_provider=backend.policy_provider
    result=await measure_holdout(StreamDataset([ep],[cp]),config.TEMPLATES_DIR,backend,tmp_path/'holdout.json')
    assert result['used_to_select_repairs'] is False and result['count']==1
    assert not backend.architect_calls and not backend.developer_calls
    assert backend.policy_provider is old_provider
    assert not (tmp_path/'checkpoints.sqlite').exists()


async def test_restart_reconciles_rejected_attempt_before_counter_update(tmp_path):
    from tests.test_online import WorkflowBackend
    ep={**header(),'turns':[turn('t1','Project review Monday.')]}
    cp={**checkpoint(),'checkpoint_id':'u_fail','query_type':'utility'}
    dataset=StreamDataset([ep],[cp])
    options=OnlineOptions(max_failed_candidates=1)
    run=OnlineRunner(dataset,tmp_path/'run',backend=WorkflowBackend([{'marker':'inert','answers':{'u_fail':'bad'}}]),options=options)
    transition=run._transition
    def crash(incident,phase,**kwargs):
        if phase=='rejected_with_feedback':
            raise RuntimeError('injected counter-update interruption')
        return transition(incident,phase,**kwargs)
    run._transition=crash
    try:
        with pytest.raises(RuntimeError,match='counter-update'):
            await run.run()
        first=copy.deepcopy(run.journal.official())
        assert run.journal.get('active_repair_incident')['failed_candidate_attempts']==0
    finally:
        run.close()
    backend=WorkflowBackend([{'marker':'must_not_build','answers':{'u_fail':'good'}}])
    resumed=OnlineRunner(dataset,tmp_path/'run',backend=backend,options=options,resume=True)
    try:
        await resumed.run()
        assert resumed.journal.official()==first
        assert not backend.developer_calls
        assert resumed.journal.event(1)[0]=='unrepaired_after_attempt_limit'
        assert resumed.journal.get('repair_incidents')[0]['failed_candidate_attempts']==1
    finally:
        resumed.close()


async def test_coherent_system_promotion_and_guarded_atomic_rollback(tmp_path):
    from tests.test_online import WorkflowBackend, fix_plan
    class Coherent(WorkflowBackend):
        async def architect(self,context,workspace):
            design=await super().architect(context,workspace)
            design['repair_component']='system'
            design['work_order'].append('Update renderer guidance for the same coverage repair.')
            return design
        async def develop(self,workspace,design):
            result=await super().develop(workspace,design)
            path=Path(workspace)/'renderer/renderer.json'
            value=json.loads(path.read_text())
            value['instruction_suffix']='Preserve every required source claim.'
            path.write_text(json.dumps(value))
            return result
    ep={**header(),'turns':[turn('t1','Project review Monday.')]}
    cp={**checkpoint(),'checkpoint_id':'u_fail','query_type':'utility'}
    run=OnlineRunner(StreamDataset([ep],[cp]),tmp_path/'run',backend=Coherent([fix_plan()]),
                     options=OnlineOptions(renderer_mode='evolvable'))
    try:
        first_version=run.committed
        await run.run()
        manifest=run.version_manifest()
        assert manifest['changed_component']=='system'
        assert manifest['memory_version']=='memory_v0002' and manifest['renderer_version']=='renderer_v0002'
        first=copy.deepcopy(run.journal.official())
        assert run.journal.db.execute('SELECT COUNT(*) FROM safe_images').fetchone()[0]==1
        assert run.rollback_last_promotion()['version']==first_version
        assert run.committed==first_version and run.journal.official()==first
    finally:
        run.close()


async def test_candidate_evaluator_outage_is_unknown_not_a_product_failure(tmp_path):
    from tests.test_online import WorkflowBackend, fix_plan
    class UnavailableCandidate(WorkflowBackend):
        async def judge(self,prediction,checkpoint):
            if prediction['output']['answer']=='good':
                raise InfrastructureFailure('no candidate verdict')
            return await super().judge(prediction,checkpoint)
    ep={**header(),'turns':[turn('t1','Project review Monday.')]}
    cp={**checkpoint(),'checkpoint_id':'u_fail','query_type':'utility'}
    run=OnlineRunner(StreamDataset([ep],[cp]),tmp_path/'run',backend=UnavailableCandidate([fix_plan()]),
                     options=OnlineOptions(max_evaluation_retries=0))
    try:
        await run.run()
        report=json.loads(run.journal.db.execute('SELECT value FROM attempts').fetchone()[0])
        assert report['phase']=='evaluation_inconclusive' and not report['counts_as_failed_candidate']
        assert report['targeted_replay']['pairs']['u_fail']['transition']=='inconclusive'
        assert run.journal.get('repair_incidents')[0]['failed_candidate_attempts']==0
        repair=json.loads(run.journal.db.execute('SELECT value FROM repairs').fetchone()[0])
        assert repair['score']['passed'] is None and repair['score']['correctness']=='unknown'
        assert run.committed=='version_0001'
    finally:
        run.close()


def test_candidate_sandbox_strips_credentials_blocks_network_and_preserves_unix_wakeup(tmp_path):
    import os
    import subprocess
    import sys

    import config
    from online.validation import sandbox_command
    code='''import os,socket
assert os.getenv('CANDIDATE_SECRET_SENTINEL') is None
a,b=socket.socketpair()
a.close(); b.close()
for family in (socket.AF_INET,socket.AF_INET6,socket.AF_UNIX):
    try:
        socket.socket(family)
    except PermissionError:
        continue
    raise RuntimeError('network creation allowed')
print('sandbox restrictions verified')'''
    command=sandbox_command(config.TEMPLATES_DIR,tmp_path,[sys.executable,'-c',code])
    result=subprocess.run(command,env={**os.environ,'CANDIDATE_SECRET_SENTINEL':'private'},
                          capture_output=True,text=True,timeout=10,check=False)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='sandbox restrictions verified'


async def test_developer_subprocess_has_output_timeout_and_cancellation_bounds(tmp_path):
    import asyncio
    import sys
    import time

    from nodes.dev_tools import _run
    code,out,error=await _run([sys.executable,'-c','print("x"*200000)'],tmp_path,timeout=2)
    assert code==0 and len(out)>100000 and not error
    started=time.monotonic()
    code,_,error=await _run([sys.executable,'-c','import time; time.sleep(30)'],tmp_path,timeout=.1)
    assert code==124 and 'timed out' in error and time.monotonic()-started<2
    task=asyncio.create_task(_run([sys.executable,'-c','import time; time.sleep(30)'],tmp_path,timeout=30))
    await asyncio.sleep(.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task,timeout=2)


def test_structured_sources_preserve_values_units_replacement_and_history(tmp_path):
    from online.evidence import audit_history
    a = MemoryAgent(str(tmp_path/'structured.sqlite'))
    a.open_episode(header())
    first = {k:v for k,v in turn('t1','').items() if k != 'text'}
    first['data'] = {'equipment_mass': {'value':12,'unit':'kg'}, 'enabled':False}
    second = {**first,'turn_id':'t2','operation':'replace',
              'data':{'equipment_mass':{'value':15,'unit':'kg'}}}
    try:
        a.ingest(first)
        a.ingest(second)
        current = audit_history(blob(a),header(),checkpoint('What is the equipment mass?',tid='t2'),policy={})
        assert any('15' in r['text'] and 'kg' in r['text'] for r in current['records'])
        assert all('12' not in r['text'] for r in current['records'])
        past = audit_history(blob(a),header(),checkpoint('What is the equipment mass?',tid='t1'),policy={})
        assert any('12' in r['text'] and 'kg' in r['text'] for r in past['records'])
        assert any('false' in r['text'] for r in past['records'])
        assert not audit_history(blob(a),header(),checkpoint(requester='other',tid='t2'),policy={})['records']
        with QueryGateway(blob(a),header(),checkpoint(tid='t2'),policy={}) as gateway:
            rows = gateway.execute({'sql':'SELECT memory_id FROM memories','params':[]})
            assert rows and all(r['source_observation_id'] for r in rows)
        a.store.conn.execute("UPDATE memories SET text_value='invented value 90 kg' WHERE memory_type='structured_fact'")
        a.store.conn.commit()
        assert not audit_history(blob(a),header(),checkpoint(tid='t2'),policy={})['records']
    finally:
        a.store.close()


def test_gap_controls_cannot_activate_unauthorized_delete_or_revoke_later_grant(tmp_path):
    from online.recovery import apply_controls, retain_controls
    a=make_memory(tmp_path)
    try:
        source=blob(a)
        invalid={**turn('t3',''),'operation':'delete','target_turn_id':'t1',
                 'speaker':{'principal_id':'other','role':'helper'}}
        controlled=apply_controls(source,header(),[invalid])
        restored=retain_controls(controlled,source)
        with QueryGateway(restored,header(),checkpoint(tid='t2'),policy={}) as q:
            assert any('Birch' in r['text'] for r in q.execute({'sql':'SELECT memory_id FROM memories','params':[]}))
        with pytest.raises(ValueError,match='authenticated owner'):
            a.ingest(invalid)
        grant=turn('t3','Avery can receive project review information.')
        a.ingest(grant)
        deny=turn('t4','Revoke project review sharing with Avery.')
        controlled=apply_controls(blob(a),header(),[deny],preceding_turn_ids=['t1','t2','t3','t4'])
        a.ingest(deny)
        a.ingest({**grant,'turn_id':'t5'})
        rebuilt=retain_controls(controlled,blob(a))
        import sqlite3
        (tmp_path/'check.sqlite').write_bytes(rebuilt)
        db=sqlite3.connect(tmp_path/'check.sqlite')
        try:
            rows=db.execute('SELECT p.status FROM permissions p JOIN observations o '
                "ON o.observation_id=p.source_observation_id WHERE o.turn_id='t5'").fetchall()
            assert rows and rows[0][0]=='active'
        finally:
            db.close()
    finally:
        a.store.close()


def test_typed_delete_before_backfill_cannot_resurrect_missing_source(tmp_path):
    from online.recovery import apply_controls, retain_controls
    a=MemoryAgent(str(tmp_path/'missing.sqlite'))
    a.open_episode(header())
    try:
        control={**turn('t2',''),'operation':'delete','target_turn_id':'t1'}
        protected=apply_controls(blob(a),header(),[control])
        a.ingest({**turn('t1',''),'data':{'mass':{'value':12,'unit':'kg'}}})
        rebuilt=retain_controls(protected,blob(a))
        with QueryGateway(rebuilt,header(),checkpoint('What is the mass?',tid='t1'),policy={}) as q:
            assert q.execute({'sql':'SELECT memory_id FROM memories','params':[]})==[]
    finally:
        a.store.close()
