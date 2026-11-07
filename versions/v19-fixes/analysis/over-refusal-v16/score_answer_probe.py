"""Score a finished answer-only probe; evaluator annotations never enter answering."""
import json
import argparse
import os
from pathlib import Path
from agentic_memory.privacy.processors import CompletionProcessor
from evaluation.gatemem_medical import openrouter,runner
from evaluation.gatemem_medical.scoring import GateMemScorer

b=Path(__file__).resolve().parent
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--candidate',default='candidate-13');p.add_argument('--prefix',default='v14')
args=p.parse_args()
result=json.loads((b/f'{args.prefix}-answer-probe-result.json').read_text())
prediction=next(p for p in map(json.loads,(b/args.candidate/'official_predictions.jsonl').read_text().splitlines())
    if p['checkpoint_id'].endswith('_02'))
context=prediction['output']['retrieved_memory']
prediction['output']={k:result['output'][k] for k in ['action','answer','answer_structured','used_record_ids']}
prediction['output']['retrieved_memory']=context
_,checkpoints=runner.load_data(Path('/home/thaddus/GateMem/bench/data/medical'))
checkpoint=next(c for c in checkpoints if c['checkpoint_id']==prediction['checkpoint_id'])
openrouter.configure(runner.ROOT/'.env')
judge=CompletionProcessor('judge',openrouter.BASE_URL,openrouter.JUDGE_MODEL,os.environ['AMEM_OPENROUTER_API_KEY'])
score=GateMemScorer('/home/thaddus/GateMem',judge).score(checkpoint,prediction)
(b/f'{args.prefix}-answer-probe-score.json').write_text(json.dumps(score,indent=2)+'\n')
print(json.dumps(score),flush=True)
