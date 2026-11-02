"""Provider limits and budget pause verified with a fake HTTP transport."""
import io
import json
from pathlib import Path
import urllib.error

import pytest

from agentic_memory.privacy.processors import CompletionProcessor, ProcessorBudgetFailure, ProcessorDeadlineFailure
from evaluation.gatemem_medical.recovery import RecoveryController


class Response:
    def __init__(self,value):self.body=json.dumps(value).encode()
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self):return self.body


@pytest.mark.parametrize('stage,limit',[
    ('policy_classification',512),('query_policy',512),('answer',4096),
    ('answer_render',4096),('combined_incremental',2048),('binding_maintenance',2048),
])
def test_v20_stage_caps_are_sent_to_provider(monkeypatch,stage,limit):
    calls=[]
    def request(req,timeout):
        calls.append(json.loads(req.data))
        return Response({'model':'mock','choices':[{'message':{'content':'{"ok":true}'}}],
                         'usage':{'prompt_tokens':10,'completion_tokens':5}})
    monkeypatch.setattr('urllib.request.urlopen',request)
    processor=CompletionProcessor('test','http://127.0.0.1:1/v1','mock')
    assert processor.complete('Return JSON.',{'stage':stage,'permission_version':'20'})=={'ok':True}
    assert calls[0]['max_tokens']==limit
    assert processor.last_effective_limits['max_tokens']==limit


def test_legacy_query_limit_is_unchanged(monkeypatch):
    calls=[]
    def request(req,timeout):
        calls.append(json.loads(req.data))
        return Response({'model':'mock','choices':[{'message':{'content':'{"ok":true}'}}]})
    monkeypatch.setattr('urllib.request.urlopen',request)
    CompletionProcessor('test','http://127.0.0.1:1/v1','mock').complete('JSON',{'query':'hello'})
    assert calls[0]['max_tokens']==8192


def test_402_is_not_retried_and_checkpoint_says_paused_budget(monkeypatch,tmp_path):
    calls=[]
    def request(req,timeout):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url,402,'Payment Required',{},
            io.BytesIO(b'{"error":{"message":"Insufficient key allowance"}}'))
    monkeypatch.setattr('urllib.request.urlopen',request)
    processor=CompletionProcessor('test','http://127.0.0.1:1/v1','mock')
    recovery=RecoveryController(tmp_path,sleep=lambda _:pytest.fail('Budget failure must not sleep'),progress=lambda *a,**k:None)
    with pytest.raises(ProcessorBudgetFailure):
        recovery(lambda:processor.complete('JSON',{'permission_version':'20','stage':'answer'}),'answer checkpoint')
    assert len(calls)==1
    state=json.loads((tmp_path/'recovery.json').read_text())
    assert state['state']=='paused_budget'
    assert state['failures']==1


def test_network_timeout_is_clipped_to_remaining_deadline(monkeypatch):
    calls=[]
    monkeypatch.setattr('time.monotonic',lambda:100.)
    def request(req,timeout):
        calls.append(timeout)
        return Response({'model':'mock','choices':[{'message':{'content':'{"ok":true}'}}]})
    monkeypatch.setattr('urllib.request.urlopen',request)
    p=CompletionProcessor('test','http://127.0.0.1:1/v1','mock')
    p._deadline=105.
    p.complete('JSON',{'permission_version':'20','stage':'answer'})
    assert calls==[5.]
    p._deadline=99.
    with pytest.raises(ProcessorDeadlineFailure):p.complete('JSON',{'permission_version':'20'})
    assert calls==[5.]


def test_live_v20_requires_explicit_paid_opt_in_before_processor_creation(monkeypatch):
    from evaluation.gatemem_medical import runner
    def forbidden(*a,**kw):pytest.fail('A live processor must not be created before paid opt-in')
    monkeypatch.setattr(runner,'processors',forbidden)
    with pytest.raises(SystemExit) as exc:runner.main(['--permission-version','20'])
    assert exc.value.code==2


def test_openrouter_wrapper_cannot_spend_on_preflight_before_opt_in(monkeypatch,tmp_path):
    from evaluation.gatemem_medical import openrouter
    def forbidden(*a,**kw):pytest.fail('No configuration or preflight before paid opt-in')
    monkeypatch.setattr(openrouter,'configure',forbidden)
    monkeypatch.setattr(openrouter,'preflight',forbidden)
    with pytest.raises(SystemExit) as exc:
        openrouter.main(['--permission-version','20','--run-dir',str(tmp_path/'new-run')])
    assert exc.value.code==2
    assert not (tmp_path/'new-run').exists()


def test_runner_budget_pause_saves_prediction_and_returns_resumable_exit(monkeypatch,tmp_path,capsys):
    from evaluation.gatemem_medical import runner
    from agentic_memory.privacy.processors import HashEmbedder,ScriptedProcessor
    class BudgetJudge:
        model='mock-judge'
        def complete(self,*a,**kw):raise ProcessorBudgetFailure('HTTP 402: key allowance exhausted')
    monkeypatch.setattr(runner,'processors',lambda offline:(HashEmbedder(),ScriptedProcessor(),BudgetJudge()))
    run=tmp_path/'run'
    assert runner.main(['--permission-version','20','--offline','--run-dir',str(run),
                        '--max-checkpoints','1','--no-evolution'])==75
    output=capsys.readouterr().out
    assert 'paused_budget' in output
    assert 'Traceback' not in output
    assert json.loads((run/'recovery.json').read_text())['state']=='paused_budget'
    summary=json.loads((run/'summary.json').read_text())
    assert summary['coverage']['official_predictions']==1
    assert summary['coverage']['scored_checkpoints']==0
    assert summary['complete'] is False


def test_successful_operation_clears_budget_pause_without_rewriting_history(tmp_path):
    recovery=RecoveryController(tmp_path,progress=lambda *a,**k:None)
    def failed():raise ProcessorBudgetFailure('HTTP 402')
    with pytest.raises(ProcessorBudgetFailure):recovery(failed,'answer')
    assert recovery(lambda:'saved','answer')=='saved'
    events=[json.loads(x) for x in (tmp_path/'recovery-events.jsonl').read_text().splitlines()]
    assert [e['state'] for e in events]==['paused_budget','running']
    assert events[-1]['resumed_after_budget_pause']


def test_preflight_budget_pause_is_graceful_and_makes_no_native_run(monkeypatch,tmp_path,capsys):
    from evaluation.gatemem_medical import openrouter
    monkeypatch.setattr(openrouter,'configure',lambda *a:None)
    def failed():raise ProcessorBudgetFailure('HTTP 402')
    monkeypatch.setattr(openrouter,'preflight',failed)
    run=tmp_path/'new-run'
    assert openrouter.main(['--permission-version','20','--allow-paid-api','--run-dir',str(run)])==75
    assert json.loads((run/'recovery.json').read_text())['state']=='paused_budget'
    assert not (run/'evaluation.sqlite').exists()
    assert 'Traceback' not in capsys.readouterr().out
