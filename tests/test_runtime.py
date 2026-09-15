from dataclasses import asdict, replace
from pathlib import Path
import time

import pytest

from robot_vllm.libero import filter_visible
from robot_vllm.policies import CodePolicy
from robot_vllm.protocol import (Decision, Observation, Proposal,
                                 ProtocolError, ProviderError, validate_proposal)
from robot_vllm.runtime import RunStore, Runtime


class FixtureEnvironment:
    """Contract fixture. Its results are never LIBERO simulation evidence."""
    backend_name = "fixture"

    def __init__(self, verdict=False):
        self.verdict = verdict
        self.version = 0
        self.actions = []
        self.judge_calls = 0
        self.closed = False

    def obs(self):
        return Observation(self.episode_id, self.version, time.monotonic(),
                           "move the robot", {}, {"robot0_eef_pos": [0.0, 0.0, 0.0]})

    def reset(self, episode_id, seed):
        self.episode_id = episode_id
        return self.obs()

    def step(self, action):
        self.actions.append(action)
        self.version += 1
        return self.obs()

    def final_verdict(self):
        self.judge_calls += 1
        return self.verdict

    def close(self):
        self.closed = True


def manifest(seed=1, init_index=None):
    return {"backend": "fixture", "task": "contract/0", "seed": seed,
            "init_index": init_index, "category": "contract_fixture"}


def test_closed_loop_preserves_evidence_and_judges_only_after_policy(tmp_path):
    env = FixtureEnvironment()
    class InspectPolicy(CodePolicy):
        def decide(self, observation, feedback):
            assert env.judge_calls == 0
            assert set(asdict(observation)) == {"episode_id", "version", "captured_at",
                                               "instruction", "images", "proprio"}
            if feedback:
                assert feedback.status == "completed"
                assert feedback.observation_version == observation.version
            return super().decide(observation, feedback)
    result = Runtime(RunStore(tmp_path)).run(lambda _: env, InspectPolicy(), manifest())
    assert result["status"] == "failure"
    assert result["task_denominator"] == 1
    assert result["metrics"]["executed_steps"] == 12
    assert env.closed and env.judge_calls == 1
    directory = Path(result["run_dir"])
    visible = (directory / "events.jsonl").read_text()
    assert "simulator_verdict" not in visible and '"reward"' not in visible
    assert len((directory / "trajectory.jsonl").read_text().splitlines()) == 12


@pytest.mark.parametrize("mutation,reason", [
    ({"based_on": 9}, "stale_observation"),
    ({"episode_id": "elsewhere"}, "wrong_episode"),
    ({"expires_at": 0}, "expired_proposal"),
    ({"action_spec": "joint_position"}, "incompatible_capabilities"),
    ({"resources": ("arm",)}, "incompatible_capabilities"),
    ({"payload": {"actions": [[0] * 8]}}, "invalid_action_dimension"),
    ({"payload": {"actions": [[0] * 6 + [float("nan")]]}}, "invalid_action_value"),
    ({"payload": {"actions": [[0] * 6 + [float("inf")]]}}, "invalid_action_value"),
    ({"payload": {"actions": [[0] * 6 + [2]]}}, "invalid_action_value"),
    ({"payload": {"actions": [[0] * 6 + [True]]}}, "invalid_action_value"),
])
def test_rejected_action_never_reaches_environment(tmp_path, mutation, reason):
    env = FixtureEnvironment()
    class BadPolicy:
        name = "invalid-fixture"
        def decide(self, observation, feedback):
            p = Proposal.for_observation(observation, "action_chunk", {"actions": [[0] * 7]})
            return Decision(replace(p, **mutation))
    result = Runtime(RunStore(tmp_path)).run(lambda _: env, BadPolicy(), manifest())
    assert env.actions == []
    assert result["status"] == "failure"
    assert result["reason"] == "rejection_budget"
    assert reason in (Path(result["run_dir"]) / "events.jsonl").read_text()


def test_whole_chunk_validated_before_first_step(tmp_path):
    env = FixtureEnvironment()
    class BadTail:
        name = "bad-tail"
        def decide(self, obs, feedback):
            return Decision(Proposal.for_observation(obs, "action_chunk",
                           {"actions": [[0] * 7, [0] * 6 + [100]]}))
    Runtime(RunStore(tmp_path)).run(lambda _: env, BadTail(), manifest())
    assert env.actions == []


def test_duplicate_proposal_rejected():
    obs = Observation("e", 0, time.monotonic(), "task", {}, {})
    p = Proposal.for_observation(obs, "action_chunk", {"actions": [[0] * 7]})
    with pytest.raises(ProtocolError, match="duplicate_proposal"):
        validate_proposal(p, obs, {p.proposal_id}, 8)


def test_runtime_deadline_cannot_be_extended_by_policy(tmp_path):
    env = FixtureEnvironment()
    class SlowPolicy:
        name = "slow-fixture"
        def decide(self, obs, feedback):
            time.sleep(0.02)
            return Decision(Proposal.for_observation(obs, "action_chunk",
                            {"actions": [[0] * 7]}, ttl_s=100000))
    result = Runtime(RunStore(tmp_path), proposal_ttl_s=0.005).run(
        lambda _: env, SlowPolicy(), manifest())
    assert result["metrics"]["executed_steps"] == 0


def test_provider_failure_is_excluded_and_environment_closed(tmp_path):
    env = FixtureEnvironment()
    class BrokenProvider:
        name = "offline-fixture"
        def decide(self, observation, feedback):
            raise ProviderError("connection refused")
    result = Runtime(RunStore(tmp_path)).run(lambda _: env, BrokenProvider(), manifest())
    assert result["status"] == "infra" and result["task_denominator"] == 0
    assert env.closed and env.judge_calls == 0


def test_success_resume_and_canonical_init_protection(tmp_path):
    runtime = Runtime(RunStore(tmp_path))
    result = runtime.run(lambda _: FixtureEnvironment(True), CodePolicy(), manifest(1, 2))
    assert result["status"] == "success"
    with pytest.raises(RuntimeError, match="already_successful"):
        runtime.run(lambda _: pytest.fail("must not construct env"), CodePolicy(), manifest(99, 2))


def test_concurrent_claim_and_crashed_claim(tmp_path):
    store = RunStore(tmp_path)
    directory, lock = store.create(manifest())
    with pytest.raises(RuntimeError, match="already_running"):
        store.create(manifest())
    lock.close()
    second, lock2 = store.create(manifest())
    assert second != directory and (directory / "manifest.json").exists()
    lock2.close()


def test_observation_allowlist_blocks_hidden_state():
    import numpy as np
    raw = {"agentview_image": np.zeros((2, 2, 3), dtype=np.uint8),
           "robot0_joint_pos": [1, 2], "robot0_hidden_goal": [42],
           "milk_pos": [1, 2, 3], "success": True, "reward": 1}
    images, proprio = filter_visible(raw)
    assert set(images) == {"agentview"}
    assert set(proprio) == {"robot0_joint_pos"}


def test_simulator_infrastructure_exception_is_not_failure(tmp_path):
    class BrokenSimulator(FixtureEnvironment):
        def step(self, action):
            raise RuntimeError("rendering unavailable")
    env = BrokenSimulator()
    result = Runtime(RunStore(tmp_path)).run(lambda _: env, CodePolicy(), manifest())
    assert result["status"] == "infra" and result["task_denominator"] == 0
    assert env.judge_calls == 0 and env.closed
