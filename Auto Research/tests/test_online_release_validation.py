"""Release validation must not depend on a bounded diagnostic report."""
import copy
import json

import pytest

from online.backend import Backend
from online.evidence import SCAN_LIMIT, audit_history
from online.interfaces import StreamDataset
from online.runner import OnlineOptions, OnlineRunner, RepairBlocked
from online.validation import ValidationFailure
from templates.memory_system.agent import MemoryAgent
from tests.test_online_evidence import blob, checkpoint, header, turn


@pytest.fixture
def memory(tmp_path):
    agent = MemoryAgent(str(tmp_path / 'memory.sqlite'))
    agent.open_episode(header())
    agent.ingest(turn('t1', 'The project review starts at 14:30 in room Birch.'))
    try:
        yield agent
    finally:
        agent.store.close()


def decision_for(row):
    return {'action': 'answer', 'evidence': [{
        'record_id': row['memory_id'], 'text': row['text_value'],
        'source_observation_id': row['source_observation_id'], 'status': 'active'}],
        'answer_plan': {'schema_version': 2, 'claims': [{
            'claim_id': 'selected', 'source_record_id': row['memory_id'],
            'fact': ' '.join(row['text_value'].split()), 'required': True}]}}


@pytest.mark.parametrize('extra_turns', [70, 300])
async def test_release_accepts_selected_record_outside_report_or_scan_limit(memory, tmp_path, extra_turns):
    for index in range(2, extra_turns + 2):
        memory.ingest(turn(f't{index}', f'Inventory item {index} has code amber{index}.'))
    cp = checkpoint('What are the inventory codes?', tid=f't{extra_turns + 1}')
    image = blob(memory)
    audit = audit_history(image, header(), cp, policy={})
    reported = {r['record_id'] for r in audit['records']}
    selected = next(row for row in memory.store.conn.execute(
        "SELECT * FROM memories WHERE memory_type='derived_fact' ORDER BY seq,memory_id")
        if row['memory_id'] not in reported)
    assert not audit['complete']
    if extra_turns == 300:
        # This selected record is older than the finite historical scan.
        newer = memory.store.conn.execute('SELECT COUNT(*) FROM memories WHERE seq>?',
                                          (selected['seq'],)).fetchone()[0]
        assert newer > SCAN_LIMIT
    prediction = await Backend(mock=True).predict(
        tmp_path / 'workspace', image, header(), cp, memory_decision=decision_for(selected))
    assert prediction['output']['answer'] == ' '.join(selected['text_value'].split())


def test_release_accepts_source_longer_than_report_text_limit(memory):
    text = 'Inventory description is ' + 'amber ' * 700
    memory.ingest(turn('t2', text))
    cp = checkpoint('What is the inventory description?', tid='t2')
    selected = memory.store.conn.execute(
        "SELECT * FROM memories WHERE memory_type='derived_fact' AND length(text_value)>4000").fetchone()
    assert selected is not None
    image = blob(memory)
    audit = audit_history(image, header(), cp, policy={})
    assert selected['memory_id'] not in {r['record_id'] for r in audit['records']}
    Backend._validate_release(image, header(), cp, decision_for(selected), policy={})


def test_release_checks_all_selected_records_past_64(memory):
    for index in range(2, 72):
        memory.ingest(turn(f't{index}', f'Inventory item {index} has code amber{index}.'))
    cp = checkpoint('What are the inventory codes?', tid='t71')
    records = memory.store.conn.execute(
        "SELECT * FROM memories WHERE memory_type='derived_fact' ORDER BY seq,memory_id").fetchall()
    decision = {'action': 'answer', 'evidence': [], 'answer_plan': {'claims': []}}
    for index, row in enumerate(records):
        selected = decision_for(row)
        decision['evidence'].extend(selected['evidence'])
        selected['answer_plan']['claims'][0]['claim_id'] = f'claim_{index}'
        decision['answer_plan']['claims'].extend(selected['answer_plan']['claims'])
    assert len(decision['evidence']) > 64
    image = blob(memory)
    Backend._validate_release(image, header(), cp, decision, policy={})
    decision['evidence'][-1]['text'] = 'Unsupported text past the reporting limit.'
    with pytest.raises(ValidationFailure, match='independent visibility'):
        Backend._validate_release(image, header(), cp, decision, policy={})


def test_release_accepts_record_outside_report_total_character_budget(memory):
    for index in range(2, 57):
        memory.ingest(turn(f't{index}', f'Inventory item {index} description is ' + 'amber ' * 100))
    cp = checkpoint('What are the inventory descriptions?', tid='t56')
    image = blob(memory)
    audit = audit_history(image, header(), cp, policy={})
    rows = memory.store.conn.execute(
        "SELECT * FROM memories WHERE memory_type='derived_fact' ORDER BY seq,memory_id").fetchall()
    assert len(rows) < 64
    assert sum(len(row['text_value']) for row in rows) > 24000
    reported = {r['record_id'] for r in audit['records']}
    selected = next(row for row in rows if row['memory_id'] not in reported)
    assert not audit['complete']
    Backend._validate_release(image, header(), cp, decision_for(selected), policy={})


