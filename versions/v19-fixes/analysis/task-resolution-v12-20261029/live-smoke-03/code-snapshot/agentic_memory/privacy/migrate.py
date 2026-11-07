"""Copy an offline legacy database to a NEW two-store pair; never alter input.

python -m agentic_memory.privacy.migrate LEGACY.sqlite NEW/memory.sqlite
Stop writers and back up a consistent snapshot before invoking this command.
Vector rows are intentionally omitted: rebuild with the actual approved embedder.
"""
import argparse
from pathlib import Path
import sqlite3
from .storage import open_pair


def migrate(source, destination):
    source=Path(source).resolve();destination=Path(destination).resolve()
    privacy=destination.with_suffix('.privacy.sqlite')
    if source == destination or destination.exists() or privacy.exists():
        raise ValueError('Migration requires new output paths')
    legacy=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    legacy.execute('BEGIN')
    tables={r[0] for r in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'resources','policies','memberships','authority_owners'} <= tables:
        legacy.close();raise ValueError('Not a supported legacy store')
    db,_,_=open_pair(destination)
    try:
        db.execute('BEGIN IMMEDIATE')
        for table in ('policies','memberships','authority_owners','resources','provenance','tombstones','events','metadata','source_views'):
            if table not in tables: continue
            for row in legacy.execute('SELECT * FROM '+table):
                if table == 'metadata' and row[0] == 'store_pair': continue
                db.execute('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in row)+')',tuple(row))
        for rid,pid in db.execute('SELECT id,policy_id FROM resources').fetchall():
            if not db.execute('SELECT 1 FROM policies WHERE id=?',(pid,)).fetchone():
                raise ValueError('Legacy resource references missing policy')
            db.execute("INSERT INTO access_edges VALUES (?,?,'')",(pid,rid))
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction: db.execute('ROLLBACK')
        raise
    finally:
        db.close();legacy.close()
    return destination,privacy

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source');p.add_argument('destination')
    a=p.parse_args();print(migrate(a.source,a.destination))
