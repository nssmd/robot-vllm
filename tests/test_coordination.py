import asyncio
from dataclasses import replace

import pytest

from robot_vllm.adapters.mock import MockArm
from robot_vllm.control import DeviceDescription, DeviceRuntime, DriverResult, Rejected


def setup(runtime, drivers):
    names = []
    for driver in drivers:
        runtime.register_device(DeviceDescription(driver.name, "robot", "arm"))
        cap = replace(driver.capability(), metadata={"device_id": driver.name})
        runtime.register(cap, driver)
        names.append(cap.name)
    runtime.register_group("robot.coordinated", names)
    return names


async def group_submit(runtime, names, request="group", steps=5):
    obs = await runtime.observe("robot.coordinated")
    return await runtime.submit(owner="astra", request_id=request, capability="robot.coordinated",
                                observation_id=obs["observation_id"], arguments={"members": {
                                    n: {"target": 0.3, "steps": steps} for n in names}})


@pytest.mark.parametrize("count", [1, 2, 4])
def test_n_arm_group_executes_and_reserves_every_arm(tmp_path, count):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        arms = [MockArm(f"robot.arm_{i}") for i in range(count)]
        names = setup(rt, arms)
        op = await group_submit(rt, names)
        assert len(rt.occupied) == count
        assert len(rt.topology()["devices"]) == count
        result = await rt.wait(op["execution_id"])
        assert result["status"] == "completed"
        assert set(result["detail"]["members"]) == set(names)
        assert all(a.started == 1 and a.steps == 5 for a in arms)
        assert result["detail"]["physical_start_synchronization"] is False
        await rt.close()
    asyncio.run(scenario())


def test_all_members_validate_before_any_motion(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        arms = [MockArm("left"), MockArm("right")]
        names = setup(rt, arms)
        obs = await rt.observe("robot.coordinated")
        with pytest.raises(Rejected, match="out_of_range"):
            await rt.submit(owner="astra", request_id="invalid", capability="robot.coordinated",
                            observation_id=obs["observation_id"], arguments={"members": {
                                names[0]: {"target": 0.2, "steps": 5},
                                names[1]: {"target": 7, "steps": 5}}})
        assert all(a.started == 0 for a in arms) and not rt.occupied
        await rt.close()
    asyncio.run(scenario())


def test_group_conflict_never_reserves_only_one_arm(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        arms = [MockArm("left"), MockArm("right")]
        names = setup(rt, arms)
        obs = await rt.observe(names[1])
        single = await rt.submit(owner="vla", request_id="single", capability=names[1],
                                 observation_id=obs["observation_id"], arguments={"target": 0.1, "steps": 10})
        with pytest.raises(Rejected, match="resource_busy"):
            await group_submit(rt, names)
        assert "left/motion" not in rt.occupied
        await rt.wait(single["execution_id"])
        await rt.close()
    asyncio.run(scenario())


def test_member_failure_cancels_peer_and_retains_reservation_until_settled(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        class Failed(MockArm):
            async def execute(self, arguments, cancel, feedback):
                await asyncio.sleep(0.02)
                return DriverResult("failed", {"controller_error": "fixture"})
        arms = [Failed("left"), MockArm("right", stop_delay_s=0.08)]
        names = setup(rt, arms)
        op = await group_submit(rt, names, steps=100)
        await asyncio.sleep(0.05)
        assert set(rt.occupied) == {"left/motion", "right/motion"}
        result = await rt.wait(op["execution_id"])
        assert result["status"] == "failed" and result["settled"]
        assert result["detail"]["members"][names[1]]["status"] == "canceled"
        assert arms[1].steps < 100 and not rt.occupied
        await rt.close()
    asyncio.run(scenario())


def test_unknown_member_state_quarantines_whole_group(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        class Lost(MockArm):
            async def execute(self, arguments, cancel, feedback):
                await asyncio.sleep(0.01)
                raise ConnectionError("lost acknowledgement")
        names = setup(rt, [Lost("left"), MockArm("right")])
        op = await group_submit(rt, names, steps=20)
        result = await rt.wait(op["execution_id"])
        assert result["status"] == "uncertain" and not result["settled"]
        assert set(rt.occupied) == {"left/motion", "right/motion"}
        await rt.close()
        rt.journal.close()
    asyncio.run(scenario())


def test_aliases_of_same_actuator_cannot_be_parallel_group_members(tmp_path):
    async def scenario():
        rt = DeviceRuntime(tmp_path)
        arm = MockArm("arm")
        rt.register(arm.capability(), arm)
        rt.register(replace(arm.capability(), name="arm.other_mode"), arm)
        with pytest.raises(Rejected, match="share_actuator"):
            rt.register_group("bad", ["arm.move", "arm.other_mode"])
        await rt.close()
    asyncio.run(scenario())