@pytest.mark.parametrize('defect', [
    'denied', 'deleted', 'expired', 'cancelled', 'future', 'unproven_source',
    'forged_text', 'forged_source', 'forged_claim',
])
def test_release_still_rejects_forbidden_or_tampered_records(memory, defect):
    selected = memory.store.conn.execute(
        "SELECT * FROM memories WHERE memory_type='derived_fact'").fetchone()
    cp = checkpoint()
    if defect == 'future':
        memory.ingest(turn('t2', 'Inventory item has code violet.'))
        selected = memory.store.conn.execute(
            "SELECT m.* FROM memories m JOIN observations o ON o.observation_id=m.source_observation_id "
            "WHERE m.memory_type='derived_fact' AND o.turn_id='t2'").fetchone()
    decision = decision_for(selected)
    if defect == 'denied':
        cp = checkpoint(requester='other')
    elif defect == 'deleted':
        memory.store.tombstone(selected['memory_id'], 'owner')
    elif defect == 'expired':
        memory.store.conn.execute("UPDATE memories SET valid_until='2025-12-31T00:00:00Z'")
    elif defect == 'cancelled':
        memory.store.conn.execute("UPDATE memories SET status='cancelled'")
    elif defect == 'unproven_source':
        memory.store.conn.execute("UPDATE memories SET text_value='Invented source fact.' WHERE memory_id=?",
                                  (selected['memory_id'],))
        decision['evidence'][0]['text'] = 'Invented source fact.'
        decision['answer_plan']['claims'][0]['fact'] = 'Invented source fact.'
    elif defect == 'forged_text':
        decision['evidence'][0]['text'] = 'Invented source fact.'
    elif defect == 'forged_source':
        decision['evidence'][0]['source_observation_id'] = 'invented_observation'
    elif defect == 'forged_claim':
        decision['answer_plan']['claims'][0]['fact'] = 'Invented source fact.'
    with pytest.raises(ValidationFailure):
        Backend._validate_release(blob(memory), header(), cp, decision, policy={})


def test_release_enforces_scoped_projection(memory):
    memory.ingest(turn('t2', 'Avery may receive meeting time and room logistics only; no budget or contact numbers.'))
    memory.ingest(turn('t3', 'Meeting Monday at 10:00 is the confidential merger review in room Cedar.'))
    cp = checkpoint('What time and where is the meeting?', requester='other', tid='t3')
    decision = memory.query(cp)
    assert decision['evidence'] and all('merger' not in r['text'] for r in decision['evidence'])
    image = blob(memory)
    Backend._validate_release(image, header(), cp, decision, policy={})
    expanded = copy.deepcopy(decision)
    selected = next(r for r in expanded['evidence'] if 'Cedar' in r['text'])
    full_text = memory.store.conn.execute('SELECT text_value FROM memories WHERE memory_id=?',
                                          (selected['record_id'],)).fetchone()[0]
    assert 'merger' in full_text
    selected['text'] = full_text
    with pytest.raises(ValidationFailure, match='independent visibility'):
        Backend._validate_release(image, header(), cp, expanded, policy={})


@pytest.mark.parametrize('max_infra_retries', [0, 2])
async def test_validation_failure_blocks_without_recording_or_reingesting_prediction(tmp_path, max_infra_retries):
    class RejectingBackend(Backend):
        reject = True
        ingestions = 0

        async def memory(self, *args, **kwargs):
            self.ingestions += kwargs.get('delta') is not None
            return await super().memory(*args, **kwargs)

        async def predict(self, *args, **kwargs):
            if self.reject and args[3]['checkpoint_id'] == 'q2':
                raise ValidationFailure('release evidence failed independent visibility/provenance check',
                                        stage='authorization_evidence')
            return await super().predict(*args, **kwargs)

    data = StreamDataset([{**header(), 'turns': [turn(
        't1', 'The project review starts at 14:30 in room Birch.')]}], [
            {**checkpoint(), 'checkpoint_id': 'q1'}, {**checkpoint(), 'checkpoint_id': 'q2'}])
    backend = RejectingBackend(mock=True)
    path = tmp_path / 'run'
    options = OnlineOptions(max_infra_retries=max_infra_retries)
    run = OnlineRunner(data, path, backend=backend, options=options)
    try:
        with pytest.raises(RepairBlocked, match='release validation'):
            await run.run()
        assert run.journal.get('status') == 'validation_blocked'
        assert run.journal.get('event_boundary_index') == 1
        phase, _, image = run.journal.event(1)
        assert phase == 'ingested' and image
        previous = run.journal.official()
        assert len(previous) == 1 and previous[0]['checkpoint_id'] == 'q1'
        assert backend.ingestions == 1
        report = json.loads((path / 'events/boundary_0001/release_validation_failure.json').read_text())
        assert report['stage'] == 'authorization_evidence'
        assert report['checkpoint_id'] == 'q2'
    finally:
        run.close()

    backend.reject = False
    run = OnlineRunner(data, path, backend=backend, options=options, resume=True)
    try:
        with pytest.raises(RepairBlocked, match='release validation'):
            await run.run()
        assert run.journal.official() == previous
        assert backend.ingestions == 1
    finally:
        run.close()

    run = OnlineRunner(data, path, backend=backend, options=options, resume=True, retry_blocked=True)
    try:
        result = await run.run()
        assert result['status'] == 'complete'
        official = run.journal.official()
        assert len(official) == 2
        assert official[0]['prediction'] == previous[0]['prediction']
        assert run.journal.event(1)[2] == image
        assert backend.ingestions == 1
        usage = run.journal.db.execute(
            "SELECT invocations,retries FROM operations WHERE key='boundary_0001/predict/q2'").fetchone()
        assert tuple(usage) == (2, 0)
    finally:
        run.close()
