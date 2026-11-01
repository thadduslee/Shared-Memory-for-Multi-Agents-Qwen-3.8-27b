"""Answer-layer replay from fields already retrieved by the real permission runtime.

This is a focused diagnostic, not an official full-prefix prediction or score.
"""
import json,os,sys
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from evaluation.gatemem_medical.runner import load_environment,answer
from evaluation.gatemem_medical.adapter import visible_query
from evaluation.gatemem_medical.scoring import GateMemScorer
from agentic_memory.privacy.processors import CompletionProcessor
load_environment(ROOT/'.env')
path=Path(__file__).with_name('clinical-smoke.json')
result=json.loads(path.read_text())
texts=result['texts']['patient_elena_park']
if not texts:raise SystemExit('No patient-authorized evidence; do not invoke answer probe.')
checkpoints=[json.loads(s) for s in Path('/home/thaddus/GateMem/bench/data/medical/checkpoints.jsonl').read_text().splitlines()]
checkpoint=next(c for c in checkpoints if '_001_' in c['episode_id'] and c['checkpoint_id'].endswith('_ckpt_01'))
class AuthorizedEvidenceReplay:
 def retrieve(self,ctx,query,**kwargs):
  evidence=[{'id':f'approved:{i}','text':t,'current':True} for i,t in enumerate(texts)]
  return {'evidence':evidence,'input_ids':[r['id'] for r in evidence]}
 def process(self,ctx,processor,ids,prompt,payload,**kwargs):return processor.complete(prompt,payload,**kwargs)
 def release(self,ctx,ids,output):return output
adapter=SimpleNamespace(query_context=lambda visible:SimpleNamespace(principal_id='patient_elena_park'))
model=CompletionProcessor('vllm:qwen',os.environ['AMEM_LLM_BASE_URL'],os.getenv('AMEM_LLM_MODEL','Qwen/Qwen3.8-27B'),os.getenv('AMEM_LLM_API_KEY',''),max_tokens=2048)
judge=CompletionProcessor('judge:independent',os.environ['AMEM_JUDGE_BASE_URL'],os.getenv('AMEM_JUDGE_MODEL','Qwen/Qwen3-14B'),os.getenv('AMEM_JUDGE_API_KEY',''),max_tokens=2048)
prediction=answer(AuthorizedEvidenceReplay(),adapter,visible_query(checkpoint),model,top_k=30)
score=GateMemScorer('/home/thaddus/GateMem',judge).score(checkpoint,prediction)
output={'diagnostic_only':True,'prediction':prediction,'score':score}
Path(__file__).with_name('clinical-answer-probe.json').write_text(json.dumps(output,indent=2)+'\n')
print(json.dumps(output,indent=2),flush=True)
if score.get('utility_correct') is not True:raise SystemExit('Focused medication utility probe failed.')
