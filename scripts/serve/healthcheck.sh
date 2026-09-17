#!/usr/bin/env bash
# Pre-flight for a long run. Cheap, and it catches the failures that otherwise
# only show up as a wrong number three hours in.
#
#   scripts/serve/healthcheck.sh            # check the evaluator on :8000
#   scripts/serve/healthcheck.sh 8001       # check the judge
#
# Generalises the repo's verify_qwen38.sh. The checks, and what each one is
# standing guard over:
#
#   served-model-name matches  -> a mismatch 404s EVERY request; the run then
#                                 reports render_degraded, or worse, does not.
#   thinking-disable fires     -> config.vllm_chat_template_kwargs only emits
#                                 enable_thinking=false for a "Qwen/"-prefixed
#                                 name. The bare slug silently skips it.
#   reasoning split out        -> without --reasoning-parser the chain-of-thought
#                                 is concatenated into `content`, which corrupts
#                                 the graded answer and breaks every JSON parse.
#   content is non-empty       -> an empty answer for a checkpoint that DID
#                                 retrieve reads downstream as a retrieval
#                                 failure, which is a lie about the system.
#   loose JSON parse accepts   -> the harness parses `content` as JSON.

set -euo pipefail
PORT="${1:-${EVALUATOR_PORT:-8000}}"
KEY="${LOCAL_API_KEY:-sk-local-dummy-key}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${HEALTHCHECK_PYTHON:-/home/lweilun/miniconda3/envs/gatemem-auto/bin/python}"

"$PY" - "$PORT" "$KEY" "$REPO_ROOT" <<'PY'
import json, sys, urllib.request

port, key, repo_root = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, f"{repo_root}/Auto Research")
base = f"http://127.0.0.1:{port}/v1"

def get(path):
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

def post(path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.load(r)

try:
    served = [m["id"] for m in get("/models")["data"]]
except Exception as exc:
    print(f"  [FAIL] server answering on :{port} -> {exc}")
    sys.exit(1)
print(f"  [PASS] server answering on :{port}; serving {served}")

checks = {}

# --- the slug contract -------------------------------------------------
import config
want = config.EVALUATOR_MODEL
checks["EVALUATOR_MODEL matches a served name"] = want in served
if want not in served:
    print(f"\n  EVALUATOR_MODEL={want!r} is not served here ({served}).")
    print("  Every request would 404. Fix with:")
    print(f"    export EVALUATOR_MODEL={served[0]}\n")

# --- the thinking-disable ----------------------------------------------
kwargs = config.vllm_chat_template_kwargs(want)
checks["thinking-disable fires for this slug"] = kwargs == {"enable_thinking": False}
if kwargs != {"enable_thinking": False}:
    print(f"\n  vllm_chat_template_kwargs({want!r}) -> {kwargs}")
    print("  Thinking will NOT be disabled. The evaluator reasons past the read")
    print("  timeout and records an empty answer for every checkpoint that")
    print(f"  retrieved. Serve and export the prefixed name instead ({served[0]!r}).\n")

# --- a real generation --------------------------------------------------
prompt = (
    "Return STRICT JSON only with keys: summary (string), keywords (array of strings).\n"
    "Keep summary <= 20 words.\n\n"
    "text: Please delete my old address from the record, I moved last month.\n"
)
body = {"model": served[0], "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2, "max_tokens": 2048}
if kwargs:
    body["chat_template_kwargs"] = kwargs

msg = post("/chat/completions", body)["choices"][0]["message"]
content = msg.get("content") or ""
# vLLM 0.26 returns the split trace as "reasoning"; older builds call it
# "reasoning_content". Accept either -- verify_qwen38.sh learned this the hard way.
reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""

checks["content has no <think> tags"] = "<think>" not in content and "</think>" not in content
checks["content is non-empty"] = bool(content.strip())

def parse_loose(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        try:
            return json.loads(text[start:end + 1]) if start >= 0 < end else None
        except Exception:
            return None

checks["content parses as JSON"] = isinstance(parse_loose(content), dict)

print()
for name, ok in checks.items():
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
print(f"\n  reasoning_chars={len(reasoning)}  content_preview={content[:90]!r}")
ok = all(checks.values())
print("\nRESULT:", "PASS - safe to start a run" if ok else "FAIL - do not start a run")
sys.exit(0 if ok else 1)
PY
