"""One authoritative async coordinator for multiple named robot resources.

Cancellation requests never imply physical stop. Resources remain occupied until
the driver reports settlement; lost acknowledgements quarantine them.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import shutil
import time
from typing import Callable, Protocol
import uuid

from .runtime import Journal, write_json


class Rejected(ValueError):
    """A typed rejection before device execution."""


def clone(value):
    return json.loads(json.dumps(value, allow_nan=False))


def check_schema(value, schema, path="arguments"):
    """Small strict JSON-schema subset shared by tools and pre-dispatch validation."""
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}
    if kind in types and type(value) is not types[kind]:
        raise Rejected(f"invalid_type:{path}")
    if kind == "number" and (type(value) not in (float, int) or not math.isfinite(value)):
        raise Rejected(f"invalid_number:{path}")
    if kind in ("number", "integer"):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise Rejected(f"out_of_range:{path}")
    if "enum" in schema and value not in schema["enum"]:
        raise Rejected(f"invalid_enum:{path}")
    if kind == "object":
        properties = schema.get("properties", {})
        if set(schema.get("required", ())) - value.keys():
            raise Rejected(f"missing_field:{path}")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise Rejected(f"unknown_field:{path}")
        for name, item in value.items():
            if name in properties:
                check_schema(item, properties[name], path + "." + name)
    if kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf):
            raise Rejected(f"invalid_length:{path}")
        for item in value:
            check_schema(item, schema.get("items", {}), path + "[]")


@dataclass(frozen=True)
class Capability:
    name: str
    resources: tuple[str, ...]
    description: str
    arguments: dict
    backend: str
    max_duration_s: float = 30.0
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DriverResult:
    status: str  # completed | canceled | failed
    detail: dict = field(default_factory=dict)
    settled: bool = True


@dataclass(frozen=True)
class DeviceDescription:
    device_id: str
    robot_id: str
    kind: str
    frame_id: str | None = None


class Driver(Protocol):
    async def observe(self) -> dict: ...
    def validate(self, arguments: dict) -> None: ...
    async def execute(self, arguments: dict, cancel: asyncio.Event,
                      feedback: Callable[[dict], None]) -> DriverResult: ...


@dataclass
class Execution:
    execution_id: str
    owner: str
    capability: Capability
    arguments: dict
    timeout_s: float
    dispatch_deadline: float
    status: str = "accepted"
    reason: str = ""
    detail: dict = field(default_factory=dict)
    progress: dict = field(default_factory=dict)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    settled: bool = False
    created_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    task: asyncio.Task | None = None


class DeviceRuntime:
    def __init__(self, output: Path, *, observation_ttl_s=10.0, cancel_timeout_s=2.0, state_dir=None):
        if not 0 < observation_ttl_s <= 3600 or not 0 < cancel_timeout_s <= 60:
            raise ValueError("invalid runtime time limits")
        self.runtime_id = uuid.uuid4().hex
        from .state import StateStore
        self.store = StateStore(state_dir) if state_dir is not None else None
        self.recovery_pending = {e["execution_id"]: e for e in self.store.executions()
                                 if not e["settled"]} if self.store else {}
        self.output = Path(output) / self.runtime_id
        self.output.mkdir(parents=True)
        source = self.output / "runtime-source"
        shutil.copytree(Path(__file__).parent, source,
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.journal = Journal(self.output / "control-events.jsonl")
        self.ttl, self.cancel_timeout = observation_ttl_s, cancel_timeout_s
        self.capabilities: dict[str, tuple[Capability, Driver]] = {}
        self.devices: dict[str, DeviceDescription] = {}
        self.epochs: dict[str, int] = {}
        self.occupied: dict[str, str] = {}
        self.policy_reservations: dict[str, str] = {}
        self.observations: dict[str, dict] = {}
        self.executions: dict[str, Execution] = {}
        self.requests: dict[tuple[str, str], tuple[str, str]] = {}
        self.lock = asyncio.Lock()
        self.closing = False
        self.closed = False
        self.frozen = False
        self.started = time.monotonic()
        write_json(self.output / "manifest.json", {
            "runtime_id": self.runtime_id, "schema": "robot_runtime.v0.3",
            "created_at_unix": time.time(), "observation_ttl_s": self.ttl,
            "cancel_timeout_s": self.cancel_timeout,
            "category": "runtime_control", "runtime_source": "runtime-source",
        })

    def register_device(self, device: DeviceDescription):
        if self.frozen or self.closing:
            raise Rejected("registry_frozen")
        if (not device.device_id or not device.robot_id or not device.kind
                or device.device_id in self.devices):
            raise Rejected("invalid_or_duplicate_device")
        self.devices[device.device_id] = device
        self.journal.emit("device_registered", device=asdict(device))

    def topology(self):
        return {"devices": [clone(asdict(d)) for d in self.devices.values()],
                "groups": [clone(asdict(c)) for c, _ in self.capabilities.values()
                           if c.backend == "coordinated"]}

    def register_group(self, name: str, members: list[str], *, shared_resources=()):
        from .coordination import CoordinatedDriver
        if not members or len(members) != len(set(members)):
            raise Rejected("empty_or_duplicate_group_members")
        if any(member not in self.capabilities for member in members):
            raise Rejected("unknown_group_member")
        driver = CoordinatedDriver(name, {n: self.capabilities[n] for n in members},
                                   shared_resources=shared_resources)
        self.register(driver.capability(), driver)
        return driver.capability()

    def register(self, capability: Capability, driver: Driver):
        if self.frozen or self.closing:
            raise Rejected("registry_frozen")
        if not capability.name or capability.name in self.capabilities:
            raise Rejected("duplicate_or_empty_capability")
        if (not capability.resources or len(set(capability.resources)) != len(capability.resources)
                or any(not isinstance(r, str) or not r for r in capability.resources)):
            raise Rejected("invalid_resources")
        if not 0 < capability.max_duration_s <= 3600:
            raise Rejected("invalid_capability_timeout")
        device_id = capability.metadata.get("device_id")
        if device_id is not None and device_id not in self.devices:
            raise Rejected("unknown_capability_device")
        self.capabilities[capability.name] = capability, driver
        for resource in capability.resources:
            self.epochs.setdefault(resource, 0)
        self.journal.emit("capability_registered", capability=asdict(capability))

    def catalog(self):
        return [clone(asdict(cap)) for cap, _ in self.capabilities.values()]

    async def reserve_policy(self, owner, resources):
        async with self.lock:
            if not isinstance(owner, str) or not owner or not resources or any(r not in self.epochs for r in resources):
                raise Rejected("invalid_policy_reservation")
            if self.closing:
                return False
            if any(r in self.blocked_resources() or r in self.occupied or (r in self.policy_reservations and self.policy_reservations[r] != owner)
                   for r in resources):
                return False
            for resource in resources:
                self.policy_reservations[resource] = owner
                self.epochs[resource] += 1
            self.journal.emit("policy_resources_reserved", owner=owner, resources=list(resources))
            return True

    async def release_policy(self, owner):
        async with self.lock:
            released = []
            for resource, current in list(self.policy_reservations.items()):
                if current == owner:
                    del self.policy_reservations[resource]
                    self.epochs[resource] += 1
                    released.append(resource)
            if released:
                self.journal.emit("policy_resources_released", owner=owner, resources=released)

    def _freeze(self):
        if not self.frozen:
            if self.store:
                self.store.bind_catalog(self.catalog())
            write_json(self.output / "capabilities.json", {"capabilities": self.catalog()})
            write_json(self.output / "topology.json", self.topology())
            self.frozen = True

    def blocked_resources(self):
        return {r for e in self.recovery_pending.values() for r in e["resources"]}

    async def reconcile(self, execution_id):
        """Operator-only, read native results; never replay an action or force release."""
        self._freeze()
        record = self.recovery_pending.get(execution_id)
        if record is None:
            raise Rejected("unknown_recovery_execution")
        if record.get("phase") == "accepted":
            result = DriverResult("failed", {"reason": "interrupted_before_dispatch"})
        else:
            entry = self.capabilities.get(record["capability"])
            reconcile = getattr(entry[1], "reconcile", None) if entry else None
            if reconcile is None:
                return {"execution_id": execution_id, "settled": False, "reason": "driver_reconciliation_unavailable"}
            result = await asyncio.wait_for(reconcile(record.get("dispatch_locator")), timeout=10)
        if (not isinstance(result, DriverResult) or result.settled is not True
                or result.status not in ("completed", "canceled", "failed")):
            return {"execution_id": execution_id, "settled": False, "reason": "native_result_unknown"}
        async with self.lock:
            self.store.update_execution(execution_id, status=result.status, detail=clone(result.detail), settled=True)
            self.recovery_pending.pop(execution_id, None)
            for resource in record["resources"]:
                if resource in self.epochs:
                    self.epochs[resource] += 1
            self.journal.emit("recovered_execution_settled", execution_id=execution_id, result=asdict(result))
        return self.store.execution(execution_id)

    async def observe(self, capability: str):
        async with self.lock:
            if self.closing:
                raise Rejected("runtime_closing")
            self._freeze()
            if capability not in self.capabilities:
                raise Rejected("unknown_capability")
            cap, driver = self.capabilities[capability]
            before = {r: self.epochs[r] for r in cap.resources}
            was_busy = any(r in self.occupied or r in self.blocked_resources() for r in cap.resources)
            captured_at = time.monotonic()
        visible = clone(await asyncio.wait_for(driver.observe(), timeout=5.0))
        async with self.lock:
            if self.closing:
                raise Rejected("runtime_closing")
            stable = not was_busy and not any(r in self.occupied or r in self.blocked_resources() for r in cap.resources)
            stable = stable and all(self.epochs[r] == e for r, e in before.items())
            ticket = uuid.uuid4().hex
            record = {"observation_id": ticket, "runtime_id": self.runtime_id,
                      "capability": capability, "captured_at": captured_at,
                      "epochs": before, "can_dispatch": stable, "data": visible}
            now = time.monotonic()
            self.observations = {k: v for k, v in self.observations.items()
                                 if now - v["captured_at"] <= self.ttl}
            self.observations[ticket] = record
            self.journal.emit("observation", observation=record)
            return clone(record)

    def validate_observation(self, capability, observation_id):
        """Check a ticket on the coordinator loop, including terminal no-op decisions."""
        if self.closing:
            raise Rejected("runtime_closing")
        if capability not in self.capabilities:
            raise Rejected("unknown_capability")
        cap, _ = self.capabilities[capability]
        observation = self.observations.get(observation_id)
        if not observation or observation["capability"] != capability:
            raise Rejected("unknown_or_wrong_observation")
        if time.monotonic() - observation["captured_at"] > self.ttl:
            raise Rejected("expired_observation")
        if any(r in self.occupied for r in cap.resources):
            raise Rejected("resource_busy")
        if not observation["can_dispatch"] or any(
                self.epochs[r] != observation["epochs"][r] for r in cap.resources):
            raise Rejected("stale_control_context")

    async def submit(self, *, owner: str, request_id: str, capability: str,
                     observation_id: str, arguments: dict, timeout_s: float | None = None):
        if not isinstance(owner, str) or not owner or not isinstance(request_id, str) or not request_id:
            raise Rejected("invalid_request_identity")
        try:
            body = clone({"capability": capability, "observation_id": observation_id,
                          "arguments": arguments, "timeout_s": timeout_s})
        except (TypeError, ValueError) as exc:
            raise Rejected("invalid_json") from exc
        identity = json.dumps(body, sort_keys=True)
        async with self.lock:
            persisted = self.store.request(owner, request_id) if self.store else None
            if persisted:
                if persisted[0] != identity:
                    raise Rejected("idempotency_conflict")
                return self.status(persisted[1]["execution_id"])
            prior = self.requests.get((owner, request_id))
            if prior:
                if prior[0] != identity:
                    raise Rejected("idempotency_conflict")
                return self.status(prior[1])
            if self.closing:
                raise Rejected("runtime_closing")
            self._freeze()
            if capability not in self.capabilities:
                raise Rejected("unknown_capability")
            cap, driver = self.capabilities[capability]
            if self.blocked_resources().intersection(cap.resources):
                raise Rejected("resource_recovery_required")
            if any(self.policy_reservations.get(r, owner) != owner for r in cap.resources):
                raise Rejected("resource_reserved_for_policy")
            check_schema(body["arguments"], cap.arguments)
            driver.validate(body["arguments"])
            duration = cap.max_duration_s if timeout_s is None else timeout_s
            if type(duration) not in (int, float) or not 0 < duration <= cap.max_duration_s:
                raise Rejected("invalid_execution_timeout")
            self.validate_observation(capability, observation_id)
            observation = self.observations[observation_id]
            execution_id = uuid.uuid4().hex
            item = Execution(execution_id, owner, cap, body["arguments"], duration,
                             observation["captured_at"] + self.ttl)
            if self.store:
                self.store.accept_execution({"execution_id": execution_id, "owner": owner,
                    "capability": cap.name, "resources": list(cap.resources), "arguments": body["arguments"],
                    "status": "accepted", "settled": False, "phase": "accepted", "detail": {},
                    "progress": {}, "reason": ""}, request_id, identity)
            # All resources are reserved atomically under the coordinator lock.
            for resource in cap.resources:
                self.occupied[resource] = execution_id
                self.epochs[resource] += 1
            self.executions[execution_id] = item
            self.requests[(owner, request_id)] = (identity, execution_id)
            self.journal.emit("accepted", execution_id=execution_id, owner=owner,
                              request_id=request_id, capability=cap.name,
                              observation_id=observation_id, observed_epochs=observation["epochs"],
                              resources=cap.resources, arguments=item.arguments)
            item.task = asyncio.create_task(self._run(item, driver), name="execute-" + execution_id)
            return self.status(execution_id)

    def status(self, execution_id: str):
        if execution_id not in self.executions:
            stored = self.store.execution(execution_id) if self.store else None
            if stored:
                if execution_id in self.recovery_pending:
                    stored.update(status="uncertain", reason="coordinator_restart_requires_reconciliation")
                return stored
            raise Rejected("unknown_execution")
        e = self.executions[execution_id]
        return clone({"execution_id": e.execution_id, "owner": e.owner,
                      "capability": e.capability.name, "resources": e.capability.resources,
                      "status": e.status, "reason": e.reason, "settled": e.settled,
                      "progress": e.progress, "detail": e.detail,
                      "elapsed_s": (e.finished_at or time.monotonic()) - e.created_at})

    async def wait(self, execution_id: str, timeout_s=30.0):
        self.status(execution_id)
        if execution_id not in self.executions:
            return self.status(execution_id)
        if type(timeout_s) not in (int, float) or not 0 < timeout_s <= 60:
            raise Rejected("invalid_wait_timeout")
        event = self.executions[execution_id].changed
        try:
            await asyncio.wait_for(event.wait(), timeout_s)
        except asyncio.TimeoutError:
            pass  # A caller wait timeout does not cancel device execution.
        return self.status(execution_id)

    async def cancel(self, execution_id: str, *, owner: str):
        async with self.lock:
            self.status(execution_id)
            if execution_id not in self.executions:
                raise Rejected("recovered_execution_requires_reconciliation")
            e = self.executions[execution_id]
            if e.owner != owner:
                raise Rejected("not_execution_owner")
            if not e.settled:
                e.reason = e.reason or "cancel_requested"
                e.cancel.set()
                self.journal.emit("cancel_requested", execution_id=execution_id)
            return self.status(execution_id)

    async def _settle(self, e: Execution, result):
        async with self.lock:
            valid = (isinstance(result, DriverResult) and result.settled is True
                     and result.status in ("completed", "canceled", "failed"))
            try:
                detail = clone(result.detail) if isinstance(result, DriverResult) else {}
            except (ValueError, TypeError):
                valid, detail = False, {"reason": "invalid_driver_result"}
            if not valid:
                e.status, e.reason = "uncertain", "driver_settlement_unknown"
                e.detail = detail
                e.changed.set()
                self.journal.emit("uncertain", execution_id=e.execution_id, reason=e.reason)
                return
            e.status, e.detail, e.settled = result.status, detail, True
            if self.store:
                self.store.update_execution(e.execution_id, status=e.status, detail=detail, settled=True)
            e.finished_at = time.monotonic()
            for resource in e.capability.resources:
                if self.occupied.get(resource) == e.execution_id:
                    del self.occupied[resource]
                    self.epochs[resource] += 1
            self.journal.emit("settled", execution=self.status(e.execution_id))
            e.changed.set()

    async def _run(self, e: Execution, driver: Driver):
        e.status = "running"
        def feedback(data):
            # Drivers call on the coordinator loop; ROS adapter marshals thread callbacks.
            if self.closed or e.settled:
                return
            e.progress = clone(data)
            if self.store and "dispatch_locator" in e.progress:
                self.store.update_execution(e.execution_id, dispatch_locator=e.progress["dispatch_locator"])
            if e.progress.get("coordination_fault"):
                e.reason = e.reason or "device_liveness_fault"
                e.cancel.set()
            self.journal.emit("progress", execution_id=e.execution_id, data=e.progress)
        async def execute():
            try:
                if e.cancel.is_set():
                    return DriverResult("canceled", {"dispatched": False})
                if time.monotonic() >= e.dispatch_deadline:
                    return DriverResult("failed", {"dispatched": False, "reason": "dispatch_deadline_expired"})
                if self.store:
                    self.store.update_execution(e.execution_id, phase="dispatching", status="running")
                return await driver.execute(clone(e.arguments), e.cancel, feedback)
            except Exception as exc:
                return DriverResult("failed", {"exception_type": type(exc).__name__}, settled=False)
        worker = asyncio.create_task(execute())
        cancel_waiter = asyncio.create_task(e.cancel.wait())
        try:
            done, _ = await asyncio.wait((worker, cancel_waiter), timeout=e.timeout_s,
                                         return_when=asyncio.FIRST_COMPLETED)
            if worker not in done:
                e.status = "canceling"
                e.reason = e.reason or "execution_deadline"
                e.cancel.set()
                self.journal.emit("canceling", execution_id=e.execution_id, reason=e.reason)
                done, _ = await asyncio.wait((worker,), timeout=self.cancel_timeout)
                if not done:
                    e.status, e.reason = "uncertain", "stop_not_confirmed"
                    self.journal.emit("uncertain", execution_id=e.execution_id, reason=e.reason)
                    e.changed.set()
            # Leave the worker alive after stop timeout; retain the resource reservation.
            await self._settle(e, await worker)
        except asyncio.CancelledError:
            e.cancel.set()
            e.status, e.reason = "uncertain", "coordinator_interrupted"
            if not self.closed:
                self.journal.emit("uncertain", execution_id=e.execution_id, reason=e.reason)
            e.changed.set()
            raise
        finally:
            cancel_waiter.cancel()
            await asyncio.gather(cancel_waiter, return_exceptions=True)

    def health(self):
        return {"runtime_id": self.runtime_id, "durable": self.store is not None,
            "recovery_pending": list(self.recovery_pending), "resources": {
            r: {"epoch": self.epochs[r], "execution_id": self.occupied.get(r),
                "state": "quarantined" if r in self.blocked_resources() else ("quarantined" if self.executions[self.occupied[r]].status == "uncertain"
                          else "busy") if r in self.occupied else "idle"}
            for r in self.epochs}, "active_executions": len(set(self.occupied.values())),
            "policy_reservations": dict(self.policy_reservations),
            "closing": self.closing}

    async def close(self):
        if self.closed:
            return self.health()
        self.closing = True
        for e in self.executions.values():
            if not e.settled:
                e.cancel.set()
        pending = [e.task for e in self.executions.values() if e.task and not e.task.done()]
        if pending:
            await asyncio.wait(pending, timeout=self.cancel_timeout + 0.1)
        # Do not destroy clients or evidence writers while device state is unresolved.
        if self.occupied or self.policy_reservations:
            return self.health()
        self.journal.emit("runtime_closed")
        self.journal.close()
        write_json(self.output / "summary.json", {
            "runtime_id": self.runtime_id, "category": "runtime_control",
            "wall_time_s": time.monotonic() - self.started,
            "executions": [self.status(e) for e in self.executions],
            "task_verdicts": [], "note": "Action completion is not task success.",
        })
        self.closed = True
        if self.store:
            self.store.close()
        return self.health()
