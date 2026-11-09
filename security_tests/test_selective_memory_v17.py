"""Causal v17 regressions: usefulness, scoped forgetting and live enforcement."""
import json
import io
from copy import deepcopy
from unittest.mock import patch

import pytest

from security_tests import test_incremental_permissions as fixtures
from agentic_memory.privacy import AccessDenied
from agentic_memory.privacy.permissions import PermissionUpdater
from agentic_memory.privacy import incremental
from agentic_memory.privacy.answering import render_verified_evidence
from agentic_memory.privacy.system import digest


@pytest.fixture(params=['17','18'])
def runtime(request):
    h=fixtures.IncrementalTests();h.setUp()
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version=request.param)
    try:yield h
    finally:h.tearDown()


def erase(h, value, *, timing='now', effective_at=None, condition=None):
    h.classify=lambda p,s:'context'
    def policy(p):
        target=next(r['id'] for r in p['relationship_targets'] if value in r['preview'])
        return [{'op':'erase_candidates','candidate_ids':[target],'values':[value],
            'subject':h.actor(p,'owner'),'authority_source':p['new_source_id'],
            'evidence':[p['new_source_id']],'reason':'Owner requests scoped forgetting.',
            'timing':timing,'effective_at':effective_at,'condition':condition}]
    h.policy=policy
    return h.turn(condition or ('Forget the access code at '+effective_at if timing=='at' else 'Forget the access code now.'))


def test_erasure_preserves_unrelated_readiness_and_blocks_restated_value(runtime):
    h=runtime;h.setup_access();h.classify=lambda p,s:'instruction'
    h.turn('Pickup Tuesday at 4 PM. Use entrance code 4519.')
    reader=h.reader();old=h.core.retrieve(reader,'entrance code')
    before={r['key']:r['snapshot'] for r in h.core.db.execute(
        "SELECT key,snapshot FROM fact_processing WHERE job_id='incremental-reviews:work'")}
    erase(h,'4519')
    assert [r['text'] for r in h.selected()]==['Pickup Tuesday at 4 PM.']
    after={r['key']:r['snapshot'] for r in h.core.db.execute(
        "SELECT key,snapshot FROM fact_processing WHERE job_id='incremental-reviews:work'")}
    assert after and all(before[key]==value for key,value in after.items())
    with pytest.raises(AccessDenied):h.core.release(reader,old['input_ids'],{'answer':'stale code'})
    h.policy=lambda p:[];h.classify=lambda p,s:'instruction'
    h.turn('Deletion confirmed: the previous code was 4519.')
    assert all('4519' not in r['text'] for r in h.selected())


def test_future_forgetting_waits_and_is_enforced_before_sweep(runtime):
    h=runtime;h.setup_access();h.classify=lambda p,s:'instruction'
    h.turn('Pickup Tuesday at 4 PM. Use entrance code 4519.')
    erase(h,'4519',timing='at',effective_at='2026-11-03T18:00:00+00:00')
    assert any('4519' in r['text'] for r in h.selected())
    assert h.core.db.execute("SELECT state FROM scheduled_erasures").fetchone()[0]=='pending'
    h.core.clock=lambda:'2026-11-03T18:00:00+00:00'
    assert all('4519' not in r['text'] for r in h.selected())
    assert h.updater.process_scheduled_forgetting()>0
    assert tuple(h.core.db.execute('SELECT state,body FROM scheduled_erasures').fetchone())==('complete','{}')
    assert [r['text'] for r in h.selected()]==['Pickup Tuesday at 4 PM.']


def test_conditional_forgetting_needs_fresh_authority_completion(runtime):
    h=runtime;h.setup_access();h.classify=lambda p,s:'instruction';h.turn('Use entrance code 4519.')
    erase(h,'4519',timing='conditional',condition='Forget the access code after collection is complete.')
    assert any('4519' in r['text'] for r in h.selected())
    h.policy=lambda p:[{'op':'execute_erasure','erasure_id':p['pending_erasures'][0]['id'],
        'authority_source':p['new_source_id'],'reason':'The owner now confirms completion and immediate erasure.'}]
    h.turn('Collection is complete. Delete the code now.')
    assert not h.selected()
    assert h.core.db.execute('SELECT state FROM scheduled_erasures').fetchone()[0]=='complete'


