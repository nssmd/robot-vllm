"""Real DDS/ROS Action integration against explicit synthetic controller nodes.

No robot hardware, simulator success predicate, or model inference is involved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .control import DeviceRuntime, Rejected
from .runtime import write_json


def controller_main(namespace: str, ready: Path, stop_delay_s: float):
    import rclpy
    from control_msgs.action import FollowJointTrajectory
    from rclpy.action import ActionServer, CancelResponse, GoalResponse
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import MultiThreadedExecutor
    from sensor_msgs.msg import Image, JointState
    rclpy.init()
    node = rclpy.create_node("fixture_controller", namespace=namespace)
    group = ReentrantCallbackGroup()
    lock = threading.Lock()
    state = [0.0, 0.0]
    joints = ["joint_1", "joint_2"]
    publisher = node.create_publisher(JointState, "joint_states", 10)
    camera = node.create_publisher(Image, "camera/image_raw", 10)
    def publish():
        message = JointState()
        message.header.stamp = node.get_clock().now().to_msg()
        message.name = joints
        with lock:
            message.position = list(state)
        publisher.publish(message)
        image = Image()
        image.header.stamp = message.header.stamp
        image.header.frame_id = namespace + "/synthetic_camera"
        image.width, image.height, image.step, image.encoding = 16, 16, 48, "rgb8"
        intensity = max(0, min(255, int(128 + message.position[0] * 100)))
        image.data = bytes([intensity, 40, 100]) * (16 * 16)
        camera.publish(image)
    timer = node.create_timer(0.02, publish, callback_group=group)
    def execute(handle):
        start = time.monotonic()
        with lock:
            origin = list(state)
        previous_time = 0.0
        for target in handle.request.trajectory.points:
            end = target.time_from_start.sec + target.time_from_start.nanosec / 1e9
            while True:
                if handle.is_cancel_requested:
                    # Explicit delay separates cancellation acceptance from termination.
                    time.sleep(stop_delay_s)
                    handle.canceled()
                    result = FollowJointTrajectory.Result()
                    result.error_code = 0
                    return result
                elapsed = time.monotonic() - start
                fraction = min(1.0, max(0.0, (elapsed - previous_time) / (end - previous_time)))
                with lock:
                    state[:] = [a + (b - a) * fraction for a, b in zip(origin, target.positions)]
                    current = list(state)
                feedback = FollowJointTrajectory.Feedback()
                feedback.joint_names = joints
                feedback.actual.positions = current
                handle.publish_feedback(feedback)
                if fraction >= 1:
                    break
                time.sleep(0.01)
            origin, previous_time = list(target.positions), end
        handle.succeed()
        result = FollowJointTrajectory.Result()
        result.error_code = 0
        return result
    action = ActionServer(node, FollowJointTrajectory, "follow_joint_trajectory",
                          execute_callback=execute, callback_group=group,
                          goal_callback=lambda _: GoalResponse.ACCEPT,
                          cancel_callback=lambda _: CancelResponse.ACCEPT)
    executor = MultiThreadedExecutor(num_threads=3)
    executor.add_node(node)
    write_json(ready, {"pid": os.getpid(), "namespace": namespace,
                       "backend": "real_ros2_synthetic_controller"})
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown(timeout_sec=2)
        action.destroy()
        node.destroy_timer(timer)
        node.destroy_node()
        rclpy.try_shutdown()


async def until(predicate, *, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise TimeoutError("condition not reached within bounded validation window")


async def validate_ros(output: Path):
    from .adapters.ros2 import JointTrajectoryDriver, Ros2Session
    rt = DeviceRuntime(output, observation_ttl_s=10.0, cancel_timeout_s=0.1)
    processes, log_files, session = [], [], None
    checks = []
    config = {"ros_distro": os.environ.get("ROS_DISTRO"),
              "ros_domain_id": os.environ.get("ROS_DOMAIN_ID"),
              "discovery_range": os.environ.get("ROS_AUTOMATIC_DISCOVERY_RANGE"),
              "python": sys.version, "fixture": True}
    write_json(rt.output / "ros-environment.json", config)
    try:
        prefix = "runtime_validation_" + rt.runtime_id[:8]
        for name in ("arm_a", "arm_b"):
            namespace = "/" + prefix + "/" + name
            log = (rt.output / f"{name}.log").open("x")
            log_files.append(log)
            processes.append(subprocess.Popen([
                sys.executable, "-m", "robot_vllm.ros_validation", "controller",
                "--namespace", namespace, "--ready", str(rt.output / f"{name}.ready.json"),
                "--stop-delay", "0.3"], stdout=log, stderr=subprocess.STDOUT))
        await until(lambda: all((rt.output / f"{n}.ready.json").exists() for n in ("arm_a", "arm_b")))
        session = Ros2Session()
        drivers = []
        for name in ("arm_a", "arm_b"):
            namespace = "/" + prefix + "/" + name
            driver = JointTrajectoryDriver(session, name=name,
                action_name=namespace + "/follow_joint_trajectory", joint_state_topic=namespace + "/joint_states",
                joint_names=["joint_1", "joint_2"], limits=[[-1.0, 1.0], [-1.0, 1.0]])
            drivers.append(driver)
            rt.register(driver.capability(), driver)
        await until(lambda: all(d.state is not None for d in drivers))

        async def command(name, request, position, duration, owner="astra", observation=None):
            obs = observation or await rt.observe(name + ".trajectory")
            args = {"points": [{"positions": [position, -position], "time_from_start_s": duration}]}
            return await rt.submit(owner=owner, request_id=request, capability=name + ".trajectory",
                                   observation_id=obs["observation_id"], arguments=args)

        obs_a, obs_b = await asyncio.gather(rt.observe("arm_a.trajectory"), rt.observe("arm_b.trajectory"))
        a = await command("arm_a", "first-a", 0.2, 0.5, observation=obs_a)
        b = await command("arm_b", "first-b", -0.2, 0.5, observation=obs_b)
        assert rt.health()["active_executions"] == 2
        duplicate = await command("arm_a", "first-a", 0.2, 0.5, observation=obs_a)
        assert duplicate["execution_id"] == a["execution_id"]
        checks.append("idempotent_duplicate_no_second_dispatch")
        first_results = await asyncio.gather(rt.wait(a["execution_id"]), rt.wait(b["execution_id"]))
        assert all(r["status"] == "completed" and r["settled"] for r in first_results), first_results
        # Feedback from both action servers arrived while both executions existed.
        assert all(r["progress"].get("positions_rad") for r in first_results)
        checks.append("two_ros_action_servers_execute_with_feedback")
        try:
            await command("arm_a", "late-old-context", 0.1, 0.2, observation=obs_a)
            raise AssertionError("old control context was accepted")
        except Rejected as exc:
            assert str(exc) == "stale_control_context"
        checks.append("stale_context_rejected_after_execution")
        try:
            await command("arm_a", "outside-limits", 9.0, 0.2)
            raise AssertionError("joint limit violation was accepted")
        except Rejected as exc:
            assert str(exc) == "joint_limit_exceeded"
        checks.append("joint_limits_checked_before_ros_dispatch")

        moving = await command("arm_a", "cancel-me", 0.8, 2.0)
        await until(lambda: rt.status(moving["execution_id"])["progress"].get("positions_rad"))
        await rt.cancel(moving["execution_id"], owner="astra")
        uncertain = await rt.wait(moving["execution_id"])
        assert uncertain["status"] == "uncertain" and not uncertain["settled"], uncertain
        assert rt.health()["resources"]["arm_a/motion"]["state"] == "quarantined"
        try:
            await command("arm_a", "premature-handoff", 0.1, 0.1, owner="vla")
            raise AssertionError("device was released before cancellation result")
        except Rejected as exc:
            assert str(exc) == "resource_busy"
        checks.append("cancel_ack_and_stop_timeout_do_not_release_device")
        await until(lambda: rt.status(moving["execution_id"])["settled"])
        assert rt.status(moving["execution_id"])["status"] == "canceled"
        checks.append("late_native_canceled_result_releases_quarantine")
        handoff = await command("arm_a", "vla-handoff", 0.1, 0.2, owner="vla")
        assert (await rt.wait(handoff["execution_id"]))["status"] == "completed"
        checks.append("new_owner_executes_after_confirmed_settlement")
        await rt.close()
        report = {"status": "passed", "checks": checks, "run_dir": str(rt.output.resolve()),
                  "backend": "real_ros2_synthetic_controllers", "controller_processes": len(processes),
                  "execution_counts": {s: sum(e.status == s for e in rt.executions.values())
                                       for s in ("completed", "canceled", "failed", "uncertain")},
                  "active_workers_at_completion": 0, "task_verdicts": [],
                  "note": "Real ROS 2 transport/actions; synthetic devices; no physical robot or model evaluation."}
        write_json(rt.output / "ros-validation.json", report)
        return report
    except Exception as exc:
        import traceback
        write_json(rt.output / "ros-validation-failure.json", {
            "exception": type(exc).__name__, "message": str(exc),
            "checks_completed": checks, "traceback": traceback.format_exc()})
        raise
    finally:
        await rt.close()
        if session is not None:
            session.close()
        # These are our own synthetic controller processes; no other ROS nodes are touched.
        for process in processes:
            if process.poll() is None:
                process.send_signal(2)
        for process in processes:
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                await asyncio.to_thread(process.wait, timeout=5)
        for log in log_files:
            log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    controller = sub.add_parser("controller")
    controller.add_argument("--namespace", required=True)
    controller.add_argument("--ready", required=True, type=Path)
    controller.add_argument("--stop-delay", type=float, default=0.3)
    validation = sub.add_parser("validate")
    validation.add_argument("--output", type=Path, default=Path("runs/ros2-validation"))
    args = parser.parse_args()
    if args.command == "controller":
        controller_main(args.namespace, args.ready, args.stop_delay)
    else:
        print(json.dumps(asyncio.run(validate_ros(args.output)), indent=2))


if __name__ == "__main__":
    main()
