"""Deterministic async device fixture, never a simulator or trained-policy result."""

import asyncio

from ..control import Capability, DriverResult


class MockArm:
    def __init__(self, name: str, *, tick_s=0.01, stop_delay_s=0.0):
        self.name, self.tick_s, self.stop_delay_s = name, tick_s, stop_delay_s
        self.position = 0.0
        self.started = 0
        self.steps = 0

    def capability(self):
        return Capability(self.name + ".move", (self.name + "/motion",),
            "Move a synthetic 1D test arm. Contract fixture, not physical hardware.",
            {"type": "object", "properties": {
                "target": {"type": "number", "minimum": -1, "maximum": 1},
                "steps": {"type": "integer", "minimum": 1, "maximum": 100}},
             "required": ["target", "steps"], "additionalProperties": False}, "mock", 10.0)

    def validate(self, arguments):
        pass

    async def observe(self):
        return {"device": self.name, "position": self.position, "fixture": True}

    async def execute(self, arguments, cancel, feedback):
        self.started += 1
        origin = self.position
        for step in range(arguments["steps"]):
            if cancel.is_set():
                await asyncio.sleep(self.stop_delay_s)
                return DriverResult("canceled", {"stopped": True})
            await asyncio.sleep(self.tick_s)
            self.position = origin + (arguments["target"] - origin) * (step + 1) / arguments["steps"]
            self.steps += 1
            feedback({"completed_steps": step + 1, "position": self.position})
        return DriverResult("completed", {"position": self.position})
