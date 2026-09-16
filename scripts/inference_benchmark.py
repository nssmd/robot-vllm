"""Measure coordinator concurrency with explicit HTTP/model/device fixtures.

This measures overlap of configured artificial response delays, not GPT/VLA
compute speed or manipulation success. No provider keys or ROS are needed.
"""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import statistics
import threading
import time
import uuid

from robot_vllm.control import DeviceRuntime
from robot_vllm.deployment import Deployment, mock_topology
from robot_vllm.platform import RobotSystem
from robot_vllm.runtime import write_json


async def measure(root, endpoint, robots, rounds, concurrency):
    runtime = DeviceRuntime(root, observation_ttl_s=10)
    deployment = Deployment(runtime, mock_topology(robots))
    system = RobotSystem(runtime, {"gp6": {"kind": "openai_chat", "model": "latency-fixture",
        "endpoint": endpoint, "key_env": "", "inference": {"max_concurrency": concurrency}}},
        max_parallel=robots, max_replans=0)
    plan = {"schema": "robot_runtime.dag.v1", "nodes": [{"id": f"robot_{i}",
        "capability": f"robot.arm_{i}.move", "policy": "gp6", "max_rounds": rounds,
        "timeout_s": 120} for i in range(1, robots + 1)]}
    try:
        result = await system.run("Exercise each fixture robot for the same number of rounds.", plan=plan,
                                  deadline_s=120)
        calls = result["model_metrics"]["calls"]
        pool = system.endpoints["gp6"].pool.snapshot()
        report = {"concurrency": concurrency, "status": result["status"],
            "wall_time_s": result["wall_time_s"], "model_calls": len(calls),
            "completed_actions": sum(driver.started for driver in deployment.drivers),
            "median_queue_time_s": statistics.median(c["queue_time_s"] for c in calls) if calls else None,
            "median_model_call_time_s": statistics.median(c["wall_time_s"] for c in calls) if calls else None,
            "inference": pool, "task_verdict": None, "run_dir": result["run_dir"]}
        write_json(root / "measurement.json", report)
        if (result["status"] != "completed" or len(calls) != robots * rounds
                or report["completed_actions"] != robots * rounds):
            raise RuntimeError(f"fixture validation failed; inspect {root}")
        return report
    finally:
        await system.close()
        await deployment.close()


async def run(args):
    class Fixture(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            context = json.loads(request["messages"][1]["content"][0]["text"])
            time.sleep(args.latency_ms / 1000)
            proposal = {"schema": "robot_runtime.node_proposal.v1",
                "observation_id": context["observation"]["observation_id"],
                "capability": context["node"]["capability"],
                "arguments": {"target": .2, "steps": 1},
                "done": context["round"] + 1 >= context["node"]["max_rounds"]}
            body = json.dumps({"model": "latency-fixture", "choices": [
                {"message": {"content": json.dumps(proposal)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    root = args.output / uuid.uuid4().hex
    root.mkdir(parents=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/chat/completions"
        measurements = []
        for concurrency in (1, args.robots):
            measurements.append(await measure(root / f"concurrency-{concurrency}", endpoint,
                                              args.robots, args.rounds, concurrency))
        report = {"schema": "robot_runtime.inference_benchmark.v1", "status": "passed",
            "category": "synthetic_http_latency_overlap", "robots": args.robots,
            "rounds_per_robot": args.rounds, "fixture_latency_ms": args.latency_ms,
            "measurements": measurements,
            "wall_time_ratio_serial_over_parallel": measurements[0]["wall_time_s"] / measurements[1]["wall_time_s"],
            "task_verdicts": [], "pretrained_inference": False, "active_device_workers_at_completion": 0}
        write_json(root / "result.json", report)
        print(json.dumps(report, indent=2))
        print("Evidence:", root / "result.json")
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robots", type=int, choices=range(2, 9), default=4)
    parser.add_argument("--rounds", type=int, choices=range(1, 9), default=3)
    parser.add_argument("--latency-ms", type=int, choices=range(1, 501), default=100)
    parser.add_argument("--output", type=Path, default=Path("runs/inference-benchmark"))
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
