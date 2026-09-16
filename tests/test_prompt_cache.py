import asyncio
import json

import pytest

from robot_vllm.models import ModelEndpoint, ModelMeter, cache_text_blocks, multimodal_content
from robot_vllm.runtime import Journal


def context(trial):
    return {"schema": "robot_runtime.planning_context.v1", "task_id": f"trial-{trial}",
            "task": "reach target", "capabilities": [{"name": "arm", "units": "meters"}],
            "topology": {"robots": ["a", "b"]}, "policies": ["code"],
            "observations": {"position": trial}, "completed": {}, "previous_result": None}


def test_reordered_prefix_preserves_all_fields_and_fresh_trial_suffix():
    a = context(1)
    b = context(2)
    x, _ = multimodal_content(a, cache_layout=True)
    y, _ = multimodal_content(b, cache_layout=True)
    assert json.loads(x) == a and json.loads(y) == b
    assert x.split('"completed"')[0] == y.split('"completed"')[0]
    first, _ = cache_text_blocks(a)
    second, _ = cache_text_blocks(b)
    assert first[0] == second[0] and first[1] != second[1]
    assert 'trial-' not in first[0] and 'observations' not in first[0]
    assert 'trial-2' in second[1]


@pytest.mark.parametrize("details", [None, {}, {"cached_tokens": True}, {"cached_tokens": -1}, {"cached_tokens": 11}])
def test_missing_or_invalid_cache_usage_is_not_reported_as_zero(tmp_path, details):
    journal = Journal(tmp_path / "log.jsonl")
    meter = ModelMeter(journal)
    meter.record(model="test", role="planner", kind="openai_responses", elapsed=1,
                 status="completed", usage={"input_tokens": 10, "input_tokens_details": details})
    assert meter.summary()["cache_unmetered_call_count"] == 1
    assert meter.summary()["cache_hit_token_ratio"] is None
    journal.close()


@pytest.mark.parametrize("kind", ["openai_chat", "openai_responses"])
def test_actual_wire_breakpoint_and_usage_mapping(monkeypatch, tmp_path, kind):
    requests = []
    def post(url, body, key, timeout):
        requests.append(body)
        usage = ({"prompt_tokens": 2000, "completion_tokens": 10, "total_tokens": 2010,
                  "prompt_tokens_details": {"cached_tokens": 1600}} if kind == "openai_chat" else
                 {"input_tokens": 2000, "output_tokens": 10, "total_tokens": 2010,
                  "input_tokens_details": {"cached_tokens": 1600, "cache_write_tokens": 0}})
        return {"usage": usage, "choices": [{"message": {"content": '{"ok":true}'}}],
                "output_text": '{"ok":true}'}
    monkeypatch.setattr("robot_vllm.models.post_json", post)
    async def scenario():
        journal = Journal(tmp_path / "calls.jsonl")
        meter = ModelMeter(journal)
        endpoint = ModelEndpoint("planner", {"kind": kind, "model": "test", "endpoint": "http://unused.invalid",
            "cache_layout": True, "cache_breakpoint": True, "prompt_cache_key": "same-task", "max_output_tokens": 512})
        for trial in (1, 2):
            await endpoint.generate(instruction="return JSON", context=context(trial), role="planner", meter=meter)
        field = "messages" if kind == "openai_chat" else "input"
        blocks = [r[field][-1]["content"] for r in requests]
        assert blocks[0][0] == blocks[1][0]
        assert blocks[0][1] != blocks[1][1]
        assert blocks[0][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert "prompt_cache_breakpoint" not in blocks[0][1]
        assert requests[0]["prompt_cache_key"] == requests[1]["prompt_cache_key"] == "same-task"
        assert requests[0]["max_output_tokens" if kind == "openai_responses" else "max_completion_tokens"] == 512
        if kind == "openai_responses":
            assert blocks[0][0]["text"].startswith("Return JSON.")
        assert meter.summary()["cached_input_tokens"] == 3200
        assert meter.summary()["uncached_input_tokens"] == 800
        assert meter.summary()["cache_hit_token_ratio"] == .8
        journal.close()
    asyncio.run(scenario())
