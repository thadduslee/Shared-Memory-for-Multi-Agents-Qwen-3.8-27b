#!/usr/bin/env python3
"""Copy a finished run into this repository without its machine-local state, or index one in place.

    python pipeline/export_run.py SOURCE_RUN_DIR DEST_DIR [--skip-dir NAME ...]
    python pipeline/export_run.py RUN_DIR RUN_DIR          # index only, nothing is copied

Left out of the copy (but listed with sha256 and size in EXPORT_MANIFEST.json, so a
reader can tell exactly what existed and check a local copy against it):

  - SQLite stores and journals (*.sqlite, *.sqlite-wal, *.sqlite-shm): the memory
    stores reach 220 MB per episode, above GitHub's 100 MB file limit, and they are
    resumable only on the machine that wrote them;
  - lock files (*.lock), Python caches;
  - credential files (*.env, .env), which hold endpoint keys;
  - any directory named with --skip-dir (for example frozen-source, whose files are
    this repository's own code and are verified by scripts/verify_bundle.py instead).

Every copied file is hashed after copying. Existing destination files that differ are
refused, so an export never silently rewrites an earlier one.
"""
import argparse
import fnmatch
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

EXCLUDE_FILES = ('*.sqlite', '*.sqlite-*', '*.sqlite3', '*.lock', '*.pyc', '*.env', '.env', 'EXPORT_MANIFEST.json')
EXCLUDE_DIRS = ('__pycache__', '.pytest_cache')
REASONS = {'*.sqlite': 'sqlite runtime state', '*.sqlite-*': 'sqlite journal', '*.sqlite3': 'sqlite runtime state',
           '*.lock': 'lock file', '*.pyc': 'python cache', '*.env': 'credentials', '.env': 'credentials'}


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def excluded(name):
    return next((pattern for pattern in EXCLUDE_FILES if fnmatch.fnmatch(name, pattern)), None)


def export(source, dest, skip_dirs=()):
    source, dest = Path(source).resolve(), Path(dest).resolve()
    in_place = source == dest
    copied, left_out = [], []
    for path in sorted(source.rglob('*')):
        rel = path.relative_to(source)
        if any(part in EXCLUDE_DIRS for part in rel.parts) or path.is_dir():
            continue
        if path.is_symlink():
            left_out.append({'path': str(rel), 'reason': 'symlink', 'target': str(path.readlink())})
            continue
        top = rel.parts[0]
        if top in skip_dirs:
            left_out.append({'path': str(rel), 'reason': f'skipped directory {top}',
                             'sha256': sha256(path), 'bytes': path.stat().st_size})
            continue
        pattern = excluded(path.name)
        if pattern:
            entry = {'path': str(rel), 'reason': REASONS.get(pattern, pattern), 'bytes': path.stat().st_size}
            if pattern not in ('*.env', '.env'):
                entry['sha256'] = sha256(path)
            left_out.append(entry)
            continue
        target = dest / rel
        if not in_place:
            if target.exists() and sha256(target) != sha256(path):
                raise SystemExit(f'refusing to overwrite a different existing file: {target}')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        digest = sha256(target)
        if not in_place and digest != sha256(path):
            raise SystemExit(f'copy mismatch: {rel}')
        copied.append({'path': str(rel), 'sha256': digest, 'bytes': target.stat().st_size})
    manifest = {
        'format': 'evolving-privacy-mem-export-v1',
        'source': str(source),
        'exported_utc': datetime.now(timezone.utc).isoformat(),
        'files': copied,
        'left_out': left_out,
        'totals': {'files': len(copied), 'bytes': sum(f['bytes'] for f in copied),
                   'left_out_files': len(left_out), 'left_out_bytes': sum(f.get('bytes', 0) for f in left_out)},
    }
    dest.mkdir(parents=True, exist_ok=True)
    (dest / 'EXPORT_MANIFEST.json').write_text(json.dumps(manifest, indent=1) + '\n')
    return manifest


def add_derived(dest, rel_path, made_from, how):
    """Record a file written into an export after copying (for example a text export of a left-out SQLite)."""
    dest = Path(dest)
    path = dest / 'EXPORT_MANIFEST.json'
    manifest = json.loads(path.read_text())
    entry = {'path': str(rel_path), 'sha256': sha256(dest / rel_path), 'bytes': (dest / rel_path).stat().st_size,
             'made_from': str(made_from), 'how': how}
    manifest['derived'] = [d for d in manifest.get('derived', []) if d['path'] != str(rel_path)] + [entry]
    path.write_text(json.dumps(manifest, indent=1) + '\n')


def verify(dest):
    """Check every copied and derived file listed in an EXPORT_MANIFEST.json; returns a list of problems."""
    dest = Path(dest)
    manifest = json.loads((dest / 'EXPORT_MANIFEST.json').read_text())
    problems = []
    for entry in manifest['files'] + manifest.get('derived', []):
        path = dest / entry['path']
        if not path.is_file():
            problems.append(f'missing {path}')
        elif sha256(path) != entry['sha256']:
            problems.append(f'changed {path}')
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('source')
    ap.add_argument('dest')
    ap.add_argument('--skip-dir', action='append', default=[], help='top-level directory to leave out (hashes recorded)')
    args = ap.parse_args(argv)
    m = export(args.source, args.dest, tuple(args.skip_dir))
    t = m['totals']
    print(f"exported {t['files']} files ({t['bytes'] / 1e6:.1f} MB); left out {t['left_out_files']} "
          f"({t['left_out_bytes'] / 1e6:.1f} MB) -> {Path(args.dest) / 'EXPORT_MANIFEST.json'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
