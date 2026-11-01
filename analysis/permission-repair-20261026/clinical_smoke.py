"""Focused real-model replay of the initial clinical consultation, without grading inputs."""
import json,os,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.processors import CompletionProcessor,HashEmbedder
from evaluation.gatemem_medical.runner import load_environment
from evaluation.gatemem_medical.input_boundary import source_turn
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter
load_environment(ROOT/'.env')
model=CompletionProcessor('vllm:qwen',os.environ['AMEM_LLM_BASE_URL'],os.getenv('AMEM_LLM_MODEL','Qwen/Qwen3.8-27B'),os.getenv('AMEM_LLM_API_KEY',''),max_tokens=8192)
episode=json.loads(Path('/home/thaddus/GateMem/bench/data/medical/episodes.jsonl').read_text().splitlines()[0])
turns=[source_turn(t) for t in episode['turns'][41:54]]
start=time.monotonic()
with tempfile.TemporaryDirectory() as tmp:
 core=PrivacyMemorySystem(Path(tmp)/'memory.sqlite',HashEmbedder())
 try:
  adapter=InferenceFixtureAdapter(core,{'episode_id':'focused-clinical-consultation'},['test:hash','vllm:qwen'],model,permission_version='5',progress=lambda s:print(s,flush=True))
  for i,t in enumerate(turns):
   adapter.ingest_batch({'boundary':i},[t])
   state=adapter.updater.inspect()
   print('  patient bindings:',sum(len(a['bindings']) for a in state['assignment'].values() if a['participant']=='patient_elena_park'),flush=True)
  texts={}
  for actor in ['patient_elena_park','family_linda_park']:
   session=adapter.query_context({'asker':{'principal_id':actor}})
   texts[actor]=[r['text'] for r in core.search(session,'medications pain nausea stop use',k=30)]
  result={'seconds':time.monotonic()-start,'texts':texts,'ledger':adapter.updater.inspect()}
  (Path(__file__).with_name('clinical-smoke.json')).write_text(json.dumps(result,indent=2)+'\n')
  print(json.dumps({'seconds':result['seconds'],'texts':texts},indent=2),flush=True)
 finally:core.close()
