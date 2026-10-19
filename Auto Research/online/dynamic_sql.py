"""Trusted SQL gateway over a bounded, authorized, ephemeral projection.

Generated SQL never receives a connection to the memory image. Even queries
without WHERE clauses can see only permitted source excerpts. Result text is
not an answer: returned IDs select canonical evidence for the normal plan.
"""
import json
import math
import re
import sqlite3
import tempfile
import time
from pathlib import Path

from online.evidence import audit_history

IDENT = re.compile(r'[a-z][a-z0-9_]{0,47}\Z')


class QueryRejected(ValueError):
    pass


class QueryGateway:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __init__(self, blob, header, checkpoint, *, max_rows=64, max_steps=20000, timeout_s=.1, policy=None):
        self.max_rows = min(128, max(1, max_rows))
        self.max_steps = max(1, max_steps)
        self.timeout_s = min(1., max(.001, timeout_s))
        self.audit = audit_history(blob, header, checkpoint, policy=policy)
        self.records = {r['record_id']: r for r in self.audit.get('records', [])}
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('PRAGMA temp_store=MEMORY')
        self.conn.execute('PRAGMA trusted_schema=OFF')
        self.conn.execute('CREATE TABLE memories (memory_id TEXT PRIMARY KEY, source_observation_id TEXT, text_value TEXT, turn_id TEXT)')
        self.conn.executemany('INSERT INTO memories VALUES (?,?,?,?)', [
            (r['record_id'], r['source_observation_id'], r['text'], r['turn_id'])
            for r in self.records.values()])
        self.schema = {'memories': {'memory_id': 'TEXT', 'source_observation_id': 'TEXT',
                                   'text_value': 'TEXT', 'turn_id': 'TEXT'}}
        with tempfile.TemporaryDirectory(prefix='sql-projection-') as tmp:
            path = Path(tmp) / 'source.sqlite'
            path.write_bytes(blob)
            source = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
            source.row_factory = sqlite3.Row
            expires = time.monotonic() + self.timeout_s
            source.set_progress_handler(lambda: int(time.monotonic() > expires), 100)
            try:
                exists = source.execute("SELECT 1 FROM sqlite_master WHERE name='specialized_catalog'").fetchone()
                if exists:
                    try:
                        self._specializations(source)
                    except sqlite3.Error:
                        self.audit.setdefault('unknowns', []).append('specialized_schema_read_rejected')
                if source.execute("SELECT 1 FROM sqlite_master WHERE name='memory_edges' AND type='table'").fetchone():
                    shape = source.execute("SELECT sql FROM sqlite_master WHERE name='memory_edges'").fetchone()[0]
                    physical = bool(re.match(r'^CREATE\s+TABLE\b', shape, re.IGNORECASE))
                    edges = [tuple(r) for r in source.execute('SELECT edge_id,from_memory_id,to_memory_id,source_memory_id,relation FROM memory_edges LIMIT 257')
                        if r['from_memory_id'] in self.records and r['to_memory_id'] in self.records
                        and r['source_memory_id'] in self.records
                        and r['relation'] in self.records[r['source_memory_id']]['text']] if physical else []
                    if edges:
                        self.schema['memory_edges'] = {k:'TEXT' for k in
                            ('edge_id','from_memory_id','to_memory_id','source_memory_id','relation')}
                        self.conn.execute('CREATE TABLE memory_edges (edge_id TEXT,from_memory_id TEXT,to_memory_id TEXT,source_memory_id TEXT,relation TEXT)')
                        self.conn.executemany('INSERT INTO memory_edges VALUES (?,?,?,?,?)', edges)
            finally:
                source.close()
        self.conn.commit()
        self.conn.execute('PRAGMA query_only=ON')
        if hasattr(self.conn, 'setlimit'):
            self.conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
            self.conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 8192)
            self.conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 64)
            self.conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 30)
            self.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)
        self.conn.set_authorizer(self._authorize)

    def _specializations(self, source):
        for spec in source.execute('SELECT * FROM specialized_catalog LIMIT 16'):
            name = spec['name']
            if not IDENT.fullmatch(name) or not name.startswith('special_'):
                continue
            try:
                shape = source.execute('SELECT type,sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
                if not shape or shape[0] != 'table' or not re.match(r'^CREATE\s+TABLE\b', shape[1], re.IGNORECASE):
                    continue
                if any(row[6] for row in source.execute(f'PRAGMA table_xinfo("{name}")')):
                    continue  # generated/hidden columns are not scalar stored projections
                columns = json.loads(spec['columns_json'])
                if not isinstance(columns, dict) or not 1 <= len(columns) <= 16:
                    continue
                if any(not IDENT.fullmatch(k) or k == 'memory_id' or v not in {'TEXT', 'REAL', 'INTEGER'}
                       for k, v in columns.items()):
                    continue
                values = []
                # Restrict reads themselves, not just the final result.
                for mid, record in self.records.items():
                    if not record.get('full_source_visible'):
                        # A specialized table/column name can itself reveal a
                        # private classification of a partially visible fact.
                        # Scoped excerpts remain queryable in generic memories.
                        continue
                    row = source.execute('SELECT memory_id,' + ','.join(f'"{k}"' for k in columns)
                                         + f' FROM "{name}" WHERE memory_id=? LIMIT 1', (mid,)).fetchone()
                    if row and all(v is None or str(v).lower() in record['text'].lower() for v in tuple(row)[1:]):
                        values.append(tuple(row))
                if not values:
                    continue  # even schema names stay private without visible data
                self.conn.execute(f'CREATE TABLE "{name}" (memory_id TEXT PRIMARY KEY,' +
                                  ','.join(f'"{k}" {v}' for k, v in columns.items()) + ')')
                self.conn.executemany(f'INSERT INTO "{name}" VALUES (' + ','.join('?' for _ in range(len(columns)+1)) + ')', values)
                self.schema[name] = {'memory_id': 'TEXT', **columns}
            except (ValueError, sqlite3.Error, TypeError):
                continue

    def _authorize(self, action, arg1, arg2, database, trigger):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return (sqlite3.SQLITE_OK if database == 'main' and arg1 in self.schema and
                    (arg2 in self.schema[arg1] or arg2 == '') else sqlite3.SQLITE_DENY)
        if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() in {
                'lower', 'upper', 'length', 'coalesce', 'like', 'count', 'min', 'max', 'abs', 'substr'}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def execute(self, proposal):
        if not isinstance(proposal, dict):
            raise QueryRejected('query_shape')
        sql, params = proposal.get('sql'), proposal.get('params')
        if (not isinstance(sql, str) or len(sql) > 8192 or not re.match(r'^\s*SELECT\b', sql, re.IGNORECASE)
                or not isinstance(params, list) or len(params) > 64
                or any(type(v) not in {str, int, float, type(None)} or len(str(v)) > 4000
                       or (type(v) is int and not -(2**63) <= v < 2**63)
                       or (type(v) is float and not math.isfinite(v)) for v in params)):
            raise QueryRejected('query_shape_or_limit')
        # Literals supplied by the generator must be bound. This also rejects
        # comments, quoted values and stacked statements before preparation.
        stripped = re.sub(r'"[a-z][a-z0-9_]*"', '', sql)
        if re.search(r"[';]|--|/\*|\b\d+\b", stripped):
            raise QueryRejected('bind_values_as_parameters')
        started, ticks = time.monotonic(), 0
        def progress():
            nonlocal ticks
            ticks += 100
            return int(ticks > self.max_steps or time.monotonic() - started > self.timeout_s)
        self.conn.set_progress_handler(progress, 100)
        try:
            cursor = self.conn.execute(sql, params)
            names = [d[0] for d in cursor.description]
            positions = [i for i, name in enumerate(names) if name == 'memory_id' or name.endswith('_memory_id')]
            if not positions:
                raise QueryRejected('source_provenance_required')
            rows = cursor.fetchmany(self.max_rows + 1)
            if len(rows) > self.max_rows:
                raise QueryRejected('result_limit')
            ids = set()
            for row in rows:
                for i in positions:
                    if row[i] is None:
                        continue
                    if row[i] not in self.records:
                        raise QueryRejected('invalid_source_provenance')
                    ids.add(row[i])
            return [self.records[mid] for mid in sorted(ids)]
        except sqlite3.Error as exc:
            # SQLite error strings can contain input values; export a code only.
            raise QueryRejected('validation_or_execution_rejected') from exc
        finally:
            self.conn.set_progress_handler(None, 0)

    def close(self):
        self.conn.close()
