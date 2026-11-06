# v8 t061 model-service recovery

The full v8 run completed 60 sources and one official checkpoint before its main
model service failed at 18:28:38 Singapore time. The archived
[server log](model-server-before-recovery.log) contains the native XGrammar fatal
error at `earley_parser.cc:209`: unsupported grammar element type 5. The model
engine subsequently exited, the API returned 500 and then shut down. Client
retries ultimately reported `ProcessorFailure: ... transport URLError` because
port 8000 no longer had a listener. The saved permission job stayed in
`processor_error`; it did not publish an invalid proposal.

The main model launcher now explicitly selects the installed Guidance backend
(`llguidance` 1.7.6) instead of auto-selecting XGrammar 0.2.3. Both are supported
vLLM structured-output backends; see the
[official vLLM documentation](https://docs.vllm.ai/en/latest/features/structured_outputs/).
The judge service remains unchanged. No packages were installed or upgraded.

## Checks

- `capture_failed_request.py` reconstructs the request on copied database files.
  Its prompt/payload/schema hash exactly matches the original failed attempt.
  The schema is 337,176 serialized bytes and uses 61 observed sources.
- `check_guidance.py` compiles that exact schema using the serving model's local
  tokenizer, exercises next-token masks and token consumption for 263 tokens,
  and verifies acceptance of a complete schema-valid object without warnings.
  See [guidance-regression.json](guidance-regression.json).
- The modified launcher and prepared resume script pass `bash -n`.
- The runner's implementation digest still matches the stored v8 manifest.
  No memory engine, adapter, processor, prompt or schema code was modified.
- `recover_pending.py` holds the run lock, sets the clock to t061's saved source
  timestamp, verifies the first retry has the identical failed-request hash,
  and retries the pending job. This precedes replay of the incomplete boundary,
  so old replayed turns cannot move the clock backward for the pending request.
  The normal runner performs t061's evolution when it replays that source.
- Normal `--resume` retains completed source jobs and the saved official
  prediction. No checkpoint is rescored against later source state.

The run's `serving-recovery.json` records the backend change and preserved work.
The original manifest remains immutable. This run includes an infrastructure
recovery; alternate constrained decoders can change generations. CPU grammar
checks alone are not evidence of model quality or completion of the full run.

## Live recovery result

The [pending recovery](pending-recovery.json) completed t061 with five published
changes after three model calls on the recovered service. The first proposal
failed the original-authorization citation check, the second overlapped a question
in a mixed source, and the third passed. The host checks stayed active and no
partial proposal was published. Including the original server failure, the job
now records four attempts. This recovery is not a speed improvement claim.

The existing v8 run was resumed in `amem-v7-full` using its prepared `resume.sh`.
The 60 earlier completed jobs match their pre-recovery source hashes, proposals
and changes; see [preserved-work.json](preserved-work.json). The previous exit
status is archived in the run directory. The full benchmark remains in progress.
Permission attempt timings include the manual pending recovery; adapter source
timings for replayed turns do not include that separate recovery call.
