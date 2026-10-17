"""Pack a multi-file memory-system workspace into ONE text file, and back.

WHY THIS EXISTS
---------------
OpenEvolve's unit of evolution is a single file: the controller loads one
`initial_program`, the LLM returns one diff or one rewrite, and the evaluator is
handed one path (`process_parallel.py::_run_iteration_worker`).  The artifact we
actually evolve is a package -- `schema.sql`, `store.py`, `agent.py` -- and the
SQL schema is the part we most want the model to touch, so collapsing it into a
lone .py would hide it inside a string literal.

So the "program" is a PACKED DOCUMENT: the files concatenated under line-anchored
delimiters.  OpenEvolve's diff mode is pure text SEARCH/REPLACE, so it operates
on this without knowing anything about the format; the evaluator unpacks it back
into a real directory before anything runs.

THE DELIMITER IS DELIBERATELY UGLY.  It has to be a string that cannot occur
inside SQL, Python, or a docstring by accident, because a false positive silently
splits one file into two and the workspace stops importing.  A line that begins
with `# ==== FILE:` and ends with `====` at column 0 is the compromise: it is a
comment in both Python and (with `--`) nothing in SQL, so we anchor on the whole
line rather than trying to be a valid comment in every language at once.
"""

from __future__ import annotations

import re
from pathlib import Path

DELIM_PREFIX = "# ==== FILE: "
DELIM_SUFFIX = " ===="

# Anchored at line start; the path is non-greedy so a path containing " ====" is
# not silently truncated (it would be a pathological name, but the cost of being
# wrong here is a workspace that does not import, with no error pointing at it).
_DELIM_RE = re.compile(
    r"^" + re.escape(DELIM_PREFIX) + r"(?P<path>.+?)" + re.escape(DELIM_SUFFIX) + r"\s*$",
    re.MULTILINE,
)


def pack(files: dict[str, str]) -> str:
    """Render `{relative_path: text}` as one packed document.

    Order is sorted, not insertion order: the packed text is compared against
    the parent's byte-for-byte by OpenEvolve's dedup (`database.py`), and an
    ordering that depended on dict construction would make an identical
    workspace look like a new program.
    """
    parts: list[str] = []
    for path in sorted(files):
        body = files[path]
        if not body.endswith("\n"):
            body += "\n"
        parts.append(f"{DELIM_PREFIX}{path}{DELIM_SUFFIX}\n{body}")
    return "\n".join(parts)


def unpack(document: str) -> dict[str, str]:
    """Inverse of `pack`.  Raises ValueError on a document with no delimiters.

    A model that returns a bare rewrite with the delimiters stripped produces a
    workspace with no files, which imports nothing and scores 0 -- indistinguish-
    able from a genuinely bad design.  Raising here turns that into a named
    failure the evaluator reports as an artifact instead.
    """
    matches = list(_DELIM_RE.finditer(document))
    if not matches:
        raise ValueError(
            "packed document contains no '# ==== FILE: <path> ====' delimiters; "
            "the model probably returned a bare file instead of the packed workspace"
        )

    files: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(document)
        path = match.group("path").strip()
        _reject_traversal(path)
        files[path] = document[start:end].lstrip("\n")
    return files


def _reject_traversal(path: str) -> None:
    """The packed path becomes a real filesystem path, so it is untrusted input.

    The model writes these delimiters itself.  `../../../.ssh/authorized_keys`
    costs nothing to write and everything to have written, and an absolute path
    would escape the per-program temp workspace that isolates one candidate from
    the next.
    """
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"unsafe path in packed document: {path!r}")


def read_workspace(root: Path, patterns: tuple[str, ...] = ("**/*.py", "**/*.sql")) -> dict[str, str]:
    """Collect a real directory into the `{path: text}` shape `pack` wants."""
    files: dict[str, str] = {}
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            files[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
    return files


def write_workspace(files: dict[str, str], root: Path) -> None:
    """Materialise a packed workspace on disk under `root`."""
    for path, body in files.items():
        _reject_traversal(path)
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
