"""Read-only scope/validity/binding audit of isolated smoke stores, not a judge."""
import json
from pathlib import Path
import sqlite3

BASE=Path(__file__).resolve().parent
WORKERS={'outreach':'nurse_casey_liu','counseling':'pharmacist_owens'}


def inspect(directory):
    result={}
    for case,worker in WORKERS.items():
        path=directory/case/'memory.sqlite'
        if not path.exists():continue
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
        db.execute('ATTACH DATABASE ? AS privacy',
                   ((directory/case/'memory.privacy.sqlite').resolve().as_uri()+'?mode=ro',))
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        sources={r['id']:json.loads(r['payload']) for r in db.execute("SELECT id,payload FROM resources WHERE kind='observation'")}
        latest={}
        for r in db.execute('SELECT kind,id,body FROM permission_records ORDER BY rowid'):
            latest[(r['kind'],r['id'])]=json.loads(r['body'])
        assignments=[]
        for (kind,rid),a in latest.items():
            if kind!='assignment' or a['participant']!=worker:continue
            item={k:a.get(k) for k in ('id','participant','subject','status','information_scope','validity','activity_id')}
            item['bindings']=[{'source_turn':sources[b['source_id']]['source']['event_id'],
                'text':'\n'.join(sources[b['source_id']]['text'][start:end] for start,end in b['spans']),
                'necessity':b['necessity']} for b in a['bindings']]
            assignments.append(item)
        result[case]=assignments
        db.rollback();db.close()
    return result


def main():
    result={p.name:inspect(p) for p in sorted(BASE.glob('live-smoke-*')) if (p/'report.json').exists()}
    (BASE/'scope-validity-audit.json').write_text(json.dumps(result,indent=2)+'\n')
    print('Audited completed isolated smoke stores:',', '.join(result))


if __name__=='__main__':main()