def test_contact_preference_does_not_invalidate_read_permissions(runtime):
    h=runtime;h.setup_access();h.classify=lambda p,s:'instruction';h.turn('Pickup Tuesday at 4 PM.')
    before={r['key']:r['snapshot'] for r in h.core.db.execute("SELECT key,snapshot FROM fact_processing WHERE job_id='incremental-reviews:work'")}
    h.policy=lambda p:[{'op':'record_restriction','id':p['new_ids']['restriction'][0],
        'subject':h.actor(p,'owner'),'participants':[h.actor(p,'helper')],
        'information_scope':'Use portal messages for outreach','actions':['read','search','derive'],
        'effect':'deny','kind':'communication','authority_source':p['new_source_id'],
        'evidence':[p['new_source_id']],'blocked_assignments':[],'reason':'This is a channel preference.'}]
    h.turn('Use portal messages instead of phone calls.')
    assert not h.updater.inspect()['restriction']
    assert any('Tuesday' in r['text'] for r in h.selected())
    after={r['key']:r['snapshot'] for r in h.core.db.execute("SELECT key,snapshot FROM fact_processing WHERE job_id='incremental-reviews:work'")}
    assert all(after.get(key)==value for key,value in before.items())


def test_retained_reviews_never_bypass_subsequent_revocation(runtime):
    h=runtime;h.setup_access();h.classify=lambda p,s:'instruction'
    h.turn('Pickup Tuesday at 4 PM. Use entrance code 4519.')
    erase(h,'4519');reader=h.reader();packet=h.core.retrieve(reader,'pickup')
    h.policy=lambda p:[{'op':'revoke_assignment','assignment_id':next(iter(p['permission_state']['assignment'])),
        'authority_source':p['new_source_id'],'evidence':[p['new_source_id']],'reason':'Owner revokes this reader.'}]
    h.turn('Helper may no longer read my pickup information.')
    assert not h.selected()
    with pytest.raises(AccessDenied):h.core.release(reader,packet['input_ids'],{'answer':'stale appointment'})


def test_scalar_measurements_survive_forgetting_without_source_text(runtime):
    h=runtime;h.setup_access();h.classify=lambda p,s:'instruction';h.turn('Use entrance code 4519.')
    h.core.db.execute('INSERT INTO fact_attempts VALUES (?,?,?,?,?)',
        ('sample','combined_incremental','metrics',1,json.dumps({'elapsed_s':3.,'model_elapsed_s':2.,
         'response':{'secret':'4519'},'usage':{'prompt_tokens':100,'completion_tokens':20}})))
    from agentic_memory.privacy.fact_candidates import scrub_work
    scrub_work(h.core)
    body=h.core.db.execute("SELECT body FROM fact_attempts WHERE job_id='sample'").fetchone()[0]
    assert '4519' not in body and json.loads(body)['elapsed_s']==3.
    assert json.loads(body)['usage']['prompt_tokens']==100


def test_fingerprint_unchanged_and_does_not_copy_binding_history():
    candidate={'id':'fact','quote':'Appointment Tuesday at 4 PM.'}
    assignment={'participant':'helper','subject':'owner','actions':['read'],'evidence':{},
        'bindings':[{'source_id':str(i),'spans':[[0,10]]} for i in range(10000)]}
    state={'activity':{},'restriction':{}}
    stripped={k:deepcopy(v) for k,v in assignment.items() if k!='bindings'}
    expected=digest([candidate,stripped,None,{}])
    copied=[]
    original=deepcopy
    with patch.object(incremental,'deepcopy',side_effect=lambda value:(copied.append(value),original(value))[1]):
        assert incremental.pair_key(candidate,assignment,state)==expected
    assert all(not isinstance(v,dict) or 'bindings' not in v for v in copied)


