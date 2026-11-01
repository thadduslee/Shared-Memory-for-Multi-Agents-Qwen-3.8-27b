"""Causal-prefix medical probe; evaluator annotations enter scoring only."""
import argparse
import json
import os
from pathlib import Path
import statistics
import time

from live_probe import RecordedCompletion
from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.processors import LocalEmbedder
from evaluation.gatemem_medical.adapter import load_data,visible_query
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter
from evaluation.gatemem_medical.input_boundary import episode_header,source_turn
from evaluation.gatemem_medical.runner import answer
from evaluation.gatemem_medical.scoring import GateMemScorer


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--turns',type=int,default=54)
    args=p.parse_args()
    if args.output.exists():p.error('Use a fresh output directory')
    args.output.mkdir(parents=True,mode=0o700)
    episodes,checkpoints=load_data('/home/thaddus/GateMem/bench/data/medical')
    episode=sorted(episodes,key=lambda e:e['episode_id'])[0]
    model=RecordedCompletion('live:qwen','http://127.0.0.1:38715/v1','Qwen/Qwen3.8-27B','dummy')
    model.records=[];model.trace=args.output/'model-trace.jsonl'
    embedder=LocalEmbedder(os.getenv('AMEM_EMBEDDING_PATH') or None)
    core=PrivacyMemorySystem(args.output/'memory.sqlite',embedder)
    adapter=InferenceFixtureAdapter(core,episode_header(episode),[embedder.processor_id,model.processor_id],model)
    core.register_processor(adapter.service,model)
    report={'model':model.model,'precision':'fp8-online','official_benchmark':False,
        'initial_access_empty':not any(adapter.updater.access_state().values()),'turns':[],'predictions':[]}
    def save():(args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    try:
        selected=episode['turns'][:args.turns]
        for i,turn in enumerate(selected,1):
            before=len(model.records);started=time.perf_counter()
            adapter.ingest_batch({'boundary':i},[source_turn(turn)])
            row={'turn_id':turn['turn_id'],'seconds':time.perf_counter()-started,'calls':len(model.records)-before}
            report['turns'].append(row);save();print(row,flush=True)
        last={t['turn_id'] for t in selected}
        cases=[c for c in checkpoints if c['episode_id']==episode['episode_id'] and c['as_of_turn_id']==selected[-1]['turn_id']]
        scorer=GateMemScorer('/home/thaddus/GateMem')
        for case in cases:
            prediction=answer(core,adapter,visible_query(case),model)
            report['predictions'].append({'prediction':prediction,'auxiliary_score':scorer.score(case,prediction)})
        times=[r['seconds'] for r in report['turns']]
        report.update(mean_seconds=statistics.mean(times),median_seconds=statistics.median(times),
            max_seconds=max(times),permission_jobs={r[0]:r[1] for r in core.db.execute('SELECT status,count(*) FROM permission_jobs GROUP BY status')},
            processing=adapter.processing_metrics())
        save();print(json.dumps({k:v for k,v in report.items() if k!='turns'},indent=2))
    finally:save();core.close()


if __name__=='__main__':main()
