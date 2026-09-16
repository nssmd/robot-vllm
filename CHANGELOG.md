# Changelog

## Unreleased

- Add a runnable native ROS 2 four-arm shared-state/model-call comparison with
  changing paired initial states, measured provider tokens and final joint feedback.

- Add opt-in cross-trial GPT prompt-prefix ordering, provider cache keys and explicit
  breakpoints, cached-input/write metering and an actual-endpoint comparison script.
- Ensure Responses JSON mode includes JSON in the user input, and allow explicit
  output-token budgets. Actual cache-hit gains are not yet established.

- Add bounded shared-perception caching with immutable frame/query identities,
  concurrent subscriber sharing, source-generation invalidation and expiry.
- Add an actual-model MuJoCo visual-reaching comparison with independent,
  shared and compact-shared sensing, provider token metering, preserved manifests,
  and paired latency/blocking analysis.

- Share bounded FIFO inference admission across all tasks using a model alias;
  sample observations after admission and retain capacity for canceled HTTP calls
  until the underlying transport finishes.
- Cancel queued/inflight model waits on DAG stop and reject expired terminal
  no-action decisions as well as expired actions.
- Expose authenticated `/inference` capacity metrics, per-call queue wait, a local
  HTTP latency-overlap benchmark and the robot-serving architecture document.

- Validate VLA request envelopes before inference and return explicit 502/504
  provider errors without exposing upstream exception text.
- Classify OpenPI timeouts separately and verify fresh-connection recovery after
  metadata or inference timeouts, plus bridge admission recovery after failures.
- Ignore the ROS virtual environment used by the documented walkthrough.
- Require FastAPI 0.108.0 or newer so HTTP body-limit middleware can replay
  cached request bodies in ROS environments inheriting older system packages.
- Declare the repository's Ruff checks explicitly for consistent local validation.

## 0.5.0

- Add official OpenPI client as a pinned optional third-party dependency and expose π0.5-DROID as a VLA provider.
- Map DROID camera/state inputs and normalized velocity/gripper outputs explicitly; dispatch bounded prefixes to arm/gripper groups.
- Add ROS GripperCommand and Trigger service adapters plus a complete two-robot quickstart in mock and actual ROS transport modes.
- Expand README with installation, expected output, real GPT-6/OpenPI endpoints, multi-host Zenoh routing, and task API examples.


## 0.4.0

- Persist execution intent, native action locators, settlement, task request IDs
  and completed-node checkpoints with SQLite WAL/FULL and an exclusive local lock.
- Quarantine unresolved resources after restart; reconcile original ROS goal IDs
  and retain completed nodes on explicit task resumption without trajectory replay.
- Monitor ROS sensor liveness, cancel group peers, support optional goal-scoped
  robot-side leases and bounded native-result queries after reconnect.
- Reject duplicate physical action endpoints and invalid deployment configuration
  before opening devices. Add offline configuration validation.
- Add bounded HTTP admission, durable task idempotency, recovery/resume endpoints,
  request-size limits, liveness/readiness and Prometheus gauges.
- Add an operator-owned VLA serving bridge and explicit joint-action codecs.
- Add Docker/Compose/systemd templates, crash-boundary tests, ROS restart smoke,
  package and CI workflows, architecture/API/deployment documentation.

Existing Python calls without `state_dir` remain ephemeral. Service mode defaults
to `state/`. Plugin constructor parameters now live under an explicit `options`
object; plugins cannot override configured device identity. The legacy isolated
episode interface and multi-robot task interface retain separate schema identifiers.

Task/DAG completion remains separate from simulator task success. No production
hardware certification, active/active failover or trained-policy performance claim
is implied by this version.
