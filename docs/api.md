# API and Python integration

The HTTP API represents one trusted application/operator boundary. Configure
`ROBOT_RUNTIME_TOKEN` and send `Authorization: Bearer <token>`. It is not per-user
multi-tenant authorization. Local Python APIs use the same runtime contracts.

| Endpoint | Behavior |
| --- | --- |
| `GET /livez` | Process liveness and version; no device details |
| `GET /readyz` | 200 when observations are available and restart recovery is clear; otherwise 503 |
| `GET /health` | Resource epochs, busy/quarantined states, pending recovery and active executions |
| `GET /metrics` | Prometheus gauges for active executions, recovery and quarantine |
| `GET /inference` | Authenticated per-model-alias queue, capacity, transport and cancellation counters for this coordinator process |
| `GET /tools` | Capability, topology and model-facing tool schemas |
| `POST /tools/{name}` | Observe, execute, inspect, wait or cancel operations |
| `POST /tasks` | Start a task using a provided DAG or configured planner |
| `GET /tasks/{id}` | Task state and current execution feedback; persisted tasks remain queryable after restart |
| `POST /tasks/{id}/cancel` | Request task cancellation; does not itself confirm device stop |
| `POST /tasks/{id}/resume` | Explicit durable resumption after native settlement, retaining completed nodes |
| `GET /recovery` | Inspect unsettled executions from an earlier process |
| `POST /recovery/{id}/reconcile` | Query native results; never replay an action or force release |

## Task submission

With the default mock topology, submit this JSON to `POST /tasks`:

```json
{
  "request_id": "application-task-001",
  "task": "Move the first test arm",
  "deadline_s": 30,
  "plan": {
    "schema": "robot_runtime.dag.v1",
    "nodes": [{
      "id": "move",
      "capability": "robot.arm_1.move",
      "policy": "code",
      "arguments": {"target": 0.2, "steps": 10}
    }]
  }
}
```

Reusing a task `request_id` with the same request returns its existing task ID,
including after restart. Reusing it with different content returns 409. Admission
beyond `max_active_tasks` returns 429; invalid contracts return 422; bodies above
the configured limit return 413. Retrying an accepted request does not start a
second task. Omitting `request_id` intentionally creates a new task.

Device operations have a separate `(owner, request_id)` idempotency key and bind
the exact observation ticket and arguments. `wait_execution` timeout is a caller
wait limit, not an action cancellation. Settlement is reported explicitly.

Recovery and resume are application/operator operations and are not included in
the model-facing `RuntimeTools` schema. Pending native execution state blocks
resumption. A completed action missing its semantic node checkpoint also blocks
resumption rather than assuming the action should be repeated.

## Python

```python
import asyncio
from pathlib import Path
from robot_vllm.control import DeviceRuntime
from robot_vllm.deployment import Deployment, mock_topology
from robot_vllm.platform import RobotSystem

async def main():
    runtime = DeviceRuntime(Path("runs/sdk"), state_dir=Path("state/sdk"))
    deployment = Deployment(runtime, mock_topology(2))
    system = RobotSystem(runtime, max_active_tasks=8)
    try:
        await deployment.ready()
        # system.start(task, plan=..., request_id=...) returns a task ID.
        # system.status(id), await system.cancel(id), system.resume(id).
        print(runtime.topology())
    finally:
        await system.close()
        await deployment.close()

asyncio.run(main())
```

Model-serving calls and task/DAG completion remain distinct from task success.
A task result always leaves `task_verdict` null; the independent task evaluator
owns that verdict. Per-task model usage and phase timing accompany the result.
Per-call `queue_time_s` and the task's `inference_queue_time_s` report admission
waiting for dispatched calls. Model `wall_time_s` excludes admission waiting;
after cancellation it stops at caller cancellation, while the background transport
remains counted by `/inference` until it returns. Counters from `/inference` are
process totals, not per-task verdicts or checkpoint-compute timing. See
[robot-serving behavior](ROBOT_SERVING.md) for queue limits and cancellation.
Concurrent node action times are accumulated work, so their sum may exceed wall
time; recovery time overlaps work spent in recovery revisions.