def test_answer_keeps_selected_portal_instruction_and_removes_guessed_prose():
    evidence=[{'id':'callback','text':'Use the updated callback route with generic voicemail only.'},
        {'id':'portal','text':'Use portal messaging for detailed results.'}]
    output={'answer':'A guessed private diagnosis.', 'action':'answer',
        'evidence_roles':{'0':'current','1':'condition'},'used_record_ids':['callback','portal']}
    parts=[{'status':'supported','evidence_ids':['callback','portal']}]
    result=render_verified_evidence(output,evidence,parts)
    assert 'portal messaging for detailed results' in result['answer']
    assert 'guessed private diagnosis' not in result['answer']
    assert set(result['used_record_ids'])=={'callback','portal'}


def test_responsive_question_can_grant_worker_without_becoming_a_fact(runtime):
    h=runtime;h.turn("I'm Sam. I can help with machine startup.",actor='helper')
    h.policy=lambda p:[h.grant(p)]
    complete=h.model.complete
    def response(system,p,*,schema=None):
        result=complete(system,p,schema=schema)
        for op in result.get('p',[]):
            if op['o']=='fields':
                worker=op['i'];keep={k:op[k] for k in ('p','s','e','r','v')}
                op.clear();op.update(o='activity',via='response',i=p['new_ids']['grant'][1],wi=worker,
                    q=p['sources'][0]['id'],work='machine startup assistance',wf=['startup safety instructions'],wv={},**keep)
        return result
    h.model.complete=response;h.classify=lambda p,s:'question'
    h.turn('Sam, can I start it with the cover removed?')
    grants=h.updater.inspect()['assignment']
    assert len(grants)==2 and len({a['id'] for a in grants.values()})==2
    worker=next(a for a in grants.values() if a['participant']=='helper')
    assert worker['status']=='active' and 'ONLY these necessary information fields' in worker['information_scope']
    assert not h.updater.fact_candidates()
    h.policy=lambda p:[];h.classify=lambda p,s:'instruction';h.turn('Keep the cover installed during startup.',actor='helper')
    assert any('cover installed' in r['text'] for r in h.selected())


def test_selected_service_intent_cannot_silently_omit_worker_grant(runtime):
    h=runtime;h.turn('May I help with pickup?',actor='helper')
    original=h.model.handler
    def omit(p):
        from agentic_memory.privacy.incremental_wire import encode_fixture
        result=encode_fixture(original(p),p)
        result['c']={'0':{'intents':['service'],'recipient':h.actor(p,'helper'),
                         'existing':None,'r':'The subject accepts this worker service.'}}
        return result
    h.model.handler=omit
    from agentic_memory.privacy.permissions import PermissionRepairExhausted
    with pytest.raises(PermissionRepairExhausted):h.turn('Yes, please help with pickup.')
    assert not h.updater.inspect()['assignment']
    assert not h.selected()


def test_unrelated_actor_cannot_execute_pending_forgetting(runtime):
    h=runtime;h.setup_access();h.classify=lambda p,s:'instruction';h.turn('Use entrance code 4519.')
    erase(h,'4519',timing='conditional',condition='Forget the code when I confirm collection is complete.')
    h.policy=lambda p:[{'op':'execute_erasure','erasure_id':p['pending_erasures'][0]['id'],
        'authority_source':p['new_source_id'],'reason':'Claimed completion by an unrelated actor.'}]
    from agentic_memory.privacy.permissions import PermissionRepairExhausted
    with pytest.raises(PermissionRepairExhausted):h.turn('Collection is complete. Erase the code.',actor='stranger')
    assert h.core.db.execute('SELECT count(*) FROM source_erasures').fetchone()[0]==0
    assert h.core.db.execute('SELECT state FROM scheduled_erasures').fetchone()[0]=='pending'


