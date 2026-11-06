"""Compare captured calls and the identical first 134 source turns."""
import collections
import json
from pathlib import Path
import sqlite3
from audit import ROOT, read_jsonl, stats, write

def main():
    result = {}
    for version in ['v19','v21']:
        base=ROOT/'snapshot'/version
        c=sqlite3.connect(f'file:{base/"memory.privacy.sqlite"}?mode=ro',uri=True)
        m=sqlite3.connect(f'file:{base/"memory.sqlite"}?mode=ro',uri=True)
        observations={a:json.loads(b) for a,b in m.execute("SELECT id,payload FROM resources WHERE kind='observation'")}
        first_comp=c.execute('SELECT compartment FROM permission_jobs ORDER BY rowid LIMIT 1').fetchone()[0]
        source_jobs={j:(observations[s]['source'].get('event_id'),json.loads(v or '{}')) for j,s,v in c.execute("SELECT id,source_id,validation FROM permission_jobs WHERE compartment=? AND id LIKE 'permission-job:%'",(first_comp,))}
        wall={source_jobs[j][0]:sec for j,sec in m.execute("SELECT job_id,seconds FROM pipeline_timings WHERE stage='source_total'") if j in source_jobs and source_jobs[j][0]<='t134'}
        traces=read_jsonl(base/'model-trace.jsonl')
        groups=collections.defaultdict(list)
        current=None
        for line,t in enumerate(traces,1):
            if t['stage']=='combined_incremental':
                p=t['payload']
                new=next((s for s in p.get('sources',[]) if s['id']==p.get('new_source_id')),None)
                comp=list(p.get('bootstrap',{}).get('source_identities',{}))
                current=(new.get('source',{}).get('event_id') if new and any(k.endswith(first_comp.split(':')[-1]) for k in comp) else None)
            if current and current<='t134' and t['stage'] in ['combined_incremental','incremental_binding_repair','incremental_repair']:
                pairs=t['payload'].get('pair_context',[])
                response=t.get('response') or {}
                groups[current].append({'trace_line':line,'stage':t['stage'],'seconds':t['seconds'],'usage':t.get('usage'),
                    'review_pair_count':len(pairs),'ownership_pairs':sum(x['assignment'].get('ownership',False) for x in pairs),
                    'decision_counts':dict(collections.Counter(x.get('d') for x in response.get('b',{}).values())) if isinstance(response.get('b'),dict) else {},
                    'error_type':t.get('error_type'), 'as_of':t['payload'].get('as_of')})
        stage_data={}
        for stage in ['combined_incremental','incremental_binding_repair','incremental_repair']:
            rows=[r for values in groups.values() for r in values if r['stage']==stage]
            stage_data[stage]={'wall':stats([r['seconds'] for r in rows]),'prompt_tokens':stats([(r.get('usage') or {}).get('prompt_tokens',0) for r in rows]),
                'completion_tokens':stats([(r.get('usage') or {}).get('completion_tokens',0) for r in rows]),'review_pairs':stats([r['review_pair_count'] for r in rows])}
        result[version]={'first_134_source_wall':stats(list(wall.values())),'source_wall_by_turn':wall,'matched_prefix_model_stages':stage_data,
            'calls_by_turn':dict(groups),'calls_per_source':sum(len(v) for v in groups.values())/134,
            'receipt_boundary':'Trace order and authenticated episode source namespace; includes same-source repairs, excludes answer/judge calls.'}
        c.close();m.close()
    result['limits']=['Same source prefix and model family; different serving backend, prompts, semantic stages and judge. This is observational, not an isolated architecture or GPU experiment.',
        'Full-run V19 latency is not compared to a different episode mix without also showing the matched prefix.',
        'Provider latency, prefix cache, parallelism and hardware effects cannot be individually separated from these saved traces.']
    write(ROOT/'timing.json',result)
    for v in ['v19','v21']:
        print(v,result[v]['first_134_source_wall'],result[v]['calls_per_source'])
        print('stages',result[v]['matched_prefix_model_stages'])
        for turn in ['t001','t054','t080','t104','t110','t112','t134']:
            print(turn,result[v]['source_wall_by_turn'].get(turn),result[v]['calls_by_turn'].get(turn))

if __name__=='__main__':main()
