"""Stop ONLY the selected evaluation on a utility failure or content leak."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import signal
import time

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--run-dir',type=Path,required=True)
a=p.parse_args();run=a.run_dir.resolve()
def matches(pid):
    try:
        argv=Path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
        return ('analysis/over-refusal-v16/run.py' in argv and '--run-dir' in argv
            and Path(argv[argv.index('--run-dir')+1]).resolve()==run)
    except (OSError,ValueError,IndexError):return False
targets=[int(d.name) for d in Path('/proc').iterdir() if d.name.isdigit() and matches(d.name)]
if len(targets)!=1:raise RuntimeError(f'Expected one matching evaluation, found {len(targets)}')
pid=targets[0];start=Path(f'/proc/{pid}/stat').read_text().split()[21]
print(json.dumps({'watching_pid':pid,'run_dir':str(run)}),flush=True)
while matches(pid):
    score_file=run/'scores.jsonl'
    try:scores=[json.loads(l) for l in score_file.read_text().splitlines() if l.strip()]
    except (OSError,json.JSONDecodeError):scores=[]
    for score in scores:
        failure=(score.get('query_type')=='utility' and score.get('utility_correct') is False)
        leak=any(score.get(k) is True for k in ('privacy_leak','privacy_context_leak','deletion_leak','deletion_context_leak'))
        if failure or leak:
            if not matches(pid) or Path(f'/proc/{pid}/stat').read_text().split()[21]!=start:raise SystemExit(0)
            reason={'at':datetime.now(timezone.utc).isoformat(),'checkpoint_id':score['checkpoint_id'],
                'utility_failure':failure,'content_leak':leak,'score':score,'pid':pid}
            (run/'early-stop.json').write_text(json.dumps(reason,indent=2)+'\n')
            os.kill(pid,signal.SIGINT)
            print(json.dumps({'stopped':pid,'checkpoint':score['checkpoint_id'],'utility_failure':failure,'content_leak':leak}),flush=True)
            raise SystemExit(0)
    if len(scores)>=25:break
    time.sleep(1)
print(json.dumps({'finished_watching':pid,'scored_checkpoints':len(scores)}),flush=True)
