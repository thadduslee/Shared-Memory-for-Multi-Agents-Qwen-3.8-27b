"""Run the evolution with the self-driving Developer wired in.

`openevolve-run.py` cannot do this: `init_client` is a Python callable and the
CLI only reads YAML.  Everything else -- controller, database, cascade evaluator,
checkpointing -- is OpenEvolve's, untouched.

    python gatemem_openevolve/run_evolution.py --iterations 20
    python gatemem_openevolve/run_evolution.py --config gatemem_openevolve/config.dryrun.yaml
    python gatemem_openevolve/run_evolution.py --no-developer   # one-shot mutation instead
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# The iteration workers are separate processes that re-import `developer` by
# name to rebuild the client. They inherit this, so it has to be set before the
# pool starts, not after.
os.environ["PYTHONPATH"] = os.pathsep.join(
    p for p in (str(HERE), os.environ.get("PYTHONPATH", "")) if p
)

from openevolve.config import load_config  # noqa: E402
from openevolve.controller import OpenEvolve  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "config.local.yaml")
    parser.add_argument("--program", type=Path, default=HERE / "initial_program.txt")
    parser.add_argument("--evaluator", type=Path, default=HERE / "evaluator.py")
    parser.add_argument("--output", type=Path, default=HERE / "openevolve_output")
    parser.add_argument("--iterations", type=int, default=None)
    parser.add_argument("--checkpoint", type=Path, default=None)
    parser.add_argument(
        "--no-developer", action="store_true",
        help="use OpenEvolve's one-shot prompt->diff mutation instead of the tool loop",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    config = load_config(args.config)

    if not args.no_developer:
        from developer import init_developer_client

        for model_cfg in config.llm.models:
            model_cfg.init_client = init_developer_client
        # A tool loop is one prompt with many completions inside it, so the
        # response is a full workspace rather than a diff. Forcing it here
        # rather than trusting the YAML means --no-developer and the default
        # cannot disagree with the config about what comes back.
        config.diff_based_evolution = False
        print(f"Developer loop ENABLED -> {config.llm.models[0].name} "
              f"@ {config.llm.models[0].api_base or config.llm.api_base}")
    else:
        print("Developer loop DISABLED: one-shot prompt->diff mutation")

    controller = OpenEvolve(
        initial_program_path=str(args.program),
        evaluation_file=str(args.evaluator),
        config=config,
        output_dir=str(args.output),
    )

    best = asyncio.run(
        controller.run(iterations=args.iterations, checkpoint_path=str(args.checkpoint)
                       if args.checkpoint else None)
    )

    print("\nBest program metrics:")
    for key, value in (best.metrics if best else {}).items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
