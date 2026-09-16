"""Matched real-model evaluation of shared sensing on MuJoCo visual reaching.

Requires an operator-configured Responses endpoint; never uses a model fixture.
Baseline: four requests, each identifies its robot's target. Shared: one request
identifies all four, subscribers reuse that perception. The compact arm changes
only the model's output schema. Both variants use the same public grid decoder.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import statistics
import shutil
import time
import uuid

from robot_vllm.control import Rejected
from robot_vllm.inference import InferencePool
from robot_vllm.policies import post_json
from robot_vllm.runtime import write_json
from robot_vllm.sensing import SenseKey, SharedPerception
from robot_vllm.testing.sensing_scene import COLORS, SensingScene


def response_schema(colors, compact):
    integer = {"type": "integer", "minimum": 1, "maximum": 5}
    properties = {c: integer for c in colors} if compact else {
        "targets": {"type": "array", "items": {"type": "object", "properties": {
            "color": {"type": "string", "enum": list(colors)}, "column": integer},
            "required": ["color", "column"], "additionalProperties": False}}}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def decode(value, colors, compact):
    if compact:
        predictions = value
    else:
        if set(value) != {"targets"} or not isinstance(value["targets"], list):
            raise Rejected("invalid_target_response")
        entries = value["targets"]
        if any(set(e) != {"color", "column"} for e in entries):
            raise Rejected("invalid_target_entry")
        predictions = {e["color"]: e["column"] for e in entries}
        if len(entries) != len(predictions):
            raise Rejected("duplicate_target")
    if set(predictions) != set(colors) or any(type(v) is not int or not 1 <= v <= 5 for v in predictions.values()):
        raise Rejected("invalid_target_columns")
    return predictions


def credential(args):
    if args.azure_cli:
        from azure.identity import AzureCliCredential
        return AzureCliCredential(process_timeout=30).get_token('https://cognitiveservices.azure.com/.default').token
    token = os.environ.get(args.key_env)
    if not token:
        raise RuntimeError("model credential not configured")
    return token


async def episode(args, root, seed, variant, token):
    scene = SensingScene(seed)
    shared = SharedPerception(max_age_s=120)
    pool = InferencePool(max_concurrency=args.concurrency)
    calls, phases = [], []
    started = time.monotonic()
    compact = variant == "shared_compact"
    try:
        write_json(root / "config.json", {"seed": seed, "variant": variant, "model": args.model,
            "concurrency": args.concurrency, "phases": 2, "synthetic_model": False,
            "scene": "mujoco_four_slider_visual_reaching", "compact": compact})
        (root / "scene.xml").write_text(scene.xml)
        for phase in range(2):
            if phase:
                scene.change_targets()
                shared.invalidate("top")
            tick = time.monotonic()
            frame = scene.camera(root / f"phase-{phase}.png")
            captured = time.monotonic()
            capture_s = captured - tick
            key = SenseKey("top", f"seed-{seed}-phase-{phase}", "grid-v1", args.model,
                           "all-target-columns-v1", shared.generation("top"))
            sense_started = time.monotonic()

            async def infer(colors):
                call_id = uuid.uuid4().hex
                record = {"call_id": call_id, "phase": phase, "colors": list(colors), "status": "infra",
                          "queue_time_s": None, "service_time_s": None}
                request = {"model": args.model, "store": False,
                    "instructions": "Read only the provided camera image. Identify the numbered grid column (1-5) containing each requested colored target disk. Ignore the small black robot markers. Return the requested JSON only; do not explain.",
                    "input": [{"role": "user", "content": [
                        {"type": "input_text", "text": "Requested target colors: " + ', '.join(colors)},
                        {"type": "input_image", "image_url": "data:image/png;base64," + frame["data"]}]}],
                    "text": {"format": {"type": "json_schema", "name": "target_columns", "strict": True,
                                         "schema": response_schema(colors, compact)}},
                    "max_output_tokens": 256, "reasoning": {"effort": "low"}}
                write_json(root / f"request-{call_id}.json", request)
                try:
                    async with pool.admit() as slot:
                        record["queue_time_s"] = slot.queue_time_s
                        service_started = time.monotonic()
                        response = await slot.run(post_json, args.endpoint, request, token, 120)
                        record["service_time_s"] = time.monotonic() - service_started
                    write_json(root / f"response-{call_id}.json", response)
                    record["usage"] = response.get("usage")
                    record["reported_model"] = response.get("model")
                    if response.get("status") != "completed":
                        raise Rejected("incomplete_model_response")
                    text = ''.join(part.get("text", "") for item in response.get("output", [])
                                   for part in item.get("content", []) if part.get("type") == "output_text")
                    prediction = decode(json.loads(text), colors, compact)
                    record["status"] = "completed"
                    return prediction
                except Exception as exc:
                    record["error_type"] = type(exc).__name__
                    raise
                finally:
                    calls.append(record)
                    write_json(root / f"call-{call_id}.json", record)

            async def robot(color):
                waiting = time.monotonic()
                if variant == "independent":
                    result = await infer((color,))
                else:
                    result = await shared.get(key, captured, lambda: infer(COLORS))
                    shared.validate(key, captured)
                return color, result[color], time.monotonic() - waiting

            responses = await asyncio.gather(*(robot(color) for color in COLORS), return_exceptions=True)
            failures = [r for r in responses if isinstance(r, BaseException)]
            if failures:
                raise RuntimeError("perception request failed; retained per-call records")
            perception_s = time.monotonic() - sense_started
            predictions = {color: value for color, value, _ in responses}
            # All four independent robots start together after the sensing barrier.
            # This benchmark explicitly measures that barrier, not asynchronous motion.
            tick = time.monotonic()
            trace = scene.execute(predictions)
            action_s = time.monotonic() - tick
            verdict = scene.final_verdict()  # No evaluator state enters the model context.
            scene.camera(root / f"phase-{phase}-after.png")
            write_json(root / f"phase-{phase}-trajectory.json", trace)
            phases.append({"phase": phase, "prediction": predictions, "verdict": verdict,
                "capture_s": capture_s, "perception_s": perception_s, "action_s": action_s,
                "sensor_barrier_s": perception_s, "robot_blocked_seconds": 4 * perception_s,
                "subscriber_wait_s": {c: wait for c, _, wait in responses}, "snapshot": asdict(key)})
            write_json(root / f"phase-{phase}-verdict.json", phases[-1])
        status = "success" if all(p["verdict"]["success"] for p in phases) else "failure"
    except Exception as exc:
        status = "infra"
        write_json(root / "infrastructure.json", {"type": type(exc).__name__, "message": str(exc)})
    finally:
        await shared.close()
        scene.close()
        pool.close()
    metered = [c["usage"] for c in calls if isinstance(c.get("usage"), dict)]
    result = {"seed": seed, "variant": variant, "status": status, "wall_time_s": time.monotonic() - started,
        "calls": calls, "model_calls": len(calls), "unmetered_calls": len(calls) - len(metered),
        "tokens": {k: sum(u.get(k, 0) for u in metered) for k in ("input_tokens", "output_tokens", "total_tokens")},
        "phases": phases, "shared_perception": shared.snapshot(), "inference": pool.snapshot(),
        "sensor_barrier_s": sum(p["sensor_barrier_s"] for p in phases),
        "robot_blocked_seconds": sum(p["robot_blocked_seconds"] for p in phases),
        "max_position_error_m": max((p["verdict"]["max_position_error_m"] for p in phases), default=None)}
    write_json(root / "result.json", result)
    print(seed, variant, status, result["tokens"], round(result["wall_time_s"], 3), flush=True)
    return result


async def run(args):
    token = credential(args)
    root = args.output or Path("runs/sensing-benchmark") / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    variants = ("independent", "shared", "shared_compact")
    manifest = {"schema": "robot_runtime.sensing_benchmark.v1", "model": args.model,
        "seeds": args.seeds, "variants": list(variants), "concurrency": args.concurrency,
        "scene": "mujoco_four_slider_visual_reaching", "phases": 2,
        "order": "rotate variants by seed index", "success": "all four sliders within 0.03m in both phases",
        "usage_source": "provider response usage", "synthetic_model": False,
        "reasoning_effort": "low", "max_output_tokens": 256, "image_size": [512, 512],
        "sensor_barrier": "wait for all four targets before simultaneous control", "simulated_motion_s_per_phase": .8}
    if (root / "manifest.json").exists():
        if json.loads((root / "manifest.json").read_text()) != manifest:
            raise RuntimeError("manifest mismatch")
    else:
        write_json(root / "manifest.json", manifest)
        snapshot = root / "source"
        snapshot.mkdir()
        for file in (Path(__file__), Path("robot_vllm/sensing.py"), Path("robot_vllm/testing/sensing_scene.py"),
                     Path("robot_vllm/inference.py"), Path("robot_vllm/policies.py")):
            shutil.copyfile(file, snapshot / file.name)
    results = []
    for i, seed in enumerate(args.seeds):
        order = variants[i % 3:] + variants[:i % 3]
        for variant in order:
            case = root / f"seed-{seed}" / variant
            if (case / "result.json").exists():
                results.append(json.loads((case / "result.json").read_text()))
                continue  # Preserve every completed run, including failures and infra.
            case.mkdir(parents=True, exist_ok=True)
            results.append(await episode(args, case, seed, variant, token))
    summary = {"preliminary": True, "scope": manifest["scene"], "model": args.model,
               "variants": {}, "active_workers_at_completion": 0}
    for variant in variants:
        rows = [r for r in results if r["variant"] == variant]
        valid = [r for r in rows if r["status"] != "infra"]
        summary["variants"][variant] = {"success": sum(r["status"] == "success" for r in rows),
            "failure": sum(r["status"] == "failure" for r in rows), "infra": len(rows) - len(valid),
            "denominator": len(valid), "model_calls": sum(r["model_calls"] for r in rows),
            "tokens": {k: sum(r["tokens"][k] for r in rows) for k in ("input_tokens", "output_tokens", "total_tokens")},
            "median_wall_s": statistics.median(r["wall_time_s"] for r in valid) if valid else None,
            "median_barrier_s": statistics.median(r["sensor_barrier_s"] for r in valid) if valid else None,
            "median_robot_blocked_s": statistics.median(r["robot_blocked_seconds"] for r in valid) if valid else None}
    write_json(root / "summary.json", summary)
    print(json.dumps(summary, indent=2))
    print("Evidence:", root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.environ.get("GP6_ENDPOINT"))
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--key-env", default="GP6_API_KEY")
    parser.add_argument("--azure-cli", action="store_true")
    parser.add_argument("--seeds", type=int, nargs='+', default=[913, 914, 915, 916, 917, 918])
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or GP6_ENDPOINT required")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
