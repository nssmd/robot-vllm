"""Validated task DAGs and resource-aware execution above DeviceRuntime."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import math
import re
import time

from .control import Rejected, check_schema, clone
from .protocol import ProviderError


DAG_SCHEMA = "robot_runtime.dag.v1"


@dataclass(frozen=True)
class TaskNode:
    id: str
    capability: str
    policy: str = "code"
    depends_on: tuple[str, ...] = ()
    instruction: str = ""
    arguments: dict = field(default_factory=dict)
    max_rounds: int = 1
    timeout_s: float = 60.0


@dataclass(frozen=True)
class TaskPlan:
    nodes: tuple[TaskNode, ...]

    def to_dict(self):
        return {"schema": DAG_SCHEMA, "nodes": [clone(asdict(n)) for n in self.nodes]}

    @classmethod
    def parse(cls, payload, runtime, policies, *, completed=None):
        if (not isinstance(payload, dict) or set(payload) != {"schema", "nodes"}
                or payload["schema"] != DAG_SCHEMA or not isinstance(payload["nodes"], list)
                or not 1 <= len(payload["nodes"]) <= 64):
            raise Rejected("invalid_dag_envelope")
        nodes = []
        allowed = set(TaskNode.__dataclass_fields__)
        for raw in payload["nodes"]:
            if not isinstance(raw, dict) or raw.keys() - allowed or not {"id", "capability"} <= raw.keys():
                raise Rejected("invalid_dag_node")
            data = dict(raw)
            deps = data.get("depends_on", [])
            if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
                raise Rejected("invalid_dependencies")
            data["depends_on"] = tuple(deps)
            node = TaskNode(**data)
            if (not isinstance(node.id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", node.id)
                    or not isinstance(node.capability, str) or node.capability not in runtime.capabilities
                    or not isinstance(node.policy, str) or node.policy not in policies):
                raise Rejected("unknown_node_capability_or_policy")
            if (not isinstance(node.instruction, str) or len(node.instruction) > 12000
                    or not isinstance(node.arguments, dict) or type(node.max_rounds) is not int
                    or not 1 <= node.max_rounds <= 32 or type(node.timeout_s) not in (int, float)
                    or not math.isfinite(node.timeout_s) or not 0 < node.timeout_s <= 3600
                    or len(node.depends_on) != len(set(node.depends_on))):
                raise Rejected("invalid_node_limits")
            if node.policy == "code":
                if node.max_rounds != 1:
                    raise Rejected("code_node_requires_one_round")
                cap, driver = runtime.capabilities[node.capability]
                check_schema(node.arguments, cap.arguments)
                driver.validate(node.arguments)
            nodes.append(node)
        by_id = {n.id: n for n in nodes}
        if len(by_id) != len(nodes):
            raise Rejected("duplicate_node_id")
        if any(dep not in by_id for n in nodes for dep in n.depends_on):
            raise Rejected("unknown_dependency")
        visited = set()
        while len(visited) < len(nodes):
            ready = {n.id for n in nodes if n.id not in visited and set(n.depends_on) <= visited}
            if not ready:
                raise Rejected("cyclic_dag")
            visited.update(ready)
        # Replanning occurs only after settlement and must retain successful nodes exactly.
        for node_id, record in (completed or {}).items():
            if node_id not in by_id or clone(asdict(by_id[node_id])) != record["node"]:
                raise Rejected("completed_node_modified_or_removed")
        return cls(tuple(nodes))


@dataclass(frozen=True)
class NodeProposal:
    observation_id: str
    capability: str
    arguments: dict | None
    done: bool


class CodeNodePolicy:
    async def propose(self, context):
        return NodeProposal(context["observation"]["observation_id"], context["node"]["capability"],
                            context["node"]["arguments"], True)


class DAGScheduler:
    def __init__(self, runtime, policies, journal, *, max_parallel=4, deadline_s=300.0):
        if type(max_parallel) is not int or not 1 <= max_parallel <= 64 or not 0 < deadline_s <= 86400:
            raise ValueError("invalid scheduler limits")
        self.runtime, self.policies, self.journal = runtime, policies, journal
        self.max_parallel, self.deadline_s = max_parallel, deadline_s
        self.stop = asyncio.Event()
        self.execution_ids = set()
        self.node_tasks = set()
        self.reservation_owners = set()

    async def _execute_node(self, node, *, task_id, task, revision, predecessors):
        started = time.monotonic()
        feedback, executions = None, []
        result = {"node": clone(asdict(node)), "status": "failed", "reason": "round_budget",
                  "executions": executions, "rounds": 0,
                  "perception_time_s": 0.0, "action_time_s": 0.0}
        cap, _ = self.runtime.capabilities[node.capability]
        try:
            for step in range(node.max_rounds):
                if self.stop.is_set():
                    result.update(status="canceled", reason="dag_stopped")
                    break
                remaining = node.timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    result.update(status="failed", reason="node_deadline")
                    break
                tick = time.monotonic()
                observation = await self.runtime.observe(node.capability)
                result["perception_time_s"] += time.monotonic() - tick
                context = {"schema": "robot_runtime.node_context.v1", "task_id": task_id,
                           "task": task, "node": clone(asdict(node)), "round": step,
                           "capability": clone(asdict(cap)), "observation": observation,
                           "feedback": feedback, "predecessors": predecessors}
                proposal = await asyncio.wait_for(self.policies[node.policy].propose(context), remaining)
                result["rounds"] += 1
                if self.stop.is_set():
                    result.update(status="canceled", reason="dag_stopped_before_dispatch")
                    break
                if (not isinstance(proposal, NodeProposal) or type(proposal.done) is not bool
                        or proposal.observation_id != observation["observation_id"]
                        or proposal.capability != node.capability):
                    raise Rejected("policy_context_mismatch")
                if proposal.arguments is None:
                    if not proposal.done:
                        raise Rejected("empty_nonterminal_proposal")
                    result.update(status="completed", reason="policy_finished_subtask")
                    break
                remaining = node.timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    result.update(status="failed", reason="node_deadline_before_dispatch")
                    break
                action_started = time.monotonic()
                op = await self.runtime.submit(owner=task_id + ":" + node.id,
                    request_id=f"{task_id}:{revision}:{node.id}:{step}", capability=node.capability,
                    observation_id=proposal.observation_id, arguments=proposal.arguments,
                    timeout_s=min(cap.max_duration_s, remaining))
                self.execution_ids.add(op["execution_id"])
                executions.append(op["execution_id"])
                native_wait = asyncio.create_task(self.runtime.wait(op["execution_id"], timeout_s=60))
                stop_wait = asyncio.create_task(self.stop.wait())
                try:
                    while True:
                        done, _ = await asyncio.wait((native_wait, stop_wait), return_when=asyncio.FIRST_COMPLETED)
                        if stop_wait in done and not native_wait.done():
                            await self.runtime.cancel(op["execution_id"], owner=task_id + ":" + node.id)
                        feedback = await native_wait
                        if feedback["settled"] or feedback["status"] == "uncertain":
                            break
                        native_wait = asyncio.create_task(self.runtime.wait(op["execution_id"], timeout_s=60))
                finally:
                    stop_wait.cancel()
                    if not native_wait.done():
                        native_wait.cancel()
                    await asyncio.gather(stop_wait, native_wait, return_exceptions=True)
                    result["action_time_s"] += time.monotonic() - action_started
                result["feedback"] = feedback
                if feedback["status"] != "completed":
                    result.update(status=feedback["status"], reason=feedback["reason"] or "execution_not_completed")
                    break
                if proposal.done:
                    result.update(status="completed", reason="subtask_execution_completed")
                    break
        except asyncio.CancelledError:
            for execution_id in executions:
                if not self.runtime.executions[execution_id].settled:
                    await self.runtime.cancel(execution_id, owner=task_id + ":" + node.id)
                    await self.runtime.wait(execution_id, timeout_s=min(60, self.runtime.cancel_timeout + 0.2))
            unresolved = any(not self.runtime.executions[e].settled for e in executions)
            result.update(status="uncertain" if unresolved else "canceled", reason="dag_task_canceled")
        except ProviderError:
            result.update(status="infra", reason="model_provider_error")
        except asyncio.TimeoutError:
            result.update(status="infra", reason="policy_timeout")
        except Rejected as exc:
            result.update(status="failed", reason=str(exc))
        except Exception as exc:
            result.update(status="infra", reason="node_error:" + type(exc).__name__)
        result["wall_time_s"] = time.monotonic() - started
        if self.runtime.store:
            self.runtime.store.checkpoint(task_id, result)
        await self.runtime.release_policy(task_id + ":" + node.id)
        self.journal.emit("node_finished", node_id=node.id, result=result)
        return result

    async def run(self, plan, **kwargs):
        try:
            return await self._run_loop(plan, **kwargs)
        except asyncio.CancelledError:
            self.stop.set()
            for work in self.node_tasks:
                if not work.done():
                    work.cancel()
            values = await asyncio.gather(*self.node_tasks, return_exceptions=True)
            results = {v["node"]["id"]: v for v in values if isinstance(v, dict)}
            status = "uncertain" if any(not self.runtime.executions[e].settled for e in self.execution_ids) else "canceled"
            return {"status": status, "nodes": results, "task_verdict": None, "reason": "dag_canceled"}
        finally:
            for owner in self.reservation_owners:
                await self.runtime.release_policy(owner)

    async def _run_loop(self, plan, *, task_id, task, revision=0, completed=None):
        results = clone(completed or {})
        pending = {node.id: node for node in plan.nodes if node.id not in results}
        running = {}
        started = time.monotonic()
        peak = 0
        while pending or running:
            if time.monotonic() - started >= self.deadline_s:
                self.stop.set()
            active_resources = set(self.runtime.occupied)
            for node, _ in running.values():
                active_resources.update(self.runtime.capabilities[node.capability][0].resources)
            if not self.stop.is_set():
                for node_id, node in list(pending.items()):
                    if len(running) >= self.max_parallel:
                        break
                    if not all(dep in results and results[dep]["status"] == "completed" for dep in node.depends_on):
                        continue
                    resources = set(self.runtime.capabilities[node.capability][0].resources)
                    if resources & self.runtime.blocked_resources():
                        results[node_id] = {"node": clone(asdict(node)), "status": "uncertain",
                            "reason": "resource_recovery_required", "executions": []}
                        del pending[node_id]
                        self.stop.set()
                        break
                    if resources & active_resources:
                        continue
                    if not await self.runtime.reserve_policy(task_id + ":" + node_id, resources):
                        continue
                    self.reservation_owners.add(task_id + ":" + node_id)
                    active_resources.update(resources)
                    predecessors = {dep: results[dep] for dep in node.depends_on}
                    work = asyncio.create_task(self._execute_node(node, task_id=task_id, task=task,
                        revision=revision, predecessors=predecessors))
                    running[node_id] = node, work
                    self.node_tasks.add(work)
                    del pending[node_id]
                    self.journal.emit("node_started", node_id=node_id, policy=node.policy,
                                      capability=node.capability, revision=revision)
                    peak = max(peak, len(running))
            if not running:
                ready_but_busy = any(all(dep in results and results[dep]["status"] == "completed"
                                        for dep in n.depends_on) for n in pending.values())
                if pending and ready_but_busy and not self.stop.is_set():
                    await asyncio.sleep(0.02)
                    continue
                break
            done, _ = await asyncio.wait([work for _, work in running.values()], timeout=0.1,
                                         return_when=asyncio.FIRST_COMPLETED)
            for node_id, (_, work) in list(running.items()):
                if work in done:
                    results[node_id] = await work
                    del running[node_id]
                    if results[node_id]["status"] != "completed":
                        self.stop.set()
        for node_id, node in pending.items():
            results[node_id] = {"node": clone(asdict(node)), "status": "blocked",
                                "reason": "dag_stopped_or_dependency_failed", "executions": []}
        states = {r["status"] for r in results.values()}
        status = ("completed" if states == {"completed"} else
                  "uncertain" if "uncertain" in states else
                  "infra" if "infra" in states else
                  "canceled" if states <= {"completed", "canceled", "blocked"} and self.stop.is_set() else "failed")
        return {"status": status, "nodes": results, "peak_parallel_nodes": peak,
                "wall_time_s": time.monotonic() - started,
                "task_verdict": None, "note": "DAG completion is not a simulator task-success verdict."}
