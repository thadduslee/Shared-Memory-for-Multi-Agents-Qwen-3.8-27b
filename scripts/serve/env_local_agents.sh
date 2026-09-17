# Source this: `source scripts/serve/env_local_agents.sh`
#
# Puts FOUR of the five nodes on the local Qwen3.8-27B servers and leaves the
# Judge on OpenRouter.
#
#   Architect  -> localhost:8002  (dsh transport)
#   Developer  -> localhost:8002  (http, forced -- see below)
#   Critic     -> localhost:8002  (dsh transport)
#   Evaluator  -> localhost:8000  (dsh transport)
#   Judge      -> OpenRouter, openai/gpt-4.1        <- unchanged, needs a key
#
# Requires both servers:
#   scripts/serve/serve_evaluator.sh    # GPU 0, port 8000
#   scripts/serve/serve_agents.sh       # GPU 1, port 8002

export VLLM_API_KEY="${VLLM_API_KEY:-sk-local-dummy-key}"

export VLLM_BASE_URL="${VLLM_BASE_URL:-http://localhost:8000/v1}"
# MUST carry the "Qwen/" prefix or the thinking-disable does not fire.
export EVALUATOR_MODEL="${EVALUATOR_MODEL:-Qwen/Qwen3.8-27B}"
export VLLM_MAX_CONCURRENCY="${VLLM_MAX_CONCURRENCY:-48}"

export AGENT_BASE_URL="${AGENT_BASE_URL:-http://localhost:8002/v1}"
export AGENT_API_KEY_ENV="${AGENT_API_KEY_ENV:-VLLM_API_KEY}"
export AGENT_ROUTE="${AGENT_ROUTE:-agent}"
export AGENT_MAX_CONCURRENCY="${AGENT_MAX_CONCURRENCY:-4}"

export ARCHITECT_MODEL="${ARCHITECT_MODEL:-Qwen/Qwen3.8-27B}"
export DEVELOPER_MODEL="${DEVELOPER_MODEL:-Qwen/Qwen3.8-27B}"
export CRITIC_MODEL="${CRITIC_MODEL:-Qwen/Qwen3.8-27B}"

# COMPLETION BUDGETS ARE MANDATORY ON A LOCAL SERVER, and the shipped default
# (uncapped) does not work here. With max_tokens unset the profile passes None,
# the dsh harness substitutes its OWN default of 256000 -- sized for DeepSeek's
# 1M context -- and vLLM rejects the request outright:
#
#   max_tokens=256000 cannot be greater than max_model_len=max_total_tokens=131072
#   (parameter=max_tokens, value=256000)  [400 INVALID_REQUEST]
#
# The node then reports "empty response" and the iteration halts at MGS=0,
# which reads as a model failure rather than a config one. These numbers are
# completion caps; prompt + completion must fit AGENT_MAX_MODEL_LEN (131072).
export ARCHITECT_MAX_TOKENS="${ARCHITECT_MAX_TOKENS:-32768}"
export DEVELOPER_MAX_TOKENS="${DEVELOPER_MAX_TOKENS:-16384}"
# 65536, not 16384: at 16384 the Critic spent its whole budget on (unseen)
# reasoning and returned no text, so every iteration fell back to an
# attribution-only critique (runs_local_20iter_critic16k_aborted/iter_1).
export CRITIC_MAX_TOKENS="${CRITIC_MAX_TOKENS:-65536}"
# The Evaluator needs one too, and for the same reason -- it is also on dsh, and
# its server's max_model_len is 65536, so the harness's 256000 default is a 400.
# That failure surfaces as ANSWERER DEGRADED ("the evaluator model did not
# respond"), which points at reachability rather than at a rejected request.
# Predictions are short; this is a ceiling, not a target.
export EVALUATOR_MAX_TOKENS="${EVALUATOR_MAX_TOKENS:-4096}"

# dsh stays the default for the agentic nodes, per the brief. The harness
# reaches the local server the same way it reaches OpenRouter: `provider` is a
# CORDIS ROUTE NAME (`deepseek-official`) and `base_url` alone decides the
# endpoint -- see the DSHProfile docstring in harness/dsh_client.py.
# Qwen3.8 accepts only xhigh/medium/low; the harness's own default is "high",
# which is a 400 and surfaces as "empty response". See DSH_REASONING_EFFORT in
# config.py. "low" for the same reason OPENROUTER_REASONING_EFFORT is low.
export DSH_REASONING_EFFORT="${DSH_REASONING_EFFORT:-low}"

