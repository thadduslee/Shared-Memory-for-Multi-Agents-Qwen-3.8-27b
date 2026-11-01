"""Regression checks for the evaluator-to-agent boundary."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.gatemem_medical import runner
from evaluation.gatemem_medical.input_boundary import episode_header, source_turn
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter, EmptyPermissionProcessor


class InputBoundaryTests(unittest.TestCase):
    def test_allowlist_preserves_raw_text_without_annotations(self):
        text='My entities list is private. Mom may receive appointment times only.'
        turn={'turn_id':'t001','text':text,'speaker':{'principal_id':'actor','role':'patient',
              'permissions':{'read':'all'}},'timestamp':'2026-03-03T08:00','turn_kind':'dialogue',
              'subject_id':'secret-subject','access_level':'all','arbitrary_future_annotation':{'gold':'leak'}}
        expected={'turn_id':'t001','text':text,'speaker':{'principal_id':'actor'},
                  'timestamp':'2026-03-03T08:00','turn_kind':'dialogue'}
        original=deepcopy(turn)
        self.assertEqual(source_turn(turn),expected)
        self.assertEqual(source_turn(source_turn(turn)),expected)
        self.assertEqual(turn,original)
        self.assertEqual(episode_header({'episode_id':'e','entities':{},'turns':[turn]}),{'episode_id':'e'})

    def test_structured_metadata_cannot_hide_inside_envelope_fields(self):
        turn={'turn_id':'t001','text':'Raw text','speaker':{'principal_id':'actor'}}
        for key in ('timestamp','turn_kind','text','turn_id'):
            with self.subTest(key=key),self.assertRaises(ValueError):
                source_turn({**turn,key:{'access_level':'all'}})
        with self.assertRaises(ValueError):
            source_turn({**turn,'speaker':{'principal_id':{'access_level':'all'}}})

    def test_runner_passes_only_header_and_source_delta_and_resumes(self):
        data=Path('/home/thaddus/GateMem/bench/data/medical')
        if not data.is_dir():self.skipTest('Pinned medical fixture is not installed')
        episodes,checkpoints=runner.load_data(data)
        episodes[0]['entities']={'access_level':'EVALUATOR_ONLY_SENTINEL'}
        for turn in episodes[0]['turns']:
            turn['access_level']='EVALUATOR_ONLY_SENTINEL'
            turn['speaker']['permissions']={'gold':'EVALUATOR_ONLY_SENTINEL'}
        adapters=[];models=[]

        class BoundaryCheckedAdapter(InferenceFixtureAdapter):
            def __init__(self,core,episode,processors,processor,**kwargs):
                assert set(episode)=={'episode_id'}
                adapters.append(self)
                super().__init__(core,episode,processors,processor,**kwargs)

            def ingest_batch(self,event,turns,evolution_processor=None):
                assert set(event)=={'boundary'}
                for turn in turns:
                    assert set(turn)<={'turn_id','text','speaker','timestamp','turn_kind'}
                    assert set(turn['speaker'])=={'principal_id'}
                return super().ingest_batch(event,turns,evolution_processor)

        class RecordingProcessor(EmptyPermissionProcessor):
            def __init__(self):
                super().__init__();models.append(self)

        with tempfile.TemporaryDirectory() as tmp,redirect_stdout(io.StringIO()), \
                patch.object(runner,'load_data',return_value=(episodes,checkpoints)), \
                patch.object(runner,'InferenceFixtureAdapter',BoundaryCheckedAdapter), \
                patch.object(runner,'EmptyPermissionProcessor',RecordingProcessor):
            args=['--offline','--run-dir',tmp,'--max-boundaries','1']
            self.assertEqual(runner.main(args),0)
            self.assertEqual(runner.main([*args,'--resume']),0)
            summary=json.loads((Path(tmp)/'summary.json').read_text())
            self.assertEqual(summary['coverage']['completed_boundaries'],2)
            self.assertEqual(summary['coverage']['ingested_source_turns'],67)
            self.assertEqual(summary['agent_input_contract'],'raw-stream-v1')
            self.assertEqual(len(adapters),2)
            payload=json.dumps([model.calls for model in models])
            self.assertNotIn('EVALUATOR_ONLY_SENTINEL',payload)
            self.assertNotIn('entities.relationships',payload)
            self.assertTrue(all(not call['bootstrap']['initial_permissions'] for m in models for call in m.calls))
            self.assertTrue(all(len(call['sources'])<=6 for m in models for call in m.calls))
            self.assertTrue(all(call['context']['mode']=='single-pass' for m in models for call in m.calls))


if __name__=='__main__':unittest.main()
