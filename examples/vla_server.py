"""Load an operator-owned trained-policy adapter; no built-in synthetic fallback.

ROBOT_VLA_FACTORY=your_package:load_policy ROBOT_VLA_MODEL=your_checkpoint \
    uvicorn examples.vla_server:create_app --factory

The factory returns an object with async predict(context) -> {actions, done}.
ROBOT_VLA_CODEC points to a JSON mapping accepted by JointActionCodec.
"""
import importlib
import json
import os
from pathlib import Path

from robot_vllm.vla import JointActionCodec, create_vla_app


def create_app():
    module, factory = os.environ["ROBOT_VLA_FACTORY"].split(":", 1)
    policy = getattr(importlib.import_module(module), factory)()
    codec = JointActionCodec(**json.loads(Path(os.environ["ROBOT_VLA_CODEC"]).read_text()))
    return create_vla_app(policy, codec, model=os.environ["ROBOT_VLA_MODEL"],
                          token=os.environ.get("ROBOT_VLA_TOKEN"))
