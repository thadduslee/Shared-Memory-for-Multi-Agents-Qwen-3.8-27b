"""Exercise real localhost judge contracts before a long evaluation starts.

These are provider compatibility probes, never candidate evaluation scores.
No checkpoint annotations are passed to the memory engine.
"""
import argparse
import json
import os
from pathlib import Path
import time

from agentic_memory.privacy.processors import CompletionProcessor
from evaluation.gatemem_medical.adapter import load_data
from evaluation.gatemem_medical.scoring import GateMemScorer


def main(output):
    os.environ['AMEM_JUDGE_SCHEMA_MODE']='strict'
    _,checkpoints=load_data(Path('/home/thaddus/GateMem/bench/data/medical'))
    utility=max((c for c in checkpoints if c['query_type']=='utility'),
                key=lambda c:len((c.get('judge_spec') or {}).get('include') or []))
    cases=[utility]+[next(c for c in checkpoints if c['query_type']==kind) for kind in ('privacy','safety')]
    judge=CompletionProcessor('judge:independent','http://localhost:19000/v1','qwen3.6:35b',timeout=60,max_tokens=4096)
    scorer=GateMemScorer('/home/thaddus/GateMem',judge);scorer.workflow_version='20'
    report={'purpose':'Provider compatibility probes, not candidate scores',
            'model':judge.model,'base_url':judge.base_url,'results':[],'passed':False}
    for case in cases:
        prediction={'output':{'action':'no_memory','answer':"I don't have that information.",
            'answer_structured':{},'retrieved_memory':[]}}
        started=time.monotonic()
        row={'query_type':case['query_type'],'checkpoint_id':case['checkpoint_id'],
             'required_items':len((case.get('judge_spec') or {}).get('include') or [])}
        try:
            score=scorer.score(case,prediction)
            row.update(valid=True,verdict=score['judge'],usage=judge.usage,
                       effective_limits=judge.last_effective_limits)
            row['passed']=(score['judge']['utility_ok'] is False if case['query_type']=='utility'
                else score['judge']['privacy_leak'] is False if case['query_type']=='privacy'
                else score['judge']['deletion_leak'] is False)
        except Exception as error:
            row.update(valid=False,passed=False,error=str(error),details=getattr(error,'details',{}))
        row['seconds']=time.monotonic()-started
        report['results'].append(row)
        output.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:row[k] for k in ('query_type','required_items','valid','passed','seconds')}),flush=True)
    report['passed']=all(r['passed'] for r in report['results'])
    output.write_text(json.dumps(report,indent=2)+'\n')
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    raise SystemExit(main(parser.parse_args().output))
