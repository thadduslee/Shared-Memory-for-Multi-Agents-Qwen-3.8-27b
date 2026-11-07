"""Regressions for the actual v17 live-run failure and excess repeated work."""
import io
import json
from unittest.mock import patch

import pytest

from security_tests import test_incremental_permissions as fixtures
from agentic_memory.privacy.permissions import PermissionUpdater
from agentic_memory.privacy.processors import CompletionProcessor,ProcessorContractFailure
from agentic_memory.privacy.incremental_wire import compact_operation_variants


def test_compaction_preserves_operation_types_and_required_fields():
    def variant(subject,receipt):return {'type':'object','additionalProperties':False,
        'properties':{'o':{'type':'string','enum':['deny']},'s':{'type':'string','enum':[subject]},
            'e':{'type':'string','enum':[receipt]}},'required':['o','s','e']}
    original=[variant('one','a'),variant('two','b')]
    result=compact_operation_variants(original)
    assert len(result)==1 and result[0]['required']==['o','s','e']
    assert result[0]['properties']['s']['enum']==['one','two']
    assert original[0]['properties']['s']['enum']==['one']


def test_verbose_diagnostics_do_not_quarantine_but_control_values_still_fail(monkeypatch):
    monkeypatch.setenv('AMEM_SOURCE_SCHEMA_MODE','json')
    schema={'type':'object','additionalProperties':False,'properties':{
        'review':{'type':'string','maxLength':20},'c':{'type':'object','properties':{'0':{
            'type':'object','properties':{'r':{'type':'string','maxLength':10}},'required':['r']}},'required':['0']},
        'p':{'type':'object','properties':{'subject':{'type':'string','enum':['owner']}},'required':['subject']}},
        'required':['review','c','p']}
    body={'review':'x'*1000,'c':{'0':{'r':'x'*1000}},'p':{'subject':'owner'}}
    def reply(*a,**k):return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps(body)},'finish_reason':'stop'}]}).encode())
    with patch('urllib.request.urlopen',side_effect=reply):
        processor=CompletionProcessor('test:compact','http://localhost','test')
        result=processor.complete('Follow contract',{'permission_version':'18'},schema=schema)
        assert result['p']==body['p'] and len(result['c']['0']['r'])==10
        body['p']['subject']='unauthorized'
        with pytest.raises(ProcessorContractFailure):processor.complete('Follow contract',{'permission_version':'18'},schema=schema)


def test_invalid_main_binding_is_deferred_without_dropping_controls(monkeypatch):
    monkeypatch.setenv('AMEM_SOURCE_SCHEMA_MODE','json')
    schema={'type':'object','additionalProperties':False,'properties':{
        'p':{'type':'array','items':{'type':'string','enum':['validated-control']}},
        'b':{'type':'array','items':{'type':'object','additionalProperties':False,
            'properties':{'q':{'type':'string','enum':['Exact permitted quote']}},'required':['q']}}},'required':['p','b']}
    body={'p':['validated-control'],'b':[{'q':'invented mismatching quote'}]}
    def reply(*a,**k):return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps(body)},'finish_reason':'stop'}]}).encode())
    with patch('urllib.request.urlopen',side_effect=reply):
        processor=CompletionProcessor('test:compact','http://localhost','test')
        result=processor.complete('Follow contract',{'permission_version':'18'},schema=schema)
        assert result=={'p':['validated-control'],'b':[]}
        body['p']=['unauthorized-control']
        with pytest.raises(ProcessorContractFailure):processor.complete('Follow contract',{'permission_version':'18'},schema=schema)


def test_redundant_grant_reviews_stop_but_resume_when_covering_grant_is_revoked():
    h=fixtures.IncrementalTests();h.setUp()
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='18')
    try:
        h.setup_access();h.classify=lambda p,s:'instruction';h.turn('Pickup Tuesday at 4 PM.')
        first=next(iter(h.updater.inspect()['assignment']))
        h.classify=lambda p,s:'context';h.policy=lambda p:[h.grant(p)]
        before=len(h.model.calls);h.turn('Helper may also read my pickup schedule.')
        assert len(h.model.calls)-before==1
        from agentic_memory.privacy.fact_candidates import metrics
        assert metrics(h.updater)['pending_binding_reviews']==0 and h.selected()
        h.policy=lambda p:[{'op':'revoke_assignment','assignment_id':next(iter(p['permission_state']['assignment'])),
            'authority_source':p['new_source_id'],'evidence':[p['new_source_id']],
            'reason':'Revoke the original permission.'}]
        h.turn('Revoke the original permission.',max_attempts=1)
        assert not h.selected() and metrics(h.updater)['pending_binding_reviews']==1
        assert h.updater.flush_bindings()['status']=='complete'
        assert h.selected()
    finally:h.tearDown()


@pytest.mark.parametrize('held_subject,expected',[('owner','control_hold'),('outsider','complete')])
def test_maintenance_respects_hold_scope_instead_of_poisoning_other_subjects(held_subject,expected):
    h=fixtures.IncrementalTests();h.setUp()
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='18')
    try:
        h.setup_access();h.policy=lambda p:[]
        bad=h.turn('An unresolved event from another participant.',actor='outsider')
        h.core.db.execute("UPDATE permission_jobs SET status='quarantined' WHERE id=?",(bad['job_id'],))
        body=json.dumps({'source_id':bad['source_id'],'actor':'outsider','subject':held_subject,'reason':'Unresolved control.'})
        h.core.db.execute('INSERT INTO permission_control_holds VALUES (?,?,?,?,?,?)',
            ('test-hold','work',bad['job_id'],held_subject,body,'active'))
        h.classify=lambda p,s:'instruction';h.turn('Pickup Tuesday at 4 PM.',max_attempts=1)
        assert not h.selected()
        result=h.updater.flush_bindings()
        assert result['status']==expected
        assert bool(h.selected())==(held_subject!='owner')
        assert h.core.db.execute("SELECT state FROM permission_control_holds").fetchone()[0]=='active'
    finally:h.tearDown()
