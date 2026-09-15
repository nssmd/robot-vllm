"""Serial, block-boundary execution; post-hoc adjudication and append-only evidence."""

from __future__ import annotations

from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import shutil
import time
from typing import Callable
import uuid

from .protocol import (ACTION_SPEC, SCHEMA, Decision, Environment, Feedback, Policy,
                       ProtocolError, ProviderError, validate_proposal)
from . import __version__


def write_json(path: Path, data: dict) -> None:
    with path.open("x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())


class Journal:
    def __init__(self, path: Path):
        self.file = path.open("x", encoding="utf-8")
        self.sequence = 0

    def emit(self, event: str, **data):
        self.sequence += 1
        self.file.write(json.dumps({"sequence": self.sequence, "time": time.monotonic(),
                                   "event": event, **data}, ensure_ascii=False,
                                  allow_nan=False) + "\n")
        self.file.flush()

    def close(self):
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()


class RunStore:
    """Local/shared-filesystem registry, preserving all attempts.

    Lock scope is the configured output root. Different roots do not share protection.
    Canonical init_index replaces seed when a fixed init state is selected.
    """
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _lock(self):
        f = (self.root / "registry.lock").open("a+")
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    @staticmethod
    def key(manifest):
        init = manifest.get("init_index")
        return [manifest["backend"], manifest.get("environment_revision"),
                manifest["task"], ["init", init] if init is not None else ["seed", manifest["seed"]]]

    def create(self, manifest: dict) -> tuple[Path, object]:
        with self._lock():
            key = self.key(manifest)
            for directory in self.root.iterdir():
                mf = directory / "manifest.json"
                if not directory.is_dir() or not mf.exists():
                    continue
                previous = json.loads(mf.read_text())
                if self.key(previous) != key:
                    continue
                result = directory / "result.json"
                if result.exists():
                    record = json.loads(result.read_text())
                    if record["status"] == "success":
                        raise RuntimeError("already_successful_task_initialization")
                else:
                    # Check the attempt lock; a crashed process releases it automatically.
                    with (directory / "active.lock").open("a+") as prior_lock:
                        try:
                            fcntl.flock(prior_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError as exc:
                            raise RuntimeError("task_initialization_already_running") from exc
            episode_id = uuid.uuid4().hex
            directory = self.root / episode_id
            directory.mkdir()
            active = (directory / "active.lock").open("a+")
            fcntl.flock(active, fcntl.LOCK_EX | fcntl.LOCK_NB)
            source = directory / "runtime-source"
            source.mkdir()
            for path in Path(__file__).parent.glob("*.py"):
                shutil.copy2(path, source / path.name)
            write_json(directory / "manifest.json", {
                **manifest, "schema": SCHEMA, "episode_id": episode_id,
                "runtime_version": __version__, "action_spec": ACTION_SPEC,
                "created_at_unix": time.time(),
                "runtime_source_snapshot": "runtime-source",
            })
        return directory, active


def account_usage(metrics, usage, elapsed):
    metrics["model_calls"] += usage.model_calls
    metrics["vlm_calls"] += usage.vlm_calls
    if not usage.model_calls:
        return
    metrics["model_time_s"] += elapsed
    if usage.vlm_calls:
        metrics["vlm_time_s"] += elapsed
    if any(getattr(usage, k) is None for k in
           ("prompt_tokens", "completion_tokens", "total_tokens")):
        metrics["unmetered_call_count"] += usage.model_calls
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key)
        if type(value) is int and value >= 0:
            metrics[key] += value


class Runtime:
    def __init__(self, store: RunStore, *, max_steps=24, max_decisions=12,
                 max_chunk=8, max_rejections=3, proposal_ttl_s=60.0):
        if any(type(v) is not int or v <= 0 for v in
               (max_steps, max_decisions, max_chunk, max_rejections)):
            raise ValueError("runtime budgets must be positive integers")
        if not 0 < proposal_ttl_s <= 3600:
            raise ValueError("proposal TTL must be in (0, 3600]")
        self.store, self.max_steps, self.max_decisions = store, max_steps, max_decisions
        self.max_chunk, self.max_rejections = max_chunk, max_rejections
        self.proposal_ttl_s = proposal_ttl_s

    def run(self, env_factory: Callable[[Path], Environment], policy: Policy,
            manifest: dict) -> dict:
        manifest = {**manifest, "policy": policy.name,
                    "limits": {"steps": self.max_steps, "decisions": self.max_decisions,
                               "chunk": self.max_chunk, "rejections": self.max_rejections,
                               "proposal_ttl_s": self.proposal_ttl_s}}
        directory, active = self.store.create(manifest)
        journal = Journal(directory / "events.jsonl")
        trajectory = Journal(directory / "trajectory.jsonl")
        started = time.monotonic()
        metrics = {k: 0 for k in ("model_calls", "vlm_calls", "unmetered_call_count",
                                 "prompt_tokens", "completion_tokens", "total_tokens",
                                 "executed_steps", "decisions", "rejected_proposals")}
        metrics.update({k: 0.0 for k in ("model_time_s", "vlm_time_s", "perception_time_s",
                                       "action_time_s", "recovery_time_s", "policy_time_s")})
        env, observation, feedback = None, None, None
        status, reason, verdict = "infra", "not_started", None
        seen, consecutive_rejections = set(), 0
        try:
            env = env_factory(directory)
            tick = time.monotonic()
            observation = env.reset(directory.name, manifest["seed"])
            metrics["perception_time_s"] += time.monotonic() - tick
            journal.emit("observation", observation=asdict(observation))
            reason = "decision_budget"
            for _ in range(self.max_decisions):
                if metrics["executed_steps"] >= self.max_steps:
                    reason = "step_budget"
                    break
                metrics["decisions"] += 1
                tick = time.monotonic()
                try:
                    decision = policy.decide(observation, feedback)
                except ProviderError:
                    # Failed calls may still be billed. Preserve missing metering explicitly.
                    from .protocol import Usage
                    account_usage(metrics, getattr(policy, "last_usage", Usage()),
                                  time.monotonic() - tick)
                    raise
                finally:
                    elapsed = time.monotonic() - tick
                    metrics["policy_time_s"] += elapsed
                    if feedback is not None and feedback.status == "rejected":
                        metrics["recovery_time_s"] += elapsed
                if not isinstance(decision, Decision):
                    raise ProtocolError("invalid_decision")
                usage = decision.usage
                account_usage(metrics, usage, elapsed)
                proposal = decision.proposal
                try:
                    now = time.monotonic()
                    actions = validate_proposal(proposal, observation, seen, self.max_chunk, now)
                    if now >= observation.captured_at + self.proposal_ttl_s:
                        raise ProtocolError("runtime_observation_deadline")
                    if len(actions) > self.max_steps - metrics["executed_steps"]:
                        raise ProtocolError("remaining_step_budget")
                except ProtocolError as exc:
                    consecutive_rejections += 1
                    metrics["rejected_proposals"] += 1
                    feedback = Feedback("rejected", str(exc), observation.version)
                    journal.emit("feedback", feedback=asdict(feedback))
                    if consecutive_rejections >= self.max_rejections:
                        reason = "rejection_budget"
                        break
                    continue
                seen.add(proposal.proposal_id)
                consecutive_rejections = 0
                journal.emit("accepted", policy=policy.name, proposal=asdict(proposal), usage=asdict(usage))
                if proposal.kind == "stop":
                    reason = "policy_stop"
                    break
                steps = 0
                for action in actions:
                    tick = time.monotonic()
                    observation = env.step(action)
                    metrics["action_time_s"] += time.monotonic() - tick
                    # Environments may provide their own rendering/serialization measurement.
                    metrics["executed_steps"] += 1
                    steps += 1
                    trajectory.emit("step", proposal_id=proposal.proposal_id,
                                    action=action, observation=asdict(observation))
                feedback = Feedback("completed", "action_block_finished", observation.version,
                                    steps, proposal.proposal_id)
                journal.emit("feedback", feedback=asdict(feedback))
                journal.emit("observation", observation=asdict(observation))
            # The verdict never enters the policy-visible journal or another policy call.
            verdict = env.final_verdict()
            if type(verdict) is not bool:
                raise RuntimeError("invalid_simulator_verdict")
            status = "success" if verdict else "failure"
        except KeyboardInterrupt:
            status, reason = "infra", "interrupted"
        except ProviderError:
            status, reason = "infra", "provider_error"
        except ProtocolError as exc:
            status, reason = "infra", "policy_protocol_error:" + str(exc)
        except Exception as exc:
            # Avoid dumping provider bodies, credentials, or hidden simulator internals.
            status, reason = "infra", "runtime_error:" + type(exc).__name__
            import traceback
            write_json(directory / "infrastructure.json", {
                "exception_type": type(exc).__name__,
                "frames": [{"file": frame.filename, "line": frame.lineno, "function": frame.name}
                           for frame in traceback.extract_tb(exc.__traceback__)]})
        finally:
            if env is not None:
                try:
                    env.close()
                except Exception as exc:
                    status, reason = "infra", "close_error:" + type(exc).__name__
                if hasattr(env, "perception_time_s"):
                    # Adapter supplies exclusive action/perception buckets.
                    metrics["perception_time_s"] = env.perception_time_s
                    metrics["action_time_s"] = env.action_time_s
            metrics["wall_time_s"] = time.monotonic() - started
            metrics["token_totals_are_partial"] = metrics["unmetered_call_count"] > 0
            result = {"schema": SCHEMA, "episode_id": directory.name,
                      "status": status, "reason": reason,
                      "simulator_verdict": verdict,
                      "task_denominator": int(status in ("success", "failure")),
                      "category": manifest.get("category", "diagnostic"),
                      "metrics": metrics, "run_dir": str(directory.resolve())}
            write_json(directory / "result.json", result)
            journal.close()
            trajectory.close()
            active.close()
        return result
