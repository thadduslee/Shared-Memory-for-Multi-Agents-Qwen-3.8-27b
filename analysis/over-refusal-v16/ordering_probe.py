"""Read-only output-order experiment using an actual synthetic source prefix."""
from copy import deepcopy
import json
import os
from pathlib import Path

from agentic_memory.privacy.processors import CompletionProcessor
from evaluation.gatemem_medical import openrouter,runner

BASE=Path('analysis/over-refusal-v16')
trace=[json.loads(l) for l in (BASE/'candidate-09/model-trace.jsonl').read_text().splitlines()]
replay=iter(trace)
os.environ.setdefault('HF_HUB_OFFLINE','1')
os.environ.setdefault('CUDA_VISIBLE_DEVICES','')
openrouter.configure(runner.ROOT/'.env')
original=runner.processors

class Probe(CompletionProcessor):
    def complete(self,system,payload,*,schema=None):
        source=next((s for s in payload.get('sources',[]) if s['id']==payload.get('new_source_id')),None)
        if source and source['source']['event_id']=='t018' and payload['stage']=='combined_incremental':
            report={}
            for name,order in [('review_first',('review','c','p','f','b'))]:
                schema=deepcopy(schema)
                schema['properties']['review']={'type':'string','minLength':1,'maxLength':320}
                ordered={**schema,'properties':{k:schema['properties'][k] for k in order},'required':list(order)}
                prompt=system
                prompt+='\nFIRST emit review: identify the CURRENT speaker, whether their event requests/accepts help or RESPONDS to a particular worker task, and that actual worker. Assess this recipient independently: does their existing grant cover this activity, or does the current response support a narrow response grant? A factual reply to a worker task question authorizes that actual worker to read the necessary task fields under the configured convention. The subject self grant and other workers grants cannot substitute for this worker grant. Then complete c,p,f,b.'
                response=super().complete(prompt,payload,schema=ordered)
                report[name]={'response':response,'usage':self.usage,'seconds':self.last_elapsed_seconds}
            (BASE/'review-probe-result.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps(report),flush=True)
            raise SystemExit(0)
        expected=next(replay)
        if expected['payload']!=json.loads(json.dumps(payload)):
            print('DIFFERENCES',[(k,expected['payload'].get(k),payload.get(k)) for k in
                set(expected['payload'])|set(payload) if expected['payload'].get(k)!=payload.get(k)],flush=True)
            raise RuntimeError('Replay diverged before the selected source')
        return deepcopy(expected['response'])

def processors(offline=False):
    embedding,llm,judge=original(offline)
    return embedding,Probe(llm.processor_id,llm.base_url,llm.model,llm.api_key,
        timeout=llm.timeout,max_tokens=llm.max_tokens),judge

runner.processors=processors
runner.main(['--run-dir',str(BASE/'ordering-probe-03'),'--permission-version','16','--max-checkpoints','1'])
