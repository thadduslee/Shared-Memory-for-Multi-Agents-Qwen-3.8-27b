"""Local deadline, partial-answer and pre-retrieval privacy checks."""
from copy import deepcopy

import pytest

from agentic_memory.privacy import PermissionQueryService
from agentic_memory.privacy.processors import ProcessorDeadlineFailure
from security_tests.test_workflow_v20 import owned


class AnswerModel:
    processor_id='test:answer'
    def __init__(self):
        self.calls=[]
        self.comparison=False
        self.permitted=False
        self.subjects=[]
        self.missing=False
        self.timeout_stage=None
        self.clock=None

    def complete(self,system,payload,*,schema=None):
        self.calls.append(deepcopy(payload))
        if payload['stage']==self.timeout_stage:raise ProcessorDeadlineFailure('mock deadline')
        if self.clock is not None:self.clock[0]+=1.
        if payload['stage']=='query_policy':
            return {'sensitive_comparison':self.comparison,'permitted':self.permitted,'subjects':self.subjects}
        if payload['stage']=='policy_classification':
            return {'decisions':{str(i):False for i in range(len(payload['requests']))}}
        assert payload['stage']=='answer'
        evidence=payload['evidence']
        ids=[r['id'] for r in evidence]
        parts=[{'request':'the approved plan','status':'supported','evidence_ids':ids}]
        if self.missing:parts.append({'request':'the unavailable part','status':'out_of_scope','evidence_ids':[]})
        return {'evidence_roles':{str(i):'current' for i in range(len(evidence))},
            'answer_structured':{'current_items':[r['text'] for r in evidence],
                'continuing_items':[],'retired_items':[],'conditions_and_exceptions':[]},
            'request_parts':parts,'action':'answer','answer':'Unverified prose must not survive.',
            'used_record_ids':ids}


def configure(h):
    model=AnswerModel()
    h.core.register_processor(h.host,model)
    return PermissionQueryService(h.updater),model


def test_partial_supported_answer_corrected_without_second_prose_call(owned):
    h=owned;h.classify=lambda p,s:'instruction'
    h.turn('Continue the project preparation; the old Monday meeting was canceled.')
    service,model=configure(h);model.missing=True
    result=service.answer('owner','What is my current plan and the other information?',model)
    assert result['output']['action']=='answer'
    assert 'project preparation' in result['output']['answer']
    assert "don't have the remaining" in result['output']['answer']
    assert 'Unverified prose' not in result['output']['answer']
    assert [p['stage'] for p in model.calls]==['query_policy','answer','policy_classification']


@pytest.mark.parametrize('question',[
    'Am I the Ruiz with the Wednesday blood pressure nurse visit?',
    'Is Lila in a behavioral program or a normal primary care check?',
    'Is Lila the Chen with the dermatology biopsy, or the other Chen?',
    'Is Maya the Rivera for kidney follow-up, or the orthopedics Rivera?',
])
def test_recorded_sensitive_comparisons_declined_before_any_answer_context(owned,question,monkeypatch):
    h=owned;h.turn('Hello.',actor='helper');h.classify=lambda p,s:'instruction'
    h.turn('My confidential appointment is on Wednesday.')
    service,model=configure(h)
    model.comparison=True;model.subjects=['owner','helper'];model.permitted=True
    def no_retrieval(*args,**kwargs):pytest.fail('Comparison leaked into retrieval')
    monkeypatch.setattr(h.core,'retrieve',no_retrieval)
    result=service.answer('owner',question,model)
    assert result['output']['action']=='refuse'
    assert result['output']['retrieved_memory']==[]
    assert len(model.calls)==1
    assert 'evidence' not in model.calls[0]
    assert 'confidential appointment' not in str(model.calls[0])


def test_pending_wait_is_bounded_and_kept_separate_from_answer_time(owned,monkeypatch):
    h=owned;h.classify=lambda p,s:'instruction';h.turn('The meeting is Friday at four.')
    service,model=configure(h)
    original=service.processing_metrics
    monkeypatch.setattr(service,'processing_metrics',lambda:{**original(),'pending_binding_reviews':1})
    clock=[100.]
    monkeypatch.setattr('time.monotonic',lambda:clock[0])
    model.clock=clock
    calls=[]
    def pending(**kwargs):
        calls.append(kwargs);clock[0]+=kwargs['seconds']
        return {'status':'pending','changes':[]}
    monkeypatch.setattr(h.updater,'service_pending',pending)
    result=service.answer('owner','What is my meeting plan?',model)
    assert calls[0]['seconds']==5.
    assert calls[0]['controls'] is False
    assert result['input_manifest']['query_timing']['pending_wait_seconds']==5.
    assert result['input_manifest']['query_timing']['seconds']<=20.
    assert result['output']['action']=='answer'
    assert not hasattr(model,'_deadline')


def test_slow_pending_review_does_not_turn_approved_answer_into_refusal(owned,monkeypatch):
    h=owned;h.classify=lambda p,s:'instruction';h.turn('The meeting is Friday at four.')
    service,model=configure(h)
    original=service.processing_metrics
    monkeypatch.setattr(service,'processing_metrics',lambda:{**original(),'pending_binding_reviews':1})
    def delayed(**kwargs):raise ProcessorDeadlineFailure('review still pending')
    monkeypatch.setattr(h.updater,'service_pending',delayed)
    assert service.answer('owner','What is my meeting plan?',model)['output']['action']=='answer'


def test_answer_timeout_returns_no_hidden_existence_or_access_claim(owned):
    h=owned;h.classify=lambda p,s:'instruction';h.turn('My private appointment is Wednesday.')
    service,model=configure(h);model.timeout_stage='query_policy'
    result=service.answer('owner','What appointment is mine?',model)
    assert result['output']['action']=='no_memory'
    assert result['output']['retrieved_memory']==[]
    assert result['input_manifest']['processing']['answer_deadline_exceeded'] is True
    assert 'appointment' not in result['output']['answer']
