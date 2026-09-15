"""Policy-visible data only; no simulator references or evaluator state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import time
from typing import Any, Protocol
import uuid


ACTION_SPEC = "libero.panda.osc_pose.normalized.v1"
SCHEMA = "robot_vllm.v1"
RESOURCES = ("arm", "gripper")


class ProtocolError(ValueError):
    """Rejected policy output; must not cause an action."""


class ProviderError(RuntimeError):
    """Provider/transport failure, excluded from task denominators."""


@dataclass(frozen=True)
class Observation:
    episode_id: str
    version: int
    captured_at: float
    instruction: str
    images: dict[str, str]  # immutable PNG files written by the observation service
    proprio: dict[str, list[float]]


@dataclass(frozen=True)
class Feedback:
    status: str
    reason: str
    observation_version: int
    executed_steps: int = 0
    proposal_id: str | None = None


@dataclass(frozen=True)
class Usage:
    model_calls: int = 0
    vlm_calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class Proposal:
    episode_id: str
    based_on: int
    expires_at: float
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    action_spec: str = ACTION_SPEC
    resources: tuple[str, ...] = RESOURCES
    proposal_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @classmethod
    def for_observation(cls, obs: Observation, kind: str,
                        payload: dict[str, Any] | None = None, ttl_s: float = 60.0):
        # The deadline starts at observation capture, not after inference returns.
        return cls(obs.episode_id, obs.version, obs.captured_at + ttl_s,
                   kind, payload or {})


@dataclass(frozen=True)
class Decision:
    proposal: Proposal
    usage: Usage = field(default_factory=Usage)


class Policy(Protocol):
    name: str
    def decide(self, observation: Observation, feedback: Feedback | None) -> Decision: ...


class Environment(Protocol):
    backend_name: str
    def reset(self, episode_id: str, seed: int) -> Observation: ...
    def step(self, action: list[float]) -> Observation: ...
    def final_verdict(self) -> bool: ...
    def close(self) -> None: ...


def validate_actions(value: Any, max_chunk: int) -> list[list[float]]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= max_chunk:
        raise ProtocolError("invalid_chunk_length")
    result = []
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) != 7:
            raise ProtocolError("invalid_action_dimension")
        if any(isinstance(x, bool) or not isinstance(x, (int, float))
               or not math.isfinite(x) or not -1 <= x <= 1 for x in row):
            raise ProtocolError("invalid_action_value")
        result.append([float(x) for x in row])
    return result


def expand_skill(payload: dict[str, Any], max_chunk: int) -> list[list[float]]:
    name = payload.get("name")
    args = payload.get("arguments", {})
    if not isinstance(args, dict):
        raise ProtocolError("invalid_skill_arguments")
    steps = args.get("steps", 4)
    if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= max_chunk:
        raise ProtocolError("invalid_skill_steps")
    if name == "gripper":
        if set(args) - {"steps", "closed"} or type(args.get("closed")) is not bool:
            raise ProtocolError("invalid_gripper_arguments")
        action = [0.0] * 6 + [1.0 if args["closed"] else -1.0]
    elif name == "cartesian_delta":
        if set(args) - {"steps", "action"}:
            raise ProtocolError("invalid_delta_arguments")
        action = args.get("action")
    else:
        raise ProtocolError("unknown_skill")
    return validate_actions([action] * steps, max_chunk)


def validate_proposal(p: Proposal, obs: Observation, seen: set[str],
                      max_chunk: int, now: float | None = None) -> list[list[float]]:
    now = time.monotonic() if now is None else now
    if not isinstance(p, Proposal) or not isinstance(p.proposal_id, str) or not p.proposal_id:
        raise ProtocolError("invalid_proposal")
    if p.proposal_id in seen:
        raise ProtocolError("duplicate_proposal")
    if p.episode_id != obs.episode_id:
        raise ProtocolError("wrong_episode")
    if type(p.based_on) is not int or p.based_on != obs.version:
        raise ProtocolError("stale_observation")
    if not isinstance(p.expires_at, (int, float)) or not math.isfinite(p.expires_at) or now >= p.expires_at:
        raise ProtocolError("expired_proposal")
    if p.action_spec != ACTION_SPEC or p.resources != RESOURCES:
        raise ProtocolError("incompatible_capabilities")
    if not isinstance(p.payload, dict):
        raise ProtocolError("invalid_payload")
    if p.kind == "stop":
        return []
    if p.kind == "skill":
        return expand_skill(p.payload, max_chunk)
    if p.kind == "action_chunk":
        return validate_actions(p.payload.get("actions"), max_chunk)
    raise ProtocolError("unknown_command")


def public_record(value: Observation | Feedback | Proposal) -> dict:
    return asdict(value)
