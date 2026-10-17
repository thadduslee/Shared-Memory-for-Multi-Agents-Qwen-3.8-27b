"""The self-driving Developer, as an OpenEvolve LLM client.

WHAT THIS REPLACES
------------------
OpenEvolve's mutation step is one prompt and one reply: `_run_iteration_worker`
builds a prompt, calls `generate_with_context` once, and parses a diff out of
whatever comes back.  The model never sees a file, never runs a test, and never
finds out whether the thing it wrote imports.

`Auto Research/nodes/developer.py` is the opposite: one node, one conversation,
a real tool schema, and a loop that ends when the model says it is done or the
budget runs out.  The README there records what the one-shot version cost --
`runs_multi/run-280411c75b99` emitted 27 tool calls as plain text, parsed zero,
and evaluated byte-identical code in all four iterations.

So this class satisfies OpenEvolve's `LLMInterface` and spends the call running
that loop instead.  From OpenEvolve's side nothing changed: it asks for a
completion and gets back a program.  From the model's side it now has a
workspace it can read, edit, test and smoke-run before it answers.

WHY IT RETURNS A FULL REWRITE AND NOT A DIFF
--------------------------------------------
The loop's product is a DIRECTORY, not a reply.  Asking the model to also
narrate its changes as SEARCH/REPLACE blocks would mean emitting ~79k characters
of workspace a second time, from memory, after it has already written them to
disk -- and any transcription slip silently becomes the mutation.  Instead the
client repacks the workspace off disk and returns that, so the bytes OpenEvolve
scores are exactly the bytes the model tested.  `config.local.yaml` therefore
sets `diff_based_evolution: false`; the token cost of that decision is zero,
because the packed text is assembled locally and never passes through the model.

WHAT THE DEVELOPER MAY NOT SEE
------------------------------
`smoke` runs the agent over real checkpoints and reports the ACTIONS it took and
any crash, and nothing else.  It does not score.  The gold fields -- expected
action, judge spec, leak targets -- stay on the scoring side of the wall exactly
as they do in the LangGraph pipeline, where the Developer runs tests and the
Judge is a different node with a different profile.  A Developer that could
score would optimise the 50-checkpoint dev slice directly, and the dev gate would
stop measuring anything.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent
ORCHESTRATOR = PROJECT_ROOT / "Auto Research"

for _path in (str(_HERE), str(ORCHESTRATOR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from openevolve.llm.base import LLMInterface  # noqa: E402

import evaluator as gatemem_evaluator  # noqa: E402
from pack import pack, read_workspace, unpack, write_workspace  # noqa: E402

logger = logging.getLogger(__name__)

# OpenEvolve's iteration workers are fresh processes and `_worker_init` never
# configures logging, so the root logger sits at WARNING and every INFO line this
# module emits is discarded. In a real run that means the per-turn trace -- the
# only way to tell a read-loop from a truncation from a clean finish -- is
# invisible, and the first 20-iteration run produced exactly one diagnosable
# line. Attach our own handler, once, and do not propagate into the parent's.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s DEV[%(process)d] %(message)s"))
    logger.addHandler(_handler)
    logger.propagate = False
logger.setLevel(logging.INFO)

MAX_TURNS = int(os.environ.get("GATEMEM_DEV_MAX_TURNS", "24"))
TEST_TIMEOUT = int(os.environ.get("GATEMEM_DEV_TEST_TIMEOUT", "180"))
SMOKE_N = int(os.environ.get("GATEMEM_DEV_SMOKE_N", "8"))
MAX_TOOL_OUTPUT = 6000
MAX_NUDGES = int(os.environ.get("GATEMEM_DEV_MAX_NUDGES", "2"))
# After this many turns with nothing written, the loop interrupts. The first
# live episode spent turns 0-6 reading all 1200 lines of the workspace and then
# turns 7+ RE-reading ranges it already had -- a read-loop, not deliberation.
# Waiting for it to stop calling tools does not catch that, because it never
# stops; it just never writes either.
READ_BUDGET = int(os.environ.get("GATEMEM_DEV_READ_BUDGET", "8"))
# Turns to wait after a nudge before sending another. Without it the proactive
# check re-fires on EVERY turn past the read budget, so both nudges land
# back-to-back (observed: turns 8 and 9) and the model gets exactly one turn to
# react to the first before the budget is gone. A nudge the model has not had
# time to answer is not evidence that nudging does not work.
NUDGE_COOLDOWN = int(os.environ.get("GATEMEM_DEV_NUDGE_COOLDOWN", "3"))

TRUNCATION_NOTICE = """Your last reply was CUT OFF at the token limit before you
finished writing the tool call, so nothing was applied.

