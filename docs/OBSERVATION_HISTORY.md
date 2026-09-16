# Append new observations, retain useful visual history

`ObservationHistory` prepares Responses API messages for an observation stream.
Each newly captured frame is appended once. Previously captured images remain
historical evidence, with immutable sequence/time labels. The final query names
the current sequence; it never relabels an old image as the current observation.

This keeps the existing visual prefix stable until eviction. A compatible provider
can reuse that prefix's computation. The component does not implement the provider's
KV cache, force a cache hit, match similar patches, or reuse an action/result.
It is explicit opt-in infrastructure, not automatically enabled in every driver.

```python
from robot_vllm.observation_history import ObservationHistory

history = ObservationHistory("episode-001", max_frames=8, max_bytes=16 * 1024 * 1024)
history.append(sequence=0, captured_at=capture_time, png_base64=frame_png)
messages = history.messages("Return JSON describing the current target and its change.")
# Send messages as the Responses input. Append the next newly captured frame
# before the next call, keeping the same history instance for this episode.
```

Sequence numbers must increase; capture times cannot go backward. Byte/frame
bounds evict oldest frames, which may invalidate provider prefix matches. Start
a new instance for a new episode. Original action-ticket freshness and device
admission still apply; history is not authorization to act on stale observations.
Only retain history a task actually needs: caching does not make an expanding
logical context free, and adding unnecessary images can increase cost.

## Experiments and evidence

`scripts/history_cache_benchmark.py` replays four retained MuJoCo camera images:
before action, after action, after targets change, and after the next action.
It asks for the current red target column and whether it changed since the prior
frame. Both layouts receive the same images and question. The baseline places the
newest frame first; the candidate appends frames chronologically. Model outputs
are checked after the response against retained labels, never supplied as input.

```bash
python scripts/history_cache_benchmark.py \
  --endpoint "$GP6_ENDPOINT" --model gpt-5.5 \
  --episode /path/to/retained/shared_compact/episode \
  --output runs/my-history-comparison
```

This requires retained `phase-0.png`, `phase-0-after.png`, `phase-1.png`,
`phase-1-after.png` and corresponding `phase-*-verdict.json` files in the supplied
episode directory. Raw lab episodes are not included in Git. Calls use automatic
caching, without GPT-6-specific explicit breakpoints. Requests, responses, usage,
images, answers and failures are preserved. No policy is executed during replay;
correct image interpretation is not a new simulator task-success verdict.

The earlier GPT-5.5 fixed-reference experiment (`reference_cache_benchmark.py`)
completed 12 calls correctly, with zero reported cache reads in either layout.
This negative result is retained: a cache-friendly layout alone does not establish
that a given deployment actually reuses the visual prefix. GPT-6 separately reported
1,430 cached input tokens on a repeated two-image request, but that static-image
probe does not demonstrate benefits for changing camera streams. See the local
experiment records for measured counters; no general speedup is claimed.

### GPT-5.5 changing-observation result, 2026-09-16

The four-frame MuJoCo replay completed eight actual API calls: four per layout,
all eight answers correct, zero infrastructure errors. The current target moved
from column 5 to column 2, and both layouts recognized that change. Input counts
grew from 802 to 1,519, 2,236 and 2,953 tokens as actual new frames were appended.
Every response reported `cached_tokens: 0` and `cache_write_tokens: 0`.

Chronological and newest-first median wall times were 3.21 s and 3.76 s, respectively.
There are only four samples per layout and no measured cache hits, so this difference
is not evidence of cache acceleration. It could reflect model generation variability
or service load. Cache routing/availability on this particular deployment has not
been diagnosed; zero reported reads do not establish that visual-prefix caching is
impossible on other deployments.

Local evidence: `runs/history-cache-gpt55-20260916/summary.json`, with per-request
inputs and raw responses. The reference experiment is retained separately under
`runs/reference-cache-gpt55-20260916/`. No new robot experiment workers remain running
from these API replay checks. Focused history/cache tests passed 14/14; static checks
passed. This change does not claim a closed-loop policy improvement.
