# Deployment and operation

## Configure before connecting

Run `robot-runtime validate-config --config <file>` to reject unknown keys,
duplicate device/action endpoints, invalid joints/limits, model declarations and
resource/scheduler limits without connecting to robots or providers. Group members
must resolve to registered capabilities at deployment. Use a unique ROS action
namespace for each physical controller.

`configs/system.mock.example.json` is a complete synthetic topology.
`configs/system.ros2.example.json` defines two robot hosts and model aliases;
replace every embodiment mapping and endpoint with commissioned values. An
operator-owned SDK plugin uses `backend: plugin`, `factory: module:callable`, and
an `options` object. The callable receives the configured device name; options
cannot override that identity.

## Persistent service

```bash
robot-runtime serve --config configs/system.mock.example.json \
  --state-dir state/cell-a --output runs/cell-a
```

Only one process may open a state directory. Keep state and evidence on persistent,
local storage. Do not place SQLite state on NFS or share it among active nodes.
Back up a stopped database, or use SQLite's backup API; copying only the main file
while WAL is active may omit recent intent. Retention is an operator task: this
framework does not delete past runs, traces, failure records or videos.

Readiness requires available sensor observations and no restart quarantine.
Liveness only means the service process is available. Application task admission
is bounded and returns explicit backpressure instead of an unbounded work queue.
The configured provider and controller deadlines still need deployment-specific
values; the framework does not claim hard real-time scheduling.

## Recover after process loss

Restart with the same configuration and state directory. Use `/recovery` to
inspect persisted operations, and `/recovery/{id}/reconcile` to query their native
results. Native ROS results may be unavailable after the controller restarts or
its action result cache expires. Such operations remain quarantined; unknown is
not synonymous with stopped.

For an offline coordinator process, the equivalent administrative command is:

```bash
robot-runtime recover --config configs/system.ros2.example.json \
  --state-dir state/cell-a --execution-id SAVED_EXECUTION_ID
```

Do not run the CLI against a state directory held by the live HTTP process; use
the API instead. Once native state is resolved, explicitly resume an eligible
stored task with `/tasks/{id}/resume`. Completed node checkpoints are retained.
There is no automatic replay of interrupted tasks on service startup.

## Robot-side liveness

A ROS driver can publish goal-scoped heartbeats on `lease_topic`. Configure
`lease_period_s` below the robot's watchdog timeout with enough margin for measured
network and scheduling variation. The MuJoCo lab controller accepts
`--lease-timeout`; an ordinary hardware trajectory controller needs a commissioned
wrapper or equivalent local watchdog before this option has any stop effect.

The heartbeat carries the native goal UUID using `std_msgs/msg/String`, with
volatile best-effort depth-one QoS. It cannot renew another goal or revive an
expired lease. It is a trusted-network liveness mechanism, not identity or access
control. ROS action termination remains the runtime settlement contract.

For cross-host ROS discovery/transport, configure the deployment's DDS network
or `rmw_zenoh_cpp` sessions. Keep robot traffic and model inference endpoints
separate. The cross-host validator uses an explicit, disconnectable Zenoh TCP
path; SSH only provisions the lab and gathers evidence. Clock synchronization and
future-stamped physical execution are separate commissioning tasks.

## Container and service templates

- `deployment/Dockerfile`: Python coordinator and mock/plugin support, non-root.
- `deployment/Dockerfile.ros2`: Jazzy coordinator with ROS, Zenoh and MuJoCo dependencies.
- `deployment/compose.yaml`: bounded CPU/RAM, persistent state/evidence volumes,
  loopback host publication and token requirement. This starts synthetic devices
  unless the operator mounts and selects a robot configuration.
- `deployment/robot-runtime.service`: systemd template with persistent directories.
  Create its service account, install the environment and supply the configuration
  and environment file. ROS deployments must also provide the environment normally
  established by sourcing the ROS setup script; the ROS image already does so.

```bash
ROBOT_RUNTIME_TOKEN='choose-a-local-token' docker compose -f deployment/compose.yaml up --build
```

These are installation templates, not a claim that every Linux/network/hardware
combination is commissioned. The CI template defines Python/ROS checks and a Python image build;
activation is described in `docs/ci.md`. Use existing managed robot containers rather than restarting other
simulation or training workloads for a deployment test.
