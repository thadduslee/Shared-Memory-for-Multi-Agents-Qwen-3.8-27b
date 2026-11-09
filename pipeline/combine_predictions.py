#!/usr/bin/env python3
"""Merge a run's per-episode predictions into one source directory for evaluation.gatemem_medical.rejudge.

    python pipeline/combine_predictions.py runs/<domain> [--out runs/<domain>/combined-predictions]

The harness rejudge reads one directory holding official_predictions.jsonl and
manifest.json. Episodes run as separate harness runs, so this writes:

  official_predictions.jsonl  every episode's prediction lines, unchanged, in the dataset's
                              checkpoint order (for medical: sha256 07a4554f..., the exact
                              source of runs/medical/gpt-4.1-rejudge)
  manifest.json               the domain and each episode's manifest and predictions sha256
                              (deterministic, so a resumed rejudge sees the same source)

The archived medical rejudge recorded a different combined manifest digest (8ea8d612...);
that file was not kept, so only its predictions digest is reproducible.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def combine(run_dir, out=None):
    run_dir = Path(run_dir).resolve()
    domain = common.run_domain(run_dir)
    out = Path(out or run_dir / 'combined-predictions')
    lines, files = common.combined_predictions(run_dir, domain)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'official_predictions.jsonl').write_text(''.join(lines))
    manifest = {'format': 'evolving-privacy-mem-combined-predictions-v1', 'domain': domain,
                'checkpoints': len(lines), 'episodes': files}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=1) + '\n')
    return out, domain, common.sha256(out / 'official_predictions.jsonl')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run_dir')
    ap.add_argument('--out')
    args = ap.parse_args(argv)
    out, domain, digest = combine(args.run_dir, args.out)
    print(f'{domain}: wrote {out} (official_predictions sha256 {digest})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
