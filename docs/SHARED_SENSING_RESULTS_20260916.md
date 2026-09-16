# Shared sensing + compact output: first measured results

The frozen GPT-6/MuJoCo comparison supports a bounded claim: on a shared-camera,
four-slider visual-reaching task, shared sensing with compact output preserved
every valid matched prediction and measured trajectory while reducing actual
provider tokens, episode wall time and sensing waits. This is preliminary evidence
for this simple task, not general manipulation equivalence or hardware speedup.

## Protocol

- Actual reported model: `gpt-6-astra`; Responses API, low reasoning, 256 maximum
  output tokens, 512×512 MuJoCo camera. No synthetic model response or added delay.
- Twelve frozen seeds (1201–1212), three variants, two changed-target phases each.
  Every phase contains four independent actuated sliders. Target state is used
  only for post-execution adjudication, never supplied to perception or decoding.
- Baseline allows four concurrent requests, one color target per request.
  Shared variants use one request for all four targets with four subscribers.
  Variant order rotates by seed. All robots execute after the sensing barrier.
- Identical image geometry, actuator controller and column-to-setpoint mapping;
  independent and shared use the same explicit target-entry representation.
  Compact uses a shorter color-to-column object without omitting target decisions.
- Final success requires all four measured joint positions within 0.03 m of their
  respective targets in both phases. Each phase advances 0.8 simulated seconds;
  simulation is not wall-clock paced.

## Task verdicts and exact outcomes

| Variant | Simulator successes | Task failures | Infrastructure interruptions |
| --- | ---: | ---: | ---: |
| Independent sensing | 11 / 11 | 0 | 1 |
| Shared sensing | 11 / 11 | 0 | 1 |
| Shared + compact output | 11 / 11 | 0 | 1 |

There are 33 complete successful episode verdicts, zero task failures and three
infrastructure-interrupted records. The latter are preserved and excluded from
task denominators. They are not replaced with successful reruns. Infrastructure
occurred in seed 1206 (independent and compact) and seed 1212 (shared).

Independent vs compact has 11 valid pairs. All 11 produced identical target
predictions in both phases and **byte-equivalent JSON values for all recorded
measured trajectories**. Independent vs shared has 10 valid pairs and identical
predictions in all 10. There was no observed success regression.

Eleven successes out of eleven have a Wilson 95% success interval of approximately
74.1%–100%. This sample therefore does not establish broad statistical equivalence.

## Provider tokens

These are actual provider usage fields for a complete two-phase episode:

| Variant | Model calls | Input tokens | Output tokens | Total tokens |
| --- | ---: | ---: | ---: | ---: |
| Independent | 8 | 3,296 | 168 | 3,464 |
| Shared | 2 | 848 | 90 | 938 |
| Shared + compact | 2 | 826 | 50 | 876 |

Shared + compact reduces total tokens **74.7%**, input tokens **74.9%**, output
tokens **70.2%** and calls **75%** compared with independent sensing. Compared with
shared-only, compact output reduces output tokens 44.4%, and total tokens 6.6%.
Both the output schema and its prompt footprint change; the latter comparison
cannot be attributed solely to faster decoding.

All successful calls are metered. Across all attempts, independent/shared/compact
have respectively 4/1/1 failed calls without returned usage. Their tokens are unknown,
not measured zero. Whole-campaign metered totals are partial for that reason;
percentage comparisons above use complete, paired non-infra episodes.

## Latency and blocking

Median of per-seed fractional reductions against the matched independent case:

| Metric | Shared only (10 pairs) | Shared + compact (11 pairs) | Compact 95% paired bootstrap interval |
| --- | ---: | ---: | ---: |
| Episode wall time | 6.3% lower | **30.5% lower** | 18.0%–41.3% lower |
| All-robot sensing barrier | 7.0% lower | **32.0% lower** | 18.1%–43.0% lower |
| Sum of individual perception waits | **14.5% higher** | **9.8% lower** | 2.4%–18.8% lower |

Compact is faster in all 11 matched episode-wall-time and barrier comparisons,
and in 9/11 comparisons of summed individual wait. Shared-only wall-time and
barrier intervals include no improvement. It saves tokens reliably here but does
not independently demonstrate a reliable reduction in blocking.

Unpaired descriptive medians for independent vs compact are 10.08 vs 6.59 seconds
episode wall time, 9.63 vs 6.07 seconds sensing barrier, and 28.24 vs 24.27
robot-seconds of summed individual waits. The percentage headline uses paired
ratios, not ratios of these medians.

The harness explicitly waits for all targets before simultaneous motion. The
all-robot barrier is therefore relevant to this workload, but should not be used
as the waiting time for an independently moving robot. Both definitions are
reported; multiplying the barrier by four yields accumulated robot wait, not
another independent latency measurement. Neither Python mutex contention nor
GPU kernel blocking was measured.

## Reproducibility and limits

Raw evidence root: `runs/sensing-frozen-c4-20260916/`. It contains `manifest.json`,
the frozen source, requests/responses, before/after images, per-phase trajectories,
verdicts and infrastructure records. `analysis-final.json` contains the final
paired statistics. `comparison.png` and `comparison.pdf` visualize them. Earlier
partial analysis files and the separate development pilot remain intact.

The general suite passed 159 tests; the subsequently added decoder-focused checks
and sensing checks passed 14/14 (six decoder checks, eight existing sensing checks).
Ruff and whitespace checks passed. All campaign workers have exited.

The shared-perception module is explicit opt-in infrastructure and is exercised
by this harness. Automatic integration into every ROS driver, cross-view fusion,
automatic change detection, grasping/contact and moving cameras remain untested.
The harness deliberately signals scene changes; unit tests additionally verify
that invalidating an in-flight result rejects it and cancellation of one subscriber
does not interrupt another. Production invalidation must use actual sensor/action
events, not evaluator state.

Next useful validation: real clutter/occlusion and shared objects, with a detector
or VLM exposed only to camera images, plus mixed workloads where requests cannot
all be merged. These results do not justify assuming the same savings there.
