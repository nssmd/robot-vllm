import pytest


def test_mujoco_plant_limits_are_radians_and_feedback_is_dynamics():
    mujoco = pytest.importorskip("mujoco")
    import numpy as np
    from robot_vllm.physics_controller import MODEL_XML
    model = mujoco.MjModel.from_xml_string(MODEL_XML)
    data = mujoco.MjData(model)
    assert np.allclose(model.jnt_range, [[-1, 1], [-1, 1]])
    target = np.array([0.2, -0.15])
    data.ctrl[:] = target
    mujoco.mj_step(model, data)
    assert not np.allclose(data.qpos, target)  # feedback is not copied from the target
    for _ in range(400):
        mujoco.mj_step(model, data)
    assert np.max(np.abs(data.qpos - target)) < 0.025


def test_completed_sse_response_is_required(monkeypatch):
    import io
    import json
    from robot_vllm.policies import post_json
    from robot_vllm.protocol import ProviderError

    class Response(io.BytesIO):
        headers = {"Content-Type": "text/event-stream"}

    payload = {"model": "gpt-6-astra", "output": [], "usage": {"total_tokens": 10}}
    class Opener:
        def open(self, *args, **kwargs):
            return Response(('data: ' + json.dumps({"type": "response.completed", "response": payload}) + '\n\n').encode())
    monkeypatch.setattr("robot_vllm.policies.build_opener", lambda *args: Opener())
    assert post_json("http://unused.invalid", {}, None, 1) == payload
    class Incomplete:
        def open(self, *args, **kwargs):
            return Response(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n')
    monkeypatch.setattr("robot_vllm.policies.build_opener", lambda *args: Incomplete())
    with pytest.raises(ProviderError, match="missing_completion"):
        post_json("http://unused.invalid", {}, None, 1)


def test_dripping_stream_without_newline_cannot_extend_provider_deadline(monkeypatch):
    import io
    from types import SimpleNamespace
    from robot_vllm.policies import post_json
    from robot_vllm.protocol import ProviderError
    elapsed = [0.0]
    class Drip(io.BytesIO):
        headers = {"Content-Type": "text/event-stream"}
        def read1(self, size):
            elapsed[0] += 0.2
            return b"x"
    class Opener:
        def open(self, *args, **kwargs):
            return Drip()
    monkeypatch.setattr("robot_vllm.policies.build_opener", lambda *args: Opener())
    monkeypatch.setattr("robot_vllm.policies.time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    with pytest.raises(ProviderError, match="transport_or_envelope"):
        post_json("http://unused.invalid", {}, None, 0.3)
    assert elapsed[0] <= 0.4
