"""Explicit joint-action conversion and an optional model-serving bridge.

Weights and embodiment mappings belong to the operator. No implicit scaling,
joint reordering, padding, clipping, or missing-state replacement is performed.
"""
import asyncio
import math

from .control import Rejected


class JointActionCodec:
    def __init__(self, joint_names, limits, *, step_s=0.1, mode="absolute", max_chunk=16):
        if (not isinstance(joint_names, list) or not joint_names or any(not isinstance(j, str) or not j for j in joint_names)
                or len(set(joint_names)) != len(joint_names) or len(limits) != len(joint_names)):
            raise ValueError("invalid_codec_joint_mapping")
        if mode not in ("absolute", "delta"):
            raise ValueError("explicit_absolute_or_delta_mode_required")
        if type(max_chunk) is not int or not 1 <= max_chunk <= 100:
            raise ValueError("invalid_max_chunk")
        if type(step_s) not in (int, float) or not math.isfinite(step_s) or not 0.01 <= step_s <= 30 / max_chunk:
            raise ValueError("invalid_codec_step")
        for bounds in limits:
            if (len(bounds) != 2 or any(type(x) not in (int, float) or not math.isfinite(x) for x in bounds)
                    or bounds[0] >= bounds[1]):
                raise ValueError("invalid_codec_limits")
        self.joints, self.limits = list(joint_names), [list(b) for b in limits]
        self.step_s, self.mode, self.max_chunk = step_s, mode, max_chunk

    def encode(self, actions, observation):
        if hasattr(actions, "tolist"):
            actions = actions.tolist()
        if not isinstance(actions, list) or not 1 <= len(actions) <= self.max_chunk:
            raise Rejected("invalid_action_chunk_length")
        if observation.get("joint_names") != self.joints:
            raise Rejected("observation_joint_order_mismatch")
        position = observation.get("positions_rad")
        if (not isinstance(position, list) or len(position) != len(self.joints)
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in position)):
            raise Rejected("measured_joint_state_required")
        points = []
        for index, action in enumerate(actions):
            if (not isinstance(action, list) or len(action) != len(self.joints)
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in action)):
                raise Rejected("invalid_action_array")
            position = [a + q for a, q in zip(action, position)] if self.mode == "delta" else list(action)
            if any(not lo <= value <= hi for value, (lo, hi) in zip(position, self.limits)):
                raise Rejected("joint_limit_exceeded")
            points.append({"positions": list(position), "time_from_start_s": (index + 1) * self.step_s})
        return {"points": points}


def create_vla_app(policy, codec, *, model, token=None, max_concurrency=1):
    """Wrap a trusted async policy.predict(context) -> {actions, done} implementation.

The policy performs its own model-specific image/state preprocessing. This server
does not execute robot actions. Use an ASGI server behind a bounded request proxy.
"""
    from fastapi import FastAPI, HTTPException, Request
    import secrets
    if not isinstance(model, str) or not model or type(max_concurrency) is not int or not 1 <= max_concurrency <= 32:
        raise ValueError("invalid_vla_server_configuration")
    app = FastAPI(title="Robot Runtime VLA bridge")
    active = 0
    gate = asyncio.Lock()

    @app.post("/predict")
    async def predict(request: Request):
        nonlocal active
        if token and not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "invalid authorization")
        async with gate:
            if active >= max_concurrency:
                raise HTTPException(429, "vla_capacity_exceeded")
            active += 1
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 8_388_608:
                    raise HTTPException(413, "request_too_large")
                raw.extend(chunk)
            import json
            body = json.loads(raw)
            if body.get("schema") != "robot_runtime.vla_request.v1" or body.get("model") != model:
                raise Rejected("vla_contract_or_model_mismatch")
            context = body["context"]
            observation = context["observation"]
            prediction = await policy.predict(context)
            if not isinstance(prediction, dict) or set(prediction) != {"actions", "done"} or type(prediction["done"]) is not bool:
                raise Rejected("invalid_policy_prediction")
            actions = prediction["actions"]
            if actions is None and not prediction["done"]:
                raise Rejected("empty_nonterminal_prediction")
            arguments = None if actions is None else codec.encode(actions, observation["data"])
            return {"schema": "robot_runtime.node_proposal.v1", "model": model,
                "observation_id": observation["observation_id"], "capability": context["node"]["capability"],
                "arguments": arguments, "done": prediction["done"]}
        except (Rejected, KeyError, ValueError, TypeError) as exc:
            raise HTTPException(422, "invalid_vla_request_or_prediction:" + type(exc).__name__) from exc
        finally:
            async with gate:
                active -= 1
    return app
