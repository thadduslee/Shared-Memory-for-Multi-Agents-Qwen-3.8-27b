"""Recorded causal evaluation; hidden annotations are used only by the scorer.

python -m analysis.over-refusal-v16.run is not a valid module name; execute this
file with PYTHONPATH=. and the repository's ML environment instead.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import time
import signal
import threading

from agentic_memory.privacy.processors import CompletionProcessor
from evaluation.gatemem_medical import openrouter, runner


class RecordedCompletion(CompletionProcessor):
    def complete(self, system, payload, *, schema=None):
        started=time.monotonic()
        record={'stage':payload.get('stage','query'),'system':system,'payload':deepcopy(payload)}
        try:
            result=super().complete(system,payload,schema=schema)
            record['response']=deepcopy(result)
            return result
        except Exception as exc:
            record['error']={'type':type(exc).__name__,'reason':str(exc)}
            raise
        finally:
            record.update(seconds=time.monotonic()-started,usage=deepcopy(self.usage),
                          response_model=self.last_response_model,provider=self.last_provider,
                          wire_key_ordering=self.last_wire_key_ordering)
            with self.trace.open('a') as out:out.write(json.dumps(record)+'\n')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',type=Path,required=True)
    p.add_argument('--checkpoints',type=int,default=25)
    p.add_argument('--full',action='store_true',help='Run all 579 checkpoints and the complete source schedule, including data tails')
    p.add_argument('--boundaries',type=int)
    p.add_argument('--version',default='16')
    p.add_argument('--resume',action='store_true')
    p.add_argument('--stop-on-failure',action='store_true')
    p.add_argument('--stop-on-leak',action='store_true')
    args=p.parse_args()
    if args.full:args.checkpoints=579
    os.umask(0o077)
    os.environ.setdefault('HF_HUB_OFFLINE','1')
    os.environ.setdefault('CUDA_VISIBLE_DEVICES','')
    openrouter.configure(runner.ROOT/'.env')
    original=runner.processors
    def processors(offline=False):
        embedding,llm,judge=original(offline)
        model=RecordedCompletion(llm.processor_id,llm.base_url,llm.model,llm.api_key,
                                 timeout=llm.timeout,max_tokens=llm.max_tokens)
        model.trace=args.run_dir/'model-trace.jsonl'
        return embedding,model,judge
    runner.processors=processors
    command=['--run-dir',str(args.run_dir),'--permission-version',args.version]
    if not args.full:command+=['--max-checkpoints',str(args.checkpoints)]
    if args.boundaries is not None:command+=['--max-boundaries',str(args.boundaries)]
    if args.resume:command+=['--resume']
    if args.stop_on_failure or args.stop_on_leak:
        def monitor():
            while True:
                try:
                    scores=[json.loads(l) for l in (args.run_dir/'scores.jsonl').read_text().splitlines() if l.strip()]
                except (OSError,json.JSONDecodeError):
                    time.sleep(.5);continue
                for score in scores:
                    failure=score.get('query_type')=='utility' and score.get('utility_correct') is False
                    leak=any(score.get(k) is True for k in
                        ('privacy_leak','privacy_context_leak','deletion_leak','deletion_context_leak'))
                    if leak or (failure and args.stop_on_failure):
                        reason={'checkpoint_id':score['checkpoint_id'],'utility_failure':failure,'content_leak':leak,
                            'score':score}
                        (args.run_dir/'early-stop.json').write_text(json.dumps(reason,indent=2)+'\n')
                        print('Stopping on scored failure: '+score['checkpoint_id'],flush=True)
                        os.kill(os.getpid(),signal.SIGINT)
                        return
                if len(scores)>=args.checkpoints:return
                time.sleep(.5)
        threading.Thread(target=monitor,daemon=True).start()
    return runner.main(command)


if __name__=='__main__':raise SystemExit(main())
