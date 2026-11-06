"""Fresh/resumed evaluation through the two explicitly requested models.

The one secret lives in AMEM_OPENROUTER_API_KEY in the private .env (or process
environment). Local endpoint settings in that file are not modified.
"""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path

from agentic_memory.privacy.processors import CompletionProcessor
from . import runner

MEMORY_MODEL='qwen/qwen3.8-27b'
JUDGE_MODEL='openai/gpt-4.1'
BASE_URL='https://openrouter.ai/api/v1'


def configure(env_file):
    runner.load_environment(env_file)
    key=os.getenv('AMEM_OPENROUTER_API_KEY','').strip()
    if not key or key in {'dummy','YOUR_OPENROUTER_API_KEY'}:
        raise ValueError(f'Set AMEM_OPENROUTER_API_KEY in {env_file}; never pass it as a command argument')
    for prefix,model in [('AMEM_LLM',MEMORY_MODEL),('AMEM_JUDGE',JUDGE_MODEL)]:
        os.environ[prefix+'_BASE_URL']=BASE_URL
        os.environ[prefix+'_MODEL']=model
        os.environ[prefix+'_API_KEY']=key


def preflight():
    schema={'type':'object','additionalProperties':False,
        'properties':{'ok':{'type':'boolean','enum':[True]}},'required':['ok']}
    results=[]
    for role,model in [('memory-and-answer',MEMORY_MODEL),('judge',JUDGE_MODEL)]:
        processor=CompletionProcessor('openrouter:'+role,BASE_URL,model,
            os.environ['AMEM_OPENROUTER_API_KEY'],timeout=float(os.getenv('AMEM_TIMEOUT','300')),max_tokens=64)
        result=processor.complete('Return exactly {"ok":true}.',{'probe':True},schema=schema)
        if result!={'ok':True}:raise ValueError(f'{role} failed the structured-output preflight')
        receipt={'role':role,'requested_model':model,'response_model':processor.last_response_model,
            'provider':processor.last_provider,'seconds':processor.last_elapsed_seconds,'usage':processor.usage}
        results.append(receipt)
        print(f'OpenRouter {role}: {processor.last_response_model}; provider={processor.last_provider}; structured JSON verified',flush=True)
    return results


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,default=runner.ROOT/'.env')
    parser.add_argument('--run-dir',type=Path)
    parser.add_argument('--preflight',action='store_true')
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--permission-version',default='19')
    parser.add_argument('--max-checkpoints',type=int)
    args=parser.parse_args(argv)
    if not args.preflight and not args.run_dir:parser.error('--run-dir is required')
    if args.resume and args.preflight:parser.error('--resume requires an actual run')
    os.umask(0o077)
    configure(args.env_file)
    if args.run_dir:
        args.run_dir=args.run_dir.resolve()
        if not args.resume and any((args.run_dir/p).exists() for p in ('manifest.json','evaluation.sqlite','memory.sqlite')):
            parser.error('Use a fresh run directory or explicitly resume; historical runs are not overwritten')
        args.run_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        args.run_dir.chmod(0o700)
    receipts=preflight()
    if args.preflight:return 0
    launch={'at':datetime.now(timezone.utc).isoformat(),'base_url':BASE_URL,
        'models':{'memory':MEMORY_MODEL,'judge':JUDGE_MODEL},'permission_version':args.permission_version,
        'expected':{'episodes':21,'source_turns':4294,'checkpoints':579,'boundaries':567},
        'schedule':'episode-id-then-turn-position-v2-with-data-tails',
        'initial_requester_grants':0,'evolution':True,'offline':False,
        'key_setting':'AMEM_OPENROUTER_API_KEY','env_file':str(args.env_file.resolve()),
        'permission_token_cap':int(os.getenv('AMEM_INCREMENTAL_MAX_TOKENS',
            '2048' if int(args.permission_version.split('-',1)[0])>=16 else '1024')),
        'preflight':receipts,'resume':args.resume}
    runner.write_json(args.run_dir/('openrouter-resume.json' if args.resume else 'openrouter-launch.json'),launch)
    command=['--env-file',str(args.env_file),'--run-dir',str(args.run_dir),
        '--permission-mode','inference','--permission-version',args.permission_version]
    if args.max_checkpoints is not None:command+=['--max-checkpoints',str(args.max_checkpoints)]
    if args.resume:command.append('--resume')
    return runner.main(command)


if __name__=='__main__':raise SystemExit(main())
