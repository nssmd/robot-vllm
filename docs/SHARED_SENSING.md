# Shared perception: implementation and matched evaluation

The first frozen real-model comparison is documented in
[measured results](SHARED_SENSING_RESULTS_20260916.md), including the shared-only
negative blocking result, infrastructure exclusions and scope limits.

`SharedPerception` combines concurrent requests for the same immutable camera
snapshot and perception question. It returns independent copies of the resulting
evidence to subscribers. The key includes source, frame, calibration, processor,
query and scene generation. A bounded LRU retains completed results; in-flight
capacity is bounded separately. Canceling a subscriber does not cancel the others.

Call `invalidate(source)` after a scene-affecting action, external change or
calibration change. Old completed and in-flight results then fail generation
validation. `validate(key, captured_at)` also rejects expired evidence. A source
must assign a new frame ID whenever its pixels change. The service assumes an
operator-owned snapshot producer; it does not detect a producer lying about IDs.
It does not discover scene changes by itself or align different camera views.

The service shares perception evidence, not actuator commands. Each consumer
still needs its own capability validation and original observation ticket. General
ROS drivers have not been automatically changed to share perception; this is an
explicit service used by the new matched evaluation harness.

```python
from robot_vllm.sensing import SenseKey, SharedPerception

service = SharedPerception(max_age_s=10)
key = SenseKey("top-camera", frame_id, "calibration-v2", "detector-v1",
               "target-locations", service.generation("top-camera"))
evidence = await service.get(key, captured_at, compute_perception)
service.validate(key, captured_at)
# The action runtime validates its own observation ticket before dispatch.
```

## What the experiment measures

`scripts/sensing_benchmark.py` uses actual GPT-6 Responses calls and actual MuJoCo
dynamics. Four independent one-dimensional actuators must reach the columns of
red, green, blue and yellow target disks viewed through a shared rendered camera.
The public grid maps columns to setpoints. Model-selected columns drive the
actuators; final measured joint positions are compared to simulator targets only
after execution. Target locations are never supplied to the model or decoder.

Each episode contains two phases. All targets change between phases and the
source is explicitly invalidated. The three arms of the comparison are:

- `independent`: four requests per phase, each asks for one robot's target.
- `shared`: one request per phase asks for all targets, using the same target-entry
  output representation as the baseline; four subscribers share its result.
- `shared_compact`: the shared request uses a compact color-to-column object.
  It still supplies all target information and rejects missing/duplicate/invalid
  values. No explanation is requested in any arm.

The baseline concurrency limit is four, allowing all robot requests to overlap.
Seeds, model, camera dimensions, inference configuration and schemas are frozen;
variant order rotates by seed. A separate pilot uses a different seed and lower
concurrency and must not be combined with the frozen results. Successful cases
are never replayed on resumption; failures and infra records are retained too.

Requests, raw responses, provider usage, rendered before/after images, measured
trajectories and post-hoc verdicts are retained. The frozen run contains a source
snapshot and manifest. Credentials are supplied by the environment or Azure CLI
and are not included in these records.

```bash
# Install .[simulation] and optionally azure-identity for --azure-cli.
# Use an available rendering backend; the verified local environment uses Xvfb.
export GP6_ENDPOINT='YOUR_RESPONSES_ENDPOINT'
export GP6_API_KEY='YOUR_KEY'
xvfb-run -a env MUJOCO_GL=glfw python scripts/sensing_benchmark.py \
  --concurrency 4 --seeds 1201 1202 1203 1204 1205 1206 \
  --output runs/my-sensing-comparison
python scripts/analyze_sensing.py runs/my-sensing-comparison
```

## Metrics and limits

Tokens come from provider-reported input/output/total usage, not characters,
estimated visual token counts or fabricated fixture metering. Missing usage is
reported as unmetered. Task success requires all four actuators to be within
0.03 m after both phases. Infrastructure outcomes stay outside that denominator.

Wall time includes rendering, model waiting, simulation work and evidence writes.
`sensor_barrier_s` measures the time until all four perception results are ready;
the actuators are launched together after that barrier. `robot_blocked_seconds`
is four times the barrier duration, the sum over waiting robots. The analysis
also reports the sum of individual subscriber wait times, which does not assume
a synchronized start. These metrics are accumulated wall-clock work, not four
independent measurements of the same latency.

The simulator advances for a fixed 0.8 simulated seconds per phase as fast as the
CPU allows; this is not a real-time paced hardware experiment. The camera task is
deliberately simple, with discrete colored targets, a fixed viewpoint and known
grid calibration. It measures shared-scene perception and reaching, not grasping,
contact, cross-view matching or natural clutter. Explicit change notification in
the harness verifies invalidation; automatic change detection remains future work.

Equal predictions produce equal setpoints and thus equal final trajectories.
That is a useful equivalence check on these cases, but a small sample of perfect
success does not establish statistical non-inferiority on other robot tasks.
Paired latency/bootstrap summaries are descriptive; provider load, request order
and cache state remain potential sources of timing variation. No speedup is
guaranteed when robots have distinct views/questions or expensive joint reasoning.