Make a SMALLER edit. Use `replace` with the minimum surrounding context that is
unique -- never `write_file` on a large file, which cannot fit in one reply.
Split the change into several `replace` calls if you need to."""

NUDGE = """You have not changed a single file yet. Reading is not the deliverable
-- a workspace identical to its parent is discarded, and this iteration is wasted.

Pick the ONE change you are most confident in from what you have already read and
make it NOW with `replace` or `write_file`. Then `run_tests`, then `finish`.
Do not read anything else first."""

# The packed workspace as it appears inside OpenEvolve's prompt: the delimiters
# are the only reliable anchor, because the surrounding template text is
# configurable and the fence language is too.
_PACKED_RE = re.compile(
    r"(# ==== FILE: .+? ====\n.*?)(?=\n```|\Z)", re.DOTALL
)


# ==========================================================================
# Tools
# ==========================================================================

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List every file in the workspace with its size in bytes.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a workspace file. Use start/end to read part of a large file; "
                "line numbers are 1-based and inclusive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "e.g. memory_system/store.py"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace",
            "description": (
                "Replace an exact string in a file with another. `search` must occur "
                "EXACTLY ONCE in the file. This is the cheapest way to make a change; "
                "prefer it over write_file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "search": {"type": "string"},
                    "replace": {"type": "string"},
                },
                "required": ["path", "search", "replace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Overwrite a file with new contents, or create a new one.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run the workspace's own pytest suite. Returns the real output.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "smoke",
            "description": (
                "Run the memory system over a few real checkpoints and report the action "
                "it took on each, plus any crash and traceback. This does NOT score: no "
                "expected action, no required facts, no leak targets. Use it to find out "
                "whether your change imports and runs."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Call this when the workspace is in the state you want evaluated. "
                "Summarise what you changed and why."
            ),
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]


class Toolbox:
    """The tools, bound to one workspace.  Every one returns a string."""

    def __init__(self, root: Path):
        self.root = root
        self.files_changed: set[str] = set()
        self._reads: set[tuple] = set()
        self.finished: str | None = None

    # -- helpers ----------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        """Reject anything outside the workspace.

        The model writes these paths, so they are untrusted: `../../.ssh/...`
        costs it nothing to try and the workspace is the only thing standing
        between a bad generation and the rest of the filesystem.
        """
        target = (self.root / path).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise ValueError(f"path escapes the workspace: {path!r}")
        return target

    @staticmethod
    def _clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
        """Keep the TAIL of long output.

        A pytest run's useful part is the failure summary at the end; clipping
        the head would hand the model the collection banner and cut the reason
        anything failed.
        """
        if len(text) <= limit:
            return text
        return f"... [{len(text) - limit} chars elided] ...\n" + text[-limit:]

    # -- the tools --------------------------------------------------------

    def list_files(self) -> str:
        rows = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and not path.name.startswith("_eval_runner"):
                rows.append(f"{path.relative_to(self.root)}  ({path.stat().st_size} bytes)")
        return "\n".join(rows)

    def read_file(self, path: str, start: int | None = None, end: int | None = None) -> str:
        target = self._resolve(path)
        # Re-reading a range it already has is the read-loop's fuel. Saying so
        # costs one line and is the only signal the model gets that it is going
        # in circles -- it cannot see its own transcript length.
        key = (path, start, end)
        repeat = key in self._reads
        self._reads.add(key)
        if not target.is_file():
            return f"ERROR: no such file: {path}"
        lines = target.read_text(encoding="utf-8").splitlines()
        lo = max(1, start or 1)
        hi = min(len(lines), end or len(lines))
        numbered = "\n".join(f"{n:5d}| {lines[n - 1]}" for n in range(lo, hi + 1))
        header = f"{path} (lines {lo}-{hi} of {len(lines)})\n"
        if repeat:
            header = ("NOTE: you have already read this exact range. Stop reading and "
                      "make your change.\n") + header
        return header + self._clip(numbered)

    def replace(self, path: str, search: str, replace: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            return f"ERROR: no such file: {path}"
        body = target.read_text(encoding="utf-8")
        count = body.count(search)
        if count == 0:
            return ("ERROR: `search` not found. It must match the file byte for byte, "
                    "including indentation. Read the file again and copy the text exactly.")
        if count > 1:
            return (f"ERROR: `search` occurs {count} times; it must be unique. "
                    "Include more surrounding lines to disambiguate.")
        target.write_text(body.replace(search, replace), encoding="utf-8")
        self.files_changed.add(path)
        return f"OK: replaced 1 occurrence in {path}"

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.files_changed.add(path)
        return f"OK: wrote {len(content)} bytes to {path}"

    def run_tests(self) -> str:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "tests"],
                cwd=str(self.root), capture_output=True, text=True, timeout=TEST_TIMEOUT,
                env={**os.environ, "PYTHONPATH": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: test suite exceeded {TEST_TIMEOUT}s and was killed."
        except FileNotFoundError:
            return "ERROR: pytest is not installed in this environment."
        return self._clip(f"exit={proc.returncode}\n{proc.stdout}\n{proc.stderr}")

    def smoke(self) -> str:
        """Run the agent over a few real checkpoints.  Actions only, never scores."""
        try:
            checkpoints = gatemem_evaluator.select_dev_slice(
                gatemem_evaluator._dataset().checkpoints,
                n=gatemem_evaluator.DEV_SLICE_N,
                seed=gatemem_evaluator.DEV_SLICE_SEED,
            )[:SMOKE_N]
            manifest, episodes = gatemem_evaluator._write_manifests(self.root, checkpoints)
            (self.root / "_eval_runner.py").write_text(
                gatemem_evaluator._load_eval_runner(), encoding="utf-8"
            )
            results = gatemem_evaluator._run_shard(
                self.root, manifest, episodes, [c["checkpoint_id"] for c in checkpoints]
            )
        except Exception as exc:  # the workspace is model-authored; anything can happen
            return f"ERROR: smoke run failed to start: {exc!r}"

        rows = []
        for result in results:
            cid = result.get("checkpoint_id", "?")
            if result.get("error"):
                rows.append(f"{cid}: CRASHED {str(result['error'])[:400]}")
                if result.get("stderr"):
                    rows.append(self._clip(str(result["stderr"]), 1500))
            else:
                answer = str(result.get("answer") or "").replace("\n", " ")[:120]
                rows.append(
                    f"{cid}: action={result.get('action')} "
                    f"records={len(result.get('used_record_ids') or [])} answer={answer!r}"
                )
        crashed = sum(1 for r in results if r.get("error"))
        header = f"{len(results)} checkpoints, {crashed} crashed. No scoring is shown.\n"
        return header + self._clip("\n".join(rows))

    def finish(self, summary: str) -> str:
        self.finished = summary
        return "OK"

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        handler = getattr(self, name, None)
        if handler is None or name.startswith("_"):
            return f"ERROR: no such tool: {name}"
        try:
            return handler(**arguments)
        except TypeError as exc:
            return f"ERROR: bad arguments for {name}: {exc}"
        except Exception as exc:
            return f"ERROR: {name} raised {exc!r}"


# ==========================================================================
# The client
# ==========================================================================

DEVELOPER_SYSTEM = """You are the Developer. You have a real workspace and real
tools: read files, edit them, run the test suite, and smoke-run the memory
system over real checkpoints. Use them. Do not describe a change you have not
made -- make it, then check it.

