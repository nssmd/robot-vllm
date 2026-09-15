"""ROS controller backed by MuJoCo dynamics, for cross-host integration checks.

This is a two-joint simulated plant, not a physical robot or manipulation benchmark.
JointState.position is read from MjData.qpos; it is never the interpolated target.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import threading
import time

from .lease import GoalLease


MODEL_XML = """<mujoco model="runtime_two_joint_plant">
  <compiler angle="radian"/>
  <option timestep="0.005" integrator="implicitfast"/>
  <default>
    <joint damping="0.6" armature="0.03" limited="true" range="-1 1"/>
    <geom type="capsule" size="0.025" density="500" rgba="0.2 0.4 0.6 1"/>
  </default>
  <worldbody>
    <body name="base" pos="0 0 0.2">
      <site name="base_site" pos="0 0 0"/>
      <body name="link_1">
        <joint name="joint_1" type="hinge" axis="0 0 1"/>
        <geom fromto="0 0 0 0.25 0 0"/>
        <body name="link_2" pos="0.25 0 0">
          <site name="elbow" pos="0 0 0"/>
          <joint name="joint_2" type="hinge" axis="0 0 1"/>
          <geom fromto="0 0 0 0.2 0 0" rgba="0.7 0.4 0.1 1"/>
          <site name="tip" pos="0.2 0 0"/>
        </body>
      </body>
    </body>
  </worldbody>
  <actuator>
    <position name="servo_1" joint="joint_1" kp="80" kv="8" ctrlrange="-1 1"/>
    <position name="servo_2" joint="joint_2" kp="80" kv="8" ctrlrange="-1 1"/>
  </actuator>
