"""Small real-ROS crash/restart check with two independent MuJoCo controllers.

No physical robot, model provider, training worker, or cross-host service is used.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from robot_vllm.control import DeviceRuntime, Rejected
from robot_vllm.deployment import Deployment


async def worker(root, crash=False):
    rt = DeviceRuntime(root / "runs", state_dir=root / "state")
    deployment = Deployment(rt, json.loads((root / "topology.json").read_text()))
    try:
        await deployment.ready(15)
        if crash:
            obs = await rt.observe("pair")
            op = await rt.submit(owner="crash-test", request_id="once", capability="pair",
                observation_id=obs["observation_id"], arguments={"members": {
                    n: {"points": [{"positions": [0.5, -0.4], "time_from_start_s": 8.0}]}
                    for n in ("left.trajectory", "right.trajectory")}})
            deadline = time.monotonic() + 10
            while True:
                progress = rt.status(op["execution_id"])["progress"].get("members", {})
                if len(progress) == 2 and all("positions_rad" in p for p in progress.values()):
                    (root / "crash-receipt.json").write_text(json.dumps(op))
                    os._exit(73)  # Real process exit without Python cleanup or SQLite close.
                assert time.monotonic() < deadline
                await asyncio.sleep(0.02)
        else:
            receipt = json.loads((root / "crash-receipt.json").read_text())
            assert len(rt.blocked_resources()) == 2
            obs = await rt.observe("pair")
            try:
                await rt.submit(owner="new", request_id="must-not-run", capability="pair",
                    observation_id=obs["observation_id"], arguments={"members": {
                        n: {"points": [{"positions": [0, 0], "time_from_start_s": 1}]}
                        for n in ("left.trajectory", "right.trajectory")}})
                raise AssertionError("restarted runtime released an unresolved resource")
            except Rejected as exc:
                assert str(exc) == "resource_recovery_required"
            recovered = await rt.reconcile(receipt["execution_id"])
            assert recovered["settled"] and recovered["status"] == "failed", recovered
            assert not rt.recovery_pending
            result = {"status": "passed", "checks": ["intent_survives_hard_exit",
                "group_quarantined_after_restart", "no_replay_before_reconciliation",
                "native_uuid_results_recovered", "both_resources_released_after_terminal_results"],
                "recovered": recovered, "task_verdicts": [], "physical_robot": False}
            (root / "result.json").write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2))
    finally:
        await deployment.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("crash", "recover"))
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.worker:
        return asyncio.run(worker(args.root, args.worker == "crash"))
    root = Path("runs/ros-restart") / uuid.uuid4().hex
    root.mkdir(parents=True)
    namespace = "/restart_" + root.name[:8]
    config = {"devices": [{"name": name, "backend": "ros2", "robot_id": name,
        "action_name": namespace + "/" + name + "/follow_joint_trajectory",
        "joint_state_topic": namespace + "/" + name + "/joint_states",
        "joint_names": ["joint_1", "joint_2"], "limits": [[-1, 1], [-1, 1]],
        "state_max_age_s": 1, "lease_topic": namespace + "/" + name + "/runtime_lease"}
        for name in ("left", "right")],
        "groups": [{"name": "pair", "members": ["left.trajectory", "right.trajectory"]}]}
    (root / "topology.json").write_text(json.dumps(config, indent=2))
    processes, logs = [], []
    try:
        for name in ("left", "right"):
            log = (root / (name + ".log")).open("x")
            logs.append(log)
            processes.append(subprocess.Popen([sys.executable, "-m", "robot_vllm.physics_controller",
                "--namespace", namespace + "/" + name, "--host-label", "local-test",
                "--output", str(root / name), "--lease-timeout", "0.7"], stdout=log, stderr=log))
        command = [sys.executable, __file__, "--root", str(root), "--worker"]
        with (root / "coordinator-crash.log").open("x") as log:
            crashed = subprocess.run(command + ["crash"], stdout=log, stderr=log, timeout=30)
        assert crashed.returncode == 73, (crashed.returncode, str(root))
        subprocess.run(command + ["recover"], check=True, timeout=20)
        for name in ("left", "right"):
            events = [json.loads(line) for line in (root / name / "controller-events.jsonl").read_text().splitlines()]
            assert any(e["event"] == "hold_confirmed" and e.get("reason") == "lease_expired" for e in events)
            assert sum(e["event"] == "goal_started" for e in events) == 1
        print("ROS restart evidence:", root)
    finally:
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
