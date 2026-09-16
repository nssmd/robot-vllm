# Validation record

This repository separates framework behavior from robot-task performance. A test
harness verdict, native action completion, model API response, measured simulator
state and final manipulation success are different evidence categories.

## Framework checks

The Python suite covers schema validation, 1/2/4-arm scheduling, resource conflicts,
state freshness, group cancellation, delayed settlement, provider envelopes,
array provenance, HTTP authorization/backpressure/idempotency, local state locking,
restart quarantine, completed-node preservation and explicit VLA conversion.

`ros_restart_smoke.py` additionally performs a real coordinator process exit while
two independent MuJoCo controllers execute native ROS goals. Both controllers hold
locally on heartbeat expiry. A new coordinator reads durable UUIDs, blocks new work,
queries native results and releases both resources only after terminal replies.
Each controller receives exactly one goal; restart does not replay it.

The earlier cross-host series used two distinct computers, ROS Jazzy with Zenoh,
and independent MuJoCo plants. Five checks passed: parallel group execution and
resource exclusion; coupled cancellation; a real transport partition with local
watchdog hold and group quarantine; native reconciliation plus new work after
reconnection; and partial admission when another coordinator owns the remote arm.
One fault injection measured 0.901 s to group quarantine, 0.782 s to reachable-arm
hold and 0.411 s from reconnection to terminal reconciliation. These are individual
integration observations, not latency percentiles or physical synchronization bounds.

That series retained two development interruptions: a missing ROS Python path
before any action, and a progress-file writer error after completed checks. Passed
checks were recovered from native journals and skipped on resumption. Injected
faults intentionally abort/cancel actions; such action outcomes are not task failures
or task-success claims.

A separate actual `gpt-6-astra` call previously generated and completed a three-node
DAG across the two hosts: one model call, 2,313 prompt and 468 completion tokens,
2,781 total tokens, 7.795 s model time and 10.202 s task wall time. VLA nodes were
not used in that measurement. Raw lab records stay outside Git; no credentials,
private host setup, episode imagery or internal documents are publication inputs.

## Reproducibility and limits

The included inactive CI template defines Python versions, static checks, HTTP smoke, package builds,
ROS integration and the ROS crash/restart smoke. CI uses synthetic policies and
small controllers, with no paid model calls, GPU requirement, training or benchmark
sweep. Once activated, jobs upload their own test evidence. Remote CI has not run for this publication because the publishing credential lacks workflow-write authorization.

Local development checks support implementation claims. A GitHub badge/job only
establishes the checks actually run for that commit; deployment recipes have their
own validation scope. Preserve diagnostics rather than replacing failed records.

No trained VLA, shared-object manipulation, collision-free planning, physical clock
synchronization or hardware deployment result is claimed. Substantial experiments
belong in shared simulation scenes and commissioned multi-arm setups with independent
post-hoc verdicts, matched scenarios and explicit resource budgets.

Local release checks: 105 tests passed and one optional MuJoCo test was skipped in
the general Python environment; a ROS-compatible environment separately exercised
MuJoCo dynamics and native crash recovery. HTTP service smoke, non-root container
startup/task execution, Compose validation, lint, wheel and source builds passed.

## 0.5 OpenPI client walkthrough

The pinned official `openpi-client` package was installed directly from its GitHub
subdirectory. Both the CPU walkthrough and the native ROS 2 walkthrough completed
two parallel robot branches, two VLA calls per robot, and a dependent Trigger
service consumption. The client and ROS transports are real; default model
responses and controller observations are declared synthetic. No Pi 0.5 checkpoint
was loaded for these protocol checks. The first development attempt exposed bridge
admission set to one request and a missing PyYAML in an isolated ROS environment;
the bridge concurrency and environment were corrected, and earlier logs retained.

The 0.5 dependency environment completed 116 Python tests with one optional
MuJoCo skip. The Pi 0.5 tests explicitly invoke the installed upstream client
class; preprocessing uses its image_tools utility. Runtime configuration
validation accepts the published two-robot arm/gripper/service topology.

## 2026-09-15 bridge hardening (local, unreleased)

Reproduced HTTP 500 responses for a non-object VLA request and an upstream
provider failure. The bridge now validates envelope/ticket/capability structure
before inference and returns 422 for invalid requests, 502 for provider failures
and 504 for provider timeouts. Regression checks cover admission recovery and
ensure no action conversion occurs after provider failure. Native OpenPI checks
exercise both metadata and inference timeouts, followed by a successful new
connection.

