"""Code and HTTP model policies implement the same data-only decision interface."""

from __future__ import annotations

import base64
from dataclasses import asdict
import json
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .protocol import (ACTION_SPEC, SCHEMA, Decision, Feedback, Observation, Proposal,
                       ProviderError, Usage)


def model_observation(obs: Observation) -> dict:
    """Explicit allowlist; no run manifest, seed, BDDL, reward or evaluator fields."""
    return {"schema": SCHEMA, "episode_id": obs.episode_id,
            "observation_version": obs.version, "instruction": obs.instruction,
            "proprio": obs.proprio, "action_spec": ACTION_SPEC,
            "images": {name: {"mime_type": "image/png", "encoding": "base64",
                              "data": base64.b64encode(Path(path).read_bytes()).decode("ascii")}
                       for name, path in obs.images.items()}}


def post_json(url: str, payload: dict, api_key: str | None, timeout_s: float) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("endpoint must be an HTTP(S) URL without embedded credentials")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = Request(url, json.dumps(payload, allow_nan=False).encode(), headers, method="POST")
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    deadline = time.monotonic() + timeout_s
    def chunks(response):
        total = 0
        read = getattr(response, "read1", response.read)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("provider_wall_deadline")
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(remaining)
            chunk = read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 16 * 1024 * 1024:
                raise ProviderError("response_too_large")
            yield chunk
    def lines(response):
        pending = b""
        for chunk in chunks(response):
            pending += chunk
            parts = pending.split(b"\n")
            pending = parts.pop()
            for part in parts:
                yield part
        if pending:
            yield pending
    try:
        with build_opener(NoRedirect).open(request, timeout=timeout_s) as response:
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                event_lines = []
                for line in lines(response):
                    stripped = line.decode("utf-8").rstrip("\r\n")
                    if stripped.startswith("data:"):
                        event_lines.append(stripped[5:].lstrip())
                    elif not stripped and event_lines:
                        data = "\n".join(event_lines)
                        event_lines = []
                        if data == "[DONE]":
                            break
                        event = json.loads(data)
                        if event.get("type") == "response.completed":
                            result = event.get("response")
                            if not isinstance(result, dict):
                                raise ProviderError("invalid_completed_response")
                            return result
                        if event.get("type") in ("error", "response.failed", "response.incomplete"):
                            raise ProviderError("provider_stream_failed")
                raise ProviderError("provider_stream_missing_completion")
            body = b"".join(chunks(response))
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ProviderError("response_not_object")
            return result
    except HTTPError as exc:
        code, param = "unknown", "unknown"
        try:
            error = json.loads(exc.read(8192)).get("error", {})
            for key in ("code", "param"):
                value = error.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
                    if key == "code":
                        code = value
                    else:
                        param = value
        except (ValueError, TypeError, AttributeError):
            pass
        raise ProviderError(f"provider_http_{exc.code}:{code}:{param}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise ProviderError("provider_transport_or_envelope_error") from exc


def token_usage(response: dict, vlm: bool) -> Usage:
    raw = response.get("usage") or {}
    if not isinstance(raw, dict):
        raw = {}
    def count(key):
        value = raw.get(key)
        return value if type(value) is int and value >= 0 else None
    return Usage(1, int(vlm), count("prompt_tokens"), count("completion_tokens"), count("total_tokens"))


class CodePolicy:
    """A control-path diagnostic, not a task-solving policy or a trained VLA."""
    name = "code-smoke"

    def decide(self, observation: Observation, feedback: Feedback | None) -> Decision:
        # Responds to the actual observation version; no simulator access.
        phase = observation.version // 4
        if phase == 0:
            kind, payload = "skill", {"name": "gripper", "arguments": {"closed": False, "steps": 4}}
        elif phase == 1:
            kind, payload = "action_chunk", {"actions": [[0, 0, 0.05, 0, 0, 0, -1]] * 4}
        elif phase == 2:
            kind, payload = "skill", {"name": "gripper", "arguments": {"closed": True, "steps": 4}}
        else:
            kind, payload = "stop", {}
        return Decision(Proposal.for_observation(observation, kind, payload))


class ChatPolicy:
    """OpenAI-compatible multimodal Chat Completions adapter (not all vendor APIs)."""
    def __init__(self, endpoint: str, model: str, *, key_env="ROBOT_VLLM_API_KEY",
                 timeout_s=30.0, ttl_s=60.0):
        self.endpoint, self.model = endpoint, model
        self.key_env, self.timeout_s, self.ttl_s = key_env, timeout_s, ttl_s
        self.name = "chat:" + model
        self.last_usage = Usage()

    def decide(self, observation: Observation, feedback: Feedback | None) -> Decision:
        visible = model_observation(observation)
        images = visible.pop("images")
        content = [{"type": "text", "text": json.dumps({"observation": visible,
                    "feedback": asdict(feedback) if feedback else None})}]
        for name, frame in images.items():
            content.extend([{"type": "text", "text": "camera: " + name},
                            {"type": "image_url", "image_url": {
                                "url": "data:image/png;base64," + frame["data"]}}])
        prompt = (
            "Control the robot using only supplied observations and execution feedback. "
            "Return one JSON object: {kind: skill, name: gripper, arguments: {closed: boolean, steps: integer}} "
            "or {kind: skill, name: cartesian_delta, arguments: {action: [7 numbers], steps: integer}} "
            "or {kind: stop}. Use valid JSON with quoted keys. "
            "steps must be 1..8. Action specification is " + ACTION_SPEC + ". "
            "Panda OSC_POSE at 20 Hz: normalized dx,dy,dz,droll,dpitch,dyaw,gripper in [-1,1]. "
            "These are normalized controller inputs, not meters or radians. "
            "Gripper +1 closes and -1 opens. Images are upright RGB. "
            "Stop ends your control loop; task success is evaluated independently afterward."
        )
        self.last_usage = Usage(1, 1)
        response = post_json(self.endpoint, {"model": self.model, "messages": [
            {"role": "system", "content": prompt}, {"role": "user", "content": content}],
            "response_format": {"type": "json_object"}}, os.environ.get(self.key_env), self.timeout_s)
        usage = self.last_usage = token_usage(response, True)
        try:
            command = json.loads(response["choices"][0]["message"]["content"])
            kind = command["kind"]
            payload = {"name": command.get("name"), "arguments": command.get("arguments", {})}
            if kind not in ("skill", "stop"):
                kind = "invalid_chat_command"
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            kind, payload = "invalid_chat_response", {}
        return Decision(Proposal.for_observation(observation, kind, payload, self.ttl_s), usage)


class VLAPolicy:
    """Our explicit HTTP contract; native model servers need a matching wrapper."""
    def __init__(self, endpoint: str, model: str, *, key_env="ROBOT_VLLM_VLA_KEY",
                 timeout_s=30.0, ttl_s=60.0):
        self.endpoint, self.model = endpoint, model
        self.key_env, self.timeout_s, self.ttl_s = key_env, timeout_s, ttl_s
        self.name = "vla:" + model
        self.last_usage = Usage()

    def decide(self, observation: Observation, feedback: Feedback | None) -> Decision:
        payload = {"model": self.model, "observation": model_observation(observation),
                   "feedback": asdict(feedback) if feedback else None,
                   "capabilities": {"action_spec": ACTION_SPEC, "max_chunk": 8, "control_hz": 20}}
        self.last_usage = Usage(1, 0)
        response = post_json(self.endpoint, payload, os.environ.get(self.key_env), self.timeout_s)
        usage = self.last_usage = token_usage(response, False)
        # Version echo is mandatory; a cached response from an old frame must fail closed.
        kind = "action_chunk" if response.get("schema") == SCHEMA else "invalid_vla_schema"
        p = Proposal(response.get("episode_id", "missing"), response.get("observation_version", -1),
                     observation.captured_at + self.ttl_s, kind,
                     {"actions": response.get("actions")},
                     action_spec=response.get("action_spec", "missing"))
        return Decision(p, usage)


class AlternatingPolicy:
    """Minimal composition at completed block boundaries, not a learned router."""
    def __init__(self, first, second):
        self.policies = (first, second)
        self.index = 0
        self.last_completed = None
        self.name = "alternating(" + first.name + "," + second.name + ")"
        self.last_usage = Usage()

    def decide(self, observation, feedback):
        if feedback and feedback.status == "completed" and feedback.proposal_id != self.last_completed:
            self.index = 1 - self.index
            self.last_completed = feedback.proposal_id
        policy = self.policies[self.index]
        self.last_usage = Usage()
        try:
            return policy.decide(observation, feedback)
        finally:
            self.last_usage = getattr(policy, "last_usage", Usage())
