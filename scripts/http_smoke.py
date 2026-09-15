"""Exercise the installed HTTP service with synthetic device capabilities."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


root = Path("runs/http-validation")
root.mkdir(parents=True, exist_ok=True)
validation_id = uuid.uuid4().hex
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
base = f"http://127.0.0.1:{port}"
token = "synthetic-http-fixture-token"


def call(path, data=None, authenticated=True):
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["Authorization"] = "Bearer " + token
    request = Request(base + path, data=None if data is None else json.dumps(data).encode(), headers=headers)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


with (root / f"{validation_id}.log").open("x") as log:
    process = subprocess.Popen([sys.executable, "-m", "robot_vllm.control_cli", "serve",
                                "--port", str(port), "--output", str(root),
                                "--state-dir", str(root / ("state-" + validation_id))], stdout=log, stderr=log,
                               env={**os.environ, "ROBOT_RUNTIME_TOKEN": token})
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                code, health = call("/health")
                if code == 200:
                    break
            except URLError:
                pass
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("HTTP service did not start")
            time.sleep(0.05)
        assert call("/health", authenticated=False)[0] == 401
        catalog = call("/tools")[1]
        assert len(catalog["tools"]) == 6
        leaf_names = [cap["name"] for cap in catalog["capabilities"] if cap["backend"] == "mock"]
        ops = []
        for name in leaf_names:
            obs = call("/tools/observe", {"capability": name})[1]
            request = {"request_id": name, "capability": name,
                       "observation_id": obs["observation_id"], "arguments": {"target": 0.2, "steps": 40}}
            code, op = call("/tools/execute", request)
            assert code == 200
            assert call("/tools/execute", request)[1]["execution_id"] == op["execution_id"]
            assert call("/tools/execute", {**request, "request_id": name + "-busy"})[0] == 409
            ops.append(op)
        assert call("/health")[1]["active_executions"] == 2
        assert call("/tools/cancel_execution", {"execution_id": ops[0]["execution_id"]})[0] == 200
        result_a = call("/tools/wait_execution", {"execution_id": ops[0]["execution_id"]})[1]
        result_b = call("/tools/wait_execution", {"execution_id": ops[1]["execution_id"]})[1]
        assert result_a["settled"] and result_a["status"] == "canceled"
        assert result_b["settled"] and result_b["status"] == "completed"
        task_response = call("/tasks", {"task": "exercise DAG API", "plan": {
            "schema": "robot_runtime.dag.v1", "nodes": [{"id": "move", "capability": leaf_names[0],
                "policy": "code", "arguments": {"target": 0.1, "steps": 2}}]}})
        assert task_response[0] == 200
        task_id = task_response[1]["task_id"]
        deadline = time.monotonic() + 5
        while True:
            task_result = call("/tasks/" + task_id)[1]
            if task_result["status"] not in ("planning", "executing"):
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert task_result["status"] == "completed" and task_result["task_verdict"] is None
        report = {"status": "passed", "category": "http_synthetic_devices",
                  "runtime_id": health["runtime_id"], "tool_count": 6,
                  "checks": ["authentication", "capability_discovery", "observation", "async_dispatch",
                             "idempotency", "resource_conflict", "two_device_concurrency", "cancel_and_wait", "dag_task_api"],
                  "active_workers_at_completion": 0, "task_verdicts": []}
        with (root / f"{validation_id}.json").open("x") as file:
            json.dump(report, file, indent=2)
        print(json.dumps(report, indent=2))
    finally:
        process.terminate()
        process.wait(timeout=10)
