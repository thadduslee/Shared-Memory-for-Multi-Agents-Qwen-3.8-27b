from evaluation.gatemem_medical import openrouter
from pathlib import Path
import os,sys
openrouter.configure(Path('/home/thaddus/a-mem-variant/.env'))
os.environ["PYTHONPATH"]='/home/thaddus/a-mem-variant/runs/gatemem-medical-v18-openrouter-prefix-20261101T234928+0800/frozen-source'
os.chdir('/home/thaddus/a-mem-variant/runs/gatemem-medical-v18-openrouter-prefix-20261101T234928+0800/frozen-source')
os.execv(sys.executable,[sys.executable,"-u",'/home/thaddus/a-mem-variant/runs/gatemem-medical-v18-openrouter-prefix-20261101T234928+0800/frozen-source/analysis/over-refusal-v16/run.py',"--run-dir",'/home/thaddus/a-mem-variant/runs/gatemem-medical-v18-openrouter-prefix-20261101T234928+0800',"--version","18","--checkpoints","2"])
