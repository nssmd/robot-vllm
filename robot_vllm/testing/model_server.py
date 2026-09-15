"""Synthetic GP6/VLA HTTP responders, for protocol integration only."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path

from ..dag import DAG_SCHEMA
from ..runtime import write_json


def fixture_arguments(capability, observation=None, offset=0.04):
    data = observation or {}
    if capability["backend"] == "mock":
        return {"target": min(0.8, data.get("position", 0.0) + offset), "steps": 4}
    joints = capability["metadata"]["joint_names"]
    positions = data["positions_rad"]
    if len(positions) != len(joints) or not positions:
        raise ValueError("fixture requires actual received joint-state values")
    return {"points": [{"positions": [min(0.8, max(-0.8, p + offset)) for p in positions],
                         "time_from_start_s": 0.15}]}


def fixture_plan(context):
    caps = {c["name"]: c for c in context["capabilities"]}
    leaves = [c for c in caps.values() if c["backend"] != "coordinated"]
    groups = [c for c in caps.values() if c["backend"] == "coordinated"]
    nodes = []
    for i, cap in enumerate(leaves):
        use_vla = i == 1
        nodes.append({"id": f"prepare_{i}", "capability": cap["name"],
            "policy": "vla" if use_vla else "code", "depends_on": [],
            "instruction": "Move using the visible state", "max_rounds": 2 if use_vla else 1,
            "arguments": {} if use_vla else fixture_arguments(cap, context["observations"][cap["name"]])})
    group = groups[0]
    nodes.append({"id": "coordinate", "capability": group["name"], "policy": "code",
        "depends_on": [node["id"] for node in nodes], "arguments": {"members": {
            name: fixture_arguments(caps[name], context["observations"][name], offset=0.15)
            for name in group["metadata"]["members"]}}})
    nodes.append({"id": "gp6_finish", "capability": leaves[0]["name"], "policy": "gp6",
                  "depends_on": ["coordinate"], "instruction": "Finish with a small visible-state-based motion",
                  "arguments": {}})
    return {"schema": DAG_SCHEMA, "nodes": nodes}


class FixtureHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/vla":
                context = body["context"]
            elif self.path == "/responses":
                context = json.loads(body["input"][0]["content"][0]["text"])
            else:
                context = json.loads(body["messages"][-1]["content"][0]["text"])
            if context["schema"] == "robot_runtime.planning_context.v1":
                value = fixture_plan(context)
            else:
                value = {"schema": "robot_runtime.node_proposal.v1",
                         "observation_id": context["observation"]["observation_id"],
                         "capability": context["node"]["capability"],
                         "arguments": fixture_arguments(context["capability"], context["observation"]["data"]),
                         "done": context["round"] + 1 >= context["node"]["max_rounds"]}
            # Counts below are synthetic protocol fields, not measured model token use.
            if self.path == "/vla":
                response = value
            elif self.path == "/responses":
                response = {"output": [{"type": "message", "content": [
                    {"type": "output_text", "text": json.dumps(value)}]}],
                    "usage": {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140}}
            else:
                response = {"choices": [{"message": {"content": json.dumps(value)}}],
                            "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        except Exception as exc:
            self.send_error(400, type(exc).__name__)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready", required=True, type=Path)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    write_json(args.ready, {"pid": os.getpid(), "port": server.server_port,
                           "synthetic": True, "base_url": f"http://127.0.0.1:{server.server_port}"})
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
