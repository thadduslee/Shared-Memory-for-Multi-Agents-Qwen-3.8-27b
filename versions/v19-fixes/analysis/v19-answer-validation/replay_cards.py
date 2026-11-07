"""Retrospective answer-packet replay, not a paid or full-prefix benchmark.

Only the saved authorized answer-call packet/response enter a fresh test store.
No hidden source candidates, roles, assignments, judge specs or verdicts enter
validation. Scripted semantic labels exercise the enforcement protocol only.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile

from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.answering import answer_request
from agentic_memory.privacy.processors import HashEmbedder
from agentic_memory.privacy.answer_validation_v19 import summarize
from security_tests.test_answer_validation_v19 import EvidenceModel, all_rejected


def replay(path):
    data=path.read_bytes();card=json.loads(data)
    call=card['answer_model_calls'][-1]
    packet=call['payload'];saved=call['response']
    with tempfile.TemporaryDirectory() as tmp:
        core=PrivacyMemorySystem(Path(tmp)/'memory.sqlite',HashEmbedder())
        routes=['test:hash','test:answer'];actions=['manage_policy','ingest','read','derive','search']
        host=core.issue_session('host','app',actions=actions,compartments=['replay'],processing_routes=routes)
        reader=core.issue_session('reader','app',actions=['read','search','derive'],compartments=['replay'],processing_routes=routes)
        core.install_policy(host,'packet-only',{'allows':[{'principals':['host','reader'],'actions':actions}],
            'processors':routes})
        try:
            for row in packet['evidence']:
                core.create(host,row['id'],'memory',{'text':row['text']},policy_id='packet-only',compartment='replay')
                core.index(host,row['id'])
            model=EvidenceModel()
            def saved_proposal(payload):
                records={r['id']:r['text'] for r in payload['evidence']}
                parts=[{'id':'r'+str(i),'text':p['request']} for i,p in enumerate(saved['request_parts'])]
                claims=[{'id':'c0','request_ids':[r['id'] for r in parts],'text':saved['answer'],
                    'category':'current_items','evidence':[{'evidence_id':rid,'start':0,'end':len(records[rid]),
                        'quote':records[rid]} for rid in saved['used_record_ids']]}]
                # Also test the original renderer's exact-quote laundering path.
                for i,row in enumerate(payload['evidence'],1):
                    claims.append({'id':'c'+str(i),'request_ids':[r['id'] for r in parts],
                        'text':row['text'],'category':'current_items','evidence':[{
                        'evidence_id':row['id'],'start':0,'end':len(row['text']),'quote':row['text']}]})
                return {'requests':parts,'claims':claims}
            model.propose=saved_proposal
            model.verify=all_rejected  # explicit retrospective non-relevance fixture label
            core.register_processor(host,model)
            adapter=SimpleNamespace(updater=SimpleNamespace(version='19'),query_context=lambda _:reader,
                answer_authorization=lambda *a,**k:{'has_authorized_assignment':True,'access_denied':False},
                review_absence=lambda visible,requests,processor:[False]*len(requests))
            result=answer_request(core,adapter,{'query_text':packet['query'],'as_of_turn_id':packet.get('as_of_turn_id')},model)
            assert result['output']['action']=='no_memory' and not result['output']['retrieved_memory']
            receipt=result['input_manifest']['answer_validation']
            return {'case':path.stem,'card_sha256':hashlib.sha256(data).hexdigest(),
                'saved_authorized_packet_records':len(packet['evidence']),
                'historical_answer_action':saved['action'],'replayed_action':result['output']['action'],
                'call_stages':[p['stage'] for p in model.calls], 'receipt':receipt,
                'limits':'Scripted semantic verdict over saved authorized packet; no real-model quality or changed historical score claim.'}
        finally:core.close()


if __name__=='__main__':
    root=Path('/home/thaddus/a-mem-variant/analysis/v19-v21-evidence-review-20261102/cards/v19')
    rows=[replay(root/(name+'.json')) for name in ('003-05','003-07')]
    report={'replays':rows,'summary':summarize(r['receipt'] for r in rows),
        'real_model_validation':'still needed','paid_benchmark_launched':False}
    Path(__file__).with_name('replay-results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'cases':[{k:r[k] for k in ('case','replayed_action','call_stages')} for r in rows],
        'summary':report['summary']},indent=2))
