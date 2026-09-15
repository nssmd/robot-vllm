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
