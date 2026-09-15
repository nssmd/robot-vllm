import asyncio
import copy

import httpx
import pytest

from robot_vllm.config import validate_config
from robot_vllm.control import DeviceRuntime
from robot_vllm.control_tools import create_system_app
from robot_vllm.deployment import Deployment, mock_topology
from robot_vllm.platform import RobotSystem


@pytest.mark.parametrize("change", [
    {"unknown": 1}, {"scheduler": {"max_parallel": True}},
    {"runtime": {"cancel_timout_s": 1}}, {"models": {"vla": {"kind": "vla_json", "model": "test"}}},
    {"api": {"max_body_bytes": -1}},
])
def test_invalid_configuration_rejected_offline(change):
    config = {**mock_topology(2), **change}
    with pytest.raises(ValueError):
        validate_config(config)


def test_same_physical_action_cannot_be_aliased_as_independent_devices():
    first = {"name": "one", "backend": "ros2", "action_name": "/arm/action",
        "joint_state_topic": "/arm/joints", "joint_names": ["j1"], "limits": [[-1, 1]]}
    second = {**copy.deepcopy(first), "name": "two"}
    with pytest.raises(ValueError, match="duplicate_physical_action_endpoint"):
        validate_config({"devices": [first, second]})


def test_http_backpressure_deduplication_body_limit_and_health(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path, state_dir=tmp_path / "state")
        deployment = Deployment(rt, mock_topology(1))
        system = RobotSystem(rt, max_active_tasks=1)
        app = create_system_app(system, token="test", max_body_bytes=1024)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test",
                                     headers={"authorization": "Bearer test"}) as client:
            assert (await client.get("/readyz")).status_code == 200
            assert "robot_runtime_active_executions" in (await client.get("/metrics")).text
            assert (await client.post("/tasks", content=b"x" * 2048)).status_code == 413
            payload = {"task": "one", "request_id": "retry-key", "plan": {"schema": "robot_runtime.dag.v1",
                "nodes": [{"id": "move", "capability": "robot.arm_1.move", "arguments": {"target": 0.2, "steps": 100}}]}}
            first = await client.post("/tasks", json=payload)
            assert first.status_code == 200
            duplicate = await client.post("/tasks", json=payload)
            assert duplicate.json()["task_id"] == first.json()["task_id"]
            assert (await client.post("/tasks", json={**payload, "request_id": "another"})).status_code == 429
            assert (await client.post("/tasks", json={**payload, "task": "changed"})).status_code == 409
            await system.cancel(first.json()["task_id"])
            await system.tasks[first.json()["task_id"]]
        await system.close()
        await deployment.close()
    asyncio.run(scenario())
