import asyncio
import json

import pytest

from robot_vllm.adapters.mock import MockArm
from robot_vllm.control import DeviceRuntime, DriverResult, Rejected
from robot_vllm.dag import DAG_SCHEMA
from robot_vllm.platform import RobotSystem
from robot_vllm.state import StateStore


def node(name, target=0.1, depends_on=None):
    return {"id": name, "capability": "arm.move", "arguments": {"target": target, "steps": 1},
            "depends_on": depends_on or []}


def setup(tmp_path, driver=None):
    rt = DeviceRuntime(tmp_path / "runs", state_dir=tmp_path / "state")
    arm = driver or MockArm("arm")
    rt.register(arm.capability(), arm)
    return rt, arm


def pending(tmp_path, phase="dispatching"):
    async def prepare():
        rt, _ = setup(tmp_path)
        await rt.observe("arm.move")
        rt.store.accept_execution({"execution_id": "prior", "owner": "operator", "capability": "arm.move",
            "resources": ["arm/motion"], "status": "running", "settled": False, "phase": phase,
            "dispatch_locator": {"goal_id": "native-prior"}}, "deduplicate", "original-body")
        await rt.close()
    asyncio.run(prepare())


def test_state_directory_exclusive_and_released(tmp_path):
    store = StateStore(tmp_path)
    with pytest.raises(RuntimeError, match="state_directory_in_use"):
        StateStore(tmp_path)
    store.close()
    StateStore(tmp_path).close()


def test_pending_restart_is_quarantined_until_native_terminal_result(tmp_path):
    pending(tmp_path)
    class Reconciler(MockArm):
        known = False
        async def reconcile(self, locator):
            assert locator == {"goal_id": "native-prior"}
            return DriverResult("failed", {"native": "aborted"}, settled=self.known)
    async def scenario():
        rt, arm = setup(tmp_path, Reconciler("arm"))
        assert rt.health()["resources"]["arm/motion"]["state"] == "quarantined"
        obs = await rt.observe("arm.move")
        assert not obs["can_dispatch"]
        with pytest.raises(Rejected, match="resource_recovery_required"):
            await rt.submit(owner="new", request_id="new", capability="arm.move",
                observation_id=obs["observation_id"], arguments={"target": 0.1, "steps": 1})
        assert not (await rt.reconcile("prior"))["settled"]
        assert rt.recovery_pending
        arm.known = True
        assert (await rt.reconcile("prior"))["settled"]
        assert not rt.recovery_pending and arm.started == 0
        await rt.close()
    asyncio.run(scenario())


def test_before_dispatch_intent_recovers_without_driver_call(tmp_path):
    pending(tmp_path, phase="accepted")
    async def scenario():
        rt, arm = setup(tmp_path)
        assert (await rt.reconcile("prior"))["detail"]["reason"] == "interrupted_before_dispatch"
        assert arm.started == 0
        await rt.close()
    asyncio.run(scenario())


def test_changed_topology_cannot_rebind_unsettled_actions(tmp_path):
    pending(tmp_path)
    async def scenario():
        rt = DeviceRuntime(tmp_path / "runs", state_dir=tmp_path / "state")
        other = MockArm("different_robot")
        rt.register(other.capability(), other)
        with pytest.raises(ValueError, match="topology_changed"):
            await rt.observe("different_robot.move")
        await rt.close()
    asyncio.run(scenario())


def test_task_idempotency_survives_restart_without_replay(tmp_path):
    payload = {"schema": DAG_SCHEMA, "nodes": [node("once")]}
    async def first():
        rt, arm = setup(tmp_path)
        system = RobotSystem(rt)
        job = system.start("once", plan=payload, request_id="task-key")
        await system.tasks[job["task_id"]]
        assert arm.started == 1
        await system.close()
        await rt.close()
        return job["task_id"]
    task_id = asyncio.run(first())
    async def second():
        rt, arm = setup(tmp_path)
        system = RobotSystem(rt)
        job = system.start("once", plan=payload, request_id="task-key")
        assert job["task_id"] == task_id and job["status"] == "completed"
        assert arm.started == 0
        with pytest.raises(Rejected, match="idempotency_conflict"):
            system.start("different", plan=payload, request_id="task-key")
        await system.close()
        await rt.close()
    asyncio.run(second())


