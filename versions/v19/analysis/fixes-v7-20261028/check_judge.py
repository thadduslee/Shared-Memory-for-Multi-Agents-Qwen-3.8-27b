"""Review selected saved v6 failures with the revised independent judge only."""
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from evaluation.gatemem_medical.runner import load_environment
from evaluation.gatemem_medical.scoring import GateMemScorer
from agentic_memory.privacy.processors import CompletionProcessor


def main():
    os.environ['NO_PROXY']='127.0.0.1,localhost';os.environ['no_proxy']='127.0.0.1,localhost'
    load_environment(ROOT/'.env')
    judge=CompletionProcessor('judge:diagnostic',os.environ['AMEM_JUDGE_BASE_URL'],
        os.getenv('AMEM_JUDGE_MODEL','Qwen/Qwen3-14B'),os.getenv('AMEM_JUDGE_API_KEY',''),max_tokens=2048)
    scorer=GateMemScorer('/home/thaddus/GateMem',judge)
    source=ROOT/'runs/gatemem-medical-raw-stream-v6'
    predictions={x['checkpoint_id']:x for x in map(json.loads,(source/'official_predictions.jsonl').read_text().splitlines())}
    checkpoints={x['checkpoint_id']:x for x in map(json.loads,Path('/home/thaddus/GateMem/bench/data/medical/checkpoints.jsonl').read_text().splitlines())}
    results=[]
    for suffix in ['02','03','06','12']:
        cid=next(k for k in predictions if k.endswith('_ckpt_'+suffix))
        result=scorer.score(checkpoints[cid],predictions[cid])
        results.append(result)
        print(suffix,json.dumps({k:result.get(k) for k in ['utility_correct','privacy_context_leak','judge']}),flush=True)
    out=Path(__file__).resolve().parent/'judge-review.jsonl'
    out.write_text(''.join(json.dumps(r)+'\n' for r in results));out.chmod(0o600)
    assert all(r['utility_correct'] is False for r in results[:3]), 'Judge still accepts an incomplete saved answer'
    assert results[-1]['privacy_context_leak'] is False, 'Nondisclosure wording still flagged as context disclosure'


if __name__=='__main__':main()
