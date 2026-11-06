"""Regressions for the first localhost run's scoring and provider failures."""
import json
from pathlib import Path

import pytest

from agentic_memory.privacy.processors import CompletionProcessor,ModelContractError,portable_schema
from evaluation.gatemem_medical.scoring import GateMemScorer
from evaluation.gatemem_medical.recovery import RecoveryController
from evaluation.gatemem_medical.tests.test_v20_provider import Response


def launcher():
    import importlib.util
    spec=importlib.util.spec_from_file_location('v20_local_session',
        Path(__file__).resolve().parents[3]/'analysis/workflow-v20/local_session.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def local_env():
    return {'AMEM_LLM_BASE_URL':'http://localhost:19000/v1','AMEM_LLM_MODEL':'qwen3.8:27b',
            'AMEM_JUDGE_BASE_URL':'http://localhost:19000/v1','AMEM_JUDGE_MODEL':'qwen3.6:35b'}


def test_startup_checks_are_small_and_do_not_admit_sources(monkeypatch):
    module=launcher();calls=[]
    def request(req,timeout):
        calls.append((json.loads(req.data),timeout,req.get_header('Authorization')))
        return Response({'model':'mock','choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]})
    monkeypatch.setattr('urllib.request.urlopen',request)
    result=module.provider_readiness(local_env())
    assert result['ready'] and len(calls)==2
    assert all(body['max_tokens']==32 and timeout==5 and key is None for body,timeout,key in calls)
    assert all(body['chat_template_kwargs']=={'enable_thinking':False} for body,_,_ in calls)


def test_startup_timeout_stops_before_another_queued_request(monkeypatch):
    module=launcher();calls=[]
    def request(req,timeout):calls.append(req);raise TimeoutError('busy')
    monkeypatch.setattr('urllib.request.urlopen',request)
    result=module.provider_readiness(local_env())
    assert not result['ready'] and len(calls)==1
    assert result['checks'][0]['error_type']=='TimeoutError'


def test_startup_rejects_truncated_or_nonobject_responses(monkeypatch):
    module=launcher()
    for content,finish in [('[]','stop'),('{"ok":true}','length')]:
        monkeypatch.setattr('urllib.request.urlopen',lambda *a,**kw:Response({
            'choices':[{'message':{'content':content},'finish_reason':finish}]}))
        assert not module.provider_readiness(local_env())['ready']


def test_provider_failure_cannot_start_benchmark_worker(monkeypatch,tmp_path):
    module=launcher();run=tmp_path
    (run/'launch.json').write_text(json.dumps({'command':['python','unused']}))
    monkeypatch.setattr(module,'environment',lambda path:local_env())
    monkeypatch.setattr(module,'provider_readiness',lambda env:{'ready':False,'checks':[{
        'role':'LLM','ready':False,'error_type':'TimeoutError','seconds':5}]})
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**kw:pytest.fail('No source worker during provider outage'))
    monkeypatch.setattr(module.time,'sleep',lambda _:(_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):module.supervise(run,False)
    assert json.loads((run/'process.json').read_text())['runner_pid'] is None
    assert json.loads((run/'provider-status.json').read_text())['ready'] is False
    assert not (run/'evaluation.sqlite').exists()


@pytest.mark.parametrize('http_status,exit_code',[(401,78),(402,75)])
def test_permanent_startup_rejection_pauses_without_retry(monkeypatch,tmp_path,http_status,exit_code):
    import urllib.error
    module=launcher()
    def request(req,timeout):raise urllib.error.HTTPError(req.full_url,http_status,'Rejected',{},None)
    monkeypatch.setattr('urllib.request.urlopen',request)
    env=local_env();readiness=module.provider_readiness(env)
    assert not readiness['ready'] and readiness['checks'][0]['retryable'] is False
    (tmp_path/'launch.json').write_text(json.dumps({'command':['python','unused']}))
    monkeypatch.setattr(module,'environment',lambda path:env)
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**kw:pytest.fail('No worker on rejected provider request'))
    monkeypatch.setattr(module.time,'sleep',lambda _:pytest.fail('Permanent rejection must not retry'))
    assert module.supervise(tmp_path,False)==exit_code
    assert json.loads((tmp_path/'exit-status.json').read_text())['exit_code']==exit_code


def test_schema_modes_and_token_caps_are_independent_by_stage(monkeypatch):
    monkeypatch.setenv('AMEM_SOURCE_SCHEMA_MODE','json')
    monkeypatch.setenv('AMEM_JUDGE_SCHEMA_MODE','strict')
    monkeypatch.setenv('AMEM_REVIEW_SCHEMA_MODE','portable')
    calls=[]
    schema={'type':'object','additionalProperties':False,'properties':{
        'values':{'type':'array','uniqueItems':True,'items':{'type':'integer'}}},'required':['values']}
    def request(req,timeout):
        calls.append(json.loads(req.data))
        return Response({'model':'mock','choices':[{'message':{'content':'{"values":[1,2]}'}}]})
    monkeypatch.setattr('urllib.request.urlopen',request)
    p=CompletionProcessor('test','http://localhost:19000/v1','mock',max_tokens=4096)
    for stage in ('combined_incremental','judge','binding_maintenance'):
        assert p.complete('Return JSON.',{'stage':stage,'permission_version':'20'},schema=schema)=={'values':[1,2]}
    assert [r['response_format']['type'] for r in calls]==['json_object','json_schema','json_schema']
    assert [r['max_tokens'] for r in calls]==[2048,4096,2048]
    assert 'output_contract' in json.loads(calls[0]['messages'][1]['content'])
    assert 'output_contract' not in json.loads(calls[1]['messages'][1]['content'])
    wire=calls[2]['response_format']['json_schema']['schema']
    assert 'uniqueItems' not in next(iter(wire['properties'].values()))
    def duplicate(req,timeout):
        return Response({'model':'mock','choices':[{'message':{'content':'{"values":[1,1]}'}}]})
    monkeypatch.setattr('urllib.request.urlopen',duplicate)
    with pytest.raises(ModelContractError,match='uniqueItems'):
        p.complete('Return JSON.',{'stage':'binding_maintenance','permission_version':'20'},schema=schema)


def test_portable_projection_preserves_literal_property_names_and_data():
    schema={'properties':{'uniqueItems':{'type':'string'},'data':{
        'const':{'uniqueItems':True},'enum':[{'uniqueItems':False}]},
        'values':{'type':'array','uniqueItems':True}}}
    projected=portable_schema(schema)
    assert projected['properties']['uniqueItems']=={'type':'string'}
    assert projected['properties']['data']==schema['properties']['data']
    assert projected['properties']['values']=={'type':'array'}
    assert schema['properties']['values']['uniqueItems'] is True


def case():
    checkpoint={'checkpoint_id':'seven-item-case','episode_id':'episode-1',
        'query_type':'utility','as_of_turn_id':'turn-1','query_text':'Summarize my plan.',
        'expected_action':'answer','judge_spec':{'include':['Item '+str(i) for i in range(7)]},'leak_targets':[]}
    prediction={'output':{'action':'no_memory','answer':"I don't have that information.",
        'answer_structured':{},'retrieved_memory':[]}}
    verdict={'include_coverage':[{'index':i,'covered':False,'support':'Omitted.'} for i in range(7)],
        'action_ok':False,'notes':'Required items omitted.','utility_ok':False,
        'privacy_leak':None,'deletion_leak':None}
    return checkpoint,prediction,verdict


def scorer(judge,path):
    value=GateMemScorer('/home/thaddus/GateMem',judge)
    value.workflow_version='20';value.repair_path=path
    return value


def test_real_seven_item_contract_repairs_with_specific_persistent_feedback(monkeypatch,tmp_path):
    checkpoint,prediction,verdict=case();calls=[]
    def request(req,timeout):
        body=json.loads(req.data);calls.append(json.loads(body['messages'][1]['content']))
        result={**verdict,'include_coverage':[]} if len(calls)==1 else verdict
        return Response({'model':'mock','choices':[{'message':{'content':json.dumps(result)}}]})
    monkeypatch.setattr('urllib.request.urlopen',request)
    processor=CompletionProcessor('test','http://localhost:19000/v1','mock')
    path=tmp_path/'judge-repair.json'
    with pytest.raises(ModelContractError,match='minItems'):
        scorer(processor,path).score(checkpoint,prediction)
    assert path.exists()
    # Reconstructing the scorer retains the feedback, without changing the answer.
    result=scorer(processor,path).score(checkpoint,prediction)
    assert result['utility_correct'] is False
    assert len(calls)==2 and 'correction' not in calls[0]
    assert 'minItems' in calls[1]['correction']['error']
    assert not path.exists()
    assert prediction['output']['action']=='no_memory'


def test_invalid_judge_verdicts_share_one_durable_outer_retry_budget(tmp_path):
    checkpoint,prediction,verdict=case()
    class InvalidJudge:
        def __init__(self):self.calls=[]
        def complete(self,system,payload,*,schema=None):
            self.calls.append(payload)
            return {**verdict,'include_coverage':verdict['include_coverage'][:1]}
    judge=InvalidJudge();value=scorer(judge,tmp_path/'judge-repair.json')
    retry=RecoveryController(tmp_path,sleep=lambda _:None,progress=lambda *a,**kw:None)
    with pytest.raises(ModelContractError):retry(lambda:value.score(checkpoint,prediction),'judge seven-item-case')
    assert len(judge.calls)==3
    assert all('correction' in p for p in judge.calls[1:])
    # Restarting cannot multiply three attempts into nine or reset exhaustion.
    with pytest.raises(ModelContractError):
        RecoveryController(tmp_path,sleep=lambda _:None)(lambda:value.score(checkpoint,prediction),'judge seven-item-case')
    assert len(judge.calls)==3


def test_v20_contract_pause_preserves_prediction_without_crash_traceback(monkeypatch,tmp_path,capsys):
    from evaluation.gatemem_medical import runner
    from agentic_memory.privacy.processors import HashEmbedder,ScriptedProcessor,ProcessorContractFailure
    class InvalidJudge:
        model='mock-judge'
        def complete(self,*a,**kw):raise ProcessorContractFailure('Bad seven-item verdict')
    monkeypatch.setattr(runner,'processors',lambda offline:(HashEmbedder(),ScriptedProcessor(),InvalidJudge()))
    monkeypatch.setattr(runner,'RecoveryController',lambda path:RecoveryController(path,sleep=lambda _:None,progress=lambda *a,**kw:None))
    run=tmp_path/'run'
    assert runner.main(['--permission-version','20','--offline','--run-dir',str(run),
        '--max-checkpoints','1','--no-evolution'])==76
    output=capsys.readouterr().out
    assert 'paused_contract' in output and 'Traceback' not in output
    summary=json.loads((run/'summary.json').read_text())
    assert summary['coverage']['official_predictions']==1
    assert summary['coverage']['scored_checkpoints']==0
    assert len((run/'official_predictions.jsonl').read_text().splitlines())==1
