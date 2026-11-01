from pathlib import Path
from datetime import datetime,timezone
import json,os,subprocess,sys,time,urllib.request
RUN=Path(__file__).resolve().parent
SNAPSHOT=RUN/'source_snapshot'
sys.path.insert(0,str(SNAPSHOT))
from evaluation.gatemem_medical.runner import load_environment
load_environment(Path('/home/thaddus/a-mem-variant/.env'))
PYTHON='/home/thaddus/gatemem/.venv-nb/bin/python'
def status(state,**extra):
 value={'state':state,'at':datetime.now(timezone.utc).isoformat(),**extra}
 temporary=RUN/'launch-status.json.tmp'
 temporary.write_text(json.dumps(value,indent=2)+'\n');temporary.replace(RUN/'launch-status.json')
 print(json.dumps(value),flush=True)
try:
 status('waiting_for_model_services')
 deadline=time.monotonic()+1200
 while True:
  missing=[]
  for role in ('LLM','JUDGE'):
   base=os.environ['AMEM_'+role+'_BASE_URL'].rstrip('/')
   try:
    request=urllib.request.Request(base+'/models',headers={'Authorization':'Bearer '+os.environ.get('AMEM_'+role+'_API_KEY','')})
    with urllib.request.urlopen(request,timeout=5) as response:
     if not json.load(response).get('data'):missing.append(role)
   except Exception:missing.append(role)
  if not missing:break
  if time.monotonic()>=deadline:raise RuntimeError('Model startup timed out: '+','.join(missing))
  time.sleep(5)
 status('preflight')
 base=[PYTHON,'-u','-m','evaluation.gatemem_medical','--env-file','/home/thaddus/a-mem-variant/.env']
 subprocess.run([*base,'--preflight'],cwd=SNAPSHOT,check=True)
 command=[*base,'--permission-mode','inference','--permission-version','14','--run-dir',str(RUN)]
 status('running',command=command,episodes=21,checkpoints=579,boundaries=567,source_turns=4294)
 result=subprocess.run(command,cwd=SNAPSHOT)
 if result.returncode:raise RuntimeError('Evaluation exited with code '+str(result.returncode)+'; inspect run.log and recovery.json')
 summary=json.loads((RUN/'summary.json').read_text())
 if not summary.get('complete'):raise RuntimeError('Runner exited without complete checkpoint/boundary coverage; inspect summary.json')
 status('complete',coverage=summary.get('coverage'),degraded=summary.get('degraded'))
except Exception as exc:
 status('failed',error=type(exc).__name__,message=str(exc))
 raise
