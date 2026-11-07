"""Reproduce the server rejection without mutating evaluation databases."""
import json, os, urllib.request, urllib.error
from pathlib import Path
os.umask(0o077)
p=Path(__file__).parent
r=json.loads((p/'failed-request.json').read_text())
body={'model':'Qwen/Qwen3.8-27B','messages':[{'role':'system','content':r['system']},{'role':'user','content':json.dumps(r['payload'],ensure_ascii=False,separators=(',',':'))}], 'temperature':0,'max_tokens':8192,'response_format':{'type':'json_schema','json_schema':{'name':'processor_response','strict':True,'schema':r['schema']}},'chat_template_kwargs':{'enable_thinking':False}}
req=urllib.request.Request('http://127.0.0.1:8000/v1/chat/completions',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer dummy'})
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(req,timeout=300) as response:
        result={'status':response.status,'response':json.load(response)}
except urllib.error.HTTPError as exc:
    result={'status':exc.code,'body':exc.read().decode()}
(p/'http-probe.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result) if result['status']!=200 else json.dumps({'status':200,'usage':result['response'].get('usage')}))
