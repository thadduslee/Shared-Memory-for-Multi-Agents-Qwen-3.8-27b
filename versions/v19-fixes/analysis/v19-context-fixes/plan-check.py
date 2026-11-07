"""Bounded local-model check of personal facts, note context and scoped consent."""
from pathlib import Path
import hashlib,json,sys,time
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agentic_memory.privacy import PrivacyMemorySystem
from evaluation.gatemem_medical.runner import processors
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter
OUT=Path(__file__).resolve().parent
embedder,llm,_=processors()
core=PrivacyMemorySystem(OUT/'live-memory.sqlite',embedder)
adapter=InferenceFixtureAdapter(core,{'episode_id':'context-fix-local-check'},
    [embedder.processor_id,llm.processor_id],llm,permission_version='19',progress=lambda s:print(s,flush=True))
adapter.trusted_time({'timestamp':'2026-11-03T00:00:00+00:00'})
observed=[]
adapter.answer_validation_observer=lambda value:observed.append(value)
from agentic_memory.privacy import answer_validation_v19 as validation
original=validation.validate_refs
errors=[]
def trace_refs(claim,evidence):
    try:return original(claim,evidence)
    except Exception as error:
        errors.append({'error':str(error),'claim':claim,'evidence':evidence})
        raise
validation.validate_refs=trace_refs
result=adapter.answer('person-a','What is my current plan?',llm,as_of='t007')
(OUT/'plan-diagnostic.json').write_text(json.dumps({'result':result,'observed':observed,'errors':errors},indent=2)+'\n')
print(json.dumps({'output':result['output'],'errors':errors,'proposal':observed[0]['proposal'] if observed else None},indent=2),flush=True)
core.close()
