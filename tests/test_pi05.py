import asyncio

import numpy as np
import pytest

from robot_vllm.control import Rejected
from robot_vllm.pi05 import DroidMapping, OpenPiClient
from robot_vllm.protocol import ProviderError
from robot_vllm.testing.pi05_devices import fixture_image


def mapping():
    return DroidMapping(arm_capability="arm.trajectory", gripper_capability="hand.gripper",
        joint_names=[f"j{i}" for i in range(7)], joint_limits=[[-2, 2]] * 7,
        velocity_scale_rad_s=[.2] * 7, gripper_open_m=.08, gripper_closed_m=0,
        step_s=.1, execution_horizon=4)


def data():
    return {"members": {"arm.trajectory": {"joint_names": [f"j{i}" for i in range(7)],
        "positions_rad": [.1] * 7, "images": {"front": fixture_image((30, 60, 90)), "wrist": fixture_image((90, 60, 30))}},
        "hand.gripper": {"position_m": .04}}}


def test_droid_velocities_are_integrated_and_gripper_is_meters():
    actions = np.ones((10, 8), dtype=np.float32) * .5
    actions[:, 7] = 1
    result = mapping().encode(actions, data())["members"]
    assert len(result["arm.trajectory"]["points"]) == 4
    assert result["arm.trajectory"]["points"][0]["positions"] == pytest.approx([.11] * 7)
    assert result["arm.trajectory"]["points"][-1]["positions"] == pytest.approx([.14] * 7)
    assert result["hand.gripper"]["position_m"] == 0


def test_gripper_transition_ends_prefix_for_fresh_observation():
    actions = np.zeros((10, 8))
    actions[2:, 7] = 1
    result = mapping().encode(actions, data())["members"]
    assert len(result["arm.trajectory"]["points"]) == 2
    assert result["hand.gripper"]["position_m"] == .08


def test_openpi_droid_observation_has_official_keys_and_shapes():
    pytest.importorskip("openpi_client")
    observation = mapping().observation({"task": "ignored", "node": {"instruction": "pick up the cup"},
                                        "observation": {"data": data()}})
    assert observation["prompt"] == "pick up the cup"
    assert observation["observation/joint_position"].shape == (7,)
    assert observation["observation/gripper_position"] == pytest.approx([.5])
    for name in ("observation/exterior_image_1_left", "observation/wrist_image_left"):
        assert observation[name].shape == (224, 224, 3) and observation[name].dtype == np.uint8


@pytest.mark.parametrize("positions", [[], [0] * 8, [None] * 7, [True] * 7, [float("nan")] * 7])
def test_missing_or_invalid_joint_data_is_not_zero_filled(positions):
    observed = data()
    observed["members"]["arm.trajectory"]["positions_rad"] = positions
    with pytest.raises(Rejected):
        mapping().encode(np.zeros((4, 8)), observed)


def test_velocity_limit_requires_explicit_clipping_choice():
    actions = np.zeros((10, 8))
    actions[0, 0] = 1.1
    with pytest.raises(Rejected, match="velocity_out_of_range"):
        mapping().encode(actions, data())


def test_official_openpi_client_is_invoked_over_websocket(monkeypatch):
    native = pytest.importorskip("openpi_client.websocket_client_policy")
    msgpack_numpy = pytest.importorskip("openpi_client.msgpack_numpy")
    from websockets.asyncio.server import serve
    calls = []
    original = native.WebsocketClientPolicy.infer
    def infer(self, observed):
        calls.append(type(self).__mro__)
        return original(self, observed)
    monkeypatch.setattr(native.WebsocketClientPolicy, "infer", infer)
    async def scenario():
        async def handler(ws):
            await ws.send(msgpack_numpy.packb({"fixture": True}))
            observed = msgpack_numpy.unpackb(await ws.recv())
            assert observed["observation/joint_position"].shape == (7,)
            await ws.send(msgpack_numpy.packb({"actions": np.zeros((10, 8), dtype=np.float32)}))
        async with serve(handler, "127.0.0.1", 0, compression=None) as server:
            client = OpenPiClient(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", timeout_s=2)
            result = await client.infer({"observation/joint_position": np.zeros(7)})
            assert result["actions"].shape == (10, 8)
            assert client.metadata == {"fixture": True}
        assert len(calls) == 1 and native.WebsocketClientPolicy in calls[0]
    asyncio.run(scenario())


def test_native_client_timeout_is_bounded():
    pytest.importorskip("openpi_client")
    from websockets.asyncio.server import serve
    async def scenario():
        async def handler(ws):
            await ws.wait_closed()  # No metadata frame: constructor must not wait forever.
        async with serve(handler, "127.0.0.1", 0) as server:
            client = OpenPiClient(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", timeout_s=.1)
            with pytest.raises(ProviderError, match="openpi_client_error"):
                await asyncio.wait_for(client.infer({}), timeout=2)
    asyncio.run(scenario())
