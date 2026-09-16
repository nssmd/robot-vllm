# GPT prompt caching across trials

**Diagnostic update:** GPT-5.5 has verified text and image cache hits, including
image-prefix reuse after changing suffix text. Hits were intermittent; earlier
all-zero runs remain historical observations, not proof that the deployment cannot
cache. See [provider cache diagnosis](CACHE_DIAGNOSIS.md).

For a changing camera stream, use the explicit [observation history component](OBSERVATION_HISTORY.md)
to append fresh frames while preserving useful historical prefixes. This does not
make similar new frames interchangeable. GPT-5.5 reference/history experiments
are reported separately from the earlier GPT-6 request-layout attempts below.

Trials of the same task can reuse a provider's computation of identical prompt
prefixes. This implementation reorganizes requests; it does not cache responses,
reuse past actions, or expose a previous trial's state as the current observation.

## Enable

Add these fields to the existing GPT model configuration:

```json
{
  "cache_layout": true,
  "cache_breakpoint": true,
  "prompt_cache_key": "robot-cell-a-planner-v1",
  "max_output_tokens": 512
}
```

`cache_layout` puts stable context first and deterministically serializes nested
object keys. For planners the order is schema, capability catalog, topology,
policy names and task, followed by current trial fields. For node policies it is
schema, capability, task and node definition, followed by observations and feedback.
List order is preserved. Unknown context schemas are unchanged.

`cache_breakpoint` separates that stable context and current trial data into two
user text blocks and marks the first with an explicit provider-cache breakpoint.
It sends `prompt_cache_options: {mode: explicit, ttl: "30m"}`. It requires
`cache_layout` and a supported language-model endpoint. The provider still validates
the actual prefix; using the same key does not make different inputs interchangeable.
Images stay in the current-trial suffix. Capability or task changes naturally
change the prefix and should cause a miss.

These options default off for existing deployments. Explicit breakpoints are
provider-specific: Azure documents support for GPT-5.6 and later Standard deployments,
but not older models or PTU-M deployments. Unsupported endpoints may return 400;
the client reports the error without silently retrying a different request. For
automatic prefix caching elsewhere, start with `cache_layout` alone, adding
`prompt_cache_key` only if supported. No padding is added to reach cache thresholds.

Reference: [Azure OpenAI prompt caching](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/prompt-caching),
consulted 2026-09-16. Provider behavior and pricing can change. This documentation
describes a minimum 1,024-token reusable prefix and possible additional charges for
cache writes on newer models. Measure reads and writes before estimating savings.

## Metering

Every model-call record retains prompt/completion/total tokens and now adds
`cached_input_tokens`, `uncached_input_tokens` and `cache_write_tokens` from the
provider's usage details. Responses (`input_tokens_details`) and Chat Completions
(`prompt_tokens_details`) are supported. Missing or invalid counters are unknown,
not zero. Summaries expose cache-metered/unmetered calls, partial flags and the
token-weighted cache-hit ratio over calls with valid cache metering.

Cached tokens remain part of logical input tokens. A higher cache-hit rate does
not mean a shorter prompt or fewer output tokens. Savings depend on provider pricing,
write charges and actual latency. No invoice cost is inferred by this module.

## Actual endpoint comparison

```bash
python scripts/prompt_cache_benchmark.py \
  --endpoint "$GP6_ENDPOINT" --key-env GP6_API_KEY \
  --trials 5 --interval 5 --output runs/my-prompt-cache-test
```

This makes paid model calls. It compares original ordering, stable ordering, and
stable blocks with an explicit breakpoint. All variants receive the same real
synthetic-device capability catalog with no padding; trial IDs and observed
positions change. The model must choose the negative of the current position,
which catches stale-output reuse. Each variant uses a separate stable cache key,
the first call is labeled warmup, and variant order rotates. A provider/contract
failure stops the run and retains the diagnostic rather than repeating it unchanged.

This measures actual caching and planning behavior, not physical task success.
Use a new output directory for a changed configuration. The task manifest records
the cache settings for ordinary runtime episodes too.

## Validation status (2026-09-16)

Wire-contract tests cover both API formats, identical stable blocks across changed
trial IDs/state, correct output-budget field names, and cache usage validation.
The local suite passed 181 tests with one optional OpenPI environment skip.

Actual GPT-6 attempts first exposed the endpoint's requirement to include JSON in
the user input when using JSON mode; this was corrected. Subsequent attempts,
including one after the token renewal window, returned HTTP 429 with
`rate_limit_exceeded` and exhausted remaining token quota. Those attempts stopped
and their records remain under local `runs/prompt-cache-gpt6-*` directories.
There was **no successful real-provider cache-hit or latency result**
from this comparison. Missing usage on failed calls must not be interpreted as
zero tokens or as proof that caching failed. Resume measurement when endpoint
quota is available, using a fresh output directory and preserving these attempts.
