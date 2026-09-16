import asyncio
import json
import threading

import httpx
import pytest

from robot_vllm.control import DeviceRuntime
from robot_vllm.config import validate_config
from robot_vllm.control_tools import create_system_app
from robot_vllm.deployment import Deployment, mock_topology
from robot_vllm.inference import InferencePool
from robot_vllm.platform import RobotSystem
from robot_vllm.protocol import ProviderError, ProviderTimeout


async def until(predicate):
    deadline = asyncio.get_running_loop().time() + 3
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(.001)


@pytest.mark.parametrize("options", [
    {"max_concurrency": 0}, {"max_concurrency": True}, {"max_queue": -1},
    {"queue_timeout_s": float("nan")}, {"queue_timeout_s": 0}, {"unknown": 1},
])
def test_inference_configuration_rejected_offline(options):
    with pytest.raises(ValueError):
        validate_config({**mock_topology(1), "models": model_config(**options)})


def test_queue_is_fifo_bounded_and_cancel_race_returns_granted_slot():
    async def scenario():
        pool = InferencePool(max_concurrency=1, max_queue=2)
        async with pool.admit():
            first = asyncio.create_task(pool._acquire())
            second = asyncio.create_task(pool._acquire())
            await until(lambda: pool.snapshot()["queued"] == 2)
            with pytest.raises(ProviderError, match="queue_full"):
                await pool._acquire()
            assert pool.snapshot()["overloaded"] == 1
        # The first waiter owns the newly granted slot before it resumes.
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        slot = await asyncio.wait_for(second, 1)
        assert pool.snapshot()["active_slots"] == 1
        slot.release()
        assert pool.snapshot()["active_slots"] == pool.snapshot()["queued"] == 0
        assert pool.snapshot()["canceled_queued"] == 1
    asyncio.run(scenario())


def test_queue_timeout_and_shutdown_do_not_dispatch():
    async def scenario():
        pool = InferencePool(max_concurrency=1, queue_timeout_s=.02)
        async with pool.admit():
            with pytest.raises(ProviderTimeout, match="queue_timeout"):
                await pool._acquire()
            assert pool.snapshot()["queued"] == 0
            waiter = asyncio.create_task(pool._acquire())
            await until(lambda: pool.snapshot()["queued"] == 1)
            pool.close()
            with pytest.raises(ProviderError, match="pool_closed"):
                await waiter
        assert pool.snapshot()["started"] == pool.snapshot()["active_slots"] == 0
        with pytest.raises(ProviderError, match="pool_closed"):
            await pool._acquire()
    asyncio.run(scenario())


def test_canceled_caller_retains_capacity_until_blocking_transport_finishes():
    async def scenario():
        pool = InferencePool(max_concurrency=1, max_queue=0)
        entered, release = threading.Event(), threading.Event()
        def blocking():
            entered.set()
            assert release.wait(3)
            return "late response"
        async def request():
            async with pool.admit() as slot:
                return await slot.run(blocking)
        work = asyncio.create_task(request())
        try:
            await until(entered.is_set)
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            assert pool.snapshot()["active_slots"] == pool.snapshot()["inflight_transports"] == 1
            with pytest.raises(ProviderError, match="queue_full"):
                await request()
        finally:
            release.set()
            await until(lambda: pool.snapshot()["active_slots"] == 0)
        assert await request() == "late response"
        assert pool.snapshot()["canceled_inflight"] == 1
        assert pool.snapshot()["transport_completed"] == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("concurrency", [1, 2, 4])
def test_global_transport_concurrency_and_failure_cleanup(concurrency):
    async def scenario():
        pool = InferencePool(max_concurrency=concurrency)
        release = threading.Event()
        entered, active, peak = [], 0, 0
        lock = threading.Lock()
        def blocking(index):
            nonlocal active, peak
            with lock:
                entered.append(index)
                active += 1
                peak = max(peak, active)
            try:
                assert release.wait(3)
                if index == 0:
                    raise ProviderError("fixture failure")
                return index
            finally:
                with lock:
                    active -= 1
        async def request(index):
            async with pool.admit() as slot:
                return await slot.run(blocking, index)
        work = [asyncio.create_task(request(i)) for i in range(8)]
        try:
            await until(lambda: len(entered) == concurrency)
            assert pool.snapshot()["queued"] == 8 - concurrency
        finally:
            release.set()
        results = await asyncio.gather(*work, return_exceptions=True)
        assert isinstance(results[0], ProviderError)
        assert results[1:] == list(range(1, 8))
        assert peak == concurrency
        assert pool.snapshot()["active_slots"] == pool.snapshot()["queued"] == 0
        assert pool.snapshot()["transport_errors"] == 1
    asyncio.run(scenario())


def model_config(**inference):
    return {"gp6": {"kind": "openai_chat", "model": "fixture",
            "endpoint": "http://unused.invalid", "inference": {"max_concurrency": 1, **inference}}}


