"""Offline v10/v11 request comparison at the same synthetic policy snapshot.

No model calls, network access or existing run mutations. Counts are serialized
UTF-8 bytes, not tokenizer output or latency. Run from the repository root.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))

from security_tests.test_sliding_permissions import SlidingPermissionTests
from agentic_memory.privacy.permission_context import context_view as legacy_view
from agentic_memory.privacy.permissions import INSTRUCTION, CONTRACT_NOTES, compact_input, proposal_schema
from agentic_memory.privacy import permission_state as sliding


def size(value):
    return len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode())


def main():
    case=SlidingPermissionTests();case.setUp()
    try:
        c=case.c;c.establish()
        for hour in range(10,30):
            text=f'Pickup is at {hour} PM.'
            job=c.ingest(text)
            c.apply(job,[c.binding(job['source_id'],spans=[[0,len(text)-1]])])
        case.history(80)
        job=c.ingest('Pickup is at 4 PM.')
        state=c.updater.inspect()
        c.apply(job,[c.binding(job['source_id'])])
        modern=deepcopy(c.model.calls[-1])
        sources={r['id']:json.loads(r['payload']) for r in c.core.db.execute(
            "SELECT id,payload FROM resources WHERE kind='observation' AND state='active' ORDER BY created,id")}
        old_sources,old_state,old_context=legacy_view(sources,state,job['source_id'])
        old_context['rounds_remaining']=3
        modern['coverage_required']=c.updater._coverage_principals(sources,state)
        legacy=deepcopy(modern)
        legacy.update(sources=[{'id':sid,**body} for sid,body in old_sources.items()],
                      permission_state=old_state,context=old_context,contract_notes=deepcopy(CONTRACT_NOTES),
                      binding_authorizations={aid:a['evidence']['information_scope']
                          for aid,a in old_state['assignment'].items() if a['status'] in {'active','proposed'}})
        report={}
        for name,payload,instruction in [('v10',legacy,INSTRUCTION),('v11',modern,sliding.INSTRUCTION)]:
            schema=proposal_schema(revision=payload['revision'],bootstrap=c.bootstrap,
                source_ids=[s['id'] for s in payload['sources']],require_coverage=True,
                allow_context_requests=name=='v10')
            wire,schema,_=compact_input(payload,schema)
            if name=='v11':wire,schema=sliding.response_contract(wire,schema)
            report[name]={'loaded_turns':len(wire['sources']),
                'permission_state_bytes':size(wire['permission_state']),
                'sources_bytes':size(wire['sources']),
                'instruction_bytes':len(instruction.encode()),
                'schema_bytes':size(schema),
                'request_bytes':size({'instruction':instruction,'payload':wire,'schema':schema})}
        report['request_reduction_percent']=round(100*(1-report['v11']['request_bytes']/report['v10']['request_bytes']),1)
        report['live_turns']=len(sources)
        report['retained_bindings']=len(c.updater.inspect()['assignment']['access']['bindings'])
        report['current_acl_bytes']=size(c.updater.access_state())
        report['new_fact_readable']=any('4 PM' in r['text'] for r in c.selected())
        output=Path(__file__).resolve().parent
        (output/'measurement.json').write_text(json.dumps(report,indent=2)+'\n')
        (output/'access-state.sample.json').write_text(json.dumps(c.updater.access_state(),indent=2)+'\n')
        print(json.dumps(report,indent=2))
    finally:case.tearDown()


if __name__=='__main__':main()
