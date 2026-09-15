"""GP6 planning, DAG execution, VLA policies and ROS devices in one configurable system."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from .control import DeviceRuntime
from .control_tools import create_system_app
from .dag import DAG_SCHEMA
from .deployment import Deployment, mock_topology
from .platform import RobotSystem


def demo_plan(runtime):
    leaves = [cap for cap, _ in runtime.capabilities.values() if cap.backend == "mock"]
    nodes = [{"id": f"prepare_{i}", "capability": cap.name, "policy": "code",
              "arguments": {"target": 0.2 if i % 2 == 0 else -0.2, "steps": 10}}
             for i, cap in enumerate(leaves)]
    nodes.append({"id": "coordinate", "capability": "robot.coordinated", "policy": "code",
                  "depends_on": [n["id"] for n in nodes], "arguments": {"members": {
                      cap.name: {"target": 0.3 if i % 2 == 0 else -0.3, "steps": 10}
                      for i, cap in enumerate(leaves)}}})
    return {"schema": DAG_SCHEMA, "nodes": nodes}


async def run(args):
    from .config import load_config, validate_config
    config = load_config(args.config) if args.config else validate_config(mock_topology(args.arms))
    if args.command == "validate-config":
        print(json.dumps({"status": "valid", "devices": len(config["devices"]),
                          "groups": len(config.get("groups", [])), "models": list(config.get("models", {}))}))
        return 0
    state_dir = args.state_dir or (Path("state") if args.command in ("serve", "recover") else None)
    rt = DeviceRuntime(args.output, state_dir=state_dir, **config.get("runtime", {}))
    deployment, system = None, None
    try:
        deployment = Deployment(rt, config)
        if args.command == "recover":
            results = []
            if args.execution_id:
                results.append(await rt.reconcile(args.execution_id))
            print(json.dumps({"health": rt.health(), "results": results,
                              "executions": [rt.status(e) for e in rt.recovery_pending]}, indent=2))
            return 2 if rt.recovery_pending else 0
        if args.command != "serve":
            await deployment.ready()
        else:
            rt._freeze()  # Bind persistent topology before accepting HTTP work.
        provided_plan = json.loads(args.plan.read_text()) if getattr(args, "plan", None) else None
        if args.command == "demo":
            provided_plan = demo_plan(rt)
        models = config.get("models", {})
        if provided_plan:
            used = {n.get("policy", "code") for n in provided_plan.get("nodes", [])} - {"code"}
            models = {name: cfg for name, cfg in models.items() if name in used}
        system = RobotSystem(rt, models, planner=config.get("planner", "gp6"),
                             **config.get("scheduler", {}))
        if args.command in ("demo", "run"):
            result = await system.run(getattr(args, "task", "Coordinate the test arms"), plan=provided_plan,
                                      deadline_s=getattr(args, "deadline", 300))
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "completed" else 2
        import uvicorn
        token = os.environ.get("ROBOT_RUNTIME_TOKEN")
        if args.host not in ("127.0.0.1", "::1", "localhost") and not token:
            raise ValueError("a non-loopback listener requires ROBOT_RUNTIME_TOKEN")
        app = create_system_app(system, token=token, **config.get("api", {}))
        await uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port)).serve()
        return 0
    finally:
        if system is not None:
            await system.close()
        health = await deployment.close() if deployment is not None else await rt.close()
        if health["active_executions"]:
            print(json.dumps({"unresolved_device_state": health}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "run", "serve", "validate-config", "recover"):
        command = sub.add_parser(name)
        command.add_argument("--config", type=Path)
        command.add_argument("--arms", type=int, default=2)
        command.add_argument("--output", type=Path, default=Path("runs") / ("system-" + name))
        command.add_argument("--state-dir", type=Path)
        if name == "recover":
            command.add_argument("--execution-id", help="Query a native terminal result; never force-unlock")
        if name == "run":
            command.add_argument("--task", required=True)
            command.add_argument("--plan", type=Path)
            command.add_argument("--deadline", type=float, default=300)
        if name == "serve":
            command.add_argument("--backend", choices=("mock", "ros2"), default="mock")
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=8765)
    quick = sub.add_parser("quickstart", help="Try GPT-6/OpenPI APIs and two robots without model weights")
    quick.add_argument("--transport", choices=("mock", "ros2"), default="mock")
    quick.add_argument("--real-models", action="store_true", help="Use GP6_ENDPOINT and OPENPI_URI with synthetic robot observations")
    quick.add_argument("--output", type=Path, default=Path("runs/quickstart"))
    args = parser.parse_args()
    if args.command == "quickstart":
        from .quickstart import run as quickstart
        return asyncio.run(quickstart(args))
    if getattr(args, "backend", None) == "ros2" and not args.config:
        parser.error("--backend ros2 requires --config")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
