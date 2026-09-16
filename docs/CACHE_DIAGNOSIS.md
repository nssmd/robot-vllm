# Provider cache diagnosis — 2026-09-16

**The GPT-5.5 endpoint does cache text and visual requests. A changing suffix can
reuse a visual prefix, but observed cache hits are intermittent.** The earlier
zero-read experiments do not establish that the service has caching disabled or
that adding new content always invalidates its cache.

## What was measured

All calls used the actual `gpt-5.5` deployment, its Responses API and returned
`input_tokens_details.cached_tokens`. No local response cache or request merging
was used. Each diagnostic kept instructions, image detail, model settings and
cache key fixed within its condition. One HTTP session and a stable affinity
header were used; the header's effect is not documented or independently established.

| Control | Actual input tokens | Reported cached tokens, in request order |
| --- | ---: | --- |
| Identical text-only request, three calls | 3,229 | 0 → 2,816 → 2,816 |
| Identical image request, three calls | 2,177 | 0 → 1,536 → 1,536 |
| Second image series: identical first two, then append different suffixes | 2,177 / 2,186 | 0 → 1,536 → 1,536 → 1,536 |
| Same visual message followed by a separate changing trial message | 2,197 | 1,536 → 0 → 1,536 |
| Three fixed historical views + changing current view, four calls | 2,844 | 0 → 0 → 0 → 0 |

The text control also showed an intermittent miss on its first changed suffix
(0 → 2,816 → 0 → 2,816). Another question-change probe returned orange correctly
but used a different `matches` schema because the probe's replacement question
omitted the original enum requirement. Its schema check failed and is retained;
it is not evidence of incorrect visual memory or task equivalence.

One GPT-6 cross-check returned 429 and was stopped. It contributes no cache result.
These diagnostics perform no robot actions and generate no simulator verdicts.

## Findings and remaining uncertainty

1. **The multi-arm joint-state example was too short for prefix caching.** Its
   individual inputs were 158–188 tokens; shared inputs were 179–209. It was useful
   for measuring request consolidation, not a valid demonstration of long-prefix
   reuse. Adding filler solely to achieve a hit would not be an optimization.
2. **Total input length is not reusable-prefix length.** A 2,177-token request
   does not imply 2,177 tokens precede the first changing field. Earlier reference
   experiments placed trial/state changes before some image content; unchanged
   images later in the request cannot fix an earlier prefix mismatch.
3. **The previous blanket 1,024-token assumption for GPT-5.5 was inadequate.**
   Current OpenAI documentation describes implicit boundaries at 2,048-token
   intervals for GPT-5.5 and a minimum that varies with tools, images, output schema,
   reasoning effort and verbosity. Reported cached tokens exclude hidden tokens
   and round down to 128-token multiples. Client-visible token counts therefore
   do not locate a precise internal cache boundary.
4. **The service supports suffix-changing visual reuse.** The second image series
   retained 1,536 cached tokens while suffix text changed. That is genuine provider
   cache reuse, separate from the prior shared-request token savings. The usage
   counter is aggregate; it does not label exactly how many cached tokens are visual.
5. **No unique server-side root cause has been proven for intermittent misses.**
   A changed final message produced hit → miss → hit under one session/key. Server
   routing, cache residency, eviction and internal prompt rendering are plausible
   factors. Node/cache logs are not exposed, so we cannot identify which caused a
   specific miss or claim that a stable connection pins a GPU. Fixed-prefix length
   alone also does not explain every observation.

The identical-image control took 3.37 s cold and 2.43/2.00 s on two hits. The text
control's hits were slower than its cold request. These tiny timing samples include
generation and service variability; they do not prove a stable latency speedup.
Logical input tokens remained unchanged on exact repeats. Billing savings require
the applicable provider pricing, not a reduction in the reported total-token field.

## Practical policy

- Keep task-required reference/history content and tools stable; append fresh state
  and questions. Never reuse old current images to force a hit.
- Use a stable provider-supported cache key, but treat it as a routing hint, not
  a cache-hit guarantee. Preserve image order/detail and model settings.
- Do not send GPT-6 explicit-breakpoint parameters to GPT-5.5. For a supported
  GPT-6 Standard endpoint, explicit boundaries allow direct control; that path
  remains separate from these GPT-5.5 measurements.
- Meter every call. Report cache hit/miss, logical tokens, latency and correctness
  separately. Ignore infrastructure errors in task denominators but retain them.
- Before tuning further, obtain a repeated changing-observation workload with
  enough useful shared context and matched request counts. If a minimum-length
  or routing issue persists, server telemetry/support is required for attribution.

## Reproduction and sources

```bash
python scripts/cache_diagnostic.py --endpoint "$GP6_ENDPOINT" --model gpt-5.5 \
  --image-request /path/to/retained-responses-request.json \
  --repeats 3 --suffix-trials 2 --interval 10 --output runs/cache-diagnostic
```

The text control uses existing project documentation, not random padding. The
image control takes an actual saved request. Model requests are paid; failed
responses stop the run. Credentials and endpoint configuration are not published.

Local evidence directories: `runs/cache-rootcause-gpt55-20260916/`,
`runs/cache-isolation-gpt55-20260916/`, `runs/cache-boundary-gpt55-20260916/`, and
`runs/cache-message-boundary-20260916/`. Exact bodies, responses and usage are
retained. These are separate diagnostic designs, not one pooled performance trial.

Official documentation consulted:
[OpenAI prompt caching](https://platform.openai.com/docs/guides/prompt-caching) and
[Azure prompt caching](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/prompt-caching).
Provider-specific rules differ; verify the actual deployment's behavior.
