"""Offline request-size and host-time comparison; no remote model calls."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from security_tests.test_task_resolution import TaskResolutionTests
from agentic_memory.privacy import permission_state as sliding
from agentic_memory.privacy.permissions import PermissionBootstrap,proposal_schema,compact_input
from agentic_memory.privacy.task_tracking import empty_reviews


def size(value):return len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode())


def main():
    test=TaskResolutionTests();test.setUp();captured=[]
    try:
        c=test.c;test.offer();job=c.ingest('Yes, please. Pickup is at 4 PM; private project code is 1234.')
        c.model.compact_permission_input=True
        original=c.model.complete
        def complete(system,payload,*,schema=None):
            captured.append((system,deepcopy(payload),deepcopy(schema)))
            reviews=empty_reviews(payload);test.atomic_review(payload,reviews)
            return {'revision':payload['revision'],'considered_restrictions':[],'operations':[],'task_reviews':reviews}
        c.model.complete=complete;start=time.monotonic();c.updater.run(job['job_id'])
        host_ms=1000*(time.monotonic()-start)
        system,payload,schema=captured[0]
        baseline=deepcopy(payload)
        for key in ('task_candidates','task_followup','new_assignment_ids'):baseline.pop(key,None)
        baseline['context'].pop('retrieval_results',None);baseline['context']['rounds_remaining']=0
        wire,old_schema,_=compact_input(baseline,proposal_schema(revision=payload['revision'],
            bootstrap=PermissionBootstrap(**payload['bootstrap']),source_ids=[s['id'] for s in payload['sources']]))
        wire,old_schema=sliding.response_contract(wire,old_schema)
        report={'method':'Synthetic scripted processors; same two source turns and current policy snapshot. No live calls.',
                'v11':{'message_bytes':len(sliding.INSTRUCTION.encode())+size(wire),'schema_bytes':size(old_schema)},
                'v12':{'message_bytes':len(system.encode())+size(payload),'schema_bytes':size(schema),
                       'affected_candidates':len(payload['task_candidates']),'calls_for_supported_turn':len(captured),
                       'host_processing_ms_scripted':round(host_ms,3)}}
        try:
            from tokenizers import Tokenizer
            path=next((Path.home()/'.cache/huggingface/hub/models--Qwen--Qwen3.8-27B/snapshots').glob('*/tokenizer.json'))
            tokenizer=Tokenizer.from_file(str(path))
            report['v11']['local_message_tokens']=len(tokenizer.encode(sliding.INSTRUCTION).ids)+len(tokenizer.encode(json.dumps(wire,separators=(',',':'))).ids)
            report['v12']['local_message_tokens']=len(tokenizer.encode(system).ids)+len(tokenizer.encode(json.dumps(payload,separators=(',',':'))).ids)
            report['token_note']='Cached Qwen tokenizer, messages only; excludes chat-template and grammar/compiler overhead.'
        except (ImportError,StopIteration):report['token_note']='Cached tokenizer unavailable; byte counts only.'
        c.model.complete=original;c.model.compact_permission_input=False
        calls=len(c.model.calls);t=time.monotonic()
        for i in range(10):test.apply(c.ingest(f'Unrelated event {i}.',actor='outsider'))
        report['unrelated_turns']={'turns':10,'calls':len(c.model.calls)-calls,
                                  'host_processing_ms_scripted':round(1000*(time.monotonic()-t),3)}
        report['necessary_fact_readable']=any('4 PM' in r['text'] for r in c.selected())
        report['original_runs_modified']=False
        (Path(__file__).parent/'sample-current-acl.json').write_text(json.dumps(c.updater.access_state(),indent=2)+'\n')
        path=Path(__file__).parent/'measurement.json';path.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2))
    finally:test.tearDown()


if __name__=='__main__':main()
