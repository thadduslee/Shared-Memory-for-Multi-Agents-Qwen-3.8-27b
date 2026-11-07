"""Read-only evidence-card input-boundary replay; no semantic-score claim.

Only selected_source_turns (raw source text/authenticated speaker IDs/time) enter
the adapter. Card diagnosis, entities, assignments, gold and checkpoint questions
are not runtime inputs. An offline no-op processor tests the boundary, not NLU.
"""
import hashlib
import json
from pathlib import Path
import tempfile

from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.processors import HashEmbedder
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter, EmptyPermissionProcessor
from evaluation.gatemem_medical.input_boundary import source_turn


def replay(path):
    content=path.read_bytes();card=json.loads(content)
    turns=[source_turn(t) for t in card['selected_source_turns']]
    with tempfile.TemporaryDirectory() as tmp:
        core=PrivacyMemorySystem(Path(tmp)/'memory.sqlite',HashEmbedder())
        try:
            model=EmptyPermissionProcessor()
            adapter=InferenceFixtureAdapter(core,{'episode_id':'boundary-replay-'+path.stem},
                ['test:hash','test:permission'],model,permission_version='19')
            adapter.ingest_batch({'boundary':1},turns)
            report=adapter.processing_metrics()['task_contracts_v19']
            assert report['enabled'] is False
            assert report['host_receipts']==report['dialogue_receipts']==0
            assert report['host_events_available'] is False
            assert core.db.execute("SELECT COUNT(*) FROM resources WHERE kind='host_task_evidence'").fetchone()[0]==0
            assert all(len(call.get('sources',[]))<=6 for call in model.calls)
            return {'card':path.name,'card_sha256':hashlib.sha256(content).hexdigest(),
                'source_turn_ids':[t['turn_id'] for t in turns], 'source_calls':len(model.calls),
                'host_events_manufactured':False,'task_contract_report':report,
                'limits':'Selected-source boundary replay with no-op semantic processor; not full-prefix inference or changed historical scores.'}
        finally:core.close()


if __name__=='__main__':
    root=Path('/home/thaddus/a-mem-variant/analysis/v19-v21-evidence-review-20261102/cards/v19')
    result={'replays':[replay(root/name) for name in ('001-07.json','006-04.json')],
        'real_model_validation':'still needed', 'external_assignment_events_available':False}
    target=Path(__file__).with_name('boundary-offline.json')
    target.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