Work like this:
  1. Read the failing-checkpoint evidence in the task below and decide on ONE
     coherent change. Do not attempt several unrelated ideas in one pass.
  2. Read the code you are about to change before you change it.
  3. Make the change with `replace` (cheapest) or `write_file`.
  4. Run `run_tests`. If it fails, fix it -- a workspace whose tests fail is a
     workspace that will score zero.
  5. Run `smoke`. If anything crashed, fix it.
  6. Call `finish` with a short summary of what you changed and why.

`smoke` reports the ACTION the system took on each checkpoint, never whether it
was right. You are not scored here and you cannot see the gold labels; your job
is a change that is well-motivated, imports, and passes its own tests.

You MUST call `finish` when you are done. If you stop calling tools without
calling `finish`, whatever state the workspace is in gets evaluated anyway."""


class GateMemDeveloperLLM(LLMInterface):
    """An LLMInterface that runs a tool loop instead of a single completion."""

    def __init__(self, model_cfg=None):
        from openai import OpenAI

        self.name = getattr(model_cfg, "name", "Qwen/Qwen3.8-27B")
        # `LLMEnsemble._sample_model` logs `vars(model)["model"]` on every call,
        # so a client without a `.model` ATTRIBUTE raises KeyError before it ever
        # generates. OpenAILLM happens to have one; the interface does not say so.
        # The symptom is "LLM generation failed: 'model'" on every iteration --
        # a one-word message that looks like a config problem and is not.
        self.model = self.name
        self.max_tokens = getattr(model_cfg, "max_tokens", None) or 8000
        self.temperature = getattr(model_cfg, "temperature", None)
        self.timeout = getattr(model_cfg, "timeout", None) or 900
        self.weight = getattr(model_cfg, "weight", 1.0)
        self.client = OpenAI(
            base_url=getattr(model_cfg, "api_base", None),
            api_key=getattr(model_cfg, "api_key", None) or "none",
            timeout=self.timeout,
        )
        logger.info("GateMemDeveloperLLM: %s via %s", self.name, getattr(model_cfg, "api_base", None))

    # -- LLMInterface -----------------------------------------------------

    async def generate(self, prompt: str, **kwargs) -> str:
        return await self.generate_with_context(
            system_message=kwargs.pop("system_message", "") or "",
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )

    async def generate_with_context(self, system_message: str, messages: list[dict], **kwargs) -> str:
        import asyncio

        return await asyncio.get_event_loop().run_in_executor(
            None, self._run, system_message, messages
        )

    # -- the loop ---------------------------------------------------------

    def _run(self, system_message: str, messages: list[dict]) -> str:
        task = "\n\n".join(m.get("content", "") for m in messages)
        parent = _extract_packed(task)
        if parent is None:
            # No workspace in the prompt means the templates changed shape and
            # this client is reading the wrong thing. Returning empty makes
            # OpenEvolve discard the iteration with a clear error rather than
            # evaluating some half-parsed text.
            logger.error("Developer: no packed workspace found in the prompt")
            return ""

        root = Path(tempfile.mkdtemp(prefix="gatemem_dev_"))
        try:
            write_workspace(unpack(parent), root)
            tools = Toolbox(root)
            transcript: list[dict[str, Any]] = [
                {"role": "system", "content": system_message + "\n\n" + DEVELOPER_SYSTEM},
                {"role": "user", "content": _strip_packed(task)},
            ]

            started = time.time()
            nudges = 0
            last_nudge_turn = -99
            for turn in range(MAX_TURNS):
                try:
                    response = self.client.chat.completions.create(
                        model=self.name,
                        messages=transcript,
                        tools=TOOLS,
                        tool_choice="auto",
                        max_tokens=self.max_tokens,
                        **({"temperature": self.temperature} if self.temperature is not None else {}),
                    )
                except Exception as exc:
                    logger.warning("Developer turn %d: transport error %r", turn, exc)
                    break

                message = response.choices[0].message
                calls = message.tool_calls or []

                # finish_reason and the token split are the difference between
                # "the model chose to stop" and "the model was cut off mid-thought".
                # An empty reply looks identical either way from the content alone.
                finish = response.choices[0].finish_reason
                usage = response.usage
                reasoning = getattr(message, "reasoning_content", None) or ""
                logger.info(
                    "turn %02d finish=%s prompt_tok=%s completion_tok=%s reasoning_chars=%d calls=%d",
                    turn, finish,
                    getattr(usage, "prompt_tokens", "?"),
                    getattr(usage, "completion_tokens", "?"),
                    len(reasoning), len(calls),
                )
                if finish == "length" and not calls:
                    # Tell it what happened. Re-prompting identically reproduces
                    # the truncation; asking for a smaller edit does not. A
                    # `write_file` of store.py is ~9k tokens on its own, which is
                    # why the model reaches the ceiling without emitting a call.
                    transcript.append({"role": "user", "content": TRUNCATION_NOTICE})
                    # Cut off before it could emit a tool call. Nothing about the
                    # workspace caused this and re-prompting identically will
                    # reproduce it, so say so plainly rather than counting it as
                    # the model declining to act.
                    logger.warning(
                        "turn %02d was TRUNCATED at max_tokens=%s before any tool call",
                        turn, self.max_tokens,
                    )

                # Omit `tool_calls` entirely when there are none. An empty LIST
                # is not the same as an absent key to a chat template: Qwen's
                # renders an empty tool-call block, and the next turn is then
                # prompted as though a call were in flight. Sending the key only
                # when it has content is what the OpenAI schema actually means.
                entry: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
                if calls:
                    entry["tool_calls"] = [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.function.name, "arguments": c.function.arguments}}
                        for c in calls
                    ]
                transcript.append(entry)

                if not calls:
                    # WHAT IT SAID INSTEAD. An episode that answers in prose is
                    # either describing an edit it never made or asking a
                    # question nobody will answer, and the two need opposite
                    # fixes. Without this line both look like "stopped at turn N".
                    logger.info(
                        "Developer turn %02d replied with prose, no tool call: %s",
                        turn, (message.content or "")[:600].replace("\n", " "),
                    )
                    # A tool-calling model that stops calling tools has usually
                    # decided it is done -- but an episode that stops having
                    # changed NOTHING has not built anything to evaluate, and
                    # `runs_multi/run-280411c75b99` is what that costs: four
                    # iterations of byte-identical code. So it gets told, once
                    # per nudge, that reading is not the deliverable. It is the
                    # same move as the orchestrator's mandatory gates, minus the
                    # graph edge.
                    if (
                        not tools.files_changed
                        and nudges < MAX_NUDGES
                        and turn - last_nudge_turn >= NUDGE_COOLDOWN
                    ):
                        nudges += 1
                        last_nudge_turn = turn
                        logger.info(
                            "Developer stopped at turn %d having changed nothing; nudge %d/%d",
                            turn, nudges, MAX_NUDGES,
                        )
                        transcript.append({"role": "user", "content": NUDGE})
                        continue
                    logger.info("Developer stopped at turn %d without finish()", turn)
                    break

                for call in calls:
                    try:
                        arguments = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        result = "ERROR: arguments were not valid JSON"
                        arguments = {}
                    else:
                        result = tools.call(call.function.name, arguments)
                    # The single most useful line in the log. An episode that
                    # reads for twenty turns and writes nothing is the documented
                    # failure mode of this whole design, and without this you
                    # cannot tell it apart from one that failed to call tools.
                    logger.info(
                        "  turn %02d %s(%s) -> %s",
                        turn, call.function.name,
                        ", ".join(f"{k}={str(v)[:40]!r}" for k, v in arguments.items()),
                        result.splitlines()[0][:100] if result else "",
                    )
                    transcript.append({
                        "role": "tool", "tool_call_id": call.id, "content": result,
                    })

                # Proactive interrupt: still calling tools, but nothing written.
                if (
                    not tools.files_changed
                    and turn + 1 >= READ_BUDGET
                    and nudges < MAX_NUDGES
                    and tools.finished is None
                    and turn - last_nudge_turn >= NUDGE_COOLDOWN
                ):
                    nudges += 1
                    last_nudge_turn = turn
                    logger.info(
                        "Developer has read for %d turns without writing; nudge %d/%d",
                        turn + 1, nudges, MAX_NUDGES,
                    )
                    transcript.append({"role": "user", "content": NUDGE})

                if tools.finished is not None:
                    logger.info(
                        "Developer finished at turn %d in %.0fs: %d files changed -- %s",
                        turn, time.time() - started, len(tools.files_changed),
                        tools.finished[:200],
                    )
                    break
            else:
                logger.info("Developer hit the %d-turn ceiling", MAX_TURNS)

            if not tools.files_changed:
                # An episode that changed nothing produces a child identical to
                # its parent. OpenEvolve dedups those, so returning it burns an
                # iteration for nothing; say so loudly in the log.
                logger.warning("Developer changed no files; the child will be identical")

            # Clean up our own scaffolding before repacking, or `_eval_runner.py`
            # and the manifests smoke() wrote become part of the evolved artifact.
            for scaffold in ("_eval_runner.py", "checkpoints.stripped.jsonl", "episodes.jsonl"):
                (root / scaffold).unlink(missing_ok=True)
            shutil.rmtree(root / "__pycache__", ignore_errors=True)

            document = pack(read_workspace(root))
            summary = tools.finished or "(the Developer stopped without a summary)"
            return f"{summary}\n\n```python\n{document}\n```"
        finally:
            shutil.rmtree(root, ignore_errors=True)


def _extract_packed(text: str) -> str | None:
    """Pull the packed workspace out of OpenEvolve's prompt."""
    match = _PACKED_RE.search(text)
    return match.group(1) if match else None


def _strip_packed(text: str) -> str:
    """The prompt minus the workspace body.

    The Developer reads files with a tool; leaving 79k characters of workspace in
    the first user message as well would double the context for no information,
    and on a long tool loop that is the difference between fitting and not.
    """
    return _PACKED_RE.sub(
        "[the workspace is on disk -- use list_files and read_file to see it]", text
    )


def init_developer_client(model_cfg):
    """Factory for `LLMModelConfig.init_client`.

    Module level on purpose: the config is pickled to the iteration workers
    (`process_parallel.py::_serialize_config`), and a lambda or a closure would
    not survive that. The worker re-imports this module by name, so the package
    directory has to be on PYTHONPATH -- `run_evolution.py` puts it there.
    """
    return GateMemDeveloperLLM(model_cfg)
