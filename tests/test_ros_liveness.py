import asyncio

import pytest

from robot_vllm.adapters.mock import MockArm
from robot_vllm.control import DeviceRuntime, DriverResult, Rejected
from robot_vllm.lease import GoalLease


def test_late_or_wrong_goal_heartbeat_cannot_resurrect_lease():
    lease = GoalLease(0.5)
    lease.start("first", 10.0)
    assert not lease.renew("other", 10.2)
    assert lease.renew("first", 10.3)
    assert not lease.expired(10.79)
    assert lease.expired(10.8)
    assert not lease.renew("first", 10.81)
    lease.start("second", 11.0)
    assert not lease.renew("first", 11.1)
    assert lease.expired(11.5)


def test_liveness_fault_stops_peer_and_quarantines_until_late_native_result(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path, cancel_timeout_s=0.03)
        reconnect = asyncio.Event()
        class Lost(MockArm):
            async def execute(self, arguments, cancel, feedback):
                await asyncio.sleep(0.03)
                feedback({"coordination_fault": "joint_state_unavailable_or_stale"})
                await reconnect.wait()
                return DriverResult("failed", {"error_code": -4})
        remote, local = Lost("remote"), MockArm("local", stop_delay_s=0.01)
        for arm in (remote, local):
            rt.register(arm.capability(), arm)
        rt.register_group("pair", ["remote.move", "local.move"])
        obs = await rt.observe("pair")
        op = await rt.submit(owner="coordinator", request_id="first", capability="pair",
            observation_id=obs["observation_id"], arguments={"members": {
                n: {"target": 0.2, "steps": 100} for n in ("remote.move", "local.move")}})
        result = await rt.wait(op["execution_id"], timeout_s=1)
        assert result["status"] == "uncertain" and not result["settled"]
        assert all(r["state"] == "quarantined" for r in rt.health()["resources"].values())
        assert local.steps < 100
        old_steps = local.steps
        await asyncio.sleep(0.03)
        assert local.steps == old_steps  # the reachable peer has actually stopped
        reconnect.set()
        await asyncio.wait_for(rt.executions[op["execution_id"]].task, timeout=1)
        result = rt.status(op["execution_id"])
        assert result["status"] == "failed" and result["settled"]
        assert not rt.occupied
        await rt.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", [Rejected, ConnectionError])
def test_group_rechecks_all_sensor_states_before_dispatch(tmp_path, error_type):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        class Stale(MockArm):
            stale = False
            async def observe(self):
                if self.stale:
                    raise error_type("joint_state_unavailable_or_stale")
                return await super().observe()
        local, remote = MockArm("local"), Stale("remote")
        for arm in (local, remote):
            rt.register(arm.capability(), arm)
        rt.register_group("pair", ["local.move", "remote.move"])
        obs = await rt.observe("pair")
        remote.stale = True
        op = await rt.submit(owner="coordinator", request_id="stale", capability="pair",
            observation_id=obs["observation_id"], arguments={"members": {
                n: {"target": 0.2, "steps": 5} for n in ("local.move", "remote.move")}})
        result = await rt.wait(op["execution_id"])
        assert result["status"] == "failed" and result["detail"]["dispatched"] is False
        assert local.started == remote.started == 0
        await rt.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_lease_timeout(timeout):
    with pytest.raises(ValueError):
        GoalLease(timeout)
