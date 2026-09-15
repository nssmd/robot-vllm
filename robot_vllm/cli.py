from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess

from .libero import LiberoEnvironment
from .policies import AlternatingPolicy, ChatPolicy, CodePolicy, VLAPolicy
from .runtime import RunStore, Runtime


def main():
    parser = argparse.ArgumentParser(description="Robot vLLM: LIBERO diagnostic closed loop")
    parser.add_argument("--libero-root", type=Path, required=True)
    parser.add_argument("--task", default="libero_spatial/0")
    parser.add_argument("--seed", type=int, default=20260914)
    parser.add_argument("--init-index", type=int, default=None)
    parser.add_argument("--output", type=Path, default=Path("runs"))
    parser.add_argument("--policy", choices=("code", "chat", "vla", "hybrid"), default="code")
    parser.add_argument("--chat-endpoint")
    parser.add_argument("--chat-model")
    parser.add_argument("--vla-endpoint")
    parser.add_argument("--vla-model")
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--max-decisions", type=int, default=12)
    args = parser.parse_args()
    for kind in ("chat", "vla"):
        if args.policy in (kind, "hybrid") and not all(
                getattr(args, kind + suffix) for suffix in ("_endpoint", "_model")):
            parser.error(f"{kind} requires explicit --{kind}-endpoint and --{kind}-model")
    if args.policy == "code":
        policy = CodePolicy()
    elif args.policy == "chat":
        policy = ChatPolicy(args.chat_endpoint, args.chat_model)
    elif args.policy == "vla":
        policy = VLAPolicy(args.vla_endpoint, args.vla_model)
    else:
        policy = AlternatingPolicy(ChatPolicy(args.chat_endpoint, args.chat_model),
                                   VLAPolicy(args.vla_endpoint, args.vla_model))
    revision = subprocess.run(["git", "-C", str(args.libero_root), "rev-parse", "HEAD"],
                              capture_output=True, text=True)
    if revision.returncode:
        parser.error("--libero-root must be a versioned LIBERO checkout")
    versions = {}
    for package in ("numpy", "mujoco", "robosuite", "torch", "Pillow"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "missing"
    manifest = {"backend": "libero", "category": "runtime_diagnostic",
                "task": args.task, "seed": args.seed, "init_index": args.init_index,
                "initialization": "fixed_index" if args.init_index is not None else "seeded_reset",
                "environment_root": str(args.libero_root.resolve()),
                "environment_revision": revision.stdout.strip(), "dependencies": versions,
                "controller": "OSC_POSE", "control_hz": 20, "settle_steps": 10,
                "camera_size": 128, "gripper_close": 1, "gripper_open": -1,
                "render_backend": os.environ.get("MUJOCO_GL", "egl"),
                "egl_device_id": os.environ.get("MUJOCO_EGL_DEVICE_ID"),
                "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
                "chat_model": args.chat_model, "vla_model": args.vla_model,
                "chat_endpoint": args.chat_endpoint, "vla_endpoint": args.vla_endpoint}
    runtime = Runtime(RunStore(args.output), max_steps=args.max_steps, max_decisions=args.max_decisions)
    result = runtime.run(lambda out: LiberoEnvironment(out, root=args.libero_root,
                         task=args.task, init_index=args.init_index), policy, manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "infra" else 0


if __name__ == "__main__":
    raise SystemExit(main())