</mujoco>"""


def schematic(sites, size=64):
    pixels = bytearray([240, 240, 240]) * (size * size)
    points = [(int(size * (0.12 + p[0] * 1.65)), int(size * (0.5 - p[1] * 1.65))) for p in sites]
    for a, b in zip(points, points[1:]):
        length = max(abs(b[0] - a[0]), abs(b[1] - a[1]), 1)
        for i in range(length + 1):
            x = round(a[0] + (b[0] - a[0]) * i / length)
            y = round(a[1] + (b[1] - a[1]) * i / length)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if 0 <= x + dx < size and 0 <= y + dy < size:
                        j = ((y + dy) * size + x + dx) * 3
                        pixels[j:j + 3] = bytes([25, 80, 140])
    return bytes(pixels)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--host-label", required=True)
    parser.add_argument("--lease-timeout", type=float, default=0,
                        help="Optional goal-UUID heartbeat timeout; 0 disables this extension")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    import mujoco
    import numpy as np
    import rclpy
    from control_msgs.action import FollowJointTrajectory
    from rclpy.action import ActionServer, CancelResponse, GoalResponse
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import MultiThreadedExecutor
    from sensor_msgs.msg import Image, JointState
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    from std_msgs.msg import String

    model = mujoco.MjModel.from_xml_string(MODEL_XML)
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    trace = (args.output / "physics-trace.jsonl").open("x")
    events = (args.output / "controller-events.jsonl").open("x")
    event_lock = threading.Lock()
    def event(kind, **detail):
        with event_lock:
            events.write(json.dumps({"event": kind, "unix_time": time.time(),
                "monotonic": time.monotonic(), **detail}) + "\n")
            events.flush()
    (args.output / "plant.xml").write_text(MODEL_XML)
    rclpy.init()
    node = rclpy.create_node("physics_controller", namespace=args.namespace)
    group = ReentrantCallbackGroup()
    lock = threading.Lock()
    stopping = threading.Event()
    busy = False
    lease = GoalLease(args.lease_timeout) if args.lease_timeout else None
    samples = 0
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    states = node.create_publisher(JointState, "joint_states", 10)
    images = node.create_publisher(Image, "camera/image_raw", 10)
    def heartbeat(message):
        if lease is not None:
            with lock:
                lease.renew(message.data, time.monotonic())
    lease_subscription = node.create_subscription(String, "runtime_lease", heartbeat,
        QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                   durability=DurabilityPolicy.VOLATILE), callback_group=group) if lease else None

    def tick():
        nonlocal samples
        with lock:
            for _ in range(4):
                mujoco.mj_step(model, data)
            qpos, qvel, ctrl = data.qpos.copy(), data.qvel.copy(), data.ctrl.copy()
            sites = data.site_xpos.copy()
            samples += 1
            trace.write(json.dumps({"sequence": samples, "unix_time": time.time(),
                "sim_time": float(data.time), "qpos": qpos.tolist(), "qvel": qvel.tolist(),
                "ctrl": ctrl.tolist(), "host": args.host_label}) + "\n")
            trace.flush()
        message = JointState()
        message.header.stamp = node.get_clock().now().to_msg()
        message.header.frame_id = args.namespace + "/base"
        message.name = ["joint_1", "joint_2"]
        message.position, message.velocity = qpos.tolist(), qvel.tolist()
        states.publish(message)
        image = Image()
        image.header = message.header
        image.width, image.height, image.step, image.encoding = 64, 64, 192, "rgb8"
        image.data = schematic(sites)
        images.publish(image)

    timer = node.create_timer(0.02, tick, callback_group=group)

    def accept(goal):
        nonlocal busy
        points = goal.trajectory.points
        if (goal.trajectory.joint_names != ["joint_1", "joint_2"] or not points
                or any(len(p.positions) != 2 or not all(np.isfinite(v) and -1 <= v <= 1 for v in p.positions) for p in points)):
            return GoalResponse.REJECT
        with lock:
            if busy:
                event("goal_rejected", reason="controller_busy")
                return GoalResponse.REJECT
            busy = True
        return GoalResponse.ACCEPT

    def execute(handle):
        nonlocal busy
        result = FollowJointTrajectory.Result()
        start = time.monotonic()
        goal_id = bytes(handle.goal_id.uuid).hex()
        event("goal_started", goal_id=goal_id)
        def hold(reason):
            with lock:
                data.ctrl[:] = data.qpos
            event("hold_requested", goal_id=goal_id, reason=reason)
            until = time.monotonic() + 2.0
            while time.monotonic() < until and not stopping.is_set():
                with lock:
                    velocity = data.qvel.copy()
                    position = data.qpos.copy()
                if time.monotonic() - start >= 0.1 and np.max(np.abs(velocity)) < 0.03:
                    event("hold_confirmed", goal_id=goal_id, reason=reason,
                          qpos=position.tolist(), qvel=velocity.tolist())
                    return True
                time.sleep(0.02)
            event("hold_unconfirmed", goal_id=goal_id, reason=reason)
            return False
        try:
            with lock:
                origin = data.qpos.copy()
                if lease:
                    lease.start(goal_id, start)
            previous = 0.0
            for point in handle.request.trajectory.points:
                end = point.time_from_start.sec + point.time_from_start.nanosec / 1e9
                if end <= previous:
                    handle.abort()
                    result.error_code = -1
                    return result
                target = np.asarray(point.positions, dtype=float)
                while not stopping.is_set():
                    with lock:
                        expired = lease is not None and lease.expired(time.monotonic())
                    if handle.is_cancel_requested or expired:
                        reason = "lease_expired" if expired else "cancel_requested"
                        confirmed = hold(reason)
                        if handle.is_cancel_requested and confirmed:
                            handle.canceled()
                            result.error_code = 0
                        else:
                            handle.abort()
                            result.error_code = -4
                        result.error_string = reason
                        event("goal_terminated", goal_id=goal_id, reason=reason,
                              hold_confirmed=confirmed)
                        return result
                    fraction = min(1.0, max(0.0, (time.monotonic() - start - previous) / (end - previous)))
                    with lock:
                        data.ctrl[:] = origin + (target - origin) * fraction
                        measured = data.qpos.copy()
                    feedback = FollowJointTrajectory.Feedback()
                    feedback.joint_names = ["joint_1", "joint_2"]
                    feedback.actual.positions = measured.tolist()
                    handle.publish_feedback(feedback)
                    if fraction >= 1 and np.max(np.abs(measured - target)) < 0.025:
                        break
                    if time.monotonic() - start > end + 3:
                        hold("tracking_timeout")
                        handle.abort()
                        result.error_code = -4
                        return result
                    time.sleep(0.02)
                if stopping.is_set():
                    handle.abort()
                    result.error_code = -1
                    return result
                origin, previous = target, end
            handle.succeed()
            event("goal_terminated", goal_id=goal_id, reason="completed")
            result.error_code = 0
            return result
        finally:
            with lock:
                busy = False
                if lease:
                    lease.clear()

    action = ActionServer(node, FollowJointTrajectory, "follow_joint_trajectory",
        execute_callback=execute, goal_callback=accept, cancel_callback=lambda _: CancelResponse.ACCEPT,
        callback_group=group)
    executor = MultiThreadedExecutor(num_threads=3)
    executor.add_node(node)
    (args.output / "ready.json").write_text(json.dumps({"pid": os.getpid(), "namespace": args.namespace,
        "host_label": args.host_label, "process_hostname": socket.gethostname(),
        "state_source": "mujoco.MjData.qpos", "mujoco_version": mujoco.__version__,
        "physical_robot": False, "rmw": os.environ.get("RMW_IMPLEMENTATION")}, indent=2))
    event("controller_ready", lease_timeout_s=args.lease_timeout)
    try:
        while not stopping.is_set():
            executor.spin_once(timeout_sec=0.05)
    finally:
        stopping.set()
        executor.shutdown(timeout_sec=2)
        action.destroy()
        node.destroy_timer(timer)
        if lease_subscription is not None:
            node.destroy_subscription(lease_subscription)
        node.destroy_node()
        rclpy.try_shutdown()
        trace.close()
        events.close()


if __name__ == "__main__":
    main()
