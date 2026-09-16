import asyncio
import json

import httpx
import pytest

from robot_vllm.control import DeviceRuntime, Rejected, check_schema
from robot_vllm.control_tools import create_system_app
from robot_vllm.dag import DAG_SCHEMA
from robot_vllm.deployment import Deployment, mock_topology
from robot_vllm.models import ModelEndpoint
from robot_vllm.platform import RobotSystem


def code_node(name, target=0.2, steps=1, **kw):
    return {"id": name, "capability": "robot.arm_1.move", "policy": "code",
            "arguments": {"target": target, "steps": steps}, **kw}


def test_gp6_replans_only_unfinished_work(monkeypatch, tmp_path):
    async def generate(self, *, instruction, context, role, meter, admission=None):
        if role == "planner":
            complete = context["completed"]
            if complete:
                return {"schema": DAG_SCHEMA, "nodes": [complete["prepare"]["node"],
                    code_node("finish", depends_on=["prepare"])]}
            return {"schema": DAG_SCHEMA, "nodes": [code_node("prepare"),
                code_node("finish", policy="gp6", depends_on=["prepare"])]}
        return {"schema": "robot_runtime.node_proposal.v1",
                "observation_id": context["observation"]["observation_id"],
                "capability": context["node"]["capability"],
                "arguments": {"target": 9.0, "steps": 1}, "done": True}
    monkeypatch.setattr(ModelEndpoint, "generate", generate)
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        deployment = Deployment(rt, mock_topology(1))
        system = RobotSystem(rt, {"gp6": {"kind": "openai_chat", "model": "fixture", "endpoint": "http://unused.invalid"}})
        result = await system.run("prepare then finish")
        assert result["status"] == "completed" and result["plan_revisions"] == 2
        assert deployment.drivers[0].started == 2  # prepare once; only unfinished work was repaired
        await deployment.close()
    asyncio.run(scenario())


def test_system_http_tasks_auth_status_and_cancel(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        deployment = Deployment(rt, mock_topology(2))
        system = RobotSystem(rt)
        app = create_system_app(system, token="synthetic-token")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
            assert (await client.get("/tasks/unknown")).status_code == 401
            client.headers["Authorization"] = "Bearer synthetic-token"
            payload = {"task": "test cancellation", "plan": {"schema": DAG_SCHEMA,
                        "nodes": [code_node("slow", steps=100)]}}
            response = await client.post("/tasks", json=payload)
            assert response.status_code == 200
            task_id = response.json()["task_id"]
            deadline = asyncio.get_running_loop().time() + 3
            while not rt.executions:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
            assert (await client.post(f"/tasks/{task_id}/cancel")).status_code == 200
            await system.tasks[task_id]
            result = (await client.get(f"/tasks/{task_id}")).json()
            assert result["status"] == "canceled" and result["task_verdict"] is None
            assert not rt.occupied
        await system.close()
        await deployment.close()
    asyncio.run(scenario())


def test_canceling_scheduler_coroutine_cancels_device_execution(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        deployment = Deployment(rt, mock_topology(1))
        system = RobotSystem(rt)
        work = asyncio.create_task(system.run("move", plan={"schema": DAG_SCHEMA,
                                     "nodes": [code_node("slow", steps=100)]}))
        while not rt.executions:
            await asyncio.sleep(0.01)
        work.cancel()
        result = await work
        assert result["status"] == "canceled"
        assert not rt.occupied
        await deployment.close()
    asyncio.run(scenario())


def test_two_tasks_share_one_resource_without_duplicate_inference_or_failure(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        deployment = Deployment(rt, mock_topology(1))
        system = RobotSystem(rt)
        payload = {"schema": DAG_SCHEMA, "nodes": [code_node("move", steps=5)]}
        results = await asyncio.gather(system.run("first task", plan=payload), system.run("second task", plan=payload))
        assert all(r["status"] == "completed" for r in results)
        assert deployment.drivers[0].started == 2
        assert not rt.policy_reservations and not rt.occupied
        await deployment.close()
    asyncio.run(scenario())


def test_cancel_before_job_coroutine_starts_is_terminal(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        deployment = Deployment(rt, mock_topology(1))
        system = RobotSystem(rt)
        job = system.start("cancel immediately", plan={"schema": DAG_SCHEMA, "nodes": [code_node("move")]})
        await system.cancel(job["task_id"])
        await asyncio.gather(system.tasks[job["task_id"]], return_exceptions=True)
        assert system.status(job["task_id"])["status"] == "canceled"
        assert not rt.executions and not rt.policy_reservations
        await deployment.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("positions", [[], [None, None], [float("nan"), 0.0]])
def test_missing_arrays_never_treated_as_valid_zero_vectors(positions):
    schema = {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}}
    with pytest.raises(Rejected):
        check_schema(positions, schema)
    check_schema([0.0, 0.0], schema)


def test_model_responses_api_extracts_output_and_metering(monkeypatch, tmp_path):
    captured = {}
    def post(url, body, key, timeout):
        captured.update(body)
        return {"output": [{"content": [{"type": "output_text", "text": json.dumps({"value": 1})}]}],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}}
    monkeypatch.setattr("robot_vllm.models.post_json", post)
    async def scenario():
        from robot_vllm.models import ModelMeter
        from robot_vllm.runtime import Journal
        journal = Journal(tmp_path / "model.jsonl")
        meter = ModelMeter(journal)
        client = ModelEndpoint("gp6", {"kind": "openai_responses", "endpoint": "http://unused.invalid", "model": "fixture"})
        assert await client.generate(instruction="return JSON", context={"value": 0}, role="planner", meter=meter) == {"value": 1}
        assert captured["text"]["format"]["type"] == "json_object"
        assert meter.summary()["total_tokens"] == 5
        journal.close()
    asyncio.run(scenario())
