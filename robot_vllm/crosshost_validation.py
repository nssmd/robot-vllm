"""Bounded two-host ROS/MuJoCo fault experiment; requires an existing lab container.

SSH provisions the experiment and retrieves evidence. Runtime goals, feedback,
sensors and leases travel only through ROS 2 over a disconnectable TCP relay.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import sys
import tarfile
import time
import uuid

from .control import DeviceRuntime, Rejected
from .deployment import Deployment
from .runtime import write_json


class NetworkRelay:
    """Disconnect only the remote experiment's ROS transport, never host networking."""
    def __init__(self, target_port):
        self.target_port = target_port
        self.enabled = True
        self.writers = set()
        self.tasks = set()
        self.server = None
        self.connections = 0

    async def start(self, port):
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", port)

    async def accept(self, reader, writer):
        self.tasks.add(asyncio.current_task())
        upstream = None
        pipes = []
        try:
            if not self.enabled:
                return
            source, upstream = await asyncio.open_connection("127.0.0.1", self.target_port)
            self.writers.update((writer, upstream))
            self.connections += 1
            async def copy(src, dst):
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            pipes = [asyncio.create_task(copy(reader, upstream)), asyncio.create_task(copy(source, writer))]
            await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, ConnectionError):
            pass
        finally:
            for task in pipes:
                task.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)
            for stream in (writer, upstream):
                if stream is not None:
                    self.writers.discard(stream)
                    stream.close()
            self.tasks.discard(asyncio.current_task())

    def disconnect(self):
        self.enabled = False
        for writer in list(self.writers):
            writer.close()

    async def close(self):
        self.disconnect()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


async def command(*args, timeout=30):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                               stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError(f"command failed ({proc.returncode}): {args[0]}: {stderr.decode()[-1500:]}")
    return stdout.decode()


async def until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.03)
    raise TimeoutError("bounded experiment condition timed out")


