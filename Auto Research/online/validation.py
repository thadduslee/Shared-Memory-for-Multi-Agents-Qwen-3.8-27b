"""Fail-closed candidate gates on copies, never on the committed databases."""
import ast
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from gatemem_adapter import HIDDEN_ANNOTATION_FIELDS
from nodes.dev_tools import _split_sql


class ValidationFailure(ValueError):
    """A promotion failure with an attributable stage and protected-state diff."""

    def __init__(self, message, *, stage='validation', table=None, row_difference=None,
                 origin=None, gate_reports=None):
        self.stage = stage
        self.table = table
        self.row_difference = row_difference
        self.origin = origin or stage
        self.gate_reports = gate_reports or {}
        detail = message
        if table:
            detail += f' [table={table}]'
        if row_difference:
            detail += f' [difference={json.dumps(row_difference, sort_keys=True, default=str)}]'
        detail += f' [stage={self.stage}; origin={self.origin}]'
        super().__init__(detail)


def sandbox_command(workspace, scratch, argv, *, trusted_tests=None, worker=None,
                    runtime_only=False):
    """Mount only candidate source, scratch DB, Python, and trusted tests.

    Candidate tests and memory workers execute in this filesystem namespace.
    A missing sandbox is a validation failure, never an unconfined fallback.
    """
    binary = shutil.which('bwrap')
    if not binary:
        raise ValidationFailure('bubblewrap sandbox unavailable')
    args = [binary, '--die-with-parent', '--new-session', '--unshare-pid',
            '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
            '--setenv', 'PYTHONPATH', '/workspace',
            '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
            '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib',
            '--ro-bind', '/lib64', '/lib64', '--ro-bind', '/etc', '/etc',
            '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
            '--dir', '/home']
    prefix = Path(sys.prefix).resolve()
    if prefix != Path(sys.base_prefix).resolve() and not any(prefix.is_relative_to(root)
            for root in ('/usr', '/lib', '/lib64', '/etc')):
        # Bind just the selected virtual environment, wherever it lives.
        for parent in reversed(prefix.parents):
            if parent not in {Path('/'), Path('/home'), Path('/tmp')}:
                args += ['--dir', str(parent)]
        args += ['--ro-bind', str(prefix), str(prefix)]
    if runtime_only:
        args += ['--dir', '/workspace', '--ro-bind',
                 str(Path(workspace).resolve() / 'memory_system'), '/workspace/memory_system']
    else:
        args += ['--ro-bind', str(Path(workspace).resolve()), '/workspace']
    args += ['--bind', str(Path(scratch).resolve()), '/scratch', '--chdir', '/scratch']
    if trusted_tests:
        args += ['--ro-bind', str(Path(trusted_tests).resolve()), '/trusted_tests']
    if worker:
        args += ['--ro-bind', str(Path(worker).resolve()), '/worker.py']
    args += ['--ro-bind', str(Path(__file__).with_name('sandbox_entry.py').resolve()), '/sandbox_entry.py']
    return args + ['--', sys.executable, '/sandbox_entry.py'] + list(argv)


def code_manifest(workspace):
    return {str(p.relative_to(workspace)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(workspace).rglob('*'))
            if p.is_file() and p.suffix in {'.py', '.sql', '.json'} and '__pycache__' not in p.parts}


def copy_workspace(source, destination):
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(
        '__pycache__', '.pytest_cache', '*.sqlite*', '*.db*', '.cordis'), dirs_exist_ok=True)


