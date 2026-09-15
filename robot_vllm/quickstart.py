"""Runnable GPT-6 API -> official OpenPI client -> multi-robot control walkthrough.

Default models/controllers are explicit fixtures. --real-models contacts the
operator's GPT-6 and pretrained OpenPI endpoints; it never downloads weights.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import threading

from .control import DeviceRuntime
from .deployment import Deployment
from .pi05 import create_pi05_app
from .platform import RobotSystem
from .runtime import write_json


def topology(transport, prefix):
    devices, groups, mappings = [], [], {}
    for i in (1, 2):
        name, ns = f"robot_{i}", prefix + f"/robot_{i}"
        arm, grip = name + ".arm", name + ".hand"
        mapping = {"arm_capability": arm + ".trajectory", "gripper_capability": grip + ".gripper",
            "joint_names": [f"joint_{j+1}" for j in range(7)], "joint_limits": [[-2, 2] for _ in range(7)],
            "velocity_scale_rad_s": [.1] * 7, "gripper_open_m": .08, "gripper_closed_m": 0,
            "execution_horizon": 4, "step_s": 1 / 15, "clip_normalized_velocity": True}
        mappings[name + ".control"] = mapping
        if transport == "mock":
            devices.extend([{"name": n, "robot_id": name, "backend": "plugin",
                "factory": "robot_vllm.testing.pi05_devices:" + cls} for n, cls in
                [(arm, "ArmFixture"), (grip, "GripperFixture")]])
        else:
            devices.extend([{"name": arm, "robot_id": name, "backend": "ros2",
                "action_name": ns + "/follow_joint_trajectory", "joint_state_topic": ns + "/joint_states",
                "joint_names": mapping["joint_names"], "limits": mapping["joint_limits"],
                "camera_topics": {"front": ns + "/camera/image_raw", "wrist": ns + "/wrist/image_raw"}},
                {"name": grip, "robot_id": name, "backend": "ros2_gripper", "action_name": ns + "/gripper_command",
                 "joint_state_topic": ns + "/joint_states", "joint_name": "gripper_joint"}])
        groups.append({"name": name + ".control", "members": [arm + ".trajectory", grip + ".gripper"]})
    if transport == "mock":
        devices.append({"name": "scene", "backend": "plugin", "factory": "robot_vllm.testing.pi05_devices:ServiceFixture"})
    else:
        devices.append({"name": "scene", "backend": "ros2_service", "service_name": prefix + "/robot_1/scan"})
    return {"devices": devices, "groups": groups}, mappings


def example_plan():
    return {"schema": "robot_runtime.dag.v1", "nodes": [
        {"id": f"policy_robot_{i}", "capability": f"robot_{i}.control", "policy": "pi05", "max_rounds": 2,
         "instruction": "Execute two bounded policy chunks using current camera and joint observations."}
        for i in (1, 2)] + [{"id": "consume_scene_service", "capability": "scene.trigger", "policy": "code",
            "arguments": {}, "depends_on": ["policy_robot_1", "policy_robot_2"]}]}


class PlannerFixture(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        value = {"model": "gpt6_api_fixture", "output": [{"content": [
            {"type": "output_text", "text": json.dumps(example_plan())}]}]}
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass


async def run(args):
    import numpy as np
    import uvicorn
    from websockets.asyncio.server import serve
    from openpi_client import msgpack_numpy
    runtime = DeviceRuntime(args.output, observation_ttl_s=10)
    config, mappings = topology(args.transport, "/try_" + runtime.runtime_id[:8])
    processes, logs = [], []
    planner_server = ws_server = bridge_server = bridge_task = system = deployment = None
    native_calls = []
    try:
        if args.real_models:
            uri, planner_url = os.environ["OPENPI_URI"], os.environ["GP6_ENDPOINT"]
        else:
            async def fixture(ws):
                await ws.send(msgpack_numpy.packb({"fixture": True, "checkpoint_loaded": False}))
                raw = msgpack_numpy.unpackb(await ws.recv())
                assert raw["observation/joint_position"].shape == (7,)
                assert raw["observation/exterior_image_1_left"].shape == (224, 224, 3)
                actions = np.full((10, 8), .05, dtype=np.float32)
                actions[:, 7] = 1.0 - float(raw["observation/gripper_position"][0])
                native_calls.append({"input_joints": 7, "output_shape": list(actions.shape), "fixture": True})
                await ws.send(msgpack_numpy.packb({"actions": actions}))
            ws_server = await serve(fixture, "127.0.0.1", 0, compression=None)
            uri = "ws://127.0.0.1:" + str(ws_server.sockets[0].getsockname()[1])
            planner_server = ThreadingHTTPServer(("127.0.0.1", 0), PlannerFixture)
            threading.Thread(target=planner_server.serve_forever, daemon=True).start()
            planner_url = f"http://127.0.0.1:{planner_server.server_port}/responses"
        app = create_pi05_app({"uri": uri, "mappings": mappings, "model": "pi05_droid",
                               "bridge_key_env": "ROBOT_QUICKSTART_UNUSED_KEY"})
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        bridge_port = sock.getsockname()[1]
        bridge_server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        bridge_task = asyncio.create_task(bridge_server.serve(sockets=[sock]))
        for _ in range(100):
            if bridge_server.started:
                break
            await asyncio.sleep(.02)
        if not bridge_server.started:
            raise RuntimeError("pi05_bridge_not_ready")
        if args.transport == "ros2":
            for i in (1, 2):
                path = runtime.output / f"robot-{i}.log"
                log = path.open("x")
                logs.append(log)
                processes.append(subprocess.Popen([sys.executable, "-m", "robot_vllm.ros_validation", "controller",
                    "--namespace", f"/try_{runtime.runtime_id[:8]}/robot_{i}", "--ready", str(runtime.output / f"robot-{i}.ready.json"),
                    "--joints", "7", "--with-gripper"], stdout=log, stderr=log))
        config["models"] = {"gp6": {"kind": "openai_responses", "model": os.environ.get("GP6_MODEL", "gpt-6-astra") if args.real_models else "gpt6_api_fixture",
            "endpoint": planner_url, "key_env": "GP6_API_KEY" if args.real_models else "ROBOT_QUICKSTART_UNUSED_KEY",
            "structured_output": False, "stream": args.real_models, "timeout_s": 60},
            "pi05": {"kind": "vla_json", "model": "pi05_droid", "endpoint": f"http://127.0.0.1:{bridge_port}/predict",
                     "key_env": "ROBOT_QUICKSTART_UNUSED_KEY", "timeout_s": 60}}
        write_json(runtime.output / "quickstart-config.json", config)
        deployment = Deployment(runtime, config)
        await deployment.ready(15)
        system = RobotSystem(runtime, config["models"], max_parallel=2, max_replans=0)
        print("Robot-vLLM quickstart", flush=True)
        print("Models: " + ("configured GPT-6 + OpenPI endpoint (synthetic observations)" if args.real_models else "explicit fixtures; official OpenPI client is real"), flush=True)
        print("Transport: " + ("real ROS 2 topics, actions and service; synthetic robots" if args.transport == "ros2" else "in-process synthetic robot drivers"), flush=True)
        task = ("Create exactly three DAG nodes. Run policy pi05 on robot_1.control and robot_2.control independently, "
                "each with max_rounds=2. Then invoke scene.trigger with code and empty arguments after both finish. "
                "Use no other capabilities or motions. This is a synthetic integration check, not a manipulation task.")
        result = await system.run(task, deadline_s=120)
        for name, node in result["nodes"].items():
            print(f"  {name}: {node['status']} (policy={node['node']['policy']}, rounds={node.get('rounds', 0)})", flush=True)
        assert result["status"] == "completed", result
        if not args.real_models:
            assert len(native_calls) == 4 and result["peak_parallel_nodes"] == 2
        report = {"status": "passed", "transport": args.transport, "model_mode": "configured" if args.real_models else "fixtures",
            "official_openpi_client": True, "pretrained_checkpoint_verified": False,
            "synthetic_observations_and_devices": True, "native_client_calls": len(native_calls) if not args.real_models else None,
            "peak_parallel_nodes": result["peak_parallel_nodes"], "task_verdict": None, "result": result}
        write_json(runtime.output / "quickstart.json", report)
        print("Evidence: " + str(runtime.output / "quickstart.json"), flush=True)
        return 0
    finally:
        if system:
            await system.close()
        if deployment:
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
        if bridge_server:
            bridge_server.should_exit = True
        if bridge_task:
            await bridge_task
        if ws_server:
            ws_server.close()
            await ws_server.wait_closed()
        if planner_server:
            await asyncio.to_thread(planner_server.shutdown)
            planner_server.server_close()
