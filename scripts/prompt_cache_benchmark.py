"""Actual provider-cache measurement across fresh robot planning trials.

Uses real capability catalogs and changing state. No prompt padding, result
replay, fabricated usage, or simulator-success claim. Requires a Responses API.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import time
import uuid

from robot_vllm.control import DeviceRuntime
from robot_vllm.deployment import Deployment, mock_topology
from robot_vllm.models import ModelEndpoint, ModelMeter, PLANNER_INSTRUCTION
from robot_vllm.dag import TaskPlan
from robot_vllm.runtime import Journal, write_json


async def run(args):
    if args.azure_cli:
        from azure.identity import AzureCliCredential
        os.environ["PROMPT_CACHE_TEST_KEY"] = AzureCliCredential().get_token("https://cognitiveservices.azure.com/.default").token
    root = args.output
    root.mkdir(parents=True, exist_ok=False)
    runtime = DeviceRuntime(root / "runtime")
    deployment = Deployment(runtime, mock_topology(8))
    journal = Journal(root / "calls.jsonl")
    rows = []
    namespace = uuid.uuid4().hex[:12]
    write_json(root / "manifest.json", {"model": args.model, "trials": args.trials,
        "variants": ["original", "ordered", "breakpoint"], "cache_key_namespace": namespace,
        "scope": "real provider / synthetic robot state / no motion", "warmup": "first trial per variant",
        "padding": False, "same_capability_catalog": True})
    try:
        for trial in range(args.trials):
            names = ["original", "ordered", "breakpoint"]
            names = names[trial % 3:] + names[:trial % 3]
            for name in names:
                position = .2 if trial % 2 == 0 else -.2
                for driver in deployment.drivers:
                    driver.position = position
                observations = {cap: (await runtime.observe(cap))["data"]
                    for cap, (spec, _) in runtime.capabilities.items() if spec.backend != "coordinated"}
                context = {"schema": "robot_runtime.planning_context.v1", "task_id": uuid.uuid4().hex,
                    "task": "Return exactly one code node moving robot.arm_1.move to the negative of its CURRENT observed position, with steps=1. No other nodes.",
                    "capabilities": runtime.catalog(), "topology": runtime.topology(),
                    "policies": ["code"], "completed": {}, "previous_result": None,
                    "observations": observations}
                meter = ModelMeter(journal)
                endpoint = ModelEndpoint("planner", {"kind": "openai_responses", "model": args.model,
                    "endpoint": args.endpoint, "key_env": "PROMPT_CACHE_TEST_KEY" if args.azure_cli else args.key_env,
                    "timeout_s": 120, "reasoning_effort": "low", "max_output_tokens": 512,
                    "cache_layout": name != "original", "cache_breakpoint": name == "breakpoint",
                    "prompt_cache_key": f"robot-{namespace}-{name}"})
                row = {"trial": trial, "variant": name, "warmup": trial == 0, "status": "infra"}
                write_json(root / f"context-{trial}-{name}.json", context)
                try:
                    tick = time.monotonic()
                    value = await endpoint.generate(instruction=PLANNER_INSTRUCTION, context=context, role="planner", meter=meter)
                    row["wall_time_s"] = time.monotonic() - tick
                    checked = TaskPlan.parse(value, runtime, {"code"})
                    row["output"] = value
                    row["correct_current_state"] = (len(checked.nodes) == 1
                        and checked.nodes[0].capability == "robot.arm_1.move"
                        and checked.nodes[0].arguments == {"target": -position, "steps": 1})
                    row["status"] = "completed"
                except Exception as exc:
                    row["error"] = str(exc)
                finally:
                    endpoint.pool.close()
                    row["metrics"] = meter.summary()
                    write_json(root / f"result-{trial}-{name}.json", row)
                    rows.append(row)
                    print(trial, name, row["status"], row.get("correct_current_state"),
                          row["metrics"]["prompt_tokens"], row["metrics"]["cached_input_tokens"], flush=True)
                if row["status"] == "infra":
                    raise RuntimeError("Stop on provider/contract failure; inspect retained result before retrying")
                await asyncio.sleep(args.interval)
        summary = {}
        for name in ("original", "ordered", "breakpoint"):
            valid = [r for r in rows if r["variant"] == name and r["status"] == "completed" and not r["warmup"]]
            inputs = sum(r["metrics"]["prompt_tokens"] for r in valid)
            cached = sum(r["metrics"]["cached_input_tokens"] for r in valid)
            summary[name] = {"valid_non_warmup_trials": len(valid), "input_tokens": inputs,
                "cached_input_tokens": cached, "cache_hit_token_ratio": cached / inputs if inputs else None,
                "cache_unmetered_calls": sum(r["metrics"]["cache_unmetered_call_count"] for r in valid),
                "correct_current_state": sum(r["correct_current_state"] for r in valid),
                "infra": sum(r["status"] == "infra" for r in rows if r["variant"] == name),
                "median_wall_s": statistics.median(r["wall_time_s"] for r in valid) if valid else None}
        write_json(root / "summary.json", summary)
        print(json.dumps(summary, indent=2))
    finally:
        journal.close()
        await deployment.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.environ.get("GP6_ENDPOINT"))
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--key-env", default="GP6_API_KEY")
    parser.add_argument("--azure-cli", action="store_true")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--interval", type=float, default=5, help="Seconds between provider calls")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.endpoint:
        parser.error("endpoint required")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
