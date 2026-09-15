import asyncio
from dataclasses import replace
import json
import time

import pytest

from robot_vllm.adapters.mock import MockArm
from robot_vllm.control import DeviceRuntime, Rejected
from robot_vllm.control_tools import RuntimeTools


async def dispatch(runtime, name, *, owner="astra", target=0.5, steps=5, request="one", observation=None, **kw):
    obs = observation or await runtime.observe(name)
    return await runtime.submit(owner=owner, request_id=request, capability=name,
                                observation_id=obs["observation_id"],
                                arguments={"target": target, "steps": steps}, **kw)


def test_two_devices_overlap_but_same_resource_is_exclusive(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        a, b = MockArm("a"), MockArm("b")
        for arm in (a, b):
            rt.register(arm.capability(), arm)
        obs = await rt.observe("a.move")
        first = await dispatch(rt, "a.move", observation=obs)
        second = await dispatch(rt, "b.move", request="two")
        assert rt.health()["active_executions"] == 2
        with pytest.raises(Rejected, match="resource_busy"):
            await dispatch(rt, "a.move", request="conflict", observation=obs)
        results = await asyncio.gather(rt.wait(first["execution_id"]), rt.wait(second["execution_id"]))
        assert all(r["status"] == "completed" and r["settled"] for r in results)
        assert a.started == b.started == 1
        assert not rt.health()["active_executions"]
        await rt.close()
        events = [json.loads(line) for line in (rt.output / "control-events.jsonl").read_text().splitlines()]
        observations = {e["observation"]["observation_id"] for e in events if e["event"] == "observation"}
        assert all(e["observation_id"] in observations for e in events if e["event"] == "accepted")
    asyncio.run(scenario())


def test_cancellation_holds_resource_until_stop_acknowledged(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path, cancel_timeout_s=0.01)
        arm = MockArm("arm", stop_delay_s=0.1)
        rt.register(arm.capability(), arm)
        first = await dispatch(rt, "arm.move", steps=100)
        await asyncio.sleep(0.02)
        await rt.cancel(first["execution_id"], owner="astra")
        uncertain = await rt.wait(first["execution_id"])
        assert uncertain["status"] == "uncertain" and not uncertain["settled"]
        assert rt.health()["resources"]["arm/motion"]["state"] == "quarantined"
        with pytest.raises(Rejected, match="resource_busy"):
            await dispatch(rt, "arm.move", owner="vla", request="handoff")
        await asyncio.sleep(0.15)
        assert rt.status(first["execution_id"])["status"] == "canceled"
        fresh = await dispatch(rt, "arm.move", owner="vla", request="handoff", steps=1)
        assert (await rt.wait(fresh["execution_id"]))["status"] == "completed"
        await rt.close()
    asyncio.run(scenario())


def test_idempotency_and_stale_observation_prevent_reexecution(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        arm = MockArm("arm")
        rt.register(arm.capability(), arm)
        obs = await rt.observe("arm.move")
        first = await dispatch(rt, "arm.move", observation=obs)
        duplicate = await dispatch(rt, "arm.move", observation=obs)
        assert first["execution_id"] == duplicate["execution_id"]
        with pytest.raises(Rejected, match="idempotency_conflict"):
            await dispatch(rt, "arm.move", target=0.1, observation=obs)
        await rt.wait(first["execution_id"])
        assert (await dispatch(rt, "arm.move", observation=obs))["execution_id"] == first["execution_id"]
        with pytest.raises(Rejected, match="stale_control_context"):
            await dispatch(rt, "arm.move", request="late-vla", observation=obs)
        assert arm.started == 1
        await rt.close()
    asyncio.run(scenario())


def test_deadline_and_caller_timeout_have_different_semantics(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        arm = MockArm("arm")
        rt.register(arm.capability(), arm)
        op = await dispatch(rt, "arm.move", steps=100, timeout_s=0.05)
        status = await rt.wait(op["execution_id"], timeout_s=0.005)
        assert status["status"] == "running" and not arm.steps > 20
        final = await rt.wait(op["execution_id"])
        assert final["status"] == "canceled" and final["reason"] == "execution_deadline"
        assert arm.steps < 100
        await rt.close()
    asyncio.run(scenario())


def test_atomic_multi_resource_reservation(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        a, b = MockArm("a"), MockArm("b")
        rt.register(a.capability(), a)
        rt.register(b.capability(), b)
        combo = replace(a.capability(), name="handover", resources=("a/motion", "b/motion"))
        rt.register(combo, a)
        obs = await rt.observe("handover")
        busy = await dispatch(rt, "b.move", steps=10)
        with pytest.raises(Rejected, match="resource_busy"):
            await dispatch(rt, "handover", request="both", observation=obs)
        assert rt.health()["resources"]["a/motion"]["state"] == "idle"
        await rt.wait(busy["execution_id"])
        await rt.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [float("nan"), 5, "0.2", True])
def test_bad_arguments_never_execute(tmp_path, value):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        arm = MockArm("arm")
        rt.register(arm.capability(), arm)
        with pytest.raises(Rejected):
            await dispatch(rt, "arm.move", target=value)
        assert arm.started == 0
        await rt.close()
    asyncio.run(scenario())


def test_old_observation_and_wrong_owner_rejected(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path, observation_ttl_s=0.01)
        arm = MockArm("arm")
        rt.register(arm.capability(), arm)
        old = await rt.observe("arm.move")
        await asyncio.sleep(0.02)
        with pytest.raises(Rejected, match="expired_observation"):
            await dispatch(rt, "arm.move", observation=old)
        op = await dispatch(rt, "arm.move", steps=1)
        with pytest.raises(Rejected, match="not_execution_owner"):
            await rt.cancel(op["execution_id"], owner="someone_else")
        await rt.wait(op["execution_id"])
        await rt.close()
    asyncio.run(scenario())


def test_driver_failure_cannot_silently_release_control(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        class FailingArm(MockArm):
            async def execute(self, arguments, cancel, feedback):
                raise ConnectionError("controller disconnected after dispatch")
        arm = FailingArm("arm")
        rt.register(arm.capability(), arm)
        op = await dispatch(rt, "arm.move")
        assert (await rt.wait(op["execution_id"]))["status"] == "uncertain"
        assert (await rt.close())["active_executions"] == 1
        assert not rt.closed
        # Test teardown records uncertainty; no artificial clearance or physical claim.
        rt.journal.close()
    asyncio.run(scenario())


def test_model_tools_use_same_runtime_without_ros_dependency(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        arm = MockArm("arm")
        rt.register(arm.capability(), arm)
        tools = RuntimeTools(rt)
        assert len(tools.schemas()) == 6
        obs = await tools.call("observe", {"capability": "arm.move"})
        op = await tools.call("execute", {"request_id": "toolcall-1", "capability": "arm.move",
                              "observation_id": obs["observation_id"],
                              "arguments": {"target": 0.2, "steps": 1}})
        assert (await tools.call("wait_execution", {"execution_id": op["execution_id"]}))["status"] == "completed"
        await rt.close()
    asyncio.run(scenario())


def test_dispatch_rechecks_observation_after_event_loop_delay(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path, observation_ttl_s=0.01)
        arm = MockArm("arm")
        rt.register(arm.capability(), arm)
        op = await dispatch(rt, "arm.move")
        time.sleep(0.02)  # Deliberately stall scheduling after acceptance, before dispatch.
        result = await rt.wait(op["execution_id"])
        assert result["status"] == "failed" and result["detail"]["dispatched"] is False
        assert arm.started == 0 and not rt.occupied
        await rt.close()
    asyncio.run(scenario())
