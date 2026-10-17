"""A stub OpenAI-compatible endpoint, so the whole loop can be run with no model.

WHY A SERVER AND NOT A FAKE CLIENT.  OpenEvolve can be handed a custom client
via `LLMModelConfig.init_client` (`llm/ensemble.py`), but the iteration workers
are separate processes that rebuild the config from a dict, so an injected
callable has to survive that round trip -- and, more to the point, a fake client
would skip `llm/openai.py` entirely. That module is where the SoCLaaS run will
actually break: the full-vs-bare model slug, the retry policy, the timeout. A
stub SERVER exercises the same transport the real run uses, so a dry run that
passes means the plumbing is right and only the model is missing.

WHAT IT RETURNS.  A deterministic SEARCH/REPLACE diff that makes one small, real
change to the packed workspace: it adds an index to `schema.sql`. That is enough
to prove the loop end to end -- prompt built, diff parsed, workspace unpacked,
constraint checked, 50 shards run, MGS scored, program inserted into MAP-Elites --
and the change is a no-op for the score, which is the point: a dry run that
moved MGS would leave you unsure whether the harness or the edit did it.

Usage:
    python dryrun_server.py --port 811434          # terminal 1
    GATEMEM_LLM_API_BASE=http://127.0.0.1:811434/v1 \
    GATEMEM_LLM_API_KEY=dryrun GATEMEM_LLM_MODEL=dryrun \
    python ../openevolve/openevolve-run.py ...     # terminal 2
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Both halves must match the packed initial program byte for byte, or
# `apply_diff` finds nothing and the iteration is discarded with "No valid diffs".
SEARCH = """CREATE INDEX IF NOT EXISTS idx_record_terms_hash  ON record_terms(term_hash);"""

REPLACE = """CREATE INDEX IF NOT EXISTS idx_record_terms_hash  ON record_terms(term_hash);
-- DRY RUN: added by dryrun_server.py to prove a diff round-trips into the
-- workspace. Covers the (patient, seq) lookup that as-of queries do.
CREATE INDEX IF NOT EXISTS idx_records_patient_seq ON records(patient_id, seq);"""

RESPONSE = f"""The schema indexes `records(episode_id, seq)` but as-of retrieval
also filters by patient, so that path falls back to a scan. Adding the composite
index is a pure-SQL change and keeps the store SQLite-only.

<<<<<<< SEARCH
{SEARCH}
=======
{REPLACE}
>>>>>>> REPLACE
"""


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 with an explicit close, NOT keep-alive. The OpenAI client pools
    # connections, and a keep-alive connection held open by worker 1 blocks
    # worker 2 for the whole request timeout on anything less than a threading
    # server -- which surfaces as "Connection error", i.e. exactly the symptom
    # you would misread as the endpoint being unreachable.
    protocol_version = "HTTP/1.0"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            request = {}

        body = json.dumps({
            "id": "chatcmpl-dryrun",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.get("model", "dryrun"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": RESPONSE},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[dryrun] {self.path} {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=811434)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[dryrun] stub OpenAI endpoint on http://127.0.0.1:{args.port}/v1")
    print("[dryrun] every completion returns the same schema-index diff")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dryrun] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
