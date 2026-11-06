#!/usr/bin/env python3
"""Copy a V19 worktree and evaluation exports into this repository, without runtime state."""
import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

SOURCE_DIRS = {"agentic_memory", "evaluation", "examples", "scripts", "security_tests", "tests", "Figure"}
OMIT_DIRS = {".git", "__pycache__", ".pytest_cache", ".cache", ".venv", "node_modules"}
OMIT_SUFFIXES = {".pyc", ".pyo", ".log", ".lock", ".gz", ".tmp", ".sqlite", ".sqlite3", ".db"}
SECRET_PATTERNS = (
    r"\bsk-(?:or-v1-)?[A-Za-z0-9_-]{20,}",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args]).decode().strip()


def exclusion(relative):
    if any(part in OMIT_DIRS for part in relative.parts):
        return "Git metadata, environment, or cache"
    if relative.name != ".env.example" and (
        relative.name.startswith(".env") or relative.suffix == ".env"
    ):
        return "Local endpoint/credential configuration"
    if relative.suffix in OMIT_SUFFIXES or ".sqlite-" in relative.name or ".sqlite3-" in relative.name:
        return "Runtime database, cache, log, lock, or archive"
    if relative.name in {"model-trace.jsonl", "status.tmp"}:
        return "Raw model trace or transient status"
    return None


def copy_files(source, destination, paths):
    copied, omitted = [], []
    for relative in sorted(set(paths)):
        path = source / relative
        if not path.is_file():
            continue
        reason = exclusion(relative)
        if path.stat().st_size >= 50 * 1024 * 1024:
            reason = "File exceeds the bundle's 50 MiB limit"
        if reason:
            omitted.append({"path": str(relative), "bytes": path.stat().st_size, "reason": reason})
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append({"path": str(relative), "bytes": target.stat().st_size, "sha256": digest(target)})
    return copied, omitted


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def validate_files(root):
    import re
    errors = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if exclusion(relative):
            errors.append(f"Excluded artifact present: {relative}")
        if path.stat().st_size >= 50 * 1024 * 1024:
            errors.append(f"Oversized artifact: {relative}")
        if path.suffix == ".json":
            json.loads(path.read_text())
        elif path.suffix == ".jsonl":
            for line in path.read_text().splitlines():
                if line.strip():
                    json.loads(line)
        try:
            content = path.read_text()
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if re.search(pattern, content):
                errors.append(f"Possible credential in {relative}; review before publishing")
    if errors:
        raise RuntimeError("\n".join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--evaluations", type=Path, required=True)
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    destination = args.destination.resolve()
    system = destination / "versions/v19"
    results = destination / "evaluations/v19"
    if system.exists() or results.exists():
        raise SystemExit("Bundle directories already exist; refusing to overwrite them.")
    stamp = datetime.now(timezone.utc).isoformat()
    names = subprocess.check_output([
        "git", "-C", str(args.source), "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    ]).decode().split("\0")
    paths = []
    for name in filter(None, names):
        relative = Path(name)
        if len(relative.parts) == 1 or relative.parts[0] in SOURCE_DIRS or (
            relative.parts[0] == "analysis" and len(relative.parts) > 1
            and (relative.parts[1].startswith("v19") or relative.suffix in {".md", ".py", ".sh", ".txt"})
        ):
            paths.append(relative)
    system.mkdir(parents=True)
    copied, omitted = copy_files(args.source, system, paths)
    write_json(system / "SOURCE_MANIFEST.json", {
        "exported_at_utc": stamp, "source_directory": str(args.source),
        "source_commit": git(args.source, "rev-parse", "HEAD"),
        "source_branch": git(args.source, "branch", "--show-current"),
        "source_worktree_status": git(args.source, "status", "--short").splitlines(),
        "files": copied, "excluded": omitted,
    })
    results.mkdir(parents=True)
    runs = []
    for run in sorted(args.evaluations.iterdir()):
        if not run.is_dir():
            continue
        copied, omitted = copy_files(run, results / run.name, [p.relative_to(run) for p in run.rglob("*") if p.is_file()])
        summary_path = results / run.name / "results/summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        coverage, metrics = summary.get("coverage", {}), summary.get("metrics", {})
        row = {
            "run": run.name, "has_summary": bool(summary), "complete": summary.get("complete", False),
            "coverage": coverage, "metrics": metrics, "degraded": summary.get("degraded"),
            "enrichment_complete": summary.get("evolution", {}).get("enrichment_complete"),
        }
        runs.append(row)
        write_json(results / run.name / "EXPORT_MANIFEST.json", {
            "exported_at_utc": stamp, "source_directory": str(run), "files": copied, "excluded": omitted,
        })
    write_json(results / "index.json", {"exported_at_utc": stamp, "runs": runs})
    lines = ["# V19 evaluation exports", "", "These are snapshots copied from `v19-evaluations`; original runs remain untouched.",
             "Every run has an `EXPORT_MANIFEST.json` with SHA-256 hashes and excluded files.",
             "SQLite state, credentials, runtime logs, locks, caches, and compressed archives are omitted.",
             "The JSON/JSONL exports support analysis; they are not resumable database backups.", "",
             "| Run | Scored / expected | Complete | Utility |", "|---|---:|---|---:|"]
    for row in runs:
        coverage = row["coverage"]
        count = f'{coverage.get("scored_checkpoints", 0)} / {coverage.get("expected_checkpoints", "—")}'
        utility = row["metrics"].get("utility_accuracy")
        value = f"{utility:.2%}" if utility is not None else "—"
        lines.append(f'| [{row["run"]}]({row["run"]}/) | {count} | {"Yes" if row["complete"] else "No"} | {value} |')
    lines += ["", "## Completed full run", "",
              "`v19-model-controls-full-soclaas-20261104T103839Z` completed all 579 checkpoints, 567 boundaries, and 4,294 source turns.",
              "Its 78 frozen-source hashes match `../../versions/v19` at export time.",
              "The run used SOCLAAS, local GPUs 0/3, SOCLAAS again, and finally GPUs 0/3; migration records preserve that serving history.",
              "Completion means evaluation coverage: the report still records `degraded: true` and `enrichment_complete: false`.",
              "The bundled judge-comparison report is an earlier 539-checkpoint snapshot, not the final full-run score.", "",
              "Frozen code, fixture data, configuration manifests, predictions, scores, and recovery records are retained where present.",
              "Historical launcher scripts retain their original machine paths and reference omitted local environment files; configure fresh services before rerunning.", "",
              "## Verify this bundle", "", "From the repository root, run `python scripts/verify_v19_bundle.py`.",
              "The verifier checks file hashes, JSON exports, score counts, final metrics, and credential/size exclusions.",
              "See `../../versions/v19/SNAPSHOT.md` for reproduction and test commands.", ""]
    (results / "README.md").write_text("\n".join(lines))
    main_run = results / "v19-model-controls-full-soclaas-20261104T103839Z"
    expected = json.loads((main_run / "source-hashes.json").read_text())
    for name, expected_hash in expected.items():
        if digest(system / name) != expected_hash:
            raise RuntimeError(f"V19 snapshot differs from full run: {name}")
        if digest(main_run / "frozen-source" / name) != expected_hash:
            raise RuntimeError(f"Full run frozen-source hash mismatch: {name}")
    validate_files(system)
    validate_files(results)
    print(f"Prepared {len(runs)} evaluation exports and {len(paths)} selected source files.")
    print(f"Verified {len(expected)} frozen-source hashes against the V19 worktree snapshot.")


if __name__ == "__main__":
    main()