async def validate(args):
    tag = uuid.uuid4().hex[:12]
    output = args.output.resolve() / tag
    output.mkdir(parents=True)
    remote_relative = "coordination/" + tag
    remote_dir = args.remote_root.rstrip("/") + "/" + remote_relative
    mounted = "/work/" + remote_relative
    report = {"experiment_id": tag, "category": "cross_host_integration",
              "status": "running", "checks": [], "task_verdicts": [],
              "model_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
              "total_tokens": 0, "unmetered_call_count": 0,
              "physical_robot": False, "shared_scene": False}
    previous = json.loads(args.resume_from.read_text()) if args.resume_from else {}
    passed = {c["name"] for c in previous.get("checks", []) if c["status"] == "passed"}
    passed.update(previous.get("previous_passed_checks", []))
    report["previous_passed_checks"] = sorted(passed)
    report["resume_from"] = str(args.resume_from) if args.resume_from else None
    started = time.monotonic()
    write_json(output / "manifest.json", {**{k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "experiment_id": tag, "created_at_unix": time.time(),
        "domain_id": args.domain, "transport": "rmw_zenoh_cpp", "remote_directory": remote_dir})
    procs, logs = [], []
    deployment = runtime = contender = contender_deployment = None
    remote_started = False
    relay = NetworkRelay(args.router_port)
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", args.remote_host]

    async def remote(*argv):
        return await command(*ssh, shlex.join(argv))

    async def start_process(name, argv, env=None):
        log = (output / (name + ".log")).open("x")
        logs.append(log)
        proc = await asyncio.create_subprocess_exec(*argv, stdout=log, stderr=log, env=env)
        procs.append(proc)
        return proc

    async def settle(op, timeout=15):
        await until(lambda: runtime.status(op["execution_id"])["settled"], timeout=timeout)
        return runtime.status(op["execution_id"])

    async def submit(name, target, duration=2, capability="pair", owner="coordinator"):
        obs = await runtime.observe(capability)
        trajectory = {"points": [{"positions": target, "time_from_start_s": duration}]}
        arguments = {"members": {n: trajectory for n in members}} if capability == "pair" else trajectory
        return await runtime.submit(owner=owner, request_id=name, capability=capability,
                                    observation_id=obs["observation_id"], arguments=arguments)

    try:
        remote_host = (await remote("hostname")).strip()
        assert remote_host != socket.gethostname(), "separate hosts required"
        report["hosts"] = [socket.gethostname(), remote_host]
        state = (await remote("docker", "inspect", "--format", "{{.State.Status}}", args.container)).strip()
        if state != "exited":
            raise RuntimeError("dedicated experiment container must be stopped before this run")
        await remote("mkdir", "-p", remote_dir)
        archive = output / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(Path(__file__).parent, arcname="source/robot_vllm",
                    filter=lambda info: None if "__pycache__" in info.name else info)
        await command("scp", "-q", "-o", "BatchMode=yes", str(archive),
                      args.remote_host + ":" + remote_dir + "/source.tar.gz")
        await remote("tar", "-xzf", remote_dir + "/source.tar.gz", "-C", remote_dir)
        local_config = {"mode": "client", "connect": {"endpoints": [f"tcp/127.0.0.1:{args.router_port}"]},
                        "listen": {"endpoints": []}, "scouting": {"multicast": {"enabled": False}}}
        router_config = {"mode": "router", "listen": {"endpoints": [f"tcp/127.0.0.1:{args.router_port}"]},
                         "scouting": {"multicast": {"enabled": False}}}
        remote_config = {**local_config, "connect": {"endpoints": [f"tcp/127.0.0.1:{args.remote_port}"]}}
        for name, cfg in [("router", router_config), ("local", local_config), ("remote", remote_config)]:
            write_json(output / (name + ".json5"), cfg)
        await command("scp", "-q", "-o", "BatchMode=yes", str(output / "remote.json5"),
                      args.remote_host + ":" + remote_dir + "/remote.json5")
        env = {**os.environ, "RMW_IMPLEMENTATION": "rmw_zenoh_cpp", "ROS_DOMAIN_ID": str(args.domain),
               "ZENOH_SESSION_CONFIG_URI": str(output / "local.json5")}
        os.environ.update({k: env[k] for k in ("RMW_IMPLEMENTATION", "ROS_DOMAIN_ID", "ZENOH_SESSION_CONFIG_URI")})
        await start_process("router", ["/opt/ros/jazzy/lib/rmw_zenoh_cpp/rmw_zenohd"],
                            {**env, "ZENOH_ROUTER_CONFIG_URI": str(output / "router.json5")})
        await relay.start(args.relay_port)
        await start_process("ssh-tunnel", ["ssh", "-NT", "-o", "BatchMode=yes", "-o",
            "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=5", "-R",
            f"127.0.0.1:{args.remote_port}:127.0.0.1:{args.relay_port}", args.remote_host])
        await remote("docker", "start", args.container)
        remote_started = True
        namespace = "/coordination_" + tag
        remote_python = ["env", "RMW_IMPLEMENTATION=rmw_zenoh_cpp", f"ROS_DOMAIN_ID={args.domain}",
            "ZENOH_SESSION_CONFIG_URI=" + mounted + "/remote.json5",
            "/opt/physics/bin/python", "-m", "robot_vllm.physics_controller", "--namespace",
            namespace + "/remote", "--output", mounted + "/controller", "--host-label", remote_host,
            "--lease-timeout", "0.7"]
        shell = "source /opt/ros/jazzy/setup.bash && export PYTHONPATH="
        shell += shlex.quote(mounted + "/source") + ':"${PYTHONPATH:-}" && exec ' + shlex.join(remote_python)
        shell += " > " + shlex.quote(mounted + "/controller.log") + " 2>&1"
        await remote("docker", "exec", "-d", args.container, "bash", "-lc", shell)
        await start_process("local-controller", [sys.executable, "-m", "robot_vllm.physics_controller",
            "--namespace", namespace + "/local", "--output", str(output / "local-controller"),
            "--host-label", socket.gethostname(), "--lease-timeout", "0.7"], env)
        runtime = DeviceRuntime(output / "runtime", observation_ttl_s=1, cancel_timeout_s=0.25)
        devices = [{"name": name, "robot_id": name, "backend": "ros2",
            "action_name": namespace + "/" + name + "/follow_joint_trajectory",
            "joint_state_topic": namespace + "/" + name + "/joint_states",
            "joint_names": ["joint_1", "joint_2"], "limits": [[-1, 1], [-1, 1]],
            "state_max_age_s": 0.6, "lease_topic": namespace + "/" + name + "/runtime_lease"}
            for name in ("local", "remote")]
        members = [d["name"] + ".trajectory" for d in devices]
        config = {"devices": devices, "groups": [{"name": "pair", "members": members}]}
        write_json(output / "topology.json", config)
        deployment = Deployment(runtime, config)
        await deployment.ready(timeout_s=25)
        assert all(p.returncode is None for p in procs), "experiment process exited"

        if "cross_host_group_and_resource_exclusion" not in passed:
            # Concurrent motion plus whole-group exclusion in the shared coordinator.
            pair = await submit("coordinated-with-contention", [0.3, -0.2], duration=2)
            await until(lambda: bool(runtime.status(pair["execution_id"])["progress"].get("members")))
            try:
                await submit("conflicting-owner", [0, 0], capability="local.trajectory", owner="vla")
                raise AssertionError("overlapping owner accepted")
            except Rejected as exc:
                assert str(exc) == "resource_busy", str(exc)
            result = await settle(pair)
            assert result["status"] == "completed", result
            report["checks"].append({"name": "cross_host_group_and_resource_exclusion", "status": "passed",
                                     "execution": result})
            write_json(output / f"checkpoint-{len(report['checks']):02d}.json", report)

        if "cross_host_coupled_cancel" not in passed:
            # Explicit cancellation must hold both plants, then release native goals.
            pair = await submit("cancel-both", [-0.5, 0.5], duration=8)
            await asyncio.sleep(0.4)
            await runtime.cancel(pair["execution_id"], owner="coordinator")
            result = await settle(pair)
            assert result["status"] == "canceled", result
            report["checks"].append({"name": "cross_host_coupled_cancel", "status": "passed", "execution": result})
            write_json(output / f"checkpoint-{len(report['checks']):02d}.json", report)

        if "network_partition_hold_quarantine_reconcile" not in passed:
            # A real transport partition: remote can run locally, no ROS bytes cross.
            pair = await submit("partition-during-motion", [0.7, -0.7], duration=10)
            await asyncio.sleep(0.5)
            disconnected = time.monotonic()
            relay.disconnect()
            write_json(output / "partition.json", {"local_monotonic": disconnected, "unix_time": time.time(),
                "mechanism": "close remote-only ROS TCP relay and reject reconnections"})
            await until(lambda: runtime.status(pair["execution_id"])["status"] == "uncertain", timeout=5)
            detection_s = time.monotonic() - disconnected
            quarantined = runtime.health()
            assert all(v["state"] == "quarantined" for v in quarantined["resources"].values())
            try:
                await submit("premature-local-reuse", [0, 0], capability="local.trajectory")
                raise AssertionError("quarantined group was released")
            except Rejected as exc:
                assert str(exc) == "resource_busy", str(exc)
            try:
                await runtime.observe("remote.trajectory")
                raise AssertionError("disconnected remote sensor was accepted")
            except Rejected as exc:
                assert str(exc) == "joint_state_unavailable_or_stale", str(exc)
            await asyncio.sleep(1.0)
            # The remote process remains running during the outage and must stop
            # autonomously; this independent SSH read is evidence, not a stop command.
            event_text = await remote("cat", remote_dir + "/controller/controller-events.jsonl")
            remote_events = [json.loads(line) for line in event_text.splitlines()]
            assert any(e["event"] == "hold_confirmed" and e.get("reason") == "lease_expired"
                       for e in remote_events), remote_events[-5:]
            local_events = [json.loads(line) for line in (output / "local-controller/controller-events.jsonl").read_text().splitlines()]
            assert any(e["event"] == "hold_confirmed" and e["monotonic"] >= disconnected
                       for e in local_events), local_events[-5:]
            restored = time.monotonic()
            relay.enabled = True
            await deployment.ready(timeout_s=25)
            result = await settle(pair, timeout=15)
            assert result["status"] in ("failed", "canceled") and not runtime.occupied, result
            report["checks"].append({"name": "network_partition_hold_quarantine_reconcile", "status": "passed",
                "uncertainty_detected_s": detection_s, "outage_s": restored - disconnected,
                "reconcile_s": time.monotonic() - restored, "quarantined_health": quarantined,
                "execution": result, "relay_connections": relay.connections})
            write_json(output / f"checkpoint-{len(report['checks']):02d}.json", report)

        if "fresh_goal_after_reconnection" not in passed:
            recovered = await submit("new-goal-after-reconciliation", [0.1, 0.15], duration=1.5)
            result = await settle(recovered)
            assert result["status"] == "completed", result
            report["checks"].append({"name": "fresh_goal_after_reconnection", "status": "passed", "execution": result})
            write_json(output / f"checkpoint-{len(report['checks']):02d}.json", report)

        if "remote_busy_partial_admission_cancels_local_peer" not in passed:
            contender = DeviceRuntime(output / "contender-runtime", cancel_timeout_s=0.25)
            contender_deployment = Deployment(contender, {"devices": [devices[1]]})
            await contender_deployment.ready(timeout_s=15)
            obs = await contender.observe("remote.trajectory")
            occupied = await contender.submit(owner="independent-coordinator", request_id="remote-busy",
                capability="remote.trajectory", observation_id=obs["observation_id"], arguments={
                    "points": [{"positions": [-0.3, 0.25], "time_from_start_s": 4.0}]})
            await until(lambda: bool(contender.status(occupied["execution_id"])["progress"].get("positions_rad")))
            pair = await submit("partial-admission", [0.5, -0.4], duration=4)
            result = await settle(pair)
            member_results = result["detail"]["members"]
            assert result["status"] == "failed" and result["settled"], result
            assert member_results["remote.trajectory"]["detail"]["reason"] == "goal_rejected"
            assert member_results["local.trajectory"]["status"] == "canceled"
            assert not contender.status(occupied["execution_id"])["settled"], "other owner's goal was disturbed"
            await contender.cancel(occupied["execution_id"], owner="independent-coordinator")
            await until(lambda: contender.status(occupied["execution_id"])["settled"])
            report["checks"].append({"name": "remote_busy_partial_admission_cancels_local_peer",
                "status": "passed", "execution": result,
                "independent_owner_execution": contender.status(occupied["execution_id"])})
            write_json(output / f"checkpoint-{len(report['checks']):02d}.json", report)
        report["status"] = "passed"
    except Exception as exc:
        import traceback
        report.update(status="failed", exception=type(exc).__name__, message=str(exc),
                      traceback=traceback.format_exc())
    finally:
        if contender_deployment:
            await contender_deployment.close()
        if runtime:
            report["runtime_dir"] = str(runtime.output)
            report["final_runtime_health"] = runtime.health()
            await runtime.close()
        if remote_started:
            try:
                await remote("docker", "stop", "--timeout", "5", args.container)
                await command("scp", "-qr", "-o", "BatchMode=yes", args.remote_host + ":" + remote_dir + "/controller",
                              str(output / "remote-controller"))
                await command("scp", "-q", "-o", "BatchMode=yes", args.remote_host + ":" + remote_dir + "/controller.log",
                              str(output / "remote-controller.log"))
            except Exception as exc:
                report["cleanup_error"] = str(exc)
        for proc in reversed(procs):
            if proc.returncode is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        await relay.close()
        if deployment and not runtime.occupied:
            await deployment.close()
        for log in logs:
            log.close()
        report["wall_time_s"] = time.monotonic() - started
        report["experiment_workers_at_completion"] = 0
        write_json(output / "report.json", report)
    print(json.dumps({"status": report["status"], "checks": [v["name"] for v in report["checks"]],
                      "report": str(output / "report.json"), "error": report.get("message")}, indent=2))
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-host", required=True)
    parser.add_argument("--remote-root", required=True, help="Host directory mounted at /work in the existing container")
    parser.add_argument("--container", required=True, help="Dedicated stopped ROS/MuJoCo lab container")
    parser.add_argument("--output", type=Path, default=Path("runs/crosshost-coordination"))
    parser.add_argument("--resume-from", type=Path, help="Skip checks already passed in this evidence report")
    parser.add_argument("--domain", type=int, default=185)
    parser.add_argument("--router-port", type=int, default=28447)
    parser.add_argument("--relay-port", type=int, default=28448)
    parser.add_argument("--remote-port", type=int, default=38447)
    return asyncio.run(validate(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
