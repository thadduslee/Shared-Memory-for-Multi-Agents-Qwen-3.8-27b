"""Optional typed projections. Canonical facts remain the visibility authority."""
import json
import re


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', value):
        raise ValueError('invalid schema identifier')
    return value


def register(conn, name, columns, *, reason, version=1):
    name = identifier(name)
    if not name.startswith('special_') or not columns or len(columns) > 16:
        raise ValueError('specialized table needs a special_ name and 1..16 columns')
    fields = {identifier(k): str(v).upper() for k, v in columns.items()}
    if 'memory_id' in fields or any(v not in {'TEXT', 'INTEGER', 'REAL'} for v in fields.values()):
        raise ValueError('only scalar projection fields are supported')
    if not reason or not isinstance(version, int) or version < 1:
        raise ValueError('schema changes require a version and rationale')
    previous = conn.execute('SELECT columns_json,version FROM specialized_catalog WHERE name=?', (name,)).fetchone()
    spec = json.dumps(fields, sort_keys=True)
    if previous and (previous[0] != spec or previous[1] != version):
        raise ValueError('create a new versioned projection; migrate before retiring the old one')
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{name}" (memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id) ON DELETE CASCADE, '
                 + ','.join(f'"{k}" {v}' for k, v in fields.items()) + ')')
    conn.execute('INSERT OR IGNORE INTO specialized_catalog VALUES (?,?,?,?)', (name, spec, version, reason))


def project(conn, name, memory_id, values):
    name = identifier(name)
    spec = conn.execute('SELECT columns_json FROM specialized_catalog WHERE name=?', (name,)).fetchone()
    row = conn.execute('SELECT text_value,status FROM memories WHERE memory_id=?', (memory_id,)).fetchone()
    if not spec or not row or row[1] != 'active':
        raise ValueError('projection requires a live canonical fact')
    columns = json.loads(spec[0])
    for key in columns:
        identifier(key)
    if set(values) - set(columns):
        raise ValueError('unknown projection column')
    # Projection values must be source excerpts. More elaborate interpretations
    # require a separately verified canonical fact, never an ungrounded sidecar.
    if any(v is not None and str(v).lower() not in row[0].lower() for v in values.values()):
        raise ValueError('projection value lacks canonical support')
    keys = ['memory_id', *values]
    conn.execute(f'INSERT OR REPLACE INTO "{name}" (' + ','.join(f'"{k}"' for k in keys) +  # noqa: S608 - validated identifiers
                 ') VALUES (' + ','.join('?' for _ in keys) + ')', (memory_id, *values.values()))


def purge(conn, memory_ids):
    for mid in memory_ids:
        conn.execute('DELETE FROM memory_edges WHERE from_memory_id=? OR to_memory_id=? OR source_memory_id=?', (mid,mid,mid))
    for (name,) in conn.execute('SELECT name FROM specialized_catalog'):
        identifier(name)
        if not name.startswith('special_'):
            raise ValueError('invalid projection registration')
        conn.executemany(f'DELETE FROM "{name}" WHERE memory_id=?', [(mid,) for mid in memory_ids])  # noqa: S608 - validated identifier