def dump(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {t: {'columns': [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')],
                'rows': sorted([tuple(r) for r in conn.execute(f'SELECT * FROM "{t}"')], key=repr)}
            for t in tables}


def image_signature(blob):
    """Logical content/schema, independent of SQLite page allocation."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'db'
        path.write_bytes(blob)
        conn = sqlite3.connect(path)
        try:
            return dump(conn), list(conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name'))
        finally:
            conn.close()


def reject_checkpoint_specialization(parent, candidate, visible_observed, migration_sql=''):
    """Do not accept newly embedded observed checkpoint IDs/query literals.

    Regression tests may cite observed examples; executable memory code must
    implement a general rule. This supplements paired historical replay.
    """
    def constants(root):
        values = {node.value for path in (Path(root) / 'memory_system').rglob('*.py')
                for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for key, item in value.items():
                    yield str(key)
                    yield from strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)
        for path in (Path(root) / 'memory_system' / 'policies').glob('*.json'):
            values.update(strings(json.loads(path.read_text())))
        renderer = Path(root) / 'renderer' / 'renderer.json'
        if renderer.is_file():
            values.update(strings(json.loads(renderer.read_text(encoding='utf-8'))))
        return values
    added = constants(candidate) - constants(parent)
    for path in (Path(candidate) / 'memory_system').rglob('*'):
        is_policy = path.parent.name == 'policies' and path.suffix == '.json'
        if path.is_file() and path.suffix not in {'.py', '.sql'} and not is_policy:
            raise ValidationFailure('candidate runtime contains an unverified sidecar file')
    candidate_sql = '\n'.join(p.read_text() for p in (Path(candidate) / 'memory_system').rglob('*.sql'))
    parent_sql = '\n'.join(p.read_text() for p in (Path(parent) / 'memory_system').rglob('*.sql'))
    for hidden in HIDDEN_ANNOTATION_FIELDS:
        if (hidden in added or hidden in migration_sql or
                (hidden in candidate_sql and hidden not in parent_sql)):
            raise ValidationFailure('candidate executable code references a hidden scoring field')
    for cp in visible_observed:
        for value in (cp['checkpoint_id'], cp.get('query_text', '')):
            if value and (any(value in literal for literal in added) or
                          (value in candidate_sql and value not in parent_sql) or
                          value in migration_sql):
                raise ValidationFailure('checkpoint-specific executable code is not a general repair')


def preserve(before, after, *, origin='migration_sql'):
    """Preserve history while allowing evidence-backed derived additions."""
    additive_derived = {'memories', 'relationships', 'permissions', 'semantic_links', 'specialized_catalog', 'memory_edges'}
    for table, old in before.items():
        if table not in after:
            raise ValidationFailure('protected state removed a table', stage='data_preservation',
                                    table=table, row_difference={'change': 'missing_table'},
                                    origin=origin)
        new = after[table]
        if not set(old['columns']).issubset(new['columns']):
            missing = sorted(set(old['columns']) - set(new['columns']))
            raise ValidationFailure('protected state removed columns', stage='data_preservation',
                                    table=table, row_difference={'missing_columns': missing},
                                    origin=origin)
        positions = [new['columns'].index(c) for c in old['columns']]
        projected = sorted([tuple(row[i] for i in positions) for row in new['rows']], key=repr)
        old_rows = set(old['rows'])
        new_rows = set(projected)
        removed = sorted(old_rows - new_rows, key=repr)
        added = sorted(new_rows - old_rows, key=repr)
        if removed:
            gate = ('authorization' if table in {'principals', 'relationships', 'permissions'} else
                    'forgetting' if table in {'tombstones', 'memory_terms'} else 'history')
            raise ValidationFailure(f'{gate}: existing rows changed or disappeared',
                                    stage='data_preservation', table=table,
                                    row_difference={'removed_or_changed': removed[:5]},
                                    origin=origin)
        if added and table not in additive_derived and not table.startswith('special_'):
            raise ValidationFailure('unexpected protected-state insertion',
                                    stage='data_preservation', table=table,
                                    row_difference={'added': added[:5]}, origin=origin)
    _validate_derived_additions(before, after, origin=origin)
    _validate_specializations(after, origin=origin)


def _validate_specializations(state, *, origin):
    catalog = state.get('specialized_catalog')
    if not catalog:
        return
    canonical = state.get('memories', {})
    columns = canonical.get('columns', [])
    facts = {r[columns.index('memory_id')]: r for r in canonical.get('rows', [])}
    for row in state.get('memory_edges', {}).get('rows', []):
        _, start, end, source, relation = row
        if any(mid not in facts for mid in (start,end,source)) or not relation or str(relation) not in str(facts[source][columns.index('text_value')]):
            raise ValidationFailure('relationship lacks canonical source support', stage='derived_evidence', origin=origin)
    for row in catalog['rows']:
        name, fields, version, reason = row
        if not re.fullmatch(r'special_[a-z0-9_]{1,40}', str(name)) or name not in state:
            raise ValidationFailure('invalid specialized registration', stage='derived_evidence', origin=origin)
        fields = json.loads(fields)
        table = state[name]
        if not fields or set(table['columns']) != {'memory_id', *fields} or version < 1 or not reason:
            raise ValidationFailure('specialized schema contract invalid', stage='derived_evidence', origin=origin)
        for values in table['rows']:
            mid = values[table['columns'].index('memory_id')]
            fact = facts.get(mid)
            if not fact:
                raise ValidationFailure('specialized row lacks canonical provenance', stage='derived_evidence', origin=origin)
            text = str(fact[columns.index('text_value')]).lower()
            if any(v is not None and str(v).lower() not in text for k,v in zip(table['columns'],values) if k != 'memory_id'):
                raise ValidationFailure('specialized value lacks canonical support', stage='derived_evidence', origin=origin)


def validate_runtime_derivations(before, after, *, origin='runtime'):
    """Reingestion/backfill may replace derivations, never fabricate grants."""
    _validate_derived_additions(before, after, origin=origin)
    _validate_specializations(after, origin=origin)
    def controls(state):
        table=state.get('store_meta',{})
        return {tuple(r) for r in table.get('rows',[]) if str(r[0]).startswith('security_control:')}
    if not controls(after) <= controls(before):
        raise ValidationFailure('candidate fabricated or changed a trusted security control',
                                stage='authorization_evidence',origin=origin)


def _validate_derived_additions(before, after, *, origin):
    observations = after.get('observations')
    if not observations:
        return
    obs_columns = observations['columns']
    obs_by_id = {row[obs_columns.index('observation_id')]: row
                 for row in observations['rows']}
    for table in ('memories', 'relationships', 'permissions', 'semantic_links'):
        if table not in after:
            continue
        old_rows = set(before.get(table, {}).get('rows', []))
        columns = after[table]['columns']
        stable = [i for i,c in enumerate(columns) if c != 'created_at']
        old_stable = {tuple(row[i] for i in stable) for row in old_rows}
        old_permissions = {r[columns.index('permission_id')]:r for r in old_rows} if table=='permissions' else {}
        for row in after[table]['rows']:
            if row in old_rows or tuple(row[i] for i in stable) in old_stable:
                continue
            if table=='permissions':
                previous=old_permissions.get(row[columns.index('permission_id')])
                unchanged=[i for i,c in enumerate(columns) if c not in {'status','created_at'}]
                if (previous and all(previous[i]==row[i] for i in unchanged) and
                        row[columns.index('status')] in {'superseded','revoked','expired'}):
                    continue  # a lifecycle transition does not invent a new allow
            if 'source_observation_id' not in columns:
                raise ValidationFailure('derived row has no provenance column',
                    stage='derived_evidence', table=table,
                    row_difference={'added': row}, origin=origin)
            source = row[columns.index('source_observation_id')]
            if not source or source not in obs_by_id:
                raise ValidationFailure('derived row cites no preserved observation',
                    stage='derived_evidence', table=table,
                    row_difference={'source_observation_id': source, 'added': row},
                    origin=origin)
            if table == 'permissions':
                obs = obs_by_id[source]
                raw = str(obs[obs_columns.index('raw_text')]).lower()
                if not re.search(
                        r'\b(?:may|can)\s+(?:receive|access|have|hear|see)\b|'
                        r'\b(?:authoriz\w*|consent\w*|permission\w*|revoke\w*|withdraw\w*)\b|'
                        r'\baccess\s+(?:is|was|remains|:)\b|'
                        r'\b(?:do not|must not|never)\s+(?:tell|share|disclose|give|send)\b',
                        raw):
                    raise ValidationFailure('permission addition lacks explicit authorization evidence',
                        stage='authorization_evidence', table=table,
                        row_difference={'source_observation_id': source, 'added': row},
                        origin=origin)


def migrate_blob(blob, sql):
    """Transactional execution plus literal second-execution idempotency test.

    Explicit transaction/attach/pragma controls are denied to migration SQL.
    Only the successful first image is returned; any failure discards the copy.
    """
    with tempfile.TemporaryDirectory(prefix='online-migration-') as tmp:
        path = Path(tmp) / 'memory.sqlite'
        path.write_bytes(blob)
        conn = sqlite3.connect(path)
        try:
            before = dump(conn)
            forbidden = {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
                         sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_SAVEPOINT}
            def authorizer(action, a, b, db, trigger):
                return sqlite3.SQLITE_DENY if action in forbidden else sqlite3.SQLITE_OK
            def apply():
                conn.execute('BEGIN IMMEDIATE')
                conn.set_authorizer(authorizer)
                try:
                    for statement in _split_sql(sql):
                        conn.execute(statement)
                except Exception:
                    conn.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
                    conn.rollback()
                    raise
                conn.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
                conn.commit()
            apply()
            first = dump(conn)
            preserve(before, first, origin='migration_sql')
            first_schema = list(conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name'))
            apply()
            if dump(conn) != first or list(conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')) != first_schema:
                raise ValidationFailure('migration idempotency failed')
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or list(conn.execute('PRAGMA foreign_key_check')):
                raise ValidationFailure('migration database integrity failed')
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            conn.close()
        return path.read_bytes()


def build_gates(workspace, trusted_tests):
    """Run original tests outside the Developer write surface, plus candidate tests."""
    workspace = Path(workspace).resolve()
    # Never follow a Developer-created symlink outside its workspace.
    if any(p.is_symlink() for p in workspace.rglob('*')):
        raise ValidationFailure('candidate contains symlinks')
    authoritative = Path(trusted_tests).resolve().parent / 'memory_system' / 'policies'
    proposed = workspace / 'memory_system' / 'policies'
    if authoritative.is_dir():
        old = {p.name: p.read_bytes() for p in authoritative.glob('*.json')}
        new = {p.name: p.read_bytes() for p in proposed.glob('*.json')}
        if old != new:
            raise ValidationFailure('authoritative authorization specifications are protected',
                                    stage='authorization_specification')
    env = {**os.environ, 'PYTHONPATH': str(workspace), 'PYTEST_ADDOPTS': '',
           'PYTHONDONTWRITEBYTECODE': '1'}
    reports = {}
    renderer_path = workspace / 'renderer' / 'renderer.json'
    try:
        renderer = json.loads(renderer_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValidationFailure('renderer artifact is missing or malformed',
                                stage='build_validation', origin='renderer_schema') from exc
    if (not isinstance(renderer, dict) or renderer.get('schema_version') != 1 or
            not isinstance(renderer.get('instruction_suffix'), str) or
            set(renderer) - {'schema_version', 'renderer_version', 'instruction_suffix'}):
        raise ValidationFailure('renderer artifact failed the fixed declarative schema',
                                stage='build_validation', origin='renderer_schema')
    reports['renderer_schema'] = {'ok': True, 'output': 'declarative schema valid'}
    with tempfile.TemporaryDirectory(prefix='online-tests-') as temp:
        frozen = Path(temp) / 'tests'
        copy_workspace(trusted_tests, frozen)
        syntax_check = ('import ast,pathlib,sys; '
                        'paths=list(pathlib.Path(sys.argv[1]).rglob("*.py")); '
                        '[ast.parse(p.read_text(), filename=str(p)) for p in paths]')
        commands = [('compile', [sys.executable, '-c', syntax_check, str(workspace / 'memory_system')]),
                    ('invariants', [sys.executable, '-m', 'pytest', '-q', '-c', '/dev/null', '/trusted_tests'])]
        if (workspace / 'tests').is_dir():
            commands.append(('unit_tests', [sys.executable, '-m', 'pytest', '-q', '-c', '/dev/null', '/workspace/tests']))
        for name, argv in commands:
            command = argv if name == 'compile' else sandbox_command(workspace, temp, argv, trusted_tests=frozen)
            try:
                result = subprocess.run(command, cwd=temp, env={**env, 'PYTHONPATH': '/workspace'},
                                        capture_output=True, text=True, timeout=180, check=False)
            except subprocess.TimeoutExpired as exc:
                output = ((exc.stdout or '') + (exc.stderr or ''))[-8000:]
                reports[name] = {'ok': False, 'output': output,
                                 'error': f'{name} timed out after 180 seconds'}
                raise ValidationFailure(
                    f'{name} timed out after 180 seconds: {output}',
                    stage='build_validation', origin=name, gate_reports=reports) from exc
            reports[name] = {'ok': result.returncode == 0, 'output': (result.stdout + result.stderr)[-8000:]}
            if result.returncode:
                raise ValidationFailure(
                    f'{name} failed: {reports[name]["output"]}',
                    stage='build_validation', origin=name, gate_reports=reports)
    return reports
