# Source AFTER env_local_agents.sh to move the Judge onto local Qwen3.8-27B too.
#
# SELF-JUDGING: the Judge is now the same model as the system under test, and
# under JUDGE_IMPL=gatemem it is PRIMARY on utility. Scores from this setup are
# fine for "did the loop run / did iteration N beat N-1", not for reporting.
#
# It reuses the Evaluator's server (:8000). The Judge only runs after the
# evaluation fan-out has finished, so that server is idle when it is needed,
# and no third GPU is spent.
export JUDGE_ROUTE=openai
export OPENAI_BASE_URL="${JUDGE_BASE_URL:-http://localhost:8000/v1}"
export OPENAI_API_KEY="${VLLM_API_KEY:-sk-local-dummy-key}"
export JUDGE_MODEL="${JUDGE_MODEL_LOCAL:-Qwen/Qwen3.8-27B}"
export JUDGE_TRANSPORT=http
export JUDGE_MAX_TOKENS="${JUDGE_MAX_TOKENS:-2048}"
export USE_LLM_JUDGE=true
echo "  Judge                      -> $OPENAI_BASE_URL ($JUDGE_MODEL)  [local, self-judging]"
