"""ROS 2 FollowJointTrajectory adapter with native result-based settlement.

The standard ROS action contract confirms controller-goal termination. Physical
stop/hold behavior must additionally be commissioned for the actual controller.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
import uuid

from ..control import Capability, DriverResult, Rejected


class Ros2Session:
    """A dedicated rclpy context and executor, never the application's global context."""
    def __init__(self, name=None):
        try:
            import rclpy
            from rclpy.context import Context
            from rclpy.executors import SingleThreadedExecutor
        except ImportError as exc:
            raise RuntimeError("ROS 2 is unavailable; source /opt/ros/jazzy/setup.bash and use /usr/bin/python3") from exc
        self.context = Context()
        rclpy.init(context=self.context)
        self.node = rclpy.create_node(name or "robot_runtime_" + uuid.uuid4().hex[:8], context=self.context)
        # Callbacks only copy sensor data or marshal futures; controllers run elsewhere.
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.stopping = threading.Event()
        def spin():
            while not self.stopping.is_set() and self.context.ok():
                self.executor.spin_once(timeout_sec=0.05)
        self.thread = threading.Thread(target=spin, daemon=True, name="ros2-runtime")
        self.thread.start()

    def close(self):
        self.stopping.set()
        self.executor.wake()
        self.thread.join(timeout=2.0)
        if self.thread.is_alive():
            raise RuntimeError("ROS executor did not stop; context retained")
        self.executor.shutdown(timeout_sec=2.0)
        self.node.destroy_node()
        self.context.try_shutdown()


async def await_ros(future):
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    def complete(source):
        def deliver():
            if result.done():
                return
            try:
                result.set_result(source.result())
            except Exception as exc:
                result.set_exception(exc)
        if not loop.is_closed():
            loop.call_soon_threadsafe(deliver)
    future.add_done_callback(complete)
    return await result