def test_resume_retains_completed_nodes_and_never_reexecutes_them(tmp_path):
    payload = {"schema": DAG_SCHEMA, "nodes": [node("prepare"), node("finish", 0.5, ["prepare"])]}
    class FailSecond(MockArm):
        async def execute(self, arguments, cancel, feedback):
            if arguments["target"] > 0.3:
                return DriverResult("failed", {"reason": "fixture_failure"})
            return await super().execute(arguments, cancel, feedback)
    async def first():
        rt, arm = setup(tmp_path, FailSecond("arm"))
        system = RobotSystem(rt)
        job = system.start("prepare then finish", plan=payload)
        await system.tasks[job["task_id"]]
        assert system.status(job["task_id"])["status"] == "failed"
        assert set(rt.store.completed(job["task_id"])) == {"prepare"}
        await system.close()
        await rt.close()
        return job["task_id"]
    task_id = asyncio.run(first())
    async def second():
        rt, arm = setup(tmp_path)
        system = RobotSystem(rt)
        system.resume(task_id)
        await system.tasks[task_id]
        assert system.status(task_id)["status"] == "completed"
        assert arm.started == 1 and arm.position == 0.5
        await system.close()
        await rt.close()
    asyncio.run(second())


def test_terminal_action_without_node_checkpoint_blocks_replay(tmp_path):
    async def scenario():
        rt, _ = setup(tmp_path)
        store = rt.store
        store.accept_task("task", None, "", {"task_id": "task", "status": "interrupted",
            "request": {"task": "test", "plan": {"schema": DAG_SCHEMA, "nodes": [node("move")]}, "deadline_s": 20}})
        store.accept_execution({"execution_id": "done-but-not-checkpointed", "owner": "task:move",
            "status": "completed", "settled": True}, "step", "identity")
        system = RobotSystem(rt)
        with pytest.raises(Rejected, match="resume_requires_node_resolution"):
            system.resume("task")
        await rt.close()
    asyncio.run(scenario())


def test_execution_intent_is_durable_before_driver_runs(tmp_path):
    class Inspect(MockArm):
        async def execute(self, arguments, cancel, feedback):
            records = rt.store.executions()
            assert len(records) == 1 and records[0]["phase"] == "dispatching"
            feedback({"dispatch_locator": {"goal_id": "external-goal"}})
            assert rt.store.executions()[0]["dispatch_locator"]["goal_id"] == "external-goal"
            return await super().execute(arguments, cancel, feedback)
    async def scenario():
        nonlocal rt
        rt, _ = setup(tmp_path, Inspect("arm"))
        obs = await rt.observe("arm.move")
        op = await rt.submit(owner="operator", request_id="once", capability="arm.move",
            observation_id=obs["observation_id"], arguments={"target": 0.2, "steps": 1})
        assert (await rt.wait(op["execution_id"]))["status"] == "completed"
        await rt.close()
    rt = None
    asyncio.run(scenario())


def test_persistent_action_duplicate_returns_old_result(tmp_path):
    async def scenario():
        rt, arm = setup(tmp_path)
        obs = await rt.observe("arm.move")
        request = {"owner": "operator", "request_id": "once", "capability": "arm.move",
            "observation_id": obs["observation_id"], "arguments": {"target": 0.2, "steps": 1}}
        op = await rt.submit(**request)
        await rt.wait(op["execution_id"])
        await rt.close()
        rt, arm = setup(tmp_path)
        duplicate = await rt.submit(**json.loads(json.dumps(request)))
        assert duplicate["execution_id"] == op["execution_id"] and duplicate["settled"]
        assert arm.started == 0
        await rt.close()
    asyncio.run(scenario())
