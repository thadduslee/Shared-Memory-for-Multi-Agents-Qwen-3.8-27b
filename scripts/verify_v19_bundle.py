#!/usr/bin/env python3
"""Verify published V19 file hashes, JSON exports, coverage, metrics, and credential exclusions."""
import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.repository.resolve()
    spec = importlib.util.spec_from_file_location("prepare_v19_bundle", root / "scripts/prepare_v19_bundle.py")
    preparation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preparation)
    exports = root / "evaluations/v19"
    system = root / "versions/v19"
    manifest_paths = [system / "SOURCE_MANIFEST.json", *sorted(exports.glob("*/EXPORT_MANIFEST.json"))]
    hashed_files = 0
    for path in manifest_paths:
        manifest = json.loads(path.read_text())
        for entry in manifest["files"]:
            file = path.parent / entry["path"]
            assert file.is_file(), f"Missing artifact: {file}"
            assert file.stat().st_size == entry["bytes"], f"Changed file size: {file}"
            assert sha(file) == entry["sha256"], f"Changed artifact: {file}"
            hashed_files += 1
    candidates = subprocess.check_output([
        "git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
        "versions/v19", "evaluations/v19",
    ]).decode().split("\0")
    candidates = sorted(set(filter(None, candidates)))
    for name in candidates:
        file = root / name
        assert preparation.exclusion(Path(name)) is None, f"Runtime/credential artifact: {name}"
        assert file.stat().st_size < 50 * 1024 * 1024, f"Oversized artifact: {name}"
        if file.suffix == ".json":
            json.loads(file.read_text())
        elif file.suffix == ".jsonl":
            jsonl(file)
        try:
            text = file.read_text()
        except UnicodeDecodeError:
            continue
        for pattern in preparation.SECRET_PATTERNS:
            assert re.search(pattern, text) is None, f"Possible credential: {name}"
    summaries, completed, frozen_hashes = 0, 0, 0
    for run in sorted(exports.iterdir()):
        if not run.is_dir():
            continue
        source_hashes = run / "source-hashes.json"
        if source_hashes.exists() and (run / "frozen-source").is_dir():
            for name, expected in json.loads(source_hashes.read_text()).items():
                assert sha(run / "frozen-source" / name) == expected, f"Frozen source changed: {run.name}/{name}"
                frozen_hashes += 1
        summary_path = run / "results/summary.json"
        if not summary_path.exists():
            continue
        summaries += 1
        summary = json.loads(summary_path.read_text())
        coverage, metrics = summary["coverage"], summary["metrics"]
        scores = jsonl(run / "results/scores.jsonl")
        predictions = jsonl(run / "results/official_predictions.jsonl")
        assert len(scores) == coverage["scored_checkpoints"], run.name
        assert len(predictions) == coverage["official_predictions"], run.name
        score_ids = {row["checkpoint_id"] for row in scores}
        prediction_ids = {row["checkpoint_id"] for row in predictions}
        assert len(score_ids) == len(scores) and len(prediction_ids) == len(predictions), run.name
        assert score_ids <= prediction_ids, run.name
        for category, field, metric in [
            ("utility", "utility_correct", "utility_accuracy"),
            ("privacy", "privacy_leak", "privacy_leakage_rate"),
            ("safety", "deletion_leak", "deletion_leakage_rate"),
        ]:
            rows = [row for row in scores if row["query_type"] == category and row.get(field) is not None]
            if rows:
                actual = sum(row[field] is True for row in rows) / len(rows)
                assert abs(actual - metrics[metric]) < 1e-12, f"Metric mismatch: {run.name}/{metric}"
        if summary["complete"]:
            completed += 1
            assert len(scores) == coverage["expected_checkpoints"], run.name
            assert coverage["ingested_source_turns"] == coverage["expected_source_turns"], run.name
            boundaries = json.loads((run / "results/boundary_results.json").read_text())
            assert len(boundaries) == coverage["expected_boundaries"], run.name
            assert all(row["state"] == "complete" for row in boundaries), run.name
    full = exports / "v19-model-controls-full-soclaas-20261104T103839Z"
    for name, expected in json.loads((full / "source-hashes.json").read_text()).items():
        assert sha(system / name) == expected, f"System differs from completed full run: {name}"
    print(json.dumps({
        "manifests_verified": len(manifest_paths), "artifact_hashes_verified": hashed_files,
        "frozen_source_hashes_verified": frozen_hashes, "git_visible_files_checked": len(candidates),
        "summaries_verified": summaries, "completed_runs_verified": completed,
        "credentials_and_oversized_artifacts_found": 0,
    }, indent=2))


if __name__ == "__main__":
    main()
