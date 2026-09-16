import asyncio

import httpx
import numpy as np
import pytest

from robot_vllm.control import Rejected
from robot_vllm.protocol import ProviderError, ProviderTimeout
from robot_vllm.vla import JointActionCodec, create_vla_app


def test_explicit_delta_chunk_accumulates_from_measured_state():
    codec = JointActionCodec(["a", "b"], [[-1, 1], [-1, 1]], mode="delta", step_s=0.1)
    result = codec.encode(np.array([[0.1, -0.1], [0.2, 0.1]]),
        {"joint_names": ["a", "b"], "positions_rad": [0.2, 0.3]})
    assert result["points"][0]["positions"] == pytest.approx([0.3, 0.2])
    assert result["points"][1]["positions"] == pytest.approx([0.5, 0.3])
    assert result["points"][1]["time_from_start_s"] == 0.2


@pytest.mark.parametrize("actions", [[], [[0]], [[None, 0]], [[float("nan"), 0]], [[True, 0]], [[2, 0]]])
def test_invalid_model_actions_are_rejected_not_padded_or_clipped(actions):
    codec = JointActionCodec(["a", "b"], [[-1, 1], [-1, 1]])
    with pytest.raises(Rejected):
        codec.encode(actions, {"joint_names": ["a", "b"], "positions_rad": [0, 0]})


def test_joint_order_mismatch_rejected():
    codec = JointActionCodec(["a", "b"], [[-1, 1], [-1, 1]])
    with pytest.raises(Rejected, match="joint_order"):
        codec.encode([[0, 0]], {"joint_names": ["b", "a"], "positions_rad": [0, 0]})


def test_vla_endpoint_preserves_observation_ticket_and_model_identity():
    class FixturePolicy:
        async def predict(self, context):
            return {"actions": np.array([[0.1, -0.2]]), "done": True}
    async def scenario():
        codec = JointActionCodec(["a", "b"], [[-1, 1], [-1, 1]])
        app = create_vla_app(FixturePolicy(), codec, model="explicit-test-fixture", token="test")
        body = {"schema": "robot_runtime.vla_request.v1", "model": "explicit-test-fixture", "context": {
            "node": {"capability": "arm.trajectory"}, "observation": {"observation_id": "fresh-ticket",
                "data": {"joint_names": ["a", "b"], "positions_rad": [0, 0]}}}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.post("/predict", json=body)).status_code == 401
            client.headers["authorization"] = "Bearer test"
            response = await client.post("/predict", json=body)
            assert response.status_code == 200
            result = response.json()
            assert result["observation_id"] == "fresh-ticket" and result["model"] == body["model"]
            assert result["arguments"]["points"][0]["positions"] == [0.1, -0.2]
    asyncio.run(scenario())


def request_body():
    return {"schema": "robot_runtime.vla_request.v1", "model": "fixture", "context": {
        "node": {"capability": "arm.trajectory"}, "observation": {"observation_id": "ticket",
            "data": {"joint_names": ["a"], "positions_rad": [0]}}}}


@pytest.mark.parametrize("field,value", [
    ("body", []), ("body", None), ("body", "invalid"),
    ("context", []), ("context", None),
    ("observation", []), ("node", []),
    ("data", None), ("observation_id", None), ("observation_id", ""),
    ("capability", None), ("capability", ""),
])
def test_invalid_envelope_is_rejected_before_model_call_and_releases_capacity(field, value):
    import json
    class Policy:
        calls = 0
        async def predict(self, context):
            self.calls += 1
            return {"actions": [[0]], "done": True}
    async def scenario():
        policy = Policy()
        app = create_vla_app(policy, JointActionCodec(["a"], [[-1, 1]]), model="fixture")
        body = request_body()
        if field == "body":
            body = value
        elif field == "context":
            body[field] = value
        elif field in ("observation", "node"):
            body["context"][field] = value
        elif field == "capability":
            body["context"]["node"][field] = value
        else:
            body["context"]["observation"][field] = value
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/predict", content=json.dumps(body), headers={"Content-Type": "application/json"})
            assert response.status_code == 422
            assert policy.calls == 0
            assert (await client.post("/predict", json=request_body())).status_code == 200
            assert policy.calls == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("error,status,detail", [
    (ProviderError, 502, "vla_provider_error"),
    (ProviderTimeout, 504, "vla_provider_timeout"),
    (TimeoutError, 504, "vla_provider_timeout"),
])
def test_provider_failure_has_safe_status_and_releases_capacity(error, status, detail):
    class Policy:
        calls = 0
        async def predict(self, context):
            self.calls += 1
            if self.calls == 1:
                raise error("private provider traceback /host/checkpoint")
            return {"actions": [[0]], "done": True}
    class Codec(JointActionCodec):
        calls = 0
        def encode(self, actions, observation):
            self.calls += 1
            return super().encode(actions, observation)
    async def scenario():
        policy, codec = Policy(), Codec(["a"], [[-1, 1]])
        app = create_vla_app(policy, codec, model="fixture")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/predict", json=request_body())
            assert response.status_code == status
            assert response.json() == {"detail": detail}
            assert codec.calls == 0
            assert (await client.post("/predict", json=request_body())).status_code == 200
            assert policy.calls == 2 and codec.calls == 1
    asyncio.run(scenario())