def node_plan(arm):
    return {"schema": "robot_runtime.dag.v1", "nodes": [{"id": "move", "policy": "gp6",
        "capability": f"robot.arm_{arm}.move", "timeout_s": 3}]}


def proposal_response(body):
    context = json.loads(body["messages"][1]["content"][0]["text"])
    value = {"schema": "robot_runtime.node_proposal.v1",
             "observation_id": context["observation"]["observation_id"],
             "capability": context["node"]["capability"],
             "arguments": {"target": .2, "steps": 1}, "done": True}
    return {"choices": [{"message": {"content": json.dumps(value)}}]}, context


def test_shared_model_queue_samples_robot_only_after_admission(monkeypatch, tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        deployment = Deployment(rt, mock_topology(2))
        system = RobotSystem(rt, model_config())
        pool = system.endpoints["gp6"].pool
        release, entered = threading.Event(), threading.Event()
        calls = []
        def post(url, body, key, timeout):
            response, context = proposal_response(body)
            calls.append(context)
            if len(calls) == 1:
                entered.set()
                assert release.wait(3)
            return response
        monkeypatch.setattr("robot_vllm.models.post_json", post)
        first = asyncio.create_task(system.run("first", plan=node_plan(1)))
        try:
            await until(entered.is_set)
            second = asyncio.create_task(system.run("second", plan=node_plan(2)))
            await until(lambda: pool.snapshot()["queued"] == 1)
            assert len(rt.observations) == 1
            # Sensor state changes while the second robot waits for inference.
            deployment.drivers[1].position = .45
            release.set()
            results = await asyncio.gather(first, second)
            assert all(result["status"] == "completed" for result in results)
            assert calls[1]["observation"]["data"]["position"] == .45
            assert results[1]["model_metrics"]["inference_queue_time_s"] > 0
            assert pool.snapshot()["peak_active_slots"] == 1
        finally:
            release.set()
            await system.close()
            await deployment.close()
    asyncio.run(scenario())


def test_task_cancel_discards_inflight_result_and_removes_queued_inference(monkeypatch, tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        deployment = Deployment(rt, mock_topology(2))
        system = RobotSystem(rt, model_config())
        pool = system.endpoints["gp6"].pool
        entered, release = threading.Event(), threading.Event()
        def post(url, body, key, timeout):
            entered.set()
            assert release.wait(3)
            return proposal_response(body)[0]
        monkeypatch.setattr("robot_vllm.models.post_json", post)
        first = system.start("inflight", plan=node_plan(1))["task_id"]
        try:
            await until(entered.is_set)
            second = system.start("queued", plan=node_plan(2))["task_id"]
            await until(lambda: pool.snapshot()["queued"] == 1)
            await system.cancel(second)
            await asyncio.wait_for(system.tasks[second], 1)
            assert pool.snapshot()["queued"] == 0
            await system.cancel(first)
            await asyncio.wait_for(system.tasks[first], 1)
            assert system.status(first)["status"] == system.status(second)["status"] == "canceled"
            assert not rt.executions and not rt.policy_reservations
            assert pool.snapshot()["inflight_transports"] == 1
            assert system.status(first)["model_metrics"]["calls"][0]["status"] == "canceled"
            release.set()
            await until(lambda: pool.snapshot()["inflight_transports"] == 0)
            assert not rt.executions
        finally:
            release.set()
            await system.close()
            await deployment.close()
    asyncio.run(scenario())


def test_inference_status_requires_auth_and_omits_endpoint_config(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        deployment = Deployment(rt, mock_topology(1))
        system = RobotSystem(rt, model_config())
        app = create_system_app(system, token="test")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.get("/inference")).status_code == 401
            result = await client.get("/inference", headers={"Authorization": "Bearer test"})
            assert result.status_code == 200
            assert result.json()["models"]["gp6"]["active_slots"] == 0
            assert "unused.invalid" not in result.text
        await system.close()
        await deployment.close()
    asyncio.run(scenario())


def test_model_overload_is_infra_without_observation_or_provider_call(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        deployment = Deployment(rt, mock_topology(1))
        system = RobotSystem(rt, model_config(max_queue=0))
        async with system.endpoints["gp6"].pool.admit():
            result = await system.run("overload", plan=node_plan(1))
        assert result["status"] == "infra"
        assert result["nodes"]["move"]["reason"] == "inference_queue_full"
        assert result["model_metrics"]["model_calls"] == 0
        assert result["task_verdict"] is None
        assert not rt.observations and not rt.executions
        from pathlib import Path
        manifest = json.loads((Path(result["run_dir"]) / "request.json").read_text())
        assert manifest["models"]["gp6"]["inference"]["max_queue"] == 0
        await system.close()
        await deployment.close()
    asyncio.run(scenario())
