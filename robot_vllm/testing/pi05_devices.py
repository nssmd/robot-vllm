"""Explicit synthetic robot/camera fixtures for the no-hardware Pi 0.5 walkthrough."""
import asyncio
import base64
from dataclasses import replace
import io

from ..adapters.ros2 import JointTrajectoryDriver
from ..adapters.ros2_services import GripperDriver, TriggerServiceDriver
from ..control import DriverResult


def fixture_image(color):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, format="PNG")
    return {"mime_type": "image/png", "encoding": "base64", "data": base64.b64encode(buffer.getvalue()).decode(),
            "source": "synthetic_camera_fixture"}


class ArmFixture(JointTrajectoryDriver):
    def __init__(self, name):
        self.name = name
        self.joints, self.limits = [f"joint_{i+1}" for i in range(7)], [[-2, 2] for _ in range(7)]
        self.action_name = "fixture/" + name
        self.state_topic = "fixture/joint_states"
        self.camera_topics, self.lease_topic, self.lease_publisher = {"front": "fixture/front", "wrist": "fixture/wrist"}, None, None
        self.position = [0.0] * 7

    def capability(self):
        cap = super().capability()
        return replace(cap, backend="fixture", metadata={**cap.metadata, "synthetic": True})

    async def observe(self):
        return {"joint_names": self.joints, "positions_rad": list(self.position), "synthetic": True,
                "images": {"front": fixture_image((70, 110, 180)), "wrist": fixture_image((180, 100, 60))}}

    async def execute(self, arguments, cancel, feedback):
        previous = 0
        for point in arguments["points"]:
            if cancel.is_set():
                return DriverResult("canceled", {"synthetic": True})
            await asyncio.sleep(point["time_from_start_s"] - previous)
            self.position = list(point["positions"])
            previous = point["time_from_start_s"]
            feedback({"positions_rad": self.position, "synthetic": True})
        return DriverResult("completed", {"positions_rad": self.position, "synthetic": True})


class GripperFixture(GripperDriver):
    def __init__(self, name):
        self.name, self.joint_name, self.state_scale = name, "gripper_joint", 1
        self.action_name = "fixture/" + name
        self.minimum, self.maximum = 0, .08
        self.position = .08

    def capability(self):
        cap = super().capability()
        return replace(cap, backend="fixture", metadata={**cap.metadata, "synthetic": True})

    async def observe(self):
        return {"position_m": self.position, "synthetic": True}

    async def execute(self, arguments, cancel, feedback):
        if cancel.is_set():
            return DriverResult("canceled", {"synthetic": True})
        self.position = arguments["position_m"]
        return DriverResult("completed", {"position_m": self.position, "synthetic": True})


class ServiceFixture(TriggerServiceDriver):
    def __init__(self, name):
        self.name, self.service_name, self.resources = name, "fixture/scan", (name + "/service",)

    def capability(self):
        return replace(super().capability(), backend="fixture")

    async def observe(self):
        return {"service_ready": True, "synthetic": True}

    async def execute(self, arguments, cancel, feedback):
        return DriverResult("completed", {"success": True, "message": "Synthetic service consumed; no task verdict."})
