"""N-device coordinated capabilities with group reservation and coupled stopping.

The barrier coordinates dispatch, not physical time synchronization. ROS goal
acceptance is not a distributed transaction; partial execution is reported.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import time

from .control import Capability, DriverResult, Rejected, check_schema, clone


class CoordinatedDriver:
    def __init__(self, name, members, *, shared_resources=()):
        if not members:
            raise Rejected("empty_group")
        resources = set()
        for capability, _ in members.values():
            if resources.intersection(capability.resources):
                raise Rejected("group_members_share_actuator_resources")
            resources.update(capability.resources)
        if any(not isinstance(r, str) or not r for r in shared_resources):
            raise Rejected("invalid_group_resources")
        resources.update(shared_resources)
        self.members = dict(members)
        self._capability = Capability(
            name, tuple(sorted(resources)),
            "Coordinate all members with a dispatch barrier; cancel peers on failure. Not physical clock synchronization.",
            {"type": "object", "properties": {"members": {
                "type": "object", "properties": {n: clone(cap.arguments) for n, (cap, _) in members.items()},
                "required": list(members), "additionalProperties": False}},
             "required": ["members"], "additionalProperties": False},
            "coordinated", max(cap.max_duration_s for cap, _ in members.values()),
            {"members": list(members), "coordination": "dispatch_barrier",
             "failure_policy": "cancel_all_members", "physical_start_synchronization": False})

    def capability(self):
        return self._capability

    async def observe(self):
        names = list(self.members)
        values = await asyncio.gather(*(driver.observe() for _, driver in self.members.values()))
        return {"members": dict(zip(names, values)),
                "synchronized_sensor_capture": False}

    def validate(self, arguments):
        check_schema(arguments, self._capability.arguments)
        # Every member validates before the first controller can be called.
        for name, (capability, driver) in self.members.items():
            value = arguments["members"][name]
            check_schema(value, capability.arguments)
            driver.validate(value)

    async def reconcile(self, locator):
        saved = (locator or {}).get("members", {})
        if not saved:
            return DriverResult("failed", {"reason": "group_dispatch_unknown"}, settled=False)
        results = {}
        for name, (_, driver) in self.members.items():
            member = saved.get(name)
            if member is None:
                result = DriverResult("canceled", {"dispatched": False})
            elif member.get("result", {}).get("settled") is True:
                result = DriverResult(**member["result"])
            elif hasattr(driver, "reconcile"):
                result = await driver.reconcile(member.get("locator"))
            else:
                result = DriverResult("failed", {"reason": "member_result_unknown"}, settled=False)
            results[name] = result
        settled = all(r.settled is True for r in results.values())
        status = "completed" if all(r.status == "completed" for r in results.values()) else "failed"
        return DriverResult(status, {"members": {n: asdict(r) for n, r in results.items()}}, settled=settled)

    async def execute(self, arguments, cancel, feedback):
        self.validate(arguments)
        stop = asyncio.Event()
        barrier = asyncio.Event()
        results, progress, starts, timeouts = {}, {}, {}, set()
        recoveries = {}

        async def relay_cancel():
            await cancel.wait()
            stop.set()

        def publish(name, value):
            if name not in results:
                recovery_changed = value.get("member_started") or "dispatch_locator" in value
                if recovery_changed:
                    recoveries.setdefault(name, {"started": True})
                    if "dispatch_locator" in value:
                        recoveries[name]["locator"] = clone(value["dispatch_locator"])
                # A lost member may never return a terminal result. Stop peers
                # immediately on its liveness signal, retaining all reservations.
                if value.get("coordination_fault"):
                    stop.set()
                progress[name] = clone(value)
                event = {"member": name, "members": clone(progress),
                          "group_stopping": stop.is_set(),
                          "coordination_fault": value.get("coordination_fault")}
                if recovery_changed:
                    event["dispatch_locator"] = {"members": clone(recoveries)}
                feedback(event)

        async def run_member(name, capability, driver):
            await barrier.wait()
            starts[name] = time.monotonic()
            if stop.is_set() or cancel.is_set():
                return name, DriverResult("canceled", {"dispatched": False})

            async def work():
                try:
                    publish(name, {"member_started": True})
                    value = await driver.execute(clone(arguments["members"][name]), stop,
                                                 lambda data: publish(name, data))
                    if (not isinstance(value, DriverResult)
                            or value.status not in ("completed", "canceled", "failed")
                            or type(value.settled) is not bool):
                        return DriverResult("failed", {"reason": "invalid_driver_result"}, settled=False)
                    clone(asdict(value))
                    return value
                except Exception as exc:
                    return DriverResult("failed", {"exception_type": type(exc).__name__}, settled=False)

            task = asyncio.create_task(work())
            done, _ = await asyncio.wait((task,), timeout=capability.max_duration_s)
            if not done:
                timeouts.add(name)
                stop.set()
                publish(name, {"reason": "member_execution_deadline"})
            # Never cancel the controller coroutine merely because its deadline expired.
            return name, await task

        # Check fresh observations for every member before any member dispatch.
        # This narrows, but cannot eliminate, the network race after admission.
        try:
            await self.observe()
        except Rejected as exc:
            return DriverResult("failed", {"reason": str(exc), "dispatched": False})
        except Exception as exc:
            return DriverResult("failed", {"reason": "member_observation_failed",
                "exception_type": type(exc).__name__, "dispatched": False})
        relay = asyncio.create_task(relay_cancel())
        workers = [asyncio.create_task(run_member(name, cap, driver))
                   for name, (cap, driver) in self.members.items()]
        barrier.set()
        try:
            for finished in asyncio.as_completed(workers):
                name, result = await finished
                results[name] = result
                recoveries.setdefault(name, {})["result"] = asdict(result)
                if result.status != "completed" or result.settled is not True:
                    stop.set()
                progress[name] = {"status": result.status, "settled": result.settled}
                feedback({"member": name, "members": clone(progress), "group_stopping": stop.is_set(),
                          "dispatch_locator": {"members": clone(recoveries)}})
            settled = all(result.settled is True for result in results.values())
            if not settled or timeouts or any(r.status == "failed" for r in results.values()):
                status = "failed"
            elif cancel.is_set() or any(r.status == "canceled" for r in results.values()):
                status = "canceled"
            else:
                status = "completed"
            return DriverResult(status, {
                "members": {name: asdict(result) for name, result in results.items()},
                "member_deadlines_exceeded": sorted(timeouts),
                "dispatch_skew_s": max(starts.values()) - min(starts.values()),
                "physical_start_synchronization": False,
                "partial_execution_possible": status != "completed",
            }, settled=settled)
        finally:
            relay.cancel()
            await asyncio.gather(relay, return_exceptions=True)
            # Interrupted coordinator: request peer stop, retaining native tasks for acknowledgement.
            if any(not worker.done() for worker in workers):
                stop.set()
