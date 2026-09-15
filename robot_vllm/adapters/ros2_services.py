"""Native ROS gripper actions and request/response services, configured by operators."""
import asyncio
import math
import threading
import time
import uuid

from ..control import Capability, DriverResult, Rejected
from .ros2 import await_ros


class GripperDriver:
    def __init__(self, session, *, name, action_name, joint_state_topic, joint_name,
                 state_scale=1.0, min_position_m=0.0, max_position_m=0.08, state_max_age_s=2.0):
        from control_msgs.action import GripperCommand
        from rclpy.action import ActionClient
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import JointState
        if not 0 <= min_position_m < max_position_m <= 1 or not 0 < state_scale <= 100 or not 0 < state_max_age_s <= 3600:
            raise ValueError("invalid_gripper_calibration")
        self.node, self.name, self.action_name = session.node, name, action_name
        self.action_type = GripperCommand
        self.client = ActionClient(self.node, GripperCommand, action_name)
        self.joint_name, self.state_scale = joint_name, state_scale
        self.minimum, self.maximum, self.age = min_position_m, max_position_m, state_max_age_s
        self.state, self.lock = None, threading.Lock()
        def receive(message):
            if len(message.name) != len(message.position) or joint_name not in message.name:
                return
            position = message.position[message.name.index(joint_name)] * state_scale
            if math.isfinite(position):
                with self.lock:
                    self.state = time.monotonic(), float(position)
        self.subscription = self.node.create_subscription(JointState, joint_state_topic, receive, qos_profile_sensor_data)

    def capability(self):
        return Capability(self.name + ".gripper", (self.name + "/motion",),
            "Command gripper aperture in meters and max effort in newtons; native result reports contact/goal state.",
            {"type": "object", "properties": {"position_m": {"type": "number", "minimum": self.minimum, "maximum": self.maximum},
             "max_effort_n": {"type": "number", "minimum": 0, "maximum": 100}},
             "required": ["position_m", "max_effort_n"], "additionalProperties": False}, "ros2_gripper", 10,
            {"action_name": self.action_name, "joint_name": self.joint_name, "state_scale": self.state_scale,
             "position_unit": "meters", "effort_unit": "newtons", "action_type": "control_msgs/action/GripperCommand"})

    def validate(self, arguments):
        if not self.minimum <= arguments["position_m"] <= self.maximum:
            raise Rejected("gripper_limit_exceeded")

    async def observe(self):
        with self.lock:
            state = self.state
        if state is None or time.monotonic() - state[0] > self.age:
            raise Rejected("gripper_state_unavailable_or_stale")
        return {"position_m": state[1], "sample_age_s": time.monotonic() - state[0], "joint_name": self.joint_name}

    @staticmethod
    def result(response):
        status = {4: "completed", 5: "canceled", 6: "failed"}.get(response.status)
        if status is None:
            return DriverResult("failed", {"reason": "native_goal_unknown"}, settled=False)
        return DriverResult(status, {"position_m": float(response.result.position),
            "effort_n": float(response.result.effort), "reached_goal": bool(response.result.reached_goal),
            "stalled": bool(response.result.stalled), "settlement_basis": "native_action_terminal_result"})

    async def execute(self, arguments, cancel, feedback):
        from unique_identifier_msgs.msg import UUID
        if not await asyncio.to_thread(self.client.wait_for_server, timeout_sec=1):
            return DriverResult("failed", {"reason": "gripper_server_unavailable", "dispatched": False})
        try:
            await self.observe()
        except Rejected as exc:
            return DriverResult("failed", {"reason": str(exc), "dispatched": False})
        if cancel.is_set():
            return DriverResult("canceled", {"dispatched": False})
        goal = self.action_type.Goal()
        goal.command.position, goal.command.max_effort = float(arguments["position_m"]), float(arguments["max_effort_n"])
        goal_id = uuid.uuid4()
        feedback({"dispatch_locator": {"kind": "ros2_gripper_v1", "goal_id": goal_id.hex, "action_name": self.action_name}})
        handle = await await_ros(self.client.send_goal_async(goal, goal_uuid=UUID(uuid=list(goal_id.bytes))))
        if not handle.accepted:
            return DriverResult("failed", {"reason": "goal_rejected", "dispatched": False})
        result = asyncio.create_task(await_ros(handle.get_result_async()))
        stop = asyncio.create_task(cancel.wait())
        receipt = None
        try:
            done, _ = await asyncio.wait((result, stop), return_when=asyncio.FIRST_COMPLETED)
            if result not in done:
                receipt = asyncio.create_task(await_ros(handle.cancel_goal_async()))
            return self.result(await result)
        finally:
            tasks = [t for t in (stop, receipt) if t is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reconcile(self, locator):
        from unique_identifier_msgs.msg import UUID
        if not locator or locator.get("kind") != "ros2_gripper_v1" or locator.get("action_name") != self.action_name:
            return DriverResult("failed", {"reason": "missing_native_locator"}, settled=False)
        service = self.action_type.Impl.GetResultService
        client = self.node.create_client(service, self.action_name + "/_action/get_result")
        future = None
        try:
            if not await asyncio.to_thread(client.wait_for_service, timeout_sec=1):
                return DriverResult("failed", {"reason": "result_unavailable"}, settled=False)
            request = service.Request(goal_id=UUID(uuid=list(uuid.UUID(hex=locator["goal_id"]).bytes)))
            future = client.call_async(request)
            return self.result(await asyncio.wait_for(await_ros(future), timeout=2))
        except (ValueError, asyncio.TimeoutError):
            return DriverResult("failed", {"reason": "result_unknown"}, settled=False)
        finally:
            if future is not None and not future.done():
                future.cancel()
            self.node.destroy_client(client)


class TriggerServiceDriver:
    def __init__(self, session, *, name, service_name, resources=None):
        from std_srvs.srv import Trigger
        self.name, self.service_name = name, service_name
        self.service_type = Trigger
        self.client = session.node.create_client(Trigger, service_name)
        self.resources = tuple(resources or [name + "/service"])

    def capability(self):
        return Capability(self.name + ".trigger", self.resources,
            "Invoke the configured ROS Trigger service and consume its success/message response.",
            {"type": "object", "properties": {}, "additionalProperties": False}, "ros2_service", 10,
            {"service_name": self.service_name, "service_type": "std_srvs/srv/Trigger"})

    def validate(self, arguments):
        pass

    async def observe(self):
        return {"service_ready": self.client.service_is_ready(), "service_name": self.service_name}

    async def execute(self, arguments, cancel, feedback):
        if not await asyncio.to_thread(self.client.wait_for_service, timeout_sec=1):
            return DriverResult("failed", {"reason": "service_unavailable", "dispatched": False})
        if cancel.is_set():
            return DriverResult("canceled", {"dispatched": False})
        feedback({"dispatch_locator": {"kind": "ros2_trigger_v1", "service_name": self.service_name}})
        result = await await_ros(self.client.call_async(self.service_type.Request()))
        return DriverResult("completed" if result.success else "failed",
                            {"success": bool(result.success), "message": result.message})