def test_v17_json_transport_uses_the_same_local_grammar(monkeypatch):
    from agentic_memory.privacy.processors import CompletionProcessor, ProcessorContractFailure
    monkeypatch.setenv('AMEM_SOURCE_SCHEMA_MODE','json')
    schema={'type':'object','additionalProperties':False,'properties':{'f':{'type':'object',
        'additionalProperties':False,'properties':{'0':{'type':'object','additionalProperties':False,
            'properties':{'k':{'type':'string','enum':['question']}},'required':['k']}},'required':['0']}},'required':['f']}
    received=[]
    def reply(request,**kwargs):
        received.append(json.loads(request.data))
        return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps({'f':{'0':{
            'k':'question','s':None,'e':None,'t':[],'x':[]}}})},'finish_reason':'stop'}]}).encode())
    with patch('urllib.request.urlopen',side_effect=reply):
        result=CompletionProcessor('test:http','http://localhost','test').complete('Return the contract.',
            {'permission_version':'17'},schema=schema)
    assert result=={'f':{'0':{'k':'question'}}}
    assert received[0]['response_format']=={'type':'json_object'}
    assert 'output_contract' in json.loads(received[0]['messages'][1]['content'])
    def invalid(request,**kwargs):
        return io.BytesIO(json.dumps({'choices':[{'message':{'content':'{"f":{"0":{"k":"grant"}}}'},
            'finish_reason':'stop'}]}).encode())
    with patch('urllib.request.urlopen',side_effect=invalid),pytest.raises(ProcessorContractFailure):
        CompletionProcessor('test:http','http://localhost','test').complete('Return the contract.',
            {'permission_version':'17'},schema=schema)


def test_provider_preference_preserves_required_parameters(monkeypatch):
    from agentic_memory.privacy.processors import openrouter_routing_preferences
    monkeypatch.setenv('AMEM_OPENROUTER_PROVIDER_ORDER','ModelRun, ModelRun')
    settings=openrouter_routing_preferences()
    assert settings['order']==['ModelRun']
    assert settings['allow_fallbacks'] is True and settings['require_parameters'] is True


def test_service_context_does_not_confer_control_authority():
    query={'permission_version':'17','bootstrap':{'authorities':{
        'owner':{'permission':['owner'],'restrict':['owner']},
        'worker':{'permission':['worker'],'restrict':['worker']}}},
        'permission_state':{'assignment':{'service':{'participant':'worker','subject':'owner',
            'status':'active','authorization_context':[]}},'restriction':{},'activity':{}},'authorization_sources':[]}
    view=incremental.main_query_view(query,'worker')
    assert view['service_context']['service']['information_subject']=='owner'
    assert not view['permission_state']['assignment']
    assert view['current_control_subjects']==['worker']


def test_v17_maintenance_drains_pending_pairs_with_original_authority(runtime):
    h=runtime;h.turn('May I arrange pickup?',actor='helper')
    h.classify=lambda p,s:'instruction'
    h.turn('Pickup Tuesday at 4 PM. Bring the blue permit.')
    h.classify=lambda p,s:'context';h.policy=lambda p:[h.grant(p)]
    # Simulate a source call whose repair budget left no focused review slot.
    h.turn('Helper may read my pickup instructions.',max_attempts=1)
    assert not h.selected()
    from agentic_memory.privacy.fact_candidates import metrics
    assert metrics(h.updater)['pending_binding_reviews']==2
    before=len(h.model.calls);grants=deepcopy(h.updater.inspect()['assignment'])
    with patch.object(h.updater.learner,'propose',wraps=h.updater.learner.propose) as propose:
        result=h.updater.flush_bindings(max_batches=1)
    assert result['status']=='complete' and result['batches']==1
    call=h.model.calls[before]
    assert call['permission_version']==h.updater.version and call['stage']=='binding_maintenance'
    assert all(row['assignment']['authorization_context'] for row in call['pair_context'])
    assert set(propose.call_args.args[0]) >= h.updater._evidence(next(iter(grants.values())))
    assert len(h.selected())==2 and metrics(h.updater)['pending_binding_reviews']==0
    assert set(h.updater.inspect()['assignment'])==set(grants)
    assert json.loads(h.core.db.execute("SELECT value FROM privacy_metadata WHERE key='binding-readiness:work'").fetchone()[0])=={'pending':0}


def test_fact_rows_require_complete_attribution_instead_of_a_short_branch(runtime):
    h=runtime;original=h.model.complete;seen=[]
    def broken(system,p,*,schema=None):
        result=original(system,p,schema=schema)
        if p.get('segments'):
            import jsonschema
            slot=str(p['segments'][0]['index']);row=schema['properties']['f']['properties'][slot]
            seen.append(row)
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate({'kind':'instruction'},row)
        return result
    h.model.complete=broken
    h.turn('A source introduction.')
    assert seen and all('anyOf' not in row for row in seen)
