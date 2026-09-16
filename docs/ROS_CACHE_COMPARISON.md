# Matched ROS cache-layout experiment — 2026-09-16

This experiment isolates request layout from request consolidation: both conditions
make one actual GPT-5.5 request per round, with the same four-arm capability catalog,
topology, joint-state snapshot, instruction and output budget. Only dictionary
serialization order changes: current state first versus stable catalog first.
Each condition uses a fixed cache key. Native ROS 2 controls four independent
synthetic two-joint arm controllers; this is not a physical robot experiment.

## Result

Seven paired rounds completed. The first round per condition is a cold/warmup
observation and is excluded from the six-pair efficiency summary below. Condition
order alternated. Initial positions changed between rounds and were restored to
matched positions within the configured 0.001 rad tolerance before each condition.

| Metric, six measured rounds | Current state first | Stable catalog first |
| --- | ---: | ---: |
| Actual model calls | 6 | 6 |
| Logical input tokens | 9,606 | 9,612 |
| Provider cached input tokens | 0 | 2,560 |
| Calls with a cache hit | 0/6 | 2/6 |
| Cached fraction of input | 0% | 26.6% |
| Completion tokens, including reasoning | 615 | 450 |
| Total tokens | 10,221 | 10,062 |
| Median model/perception wait | 2.47 s | 2.15 s |
| Median model wait + action/feedback time | 2.81 s | 2.49 s |

Both cache hits reused 1,280 tokens. Logical input differed by one token per request
because of serialization order; no information or calls were removed. No dummy
padding was added: the stable prefix is the actual capability catalog and topology.
The candidate's logical input count did not decrease. A real application that does
not need the full catalog should not add it just to reach a caching threshold.

Across all rounds including warmup, all 56 measured arm actions completed and
settled, peak concurrency was four, with no condition failures or infrastructure
interruptions. Reset motions are retained separately and excluded from that count.
Targets were not bitwise identical: the largest paired target difference was
0.0003185 rad. Both conditions passed the predeclared 0.001 rad rule check and
controller feedback reached their respective targets. The synthetic controller
does not establish physical tracking accuracy or manipulation success.

## What this establishes

**Stable prefix layout produced real cache reads in this ROS control workload,
without reducing model-call count.** This differs from the earlier 70.5% token
saving, which came from merging requests.

The median per-pair reductions were 6.3% for model/perception wait and 5.5% for
wait plus action/feedback time. These differ from ratios of the unpaired medians
in the table. They are descriptive results, not an isolated cache-speedup estimate:
completion/reasoning lengths differed, cache hits occurred in only two rounds,
and misses also ran faster. Reordering context may affect model generation, and
server load may vary. Total-token differences cannot be attributed to cache reads.

No billing figure is inferred. Cached reads are potentially cheaper at the
deployment's cached-input rate, but verifying invoice savings requires actual
pricing. No guarantee of cache residency or consistent latency improvement is made.

## Reproduce

With ROS 2 Jazzy and a compatible Python environment:

```bash
source /opt/ros/jazzy/setup.bash
export GP6_ENDPOINT='https://YOUR_GATEWAY/v1/responses'
export GP6_API_KEY='YOUR_KEY'
ROS_DOMAIN_ID=199 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST \
  python scripts/ros_shared_control.py --model gpt-5.5 \
  --cache-comparison --rounds 7 --interval 8 --output runs/my-ros-cache-test
```

The default mode remains the independent/shared-request comparison. The new
`--cache-comparison` mode performs no shared-request consolidation. Both modes
validate model targets before dispatch and preserve original observation tickets.
The cache-layout run uses automatic provider caching, not an API cache-disable flag.

Local evidence: `runs/ros-cache-matched-20260916/` contains the manifest, exact
model input contexts, model usage events, target/feedback results and runtime
journals. `analysis.json` records all six measured pairs. All four owned controller
processes exited after validation. Eighteen focused control/cache tests and Ruff
checks passed. These are preliminary integration observations.
