"""Build `initial_program.txt` -- generation 0 of the evolution -- from the
orchestrator's `Auto Research/templates/memory_system/`.

WHY GENERATED AND NOT CHECKED IN.  The LangGraph pipeline's iteration-0 artifact
IS that template directory, and the whole point of running both harnesses is to
compare them from the same starting point.  A checked-in copy would drift from
the template the moment anyone edits it, and the two runs would silently no
longer be comparable.  Regenerating is one command and always faithful.

Run:  python build_initial.py            # writes ./initial_program.txt
      python build_initial.py --check    # exit 1 if the file is stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pack import pack, read_workspace  # noqa: E402

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE.parent / "Auto Research" / "templates"
OUTPUT = HERE / "initial_program.txt"

# The tests ship with the artifact.  They are not scored -- MGS comes from the
# benchmark, not from the candidate's own suite -- but they are the fastest
# feedback the model has about a change it just made, and a workspace that
# arrives without them invites a rewrite that breaks RBAC silently.
INCLUDED = ("memory_system/*.py", "memory_system/*.sql", "tests/*.py")


def build() -> str:
    files = read_workspace(TEMPLATES, patterns=INCLUDED)
    if not files:
        raise SystemExit(f"no template files found under {TEMPLATES}")
    missing = [
        required for required in ("memory_system/schema.sql", "memory_system/store.py",
                                  "memory_system/agent.py", "memory_system/__init__.py")
        if required not in files
    ]
    if missing:
        raise SystemExit(f"template is missing {missing}; refusing to build a broken generation 0")
    return pack(files)


def check_template_dirs() -> list[str]:
    """Verify every config's `template_dir` still resolves.

    OpenEvolve resolves it against the WORKER process CWD and, when it misses,
    logs one line and falls back to the DEFAULT prompt -- so a run whose path
    went stale still completes, still reports an MGS, and has quietly dropped
    the SQL-only constraint from the system message. That is the failure worth
    a check: it does not look like a failure.
    """
    import re

    problems: list[str] = []
    for config in sorted(HERE.glob("config*.yaml")):
        for match in re.finditer(r'^\s*template_dir:\s*"(.+?)"', config.read_text(encoding="utf-8"), re.M):
            path = Path(match.group(1))
            if not path.is_absolute():
                problems.append(f"{config.name}: template_dir {path} is relative; "
                                "workers resolve it against their own CWD")
            elif not path.is_dir():
                problems.append(f"{config.name}: template_dir {path} does not exist")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the packed file is current")
    parser.add_argument("-o", "--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    document = build()

    problems = check_template_dirs()
    for problem in problems:
        print(f"WARNING: {problem}", file=sys.stderr)

    if args.check:
        if problems:
            return 1
        current = args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        if current != document:
            print(f"{args.output} is stale; re-run build_initial.py", file=sys.stderr)
            return 1
        print(f"{args.output} is current ({len(document)} chars)")
        return 0

    args.output.write_text(document, encoding="utf-8")
    n_files = document.count("# ==== FILE: ")
    print(f"wrote {args.output} -- {n_files} files, {len(document)} chars")
    print("NOTE: set `max_code_length` in config.yaml above that number.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
