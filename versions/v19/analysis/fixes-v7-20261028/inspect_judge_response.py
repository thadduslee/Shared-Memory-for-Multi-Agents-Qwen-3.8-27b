"""Capture one judge response to diagnose structured-output truncation."""
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from evaluation.gatemem_medical.runner import load_environment
from evaluation.gatemem_medical.scoring import GateMemScorer
import agentic_memory.privacy.processors as processors


def main():
    os.environ['NO_PROXY']='127.0.0.1,localhost';os.environ['no_proxy']='127.0.0.1,localhost'
    load_environment(ROOT/'.env')
    judge=processors.CompletionProcessor('judge:inspection',os.environ['AMEM_JUDGE_BASE_URL'],
        os.getenv('AMEM_JUDGE_MODEL','Qwen/Qwen3-14B'),os.getenv('AMEM_JUDGE_API_KEY',''),max_tokens=2048)
    source=ROOT/'runs/gatemem-medical-raw-stream-v6'
    prediction=next(x for x in map(json.loads,(source/'official_predictions.jsonl').read_text().splitlines()) if x['checkpoint_id'].endswith('_ckpt_02'))
    checkpoint=next(x for x in map(json.loads,Path('/home/thaddus/GateMem/bench/data/medical/checkpoints.jsonl').read_text().splitlines()) if x['checkpoint_id']==prediction['checkpoint_id'])
    class Captured(RuntimeError):pass
    def capture(text):
        out=Path(__file__).resolve().parent/'judge-response.txt'
        out.write_text(text);out.chmod(0o600)
        print(json.dumps({'chars':len(text),'start':text[:140],'end':text[-200:],'usage':judge.usage}),flush=True)
        raise Captured()
    processors.parse_object=capture
    try:GateMemScorer('/home/thaddus/GateMem',judge).score(checkpoint,prediction)
    except Captured:pass


if __name__=='__main__':main()
