"""Offline card-driven V19 regression in disposable SQLite, never historical writes.

PYTHONPATH=. python analysis/v19-scoped-control-holds/replay_004_01.py --card ... --output ...
Scripted extraction, permissions and answers are test fixtures, not a new
benchmark score or a real-model result. The answer uses the CURRENT answer
validation contract through its scripted test model (EvidenceModel).
"""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from agentic_memory.privacy.permissions import PermissionUpdater, PermissionRepairExhausted
from agentic_memory.privacy.processors import ProcessorContractFailure
from agentic_memory.privacy.answering import PermissionQueryService
from agentic_memory.privacy import privacy_fallback_v19 as fallback, scoped_holds_v19 as holds
from security_tests.test_incremental_permissions import IncrementalTests
from security_tests.test_answer_validation_v19 import EvidenceModel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--card', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    card_bytes = args.card.read_bytes()
    card = json.loads(card_bytes)
    turns = card['selected_source_turns']
    patient = turns[0]['speaker']['principal_id']
    h = IncrementalTests(); h.setUp()
    try:
        h.actor_names = {patient: 1}
        h.source_subject=patient
        actor = h.actor
        h.actor = lambda p, name: actor(p, patient if name == 'owner' else name)
        h.bootstrap = replace(h.bootstrap, owner_read_actions=('search','read','derive'))
        h.updater = PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19')
        h.turn('My name is Nadia Ruiz.', actor=patient)
        attempts = card['selected_control_attempts']
        failures = []
        original = h.model.complete
        def rejected(system, payload, *, schema=None):
            failure = attempts[len(failures)]['validation']
            failures.append(failure['reason'])
            exc = ProcessorContractFailure(failure['reason'])
            exc.details = failure['details']
            raise exc
        h.model.complete = rejected
        try:
            h.turn(turns[0]['text'], actor=patient)
        except PermissionRepairExhausted:
            failed = h.core.db.execute('SELECT id FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0]
            h.updater.skip_exhausted(failed,max_attempts=2)
        else:
            raise AssertionError('The saved identifier failures must remain failed')
        h.model.complete = original
        h.classify = lambda p, s: 'instruction'
        for turn in turns[1:]:
            h.turn(turn['text'], actor=turn['speaker']['principal_id'])
        reader = h.reader(patient)
        packet = h.core.retrieve(reader,card['checkpoint']['query_text'],k=100)
        released = h.core.release(reader,packet['input_ids'],packet['evidence'])
        text = '\n'.join(r['text'] for r in released)
        answer_model = EvidenceModel();h.core.register_processor(h.host, answer_model)
        answer=PermissionQueryService(h.updater).answer(patient,card['checkpoint']['query_text'],answer_model,as_of='t051')['output']
        required = card['checkpoint']['judge_spec']['include']
        assert all(value in text for value in required)
        assert answer['action']=='answer' and all(value in answer['answer'] for value in required)
        assert {c['stage'] for c in answer_model.calls} == {'answer_v19', 'verify_answer_v19'}
        row = next(r for r in h.updater.control_assessments() if r['job_id'] == failed)
        assert row['state'] == 'VALIDATED_NON_CONTROL'
        assert row['finalization']['outcome'] == 'NO_PRIVACY_INSTRUCTION' and row['finalization']['uncertainty_released']
        assert not fallback.attempts(h.updater, failed)  # complete-message grammar: no extra call
        assert patient in row['original']['boundary']['subjects']
        superseded = holds._historical_blocks(h.core, 'work', subject=patient, recipient=patient, action='read',
            text=turns[-1]['text'])
        attempts_after = h.core.db.execute('SELECT COUNT(*) FROM permission_attempts WHERE job_id=?',(failed,)).fetchone()[0]
        report = {'policy_version':fallback.POLICY_VERSION, 'evidence_card':str(args.card.resolve()),
            'card_sha256':hashlib.sha256(card_bytes).hexdigest(),
            'method':'Read-only evidence JSON; fresh temporary SQLite; scripted V19 extraction/binding; saved identifier failures retained; '
                'answer through the current enforced answer-validation contract with its scripted EvidenceModel. No external model/network calls or historical writes.',
            'result_kind':'scripted test doubles only; NOT a real-model result',
            'answer_model':'security_tests.test_answer_validation_v19.EvidenceModel (scripted)',
            'external_model_calls':0, 'scripted_source_calls':len(h.model.calls),
            'scripted_answer_calls':[c['stage'] for c in answer_model.calls],
            'extra_privacy_assessment_calls':len(fallback.attempts(h.updater, failed)),
            'historical_action':card['output']['action'],
            'original_event':turns[0], 'failed_job_id':failed,
            'original_failures':failures, 'retained_attempts':attempts_after,
            'processing_state':'quarantined', 'control_assessment':row,
            'superseded_predicate_would_block':superseded,
            'legacy_comparison':('The superseded Batch 4 predicate would also block Nadia here.' if superseded else
                'Batch 4 already exempted this complete medication question; t037/t049 are the failing ordinary '
                'messages covered by security_tests/test_privacy_fallback_episodes_v19.py.'),
            'released_current_approved_evidence':[r['text'] for r in released],
            'answer_service_output':answer,
            'required_plan_details_present':{value:value in text for value in required},
            'limits':'Controlled regression with scripted semantic decisions; does not rerun the historical prefix or claim a changed benchmark score.'}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({'released':len(released),'required_details_present':True,'retained_attempts':attempts_after}))
    finally:
        h.tearDown()


if __name__ == '__main__':
    main()
