"""Operator-owned device topology; models select capabilities, never drivers."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import importlib
import time

from .adapters.mock import MockArm
from .control import DeviceDescription


class Deployment:
    def __init__(self, runtime, config):
        from .config import validate_config
        validate_config(config)
        self.runtime, self.session, self.drivers = runtime, None, []
        devices = config.get("devices", [])
        if not isinstance(devices, list) or not devices:
            raise ValueError("at least one device is required")
        try:
            for raw in devices:
                device = dict(raw)
                backend = device.pop("backend", "ros2")
                robot_id = device.pop("robot_id", "robot")
                kind = device.pop("kind", "arm")
                frame_id = device.pop("frame_id", None)
                name = device["name"]
                if backend == "mock":
                    driver = MockArm(**device)
                elif backend in ("ros2", "ros2_gripper", "ros2_service"):
                    from .adapters.ros2 import JointTrajectoryDriver, Ros2Session
                    if self.session is None:
                        self.session = Ros2Session()
                    if backend == "ros2":
                        driver = JointTrajectoryDriver(self.session, **device)
                    else:
                        from .adapters.ros2_services import GripperDriver, TriggerServiceDriver
                        factory = GripperDriver if backend == "ros2_gripper" else TriggerServiceDriver
                        driver = factory(self.session, **device)
                elif backend == "plugin":
                    # This configuration is supplied by the operator, not a model response.
                    module, factory = device.pop("factory").split(":", 1)
                    options = device.pop("options", {})
                    if "name" in options:
                        raise ValueError("plugin options cannot override device identity")
                    driver = getattr(importlib.import_module(module), factory)(**device, **options)
                else:
                    raise ValueError("unknown device backend")
                runtime.register_device(DeviceDescription(name, robot_id, kind, frame_id))
                cap = driver.capability()
                runtime.register(replace(cap, metadata={**cap.metadata, "device_id": name}), driver)
                self.drivers.append(driver)
            for group in config.get("groups", []):
                runtime.register_group(group["name"], group["members"],
                                       shared_resources=group.get("shared_resources", ()))
        except Exception:
            if self.session is not None:
                self.session.close()
            raise

    async def ready(self, timeout_s=10.0):
        deadline = time.monotonic() + timeout_s
        pending = list(self.drivers)
        while pending and time.monotonic() < deadline:
            next_pending = []
            for driver in pending:
                try:
                    await asyncio.wait_for(driver.observe(), timeout=1.0)
                except Exception:
                    next_pending.append(driver)
            pending = next_pending
            if pending:
                await asyncio.sleep(0.05)
        if pending:
            raise RuntimeError("device_observations_not_ready")

    async def close(self):
        health = await self.runtime.close()
        if not health["active_executions"] and self.session is not None:
            self.session.close()
            self.session = None
        return health


def mock_topology(arms=2):
    if type(arms) is not int or not 1 <= arms <= 32:
        raise ValueError("arms must be 1..32")
    names = [f"robot.arm_{i+1}" for i in range(arms)]
    return {"devices": [{"name": n, "backend": "mock", "robot_id": "robot", "kind": "arm"}
                        for n in names],
            "groups": [{"name": "robot.coordinated", "members": [n + ".move" for n in names]}]}
