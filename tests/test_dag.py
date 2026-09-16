import asyncio
from dataclasses import asdict

import pytest

from robot_vllm.adapters.mock import MockArm
from robot_vllm.control import DeviceRuntime, Rejected
from robot_vllm.dag import CodeNodePolicy, DAG_SCHEMA, DAGScheduler, NodeProposal, TaskPlan
from robot_vllm.platform import RobotSystem
from robot_vllm.runtime import Journal


def node(name, capability="a.move", **kw):
    return {"id": name, "capability": capability, "arguments": {"target": 0.2, "steps": 4}, **kw}


def plan(*nodes):
    return {"schema": DAG_SCHEMA, "nodes": list(nodes)}


def setup(path):
    rt = DeviceRuntime(path)
    arms = {name: MockArm(name, tick_s=0.01) for name in ("a", "b")}
    for arm in arms.values():
        rt.register(arm.capability(), arm)
    rt.register_group("both", ["a.move", "b.move"])
    return rt, arms


@pytest.mark.parametrize("payload", [
    plan(node("a", depends_on=["b"]), node("b", depends_on=["a"])),
    plan(node("a"), node("a")),
    plan(node("a", depends_on=["missing"])),
    plan(node("a", capability="unknown")),
    plan(node("a", policy="not-configured")),
    plan(node("a", arguments={"target": None, "steps": 1})),
    plan(node("a", arguments={"target": 0.2, "steps": 0})),
])
def test_invalid_graph_rejected_before_any_motion(tmp_path, payload):
    async def scenario():
        rt, arms = setup(tmp_path)
        with pytest.raises(Rejected):
            TaskPlan.parse(payload, rt, {"code": CodeNodePolicy()})
        assert not any(arm.started for arm in arms.values())
        await rt.close()
    asyncio.run(scenario())


def test_parallel_branches_join_before_coordinated_node(tmp_path):
    async def scenario():
        rt, arms = setup(tmp_path)
        system = RobotSystem(rt, max_parallel=4)
        payload = plan(node("left"), node("right", "b.move"), node("join", "both",
            depends_on=["left", "right"], arguments={"members": {
                "a.move": {"target": 0.3, "steps": 2}, "b.move": {"target": -0.3, "steps": 2}}}))
        result = await system.run("coordinate two arms", plan=payload)
        assert result["status"] == "completed" and result["peak_parallel_nodes"] == 2
        assert result["task_verdict"] is None
        assert [arm.started for arm in arms.values()] == [2, 2]
        assert result["model_metrics"]["model_calls"] == 0
        await rt.close()
    asyncio.run(scenario())


def test_same_resource_branches_are_serialized_before_inference(tmp_path):
    async def scenario():
        rt, arms = setup(tmp_path)
        result = await RobotSystem(rt).run("serial same device", plan=plan(node("one"), node("two")))
        assert result["status"] == "completed" and result["peak_parallel_nodes"] == 1
        assert arms["a"].started == 2
        await rt.close()
    asyncio.run(scenario())


def test_vla_node_uses_fresh_observation_and_feedback_each_round(tmp_path):
    async def scenario():
        rt, arms = setup(tmp_path)
        observations = []
        class Policy:
            async def propose(self, context):
                observations.append(context)
                return NodeProposal(context["observation"]["observation_id"], "a.move",
                                    {"target": 0.2 + context["round"] * 0.1, "steps": 2}, context["round"] == 1)
        policies = {"vla": Policy()}
        checked = TaskPlan.parse(plan(node("vla", policy="vla", max_rounds=2)), rt, policies)
        log = Journal(tmp_path / "dag.jsonl")
        result = await DAGScheduler(rt, policies, log).run(checked, task_id="test", task="move")
        assert result["status"] == "completed" and arms["a"].started == 2
        assert observations[0]["observation"]["observation_id"] != observations[1]["observation"]["observation_id"]
        assert observations[1]["feedback"]["status"] == "completed"
        log.close()
        await rt.close()
    asyncio.run(scenario())


def test_failed_node_blocks_descendants(tmp_path):
    async def scenario():
        rt, arms = setup(tmp_path)
        class Invalid:
            async def propose(self, context):
                return NodeProposal("old-observation", "a.move", {"target": 0.2, "steps": 1}, True)
        policies = {"invalid": Invalid(), "code": CodeNodePolicy()}
        checked = TaskPlan.parse(plan(node("bad", policy="invalid"),
                                    node("next", "b.move", depends_on=["bad"])), rt, policies)
        log = Journal(tmp_path / "dag.jsonl")
        result = await DAGScheduler(rt, policies, log).run(checked, task_id="test", task="move")
        assert result["status"] == "failed"
        assert result["nodes"]["next"]["status"] == "blocked"
        assert not any(a.started for a in arms.values())
        log.close()
        await rt.close()
    asyncio.run(scenario())


def test_replanning_cannot_modify_completed_nodes(tmp_path):
    async def scenario():
        rt, _ = setup(tmp_path)
        policies = {"code": CodeNodePolicy()}
        original = TaskPlan.parse(plan(node("done")), rt, policies)
        completed = {"done": {"node": {**asdict(original.nodes[0]), "depends_on": []}, "status": "completed"}}
        with pytest.raises(Rejected, match="completed_node_modified"):
            TaskPlan.parse(plan(node("done", arguments={"target": 0.8, "steps": 1})), rt, policies, completed=completed)
        log = Journal(tmp_path / "dag.jsonl")
        checked = TaskPlan.parse(plan(node("done"), node("next", depends_on=["done"])), rt, policies, completed=completed)
        result = await DAGScheduler(rt, policies, log).run(checked, task_id="test", task="move", completed=completed)
        assert len(rt.executions) == 1 and result["status"] == "completed"
        log.close()
        await rt.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [None, {"target": .2, "steps": 1}])
def test_expired_model_result_cannot_move_or_finish_subtask(tmp_path, arguments):
    async def scenario():
        rt, arms = setup(tmp_path)
        class SlowPolicy:
            async def propose(self, context):
                ticket = context["observation"]["observation_id"]
                rt.observations[ticket]["captured_at"] -= rt.ttl + 1
                return NodeProposal(ticket, "a.move", arguments, True)
        policies = {"slow": SlowPolicy()}
        checked = TaskPlan.parse(plan(node("stale", policy="slow")), rt, policies)
        journal = Journal(tmp_path / "stale.jsonl")
        result = await DAGScheduler(rt, policies, journal).run(checked, task_id="stale", task="move")
        assert result["status"] == "failed"
        assert result["nodes"]["stale"]["reason"] == "expired_observation"
        assert not any(a.started for a in arms.values())
        assert not rt.policy_reservations
        journal.close()
        await rt.close()
    asyncio.run(scenario())