class JointTrajectoryDriver:
    def __init__(self, session: Ros2Session, *, name: str, action_name: str,
                 joint_state_topic: str, joint_names: list[str], limits: list[list[float]],
                 state_max_age_s=2.0, camera_topics=None, lease_topic=None,
                 lease_period_s=0.1):
        from control_msgs.action import FollowJointTrajectory
        from rclpy.action import ActionClient
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import Image, JointState
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        from std_msgs.msg import String
        if (not joint_names or len(joint_names) != len(set(joint_names))
                or len(limits) != len(joint_names)
                or any(len(v) != 2 or not all(math.isfinite(x) for x in v) or v[0] >= v[1]
                       for v in limits)):
            raise ValueError("invalid configured joints or limits")
        if not math.isfinite(state_max_age_s) or state_max_age_s <= 0:
            raise ValueError("invalid state_max_age_s")
        if not math.isfinite(lease_period_s) or lease_period_s <= 0:
            raise ValueError("invalid lease_period_s")
        self.name, self.joints, self.limits = name, list(joint_names), [list(v) for v in limits]
        self.action_name, self.state_topic = action_name, joint_state_topic
        self.node, self.action_type = session.node, FollowJointTrajectory
        self.client = ActionClient(self.node, FollowJointTrajectory, action_name)
        self.state_max_age = state_max_age_s
        self.lease_topic, self.lease_period = lease_topic, lease_period_s
        self.lease_type = String
        self.lease_publisher = self.node.create_publisher(String, lease_topic,
            QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                       durability=DurabilityPolicy.VOLATILE)) if lease_topic else None
        self.state_lock = threading.Lock()
        self.state = None
        self.camera_topics = dict(camera_topics or {})
        self.images = {}
        def receive(message):
            if len(message.name) != len(message.position):
                return
            values = dict(zip(message.name, message.position))
            if all(j in values and math.isfinite(values[j]) for j in self.joints):
                with self.state_lock:
                    self.state = time.monotonic(), [float(values[j]) for j in self.joints]
        self.subscription = self.node.create_subscription(
            JointState, joint_state_topic, receive, qos_profile_sensor_data)
        self.camera_subscriptions = []
        for name, topic in self.camera_topics.items():
            def receive_image(message, camera=name, source=topic):
                from ..sensors import ros_image_png
                try:
                    payload = ros_image_png(message)
                except ValueError:
                    with self.state_lock:
                        self.images.pop(camera, None)
                    return
                with self.state_lock:
                    self.images[camera] = time.monotonic(), {**payload, "source": source}
            self.camera_subscriptions.append(self.node.create_subscription(
                Image, topic, receive_image, qos_profile_sensor_data))

    def capability(self):
        n = len(self.joints)
        return Capability(self.name + ".trajectory", (self.name + "/motion",),
            "Execute a joint trajectory in radians/seconds; completion follows the ROS controller result.",
            {"type": "object", "properties": {"points": {"type": "array", "minItems": 1,
             "maxItems": 100, "items": {"type": "object", "properties": {
                 "positions": {"type": "array", "minItems": n, "maxItems": n,
                               "items": {"type": "number"}},
                 "time_from_start_s": {"type": "number", "minimum": 0.01, "maximum": 30}},
                "required": ["positions", "time_from_start_s"], "additionalProperties": False}}},
             "required": ["points"], "additionalProperties": False}, "ros2", 35.0,
            {"action_name": self.action_name, "joint_state_topic": self.state_topic,
             "joint_names": self.joints, "joint_limits_rad": self.limits,
             "camera_topics": self.camera_topics,
             "lease_topic": self.lease_topic,
             "robot_side_lease_required": self.lease_publisher is not None,
             "action_type": "control_msgs/action/FollowJointTrajectory"})

    def validate(self, arguments):
        previous = 0
        for point in arguments["points"]:
            if point["time_from_start_s"] <= previous:
                raise Rejected("non_increasing_trajectory_time")
            previous = point["time_from_start_s"]
            for position, bounds in zip(point["positions"], self.limits):
                if not bounds[0] <= position <= bounds[1]:
                    raise Rejected("joint_limit_exceeded")

    async def observe(self):
        with self.state_lock:
            state = self.state
            images = dict(self.images)
        if state is None or time.monotonic() - state[0] > self.state_max_age:
            raise Rejected("joint_state_unavailable_or_stale")
        if set(images) != set(self.camera_topics) or any(time.monotonic() - t > self.state_max_age for t, _ in images.values()):
            raise Rejected("camera_observation_unavailable_or_stale")
        return {"joint_names": self.joints, "positions_rad": state[1],
                "sample_age_s": time.monotonic() - state[0], "source": self.state_topic,
                "images": {name: value for name, (_, value) in images.items()}}

    async def execute(self, arguments, cancel, feedback):
        from trajectory_msgs.msg import JointTrajectoryPoint
        from unique_identifier_msgs.msg import UUID
        ready = await asyncio.to_thread(self.client.wait_for_server, timeout_sec=1.0)
        if not ready:
            return DriverResult("failed", {"reason": "action_server_unavailable", "dispatched": False})
        if cancel.is_set():
            return DriverResult("canceled", {"dispatched": False})
        try:
            await self.observe()
        except Rejected as exc:
            return DriverResult("failed", {"reason": str(exc), "dispatched": False})
        goal = self.action_type.Goal()
        goal.trajectory.joint_names = self.joints
        for point in arguments["points"]:
            item = JointTrajectoryPoint()
            item.positions = [float(v) for v in point["positions"]]
            nanos = round(point["time_from_start_s"] * 1_000_000_000)
            item.time_from_start.sec, item.time_from_start.nanosec = divmod(nanos, 1_000_000_000)
            goal.trajectory.points.append(item)
        loop = asyncio.get_running_loop()
        def on_feedback(message):
            sample = message.feedback
            value = {"joint_names": list(sample.joint_names),
                     "positions_rad": list(sample.actual.positions)}
            if not loop.is_closed():
                loop.call_soon_threadsafe(feedback, value)
        goal_id = uuid.uuid4()
        # Persist this through the runtime callback BEFORE calling send_goal.
        feedback({"dispatch_locator": {"kind": "ros2_fjt_v1", "goal_id": goal_id.hex,
                  "action_name": self.action_name}})
        stop = asyncio.Event()
        fault = None

        async def monitor():
            nonlocal fault
            next_heartbeat = 0.0
            while True:
                if cancel.is_set():
                    stop.set()
                if not stop.is_set():
                    try:
                        await self.observe()
                    except Rejected as exc:
                        fault = str(exc)
                        stop.set()
                        feedback({"coordination_fault": fault, "goal_id": goal_id.hex,
                                  "stop_confirmed": False})
                if (not stop.is_set() and self.lease_publisher is not None
                        and time.monotonic() >= next_heartbeat):
                    self.lease_publisher.publish(self.lease_type(data=goal_id.hex))
                    next_heartbeat = time.monotonic() + self.lease_period
                await asyncio.sleep(min(0.1, self.lease_period, self.state_max_age / 4))

        monitor_task = asyncio.create_task(monitor())
        cancel_task = receipt_task = result_task = None
        result_readers = []
        try:
            handle = await await_ros(self.client.send_goal_async(goal, feedback_callback=on_feedback,
                goal_uuid=UUID(uuid=list(goal_id.bytes))))
            if not handle.accepted:
                return DriverResult("failed", {"reason": "goal_rejected", "dispatched": False})
            async def read_terminal():
                result_readers.append(asyncio.create_task(await_ros(handle.get_result_async())))
                queries, next_query = 0, 0.0
                while True:
                    done, _ = await asyncio.wait(result_readers, timeout=0.1,
                                                return_when=asyncio.FIRST_COMPLETED)
                    if done:
                        return next(iter(done)).result()
                    # Native service replies can be lost across a broken TCP
                    # session. Re-query only this accepted UUID after fresh
                    # sensors return; never resend the trajectory itself.
                    if fault and queries < 3 and time.monotonic() >= next_query:
                        try:
                            await self.observe()
                        except Rejected:
                            continue
                        result_readers.append(asyncio.create_task(await_ros(handle.get_result_async())))
                        queries += 1
                        next_query = time.monotonic() + 1.0
                        feedback({"result_requery": queries, "goal_id": goal_id.hex,
                                  "stop_confirmed": False})
            result_task = asyncio.create_task(read_terminal())
            cancel_task = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait((result_task, cancel_task), return_when=asyncio.FIRST_COMPLETED)
            if result_task not in done:
                async def request_stop():
                    receipt = await await_ros(handle.cancel_goal_async())
                    feedback({"cancel_accepted": bool(receipt.goals_canceling), "stop_confirmed": False})
                # A lost cancel receipt must not hide a terminal action result.
                receipt_task = asyncio.create_task(request_stop())
            result = await result_task  # Cancel acknowledgement alone never releases the resource.
            code = int(result.result.error_code)
            status = {4: "completed", 5: "canceled", 6: "failed"}.get(result.status)
            if status is None:
                return DriverResult("failed", {"ros_status": int(result.status)}, settled=False)
            if status == "completed" and (code != 0 or fault):
                status = "failed"
            return DriverResult(status, {"ros_status": int(result.status), "error_code": code,
                "goal_id": goal_id.hex, "liveness_fault": fault,
                "settlement_basis": "native_action_terminal_result"})
        finally:
            tasks = [t for t in (monitor_task, cancel_task, receipt_task, result_task) if t is not None]
            tasks += result_readers
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reconcile(self, locator):
        from unique_identifier_msgs.msg import UUID
        if (not isinstance(locator, dict) or locator.get("kind") != "ros2_fjt_v1"
                or locator.get("action_name") != self.action_name):
            return DriverResult("failed", {"reason": "missing_or_wrong_native_locator"}, settled=False)
        try:
            goal_id = uuid.UUID(hex=locator["goal_id"])
        except (KeyError, ValueError, TypeError):
            return DriverResult("failed", {"reason": "invalid_native_goal_id"}, settled=False)
        service = self.action_type.Impl.GetResultService
        client = self.node.create_client(service, self.action_name + "/_action/get_result")
        future = None
        try:
            ready = await asyncio.to_thread(client.wait_for_service, timeout_sec=1.0)
            if not ready:
                return DriverResult("failed", {"reason": "result_service_unavailable"}, settled=False)
            request = service.Request(goal_id=UUID(uuid=list(goal_id.bytes)))
            future = client.call_async(request)
            response = await asyncio.wait_for(await_ros(future), timeout=2.0)
            status = {4: "completed", 5: "canceled", 6: "failed"}.get(response.status)
            if status is None:
                return DriverResult("failed", {"reason": "native_goal_unknown"}, settled=False)
            code = int(response.result.error_code)
            if status == "completed" and code != 0:
                status = "failed"
            return DriverResult(status, {"goal_id": goal_id.hex, "ros_status": int(response.status),
                "error_code": code, "settlement_basis": "native_result_after_restart"})
        except (asyncio.TimeoutError, RuntimeError):
            return DriverResult("failed", {"reason": "native_result_unavailable"}, settled=False)
        finally:
            if future is not None and not future.done():
                future.cancel()
            self.node.destroy_client(client)