# A LOCAL 27B ON dsh IS MUCH SLOWER THAN DeepSeek ON OpenRouter, AND THE
# SHIPPED TIMEOUTS ARE SIZED FOR THE LATTER.
# Measured here: the Architect generates at ~64 tok/s single-stream, and on the
# dsh transport it does not issue one call -- the harness runs its own
# multi-step agent loop (observed: step 5, 50 events, repeated `glob` calls).
# One Architect turn blew straight through DSH_ARCHITECT_TIMEOUT_S=1800:
#
#   dsh timeout profile=architect after 1800s (not retried -- raise its budget)
#
# The loop then correctly classifies that as TRANSPORT rather than design and
# re-runs the same iteration -- which times out again. A systematic overrun
# looks exactly like a flaky endpoint from inside the router.
export DSH_ARCHITECT_TIMEOUT_S="${DSH_ARCHITECT_TIMEOUT_S:-5400}"
export DSH_DEFAULT_TIMEOUT_S="${DSH_DEFAULT_TIMEOUT_S:-2400}"
export DSH_DEVELOPER_TIMEOUT_S="${DSH_DEVELOPER_TIMEOUT_S:-5400}"

# IF YOU WANT THIS TO FINISH IN REASONABLE TIME, OR IF LEAST PRIVILEGE MATTERS,
# SET AGENT_TRANSPORT=http. Two independent reasons, both measured here:
#   * SPEED. The Architect needs one document, not an agent loop. On http that
#     is a single call; on dsh it was 30+ minutes of harness turns.
#   * PRIVILEGE. The installed SDK takes `patches` (an OVERLAY) where this code
#     was written for `cordis` (a REPLACEMENT), so a profile can only ADD tools.
#     The Architect -- capabilities set empty, composition mounting neither
#     dsh-tool-fs nor dsh-bash-local -- still called `glob`. On the http path a
#     node has exactly the tools its caller passes, so a node that passes none
#     has none. See harness/dsh_client.py `_adapt_sdk_kwargs`.

export AGENT_TRANSPORT="${AGENT_TRANSPORT:-dsh}"
export DSH_BASE_URL="${DSH_BASE_URL:-$AGENT_BASE_URL}"
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-$AGENT_BASE_URL}"
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-$VLLM_API_KEY}"

# THE DEVELOPER CANNOT BE ON dsh, AND THIS IS NOT A SETTING.
# It is handed a real OpenAI tool schema (nodes/dev_tools.py TOOL_SCHEMAS) and
# `nodes/_transport.transport_for` routes ANY tool-carrying call over http
# whatever AGENT_TRANSPORT says -- dsh runs its own tool loop over mounted
# Cordis plugins and has no seam that would accept a schema of ours.
# Pretending otherwise fails SILENTLY: the call succeeds, the model is handed
# no tools, and it emits tool-call syntax as prose. That is run-280411c75b99 --
# 27 such blocks, zero parsed, zero bytes written, byte-identical code in all
# four iterations. Setting this to dsh would not change the behaviour; it is
# written out so the "using the http transport instead" log line reads as
# intended rather than as a fault.
export DEVELOPER_TRANSPORT="${DEVELOPER_TRANSPORT:-http}"

export EVAL_TRANSPORT="${EVAL_TRANSPORT:-dsh}"

# The Judge stays remote. Under JUDGE_IMPL=gatemem it is PRIMARY on utility --
# it can overturn the rule matcher -- so moving it onto the same local model as
# the system under test would let a shared blind spot become a score rather
# than a caught error.
export JUDGE_ROUTE="${JUDGE_ROUTE:-openrouter}"
export JUDGE_MODEL="${JUDGE_MODEL:-openai/gpt-4.1}"   # OpenRouter needs the vendor prefix
export JUDGE_TRANSPORT="${JUDGE_TRANSPORT:-http}"

export MOCK_MODE="${MOCK_MODE:-false}"

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
    echo "  note: OPENROUTER_API_KEY is unset -- the Judge (openai/gpt-4.1) will fail." >&2
    echo "        Everything else runs locally. USE_LLM_JUDGE=false scores on rules" >&2
    echo "        alone, at the cost of the utility rescue." >&2
fi

echo "  Architect/Developer/Critic -> $AGENT_BASE_URL ($ARCHITECT_MODEL)"
echo "  Evaluator                  -> $VLLM_BASE_URL ($EVALUATOR_MODEL)"
echo "  Judge                      -> $JUDGE_ROUTE ($JUDGE_MODEL)"
echo "  transports: agent=$AGENT_TRANSPORT developer=$DEVELOPER_TRANSPORT eval=$EVAL_TRANSPORT judge=$JUDGE_TRANSPORT"

# This checkout IS the GateMem repo, so the medical split is one level up.
# Default is ~/GateMem, which does not exist here.
export GATEMEM_REPO="${GATEMEM_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
