"""GP6 planning -> DAG -> model/code nodes -> shared multi-device Runtime."""

from __future__ import annotations

import asyncio
import math
import json
import re
import time
import uuid

from .control import Rejected, clone
from .dag import CodeNodePolicy, DAGScheduler, TaskPlan
from .models import GP6Planner, ModelEndpoint, ModelMeter, ModelNodePolicy
from .protocol import ProviderError
from .runtime import Journal, write_json


class RobotSystem:
    def __init__(self, runtime, model_configs=None, *, planner="gp6", max_parallel=4, max_replans=1,
                 max_active_tasks=16):
        if type(max_replans) is not int or not 0 <= max_replans <= 3:
            raise ValueError("max_replans must be 0..3")
        if type(max_parallel) is not int or not 1 <= max_parallel <= 64:
            raise ValueError("max_parallel must be 1..64")
        if type(max_active_tasks) is not int or not 1 <= max_active_tasks <= 256:
            raise ValueError("max_active_tasks must be 1..256")
        self.runtime, self.planner_name = runtime, planner
        self.endpoints = {name: ModelEndpoint(name, cfg) for name, cfg in (model_configs or {}).items()}
        if "code" in self.endpoints:
            raise ValueError("code is a reserved policy name")
        self.max_parallel, self.max_replans = max_parallel, max_replans
        self.jobs = {}
        self.tasks = {}
        self.task_records = {}
        self.request_records = {}
        self.max_active_tasks = max_active_tasks
        self.closing = False

    def _save(self, task_id, record):
        self.task_records[task_id] = clone(record)
        if self.runtime.store:
            prior = self.runtime.store.task(task_id) or {}
            self.runtime.store.update_task(task_id, {**prior, **record})

    def _validate_request(self, task, plan, deadline_s):
        if not isinstance(task, str) or not task.strip() or len(task) > 12000:
            raise Rejected("invalid_task")
        if type(deadline_s) not in (int, float) or not math.isfinite(deadline_s) or not 0 < deadline_s <= 86400:
            raise Rejected("invalid_system_deadline")
        if plan is None and self.planner_name not in self.endpoints:
            raise Rejected("planner_not_configured")
        if plan is not None:
            TaskPlan.parse(plan, self.runtime, {"code", *self.endpoints})

    async def _planning_observations(self):
        # One entry per leaf capability; no evaluator object, reward or private run manifest.
        leaves = [cap.name for cap, _ in self.runtime.capabilities.values() if cap.backend != "coordinated"]
        values = await asyncio.gather(*(self.runtime.observe(name) for name in leaves))
        return {name: value["data"] for name, value in zip(leaves, values)}

    async def _plan(self, planner, context, meter, phase_times):
        async with planner.admit() as admitted:
            tick = time.monotonic()
            observations = await self._planning_observations()
            phase_times["perception_time_s"] += time.monotonic() - tick
            tick = time.monotonic()
            try:
                return await admitted.plan({**context, "observations": observations}, meter)
            finally:
                phase_times["planning_time_s"] += time.monotonic() - tick

    def inference_status(self):
        return {"scope": "per-model-alias totals for this coordinator process",
                "models": {name: endpoint.pool.snapshot() for name, endpoint in self.endpoints.items()}}

    async def run(self, task: str, *, plan=None, deadline_s=300.0, task_id=None,
                  _completed=None, _attempt=0):
        if not isinstance(task, str) or not task.strip() or len(task) > 12000:
            raise Rejected("invalid_task")
        if type(deadline_s) not in (int, float) or not math.isfinite(deadline_s) or not 0 < deadline_s <= 86400:
            raise Rejected("invalid_system_deadline")
        if plan is None and self.planner_name not in self.endpoints:
            raise Rejected("planner_not_configured")
        task_id = task_id or uuid.uuid4().hex
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", task_id):
            raise Rejected("invalid_task_id")
        output = self.runtime.output / "tasks" / task_id
        if _attempt:
            output = output / f"attempt-{_attempt:04d}"
        output.mkdir(parents=True)
        journal = Journal(output / "events.jsonl")
        meter = ModelMeter(journal)
        policies = {"code": CodeNodePolicy(), **{name: ModelNodePolicy(endpoint, meter)
                                               for name, endpoint in self.endpoints.items()}}
        started, completed, history = time.monotonic(), clone(_completed or {}), []
        phase_times = {"planning_time_s": 0.0, "perception_time_s": 0.0,
                       "action_time_s": 0.0, "recovery_time_s": 0.0}
        planner = GP6Planner(self.endpoints[self.planner_name]) if plan is None else None
        write_json(output / "request.json", {"task_id": task_id, "task": task,
            "max_parallel": self.max_parallel, "max_replans": self.max_replans,
            "deadline_s": deadline_s, "planner": self.planner_name if planner else "provided_plan",
            "models": {name: {"kind": e.kind, "model": e.model, "timeout_s": e.timeout,
                "inference": {"max_concurrency": e.pool.limit, "max_queue": e.pool.max_queue,
                              "queue_timeout_s": e.pool.queue_timeout}}
                for name, e in self.endpoints.items()}})
        result = {"status": "infra", "reason": "not_started", "nodes": {}, "task_verdict": None}
        if self.runtime.store and self.runtime.store.task(task_id) is None:
            self.runtime.store.accept_task(task_id, None, "", {"task_id": task_id, "status": "planning",
                "request": {"task": task, "plan": plan, "deadline_s": deadline_s}, "attempt": _attempt})
        try:
            for revision in range(self.max_replans + 1):
                revision_started = time.monotonic()
                remaining = deadline_s - (time.monotonic() - started)
                if remaining <= 0:
                    result.update(status="failed", reason="system_deadline")
                    break
                if planner is not None:
                    context = {"schema": "robot_runtime.planning_context.v1", "task_id": task_id, "task": task,
                        "capabilities": self.runtime.catalog(), "topology": self.runtime.topology(),
                        "policies": list(policies),
                        "completed": completed, "previous_result": history[-1] if history else None}
                    payload = await asyncio.wait_for(self._plan(planner, context, meter, phase_times), remaining)
                else:
                    payload = plan
                write_json(output / f"candidate-{revision:03d}.json", payload)
                checked = TaskPlan.parse(payload, self.runtime, policies, completed=completed)
                write_json(output / f"plan-{revision:03d}.json", checked.to_dict())
                journal.emit("plan_validated", revision=revision, nodes=[n.id for n in checked.nodes])
                scheduler = DAGScheduler(self.runtime, policies, journal, max_parallel=self.max_parallel,
                                         deadline_s=max(0.001, deadline_s - (time.monotonic() - started)))
                self.jobs[task_id] = scheduler
                if self.runtime.store:
                    saved = self.runtime.store.task(task_id)
                    self.runtime.store.update_task(task_id, {**saved, "status": "executing",
                        "saved_plan": checked.to_dict(), "attempt": _attempt})
                if task_id in self.task_records:
                    self.task_records[task_id]["status"] = "executing"
                    self.task_records[task_id]["plan"] = checked.to_dict()
                result = await scheduler.run(checked, task_id=task_id, task=task,
                                             revision=revision + _attempt * 100, completed=completed)
                history.append(clone(result))
                for node_id, record in result["nodes"].items():
                    if node_id not in completed:
                        for metric in ("perception_time_s", "action_time_s"):
                            phase_times[metric] += record.get(metric, 0.0)
                if revision:
                    phase_times["recovery_time_s"] += time.monotonic() - revision_started
                completed = {key: value for key, value in result["nodes"].items() if value["status"] == "completed"}
                if result["status"] == "completed" or planner is None or revision == self.max_replans:
                    break
                # Replanning is forbidden while any affected controller has unresolved state.
                if any(not self.runtime.executions[e].settled for e in scheduler.execution_ids):
                    result.update(status="uncertain", reason="replan_blocked_by_unsettled_device")
                    break
                if result["status"] in ("infra", "canceled"):
                    break  # Provider/infrastructure errors need intervention, not unchanged retries.
                journal.emit("replan_requested", completed_nodes=list(completed), revision=revision + 1)
        except asyncio.CancelledError:
            result.update(status="canceled", reason="planning_canceled")
        except Rejected as exc:
            result.update(status="failed", reason=str(exc))
        except (ProviderError, asyncio.TimeoutError) as exc:
            result.update(status="infra", reason="planning_provider_error:" + type(exc).__name__)
            if isinstance(exc, ProviderError):
                result["provider_error"] = str(exc)
        except Exception as exc:
            result.update(status="infra", reason="system_error:" + type(exc).__name__)
        finally:
            self.jobs.pop(task_id, None)
            result.update(task_id=task_id, run_dir=str(output.resolve()), plan_revisions=len(history),
                          model_metrics=meter.summary(), wall_time_s=time.monotonic() - started,
                          phase_times=phase_times,
                          task_verdict=None)
            write_json(output / "result.json", result)
            self._save(task_id, result)
            journal.close()
        return result

    async def cancel(self, task_id):
        if task_id in self.jobs:
            self.jobs[task_id].stop.set()
            return
        if task_id in self.tasks and not self.tasks[task_id].done():
            self.tasks[task_id].cancel()
            return
        raise Rejected("unknown_or_finished_task")

    def _launch(self, task_id, task, kwargs):
        self._save(task_id, {"task_id": task_id, "status": "planning"})
        async def work():
            try:
                result = await self.run(task, task_id=task_id, **kwargs)
                self._save(task_id, result)
            except asyncio.CancelledError:
                self._save(task_id, {"task_id": task_id, "status": "canceled", "task_verdict": None})
            except Exception as exc:
                self._save(task_id, {"task_id": task_id, "status": "infra", "reason": type(exc).__name__, "task_verdict": None})
        self.tasks[task_id] = asyncio.create_task(work())
        def finalized(future):
            # A task canceled before its coroutine starts cannot run its own exception handler.
            if future.cancelled():
                self._save(task_id, {"task_id": task_id, "status": "canceled", "task_verdict": None})
        self.tasks[task_id].add_done_callback(finalized)
        return clone(self.task_records[task_id])

    def _admit(self):
        if self.closing or self.runtime.closing:
            raise Rejected("system_closing")
        if sum(not t.done() for t in self.tasks.values()) >= self.max_active_tasks:
            raise Rejected("task_capacity_exceeded")
        if self.runtime.store and len(self.tasks) > 1024:
            for task_id, work in list(self.tasks.items()):
                if work.done():
                    self.tasks.pop(task_id)
                    self.task_records.pop(task_id, None)

    def start(self, task, *, request_id=None, plan=None, deadline_s=300.0):
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
            raise Rejected("invalid_request_identity")
        request = clone({"task": task, "plan": plan, "deadline_s": deadline_s})
        identity = json.dumps(request, sort_keys=True)
        prior = (self.runtime.store.task_request(request_id) if self.runtime.store else
                 self.request_records.get(request_id)) if request_id else None
        if prior:
            if prior[0] != identity:
                raise Rejected("idempotency_conflict")
            return self.status(prior[1]["task_id"])
        self._admit()
        self._validate_request(task, plan, deadline_s)
        task_id = uuid.uuid4().hex
        record = {"task_id": task_id, "status": "planning", "request": request, "attempt": 0}
        if self.runtime.store:
            self.runtime.store.accept_task(task_id, request_id, identity, record)
        if request_id and not self.runtime.store:
            self.request_records[request_id] = identity, record
        return self._launch(task_id, task, {"plan": plan, "deadline_s": deadline_s})

    def resume(self, task_id):
        self._admit()
        store = self.runtime.store
        saved = store.task(task_id) if store else None
        if not saved or saved["status"] not in ("interrupted", "failed", "canceled", "infra", "uncertain"):
            raise Rejected("task_not_resumable")
        completed = store.completed(task_id)
        for execution in store.executions():
            if execution["owner"].startswith(task_id + ":"):
                node_id = execution["owner"][len(task_id) + 1:]
                if not execution["settled"]:
                    raise Rejected("resume_blocked_by_unsettled_device")
                if execution["status"] == "completed" and node_id not in completed:
                    raise Rejected("resume_requires_node_resolution")
        request = saved["request"]
        attempt = saved.get("attempt", 0) + 1
        store.update_task(task_id, {**saved, "attempt": attempt})
        return self._launch(task_id, request["task"], {"plan": request.get("plan"),
            "deadline_s": request["deadline_s"], "_completed": completed, "_attempt": attempt})

    def status(self, task_id):
        if task_id not in self.task_records:
            saved = self.runtime.store.task(task_id) if self.runtime.store else None
            if saved:
                return saved
            raise Rejected("unknown_or_finished_task")
        record = clone(self.task_records[task_id])
        if task_id in self.jobs:
            record["executions"] = [self.runtime.status(e) for e in self.jobs[task_id].execution_ids]
        return record

    async def close(self):
        self.closing = True
        for endpoint in self.endpoints.values():
            endpoint.pool.close()
        pending = [(key, work) for key, work in self.tasks.items() if not work.done()]
        for key, _ in pending:
            await self.cancel(key)
        if pending:
            _, unfinished = await asyncio.wait([work for _, work in pending], timeout=self.runtime.cancel_timeout + 0.2)
            for work in unfinished:
                work.cancel()
            await asyncio.gather(*(work for _, work in pending), return_exceptions=True)
