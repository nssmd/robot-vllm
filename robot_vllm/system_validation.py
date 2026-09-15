"""Full GP6-API -> DAG -> VLA-API/code -> coordinated Runtime -> real ROS chain.

GP6/VLA endpoints and device motion are synthetic; ROS communication is real.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from .control import DeviceRuntime
from .deployment import Deployment
from .platform import RobotSystem
from .ros_validation import until
from .runtime import write_json


async def validate(output, *, arms=2, api="openai_responses"):
    runtime = DeviceRuntime(output, observation_ttl_s=30)
    processes, logs, deployment, system = [], [], None, None
    try:
        devices = []
        for i in range(arms):
            name = f"robot_{i+1}.arm"
            namespace = f"/system_{runtime.runtime_id[:8]}/robot_{i+1}/arm"
            log = (runtime.output / f"controller-{i}.log").open("x")
            logs.append(log)
            processes.append(subprocess.Popen([sys.executable, "-m", "robot_vllm.ros_validation", "controller",
                "--namespace", namespace, "--ready", str(runtime.output / f"controller-{i}.ready.json")],
                stdout=log, stderr=log))
            devices.append({"name": name, "backend": "ros2", "robot_id": f"robot_{i+1}", "kind": "arm",
                "action_name": namespace + "/follow_joint_trajectory", "joint_state_topic": namespace + "/joint_states",
                "joint_names": ["joint_1", "joint_2"], "limits": [[-1.0, 1.0], [-1.0, 1.0]],
                "camera_topics": {"front": namespace + "/camera/image_raw"}, "state_max_age_s": 10.0})
        model_log = (runtime.output / "model-fixture.log").open("x")
        logs.append(model_log)
        processes.append(subprocess.Popen([sys.executable, "-m", "robot_vllm.testing.model_server",
            "--ready", str(runtime.output / "models.ready.json")], stdout=model_log, stderr=model_log))
        await until(lambda: all((runtime.output / f"controller-{i}.ready.json").exists() for i in range(arms))
                    and (runtime.output / "models.ready.json").exists())
        base = json.loads((runtime.output / "models.ready.json").read_text())["base_url"]
        config = {"devices": devices, "groups": [{"name": "cell.coordinated",
                  "members": [device["name"] + ".trajectory" for device in devices]}],
                  "models": {"gp6": {"kind": api, "model": "synthetic-gp6",
                      "key_env": "ROBOT_RUNTIME_UNUSED_FIXTURE_KEY",
                      "endpoint": base + ("/responses" if api == "openai_responses" else "/gp6")},
                      "vla": {"kind": "vla_json", "model": "synthetic-vla", "endpoint": base + "/vla",
                              "key_env": "ROBOT_RUNTIME_UNUSED_FIXTURE_KEY"}}}
        write_json(runtime.output / "validation-config.json", config)
        write_json(runtime.output / "validation-environment.json", {
            "ros_distro": os.environ.get("ROS_DISTRO"), "ros_domain_id": os.environ.get("ROS_DOMAIN_ID"),
            "discovery_range": os.environ.get("ROS_AUTOMATIC_DISCOVERY_RANGE"), "same_host_processes": True,
            "model_backend": "synthetic_http_fixtures", "device_backend": "synthetic_ros_controllers"})
        deployment = Deployment(runtime, config)
        await deployment.ready(timeout_s=15)
        system = RobotSystem(runtime, config["models"], max_parallel=max(2, arms), max_replans=0)
        result = await system.run("Prepare each robot, coordinate the arms, then let GP6 finish the motion.")
        assert result["status"] == "completed", result
        assert result["peak_parallel_nodes"] >= 2
        assert result["model_metrics"]["model_calls"] == 4  # upper GP6, two VLA rounds, lower GP6
        assert result["model_metrics"]["unmetered_call_count"] == 2
        assert result["nodes"]["prepare_1"]["rounds"] == 2
        assert result["nodes"]["coordinate"]["feedback"]["detail"]["physical_start_synchronization"] is False
        assert len(runtime.topology()["devices"]) == arms
        assert not runtime.health()["active_executions"]
        report = {"status": "passed", "runtime_id": runtime.runtime_id,
                  "run_dir": str(runtime.output.resolve()), "task_run_dir": result["run_dir"],
                  "dag_nodes": len(result["nodes"]), "peak_parallel_nodes": result["peak_parallel_nodes"],
                  "fixture_model_calls": result["model_metrics"]["model_calls"],
                  "ros_controller_processes": arms, "received_camera_streams": arms,
                  "runtime_execution_counts": {state: sum(e.status == state for e in runtime.executions.values())
                                                  for state in ("completed", "canceled", "failed", "uncertain")},
                  "task_verdicts": [], "actual_gp6_vla_inference_calls": 0,
                  "checks": ["gp6_generated_dag_api", "code_and_vla_parallel_branches", "vla_fresh_observation_rounds",
                             "dependency_join", "coordinated_multi_robot_capability", "gp6_lower_node_api",
                             "ros_joint_state_and_camera_transport", "all_executions_settled"],
                  "note": "Integration test only; model responses and robot motion are synthetic. ROS transport is real."}
        write_json(runtime.output / "system-validation.json", report)
        return report
    except Exception as exc:
        import traceback
        write_json(runtime.output / "system-validation-failure.json", {
            "exception": type(exc).__name__, "traceback": traceback.format_exc()})
        raise
    finally:
        if system is not None:
            await system.close()
        if deployment is not None:
            await deployment.close()
        else:
            await runtime.close()
        for process in processes:
            if process.poll() is None:
                process.send_signal(2)
        for process in processes:
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                await asyncio.to_thread(process.wait, timeout=5)
        for log in logs:
            log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/system-validation"))
    parser.add_argument("--arms", type=int, default=2)
    parser.add_argument("--api", choices=("openai_chat", "openai_responses"), default="openai_responses")
    args = parser.parse_args()
    if not 2 <= args.arms <= 8:
        parser.error("validation supports 2..8 synthetic controller processes")
    print(json.dumps(asyncio.run(validate(args.output, arms=args.arms, api=args.api)), indent=2))


if __name__ == "__main__":
    main()
