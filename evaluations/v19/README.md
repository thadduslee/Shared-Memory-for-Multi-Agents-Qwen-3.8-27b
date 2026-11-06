# V19 evaluation exports

These are snapshots copied from `v19-evaluations`; original runs remain untouched.
Every run has an `EXPORT_MANIFEST.json` with SHA-256 hashes and excluded files.
SQLite state, credentials, runtime logs, locks, caches, and compressed archives are omitted.
The JSON/JSONL exports support analysis; they are not resumable database backups.

| Run | Scored / expected | Complete | Utility |
|---|---:|---|---:|
| [v19-cited-attribution-3ep-gpu45-20261104T040026Z](v19-cited-attribution-3ep-gpu45-20261104T040026Z/) | 0 / — | No | — |
| [v19-cited-attribution-3ep-soclaas-20261104T041323Z](v19-cited-attribution-3ep-soclaas-20261104T041323Z/) | 10 / 84 | No | 20.00% |
| [v19-fixed-gpu45-20261103T110826Z](v19-fixed-gpu45-20261103T110826Z/) | 2 / 579 | No | 0.00% |
| [v19-full-gpu345-20261103T061619Z](v19-full-gpu345-20261103T061619Z/) | 0 / — | No | — |
| [v19-full-gpu45-20261103T091300Z](v19-full-gpu45-20261103T091300Z/) | 0 / 579 | No | — |
| [v19-full-gpu45-20261103T091300Z-compat1](v19-full-gpu45-20261103T091300Z-compat1/) | 5 / 579 | No | 0.00% |
| [v19-full-soclaas-20261103T064703Z](v19-full-soclaas-20261103T064703Z/) | 0 / — | No | — |
| [v19-full-soclaas-20261103T071604Z](v19-full-soclaas-20261103T071604Z/) | 0 / 579 | No | — |
| [v19-info-types-3ep-soclaas-20261104T072550Z](v19-info-types-3ep-soclaas-20261104T072550Z/) | 28 / 84 | No | 50.00% |
| [v19-model-controls-full-soclaas-20261104T103839Z](v19-model-controls-full-soclaas-20261104T103839Z/) | 579 / 579 | Yes | 76.19% |
| [v19-privacy-fallback-gpu45-20261103T153843Z](v19-privacy-fallback-gpu45-20261103T153843Z/) | 187 / 579 | No | 2.86% |

## Completed full run

`v19-model-controls-full-soclaas-20261104T103839Z` completed all 579 checkpoints, 567 boundaries, and 4,294 source turns.
Its 78 frozen-source hashes match `../../versions/v19` at export time.
The run used SOCLAAS, local GPUs 0/3, SOCLAAS again, and finally GPUs 0/3; migration records preserve that serving history.
Completion means evaluation coverage: the report still records `degraded: true` and `enrichment_complete: false`.
The bundled judge-comparison report is an earlier 539-checkpoint snapshot, not the final full-run score.

Frozen code, fixture data, configuration manifests, predictions, scores, and recovery records are retained where present.
Historical launcher scripts retain their original machine paths and reference omitted local environment files; configure fresh services before rerunning.

## Verify this bundle

From the repository root, run `python scripts/verify_v19_bundle.py`.
The verifier checks file hashes, JSON exports, score counts, final metrics, and credential/size exclusions.
See `../../versions/v19/SNAPSHOT.md` for reproduction and test commands.
