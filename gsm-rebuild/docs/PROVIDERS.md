# Model providers

All model use is optional. By default GSM runs fully offline with deterministic fakes:

| Role | Default | Optional | Contract |
|---|---|---|---|
| Embeddings | `HashingEmbedder` (blake2b feature hashing, 256-d) | `openai_compat` (`/v1/embeddings`), or `none` for lexical-only | `Embedder.embed(texts) -> vectors` |
| Event interpretation | `RuleBasedInterpreter` | `openai_compat` chat | proposals only (`CandidateMemoryOperation`), never committed directly |
| Answer generation | `DeterministicGenerator` | `openai_compat` chat | sees only permitted projections; drafts are verified |
| Semantic release verification | none | `openai_compat` chat | advisory; can only make the outcome stricter |

## Configuration (environment)

```bash
GSM_EMBEDDINGS_PROVIDER=openai_compat      # fake | openai_compat | none
GSM_EMBEDDINGS_MODEL=<embedding model>
GSM_EMBEDDING_DIM=1024                     # must match the model; vectors of other sizes are rejected
GSM_GENERATOR_PROVIDER=openai_compat       # deterministic | openai_compat
GSM_INTERPRETER_PROVIDER=openai_compat     # rules | openai_compat
GSM_SEMANTIC_VERIFIER_PROVIDER=none        # none | openai_compat
GSM_LLM_BASE_URL=http://localhost:8000/v1  # any OpenAI-compatible server (vLLM, SGLang, hosted)
GSM_LLM_MODEL=<chat model>
GSM_LLM_API_KEY=<optional>                 # never commit; read from the environment only
GSM_LLM_TIMEOUT_S=30
GSM_LLM_MAX_RETRIES=2                      # bounded; exponential backoff on 408/409/425/429/5xx/transport errors
GSM_LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'   # merged into every chat request
GSM_LLM_PRICE_INPUT_PER_MTOK=0             # for the evaluation cost estimate
GSM_LLM_PRICE_OUTPUT_PER_MTOK=0
```

`Settings.validate_for_startup()` refuses to start when an `openai_compat` provider lacks its URL or model.
Extra body must be a JSON object.

## Failure behavior

| Provider | On timeout, HTTP error, non-JSON, or schema violation |
|---|---|
| Interpreter | returns `NO_MEMORY_CHANGE`. Invalid individual operations are dropped, never repaired. |
| Generator | raises. The service renders deterministically from the same permitted evidence (`generator_failures` stat). |
| Semantic verifier | raises. The service records `SEMANTIC_VERIFIER_UNAVAILABLE` and replaces any model draft with the deterministic render. |
| Embedder | raises. Index rows are written without vectors, and queries degrade to lexical-only. |

Token counts (`usage`) and latency are recorded per provider (`ProviderMetrics`). Release audits store token
counts, never prompts or outputs.

## Adding a provider

Implement the protocol in `gsm/providers/base.py` (`Embedder`, `EventInterpreter`, `AnswerGenerator`,
`SemanticVerifier`) and wire it in `gsm/providers/factory.py`. Validate all model output with pydantic before
returning it. Raise `ProviderError(category)` with a fixed category string, never with payload text. Give the
provider no database handle. Tests can use `httpx.MockTransport` (see `tests/unit/test_openai_compat.py`).