The Python 3.12 ROS-compatible environment completed **132 tests, with one optional
MuJoCo test skipped**. This includes the installed official OpenPI client. Ruff
and whitespace checks passed. CPU and native ROS 2 quickstarts each completed
two parallel robot branches, four VLA calls and one dependent service node.
These are fixture integration checks; no pretrained checkpoint was loaded and
no manipulation task verdict was produced. Walkthrough processes exited after
their checks.

The first full-suite attempt stalled in the existing HTTP body-limit test because
the ROS virtual environment inherited FastAPI 0.101.0 / Starlette 0.27.0 from the
system. An isolated reproduction retained the same failure. FastAPI's minimum
dependency is now 0.108.0; the full successful suite above used exactly that
version with Starlette 0.32.0.post1. Diagnostic logs remain under local `runs/`.

## 2026-09-16 ROS concurrency and recovery continuation (local, unreleased)

The four-arm native ROS check passed with four concurrent DAG nodes, four camera
streams and seven completed executions; no execution failed, was canceled or
remained uncertain. Responses and motion were synthetic; the ROS transport was
real. The HTTP smoke also passed authentication, discovery, observation,
dispatch, idempotency, resource conflicts, concurrency, cancellation and DAG API
checks.

The two-controller MuJoCo crash/restart check passed. Both controllers held on
lease expiry after the coordinator's deliberate hard exit. Restart preserved
quarantine, rejected new work before reconciliation, queried the original native
goal IDs and released resources only after terminal results. Each controller's
journal recorded exactly one started goal and one lease-expiry hold. The recovered
operation is correctly marked failed after the injected crash; this is an expected
fault outcome, not a manipulation-task failure.

After installing the simulation extra, the previously skipped MuJoCo dynamics
test passed separately. All local validation workers exited. There are no new
manipulation task verdicts, pretrained-policy results or cross-host measurements
from this continuation. Local evidence pointers and counts are retained in
`runs/continuation-validation-20260916.json`.

## 2026-09-16 shared inference scheduling (local, unreleased)

The model layer now shares bounded FIFO admission across tasks per model alias.
Planner and node observations are sampled after admission. Canceled blocking HTTP
calls retain capacity until the underlying transport returns; their late results
are discarded. Queue overload and timeout are infrastructure outcomes. Expired
terminal no-action decisions are rejected before node completion.

The final suite passed **151 tests, with no skips**. Regression coverage includes
1/2/4 concurrent transport limits, queued-cancel/admission races, overload and
shutdown cleanup, retained capacity after inflight cancellation, fresh observations
after queueing, task cancellation without late actions, expired action/no-action
results, authenticated capacity queries and resolved inference limits in task
manifests. Ruff and whitespace checks passed. The native ROS/OpenPI quickstart
completed four VLA calls, two parallel robot branches and one dependent service;
the HTTP service smoke also passed. Model responses and devices were fixtures.

The local latency-overlap script compared four robots × three rounds with one vs
four inference slots. Both variants completed 12 calls and 12 actions, with no
transport errors. One preliminary sample measured 1.973 s vs 0.929 s total wall
time (2.12× ratio), with median queue waits of 0.441 s vs less than 0.001 s. Each
fixture response included an artificial 100 ms delay; HTTP, Python scheduling and
host contention also contributed. This is a single local scheduling observation,
not measured GPT-6/Pi 0.5 compute acceleration or a manipulation-success result.
Raw evidence is retained in
`runs/inference-benchmark/2f9e0394541947788dad795cbcb0d0ac/result.json` and per-variant
task journals. All test workers exited; there are no new simulator task verdicts.

Reproduce with `python scripts/inference_benchmark.py`. The inactive CI template
also includes this check; no new remote CI run or release is claimed.

## 2026-09-16 real GPT-6 shared-sensing comparison

A separate frozen MuJoCo visual-reaching comparison used actual `gpt-6-astra`
responses and provider token usage. Across 12 seeds and three variants, there
were 33 simulator-confirmed successes, zero task failures and three infrastructure
interruptions. Shared + compact output matched baseline predictions and measured
trajectories on all 11 valid pairs while reducing total tokens 74.7%, paired median
episode wall time 30.5%, the sensing barrier 32.0% and summed individual perception
wait 9.8%. Shared-only did not show equally reliable waiting-time gains. These are
preliminary results for four simple sliders viewing discrete targets through one
shared camera; they are not grasping or general robot-task results. See
[the full report](SHARED_SENSING_RESULTS_20260916.md) for intervals, raw evidence,
failure accounting, implementation boundaries and reproduction commands.

Publication check for the combined changes: 165 Python tests passed with no skips;
Ruff and whitespace checks passed. This is local verification, not a remote CI run.
