# Changelog

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
