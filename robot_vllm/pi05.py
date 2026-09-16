"""Pi 0.5 DROID adapter for OpenPI's native WebSocket/MessagePack interface.

DROID's eight outputs are normalized joint velocities and gripper position,
not eight joint angles. All robot conversions are explicit operator inputs.
"""
import asyncio
import base64
import io
import math
import os

from .control import Rejected
from .protocol import ProviderError, ProviderTimeout


class OpenPiClient:
    """Use OpenPI's official client, with bounded connection/receive operations.

    Only connection lifecycle is adapted; official infer() and msgpack_numpy
    perform all observation/action serialization. No alternate wire protocol.
    """
    def __init__(self, uri, *, timeout_s=30, api_key=None):
        if not isinstance(uri, str) or not uri.startswith(("ws://", "wss://")):
            raise ValueError("openpi_websocket_uri_required")
        if type(timeout_s) not in (float, int) or not 0 < timeout_s <= 120:
            raise ValueError("invalid_openpi_timeout")
        self.uri, self.timeout_s, self.api_key = uri, timeout_s, api_key
        self.metadata = None

    async def infer(self, observation):
        def exchange():
            from openpi_client import msgpack_numpy
            from openpi_client.websocket_client_policy import WebsocketClientPolicy
            from websockets.sync.client import connect
            timeout = self.timeout_s

            class BoundedConnection:
                def __init__(self, connection):
                    self.connection = connection
                def send(self, data):
                    return self.connection.send(data)
                def recv(self):
                    return self.connection.recv(timeout=timeout)
                def close(self):
                    return self.connection.close()

            class NativeClient(WebsocketClientPolicy):
                def _wait_for_server(native):
                    headers = {"Authorization": "Api-Key " + native._api_key} if native._api_key else None
                    connection = BoundedConnection(connect(native._uri, compression=None,
                        max_size=16 * 1024 * 1024, open_timeout=timeout, close_timeout=1,
                        additional_headers=headers))
                    try:
                        return connection, msgpack_numpy.unpackb(connection.recv())
                    except BaseException:
                        connection.close()
                        raise

            native = NativeClient(host=self.uri, api_key=self.api_key)
            try:
                # This is the upstream WebsocketClientPolicy.infer implementation.
                return native.infer(observation), native.get_server_metadata()
            finally:
                native._ws.close()
        try:
            value, metadata = await asyncio.to_thread(exchange)
            if not isinstance(value, dict) or "actions" not in value or not isinstance(metadata, dict):
                raise ProviderError("openpi_invalid_response")
            self.metadata = metadata
            return value
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderTimeout("openpi_client_timeout") from exc
        except Exception as exc:
            # Upstream server errors can contain tracebacks and host paths.
            raise ProviderError("openpi_client_error:" + type(exc).__name__) from exc


class DroidMapping:
    def __init__(self, *, arm_capability, gripper_capability, joint_names, joint_limits,
                 velocity_scale_rad_s, gripper_open_m, gripper_closed_m,
                 exterior_camera="front", wrist_camera="wrist", step_s=1 / 15,
                 execution_horizon=4, max_effort_n=20, clip_normalized_velocity=False):
        if (len(joint_names) != 7 or len(set(joint_names)) != 7 or len(joint_limits) != 7
                or len(velocity_scale_rad_s) != 7 or any(not isinstance(j, str) or not j for j in joint_names)):
            raise ValueError("droid_requires_seven_explicit_arm_joints")
        values = [step_s, gripper_open_m, gripper_closed_m, max_effort_n, *velocity_scale_rad_s]
        if any(type(v) not in (float, int) or not math.isfinite(v) for v in values):
            raise ValueError("invalid_droid_mapping")
        if (not 0.01 <= step_s <= 1 or type(execution_horizon) is not int or not 1 <= execution_horizon <= 16
                or not 0 <= gripper_closed_m < gripper_open_m <= 1 or not 0 < max_effort_n <= 100
                or any(v <= 0 for v in velocity_scale_rad_s) or type(clip_normalized_velocity) is not bool):
            raise ValueError("invalid_droid_mapping_limits")
        for bounds in joint_limits:
            if (len(bounds) != 2 or any(type(v) not in (float, int) or not math.isfinite(v) for v in bounds)
                    or bounds[0] >= bounds[1]):
                raise ValueError("invalid_droid_joint_limits")
        self.arm, self.gripper = arm_capability, gripper_capability
        self.joints, self.limits, self.scales = joint_names, joint_limits, velocity_scale_rad_s
        self.open_m, self.closed_m, self.effort = gripper_open_m, gripper_closed_m, max_effort_n
        self.exterior, self.wrist = exterior_camera, wrist_camera
        self.step_s, self.horizon, self.clip = step_s, execution_horizon, clip_normalized_velocity

    def states(self, data):
        import numpy as np
        try:
            arm, gripper = data["members"][self.arm], data["members"][self.gripper]
            if arm["joint_names"] != self.joints:
                raise Rejected("pi05_joint_order_mismatch")
            q = np.asarray(arm["positions_rad"])
            width = gripper["position_m"]
            if q.shape != (7,) or q.dtype.kind not in "fiu" or not np.isfinite(q).all() or type(width) not in (int, float) or not math.isfinite(width):
                raise Rejected("pi05_measured_state_required")
            q = q.astype(np.float64)
            if not self.closed_m - 1e-6 <= width <= self.open_m + 1e-6:
                raise Rejected("pi05_gripper_calibration_mismatch")
            return arm, q, min(1.0, max(0.0, (self.open_m - width) / (self.open_m - self.closed_m)))
        except (KeyError, TypeError, ValueError) as exc:
            raise Rejected("pi05_missing_or_invalid_measured_state") from exc

    def observation(self, context):
        import numpy as np
        from PIL import Image
        from openpi_client import image_tools
        arm, q, gripper = self.states(context["observation"]["data"])
        def picture(name):
            try:
                frame = arm["images"][name]
                if frame["mime_type"] != "image/png" or frame["encoding"] != "base64":
                    raise Rejected("pi05_png_camera_required")
                raw = base64.b64decode(frame["data"], validate=True)
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width * image.height > 16_000_000:
                        raise Rejected("pi05_image_too_large")
                    # Letterbox preserves geometry; never invent a missing camera.
                    return image_tools.resize_with_pad(np.asarray(image.convert("RGB"), dtype=np.uint8), 224, 224)
            except (KeyError, OSError, ValueError) as exc:
                raise Rejected("pi05_camera_unavailable:" + name) from exc
        return {"observation/exterior_image_1_left": picture(self.exterior),
                "observation/wrist_image_left": picture(self.wrist),
                "observation/joint_position": q.astype(np.float32),
                "observation/gripper_position": np.array([gripper], dtype=np.float32),
                "prompt": context["node"].get("instruction") or context["task"]}

    def encode(self, actions, observation):
        import numpy as np
        _, position, _ = self.states(observation)
        values = np.asarray(actions)
        if values.ndim != 2 or values.shape[1] != 8 or not 1 <= values.shape[0] <= 256 or values.dtype.kind not in "fiu" or not np.isfinite(values).all():
            raise Rejected("pi05_requires_finite_N_by_8_actions")
        selected = values[:self.horizon]
        velocities = selected[:, :7]
        if self.clip:
            velocities = np.clip(velocities, -1, 1)
        elif np.any(np.abs(velocities) > 1):
            raise Rejected("pi05_normalized_velocity_out_of_range")
        closed = selected[:, 7] > 0.5
        # Stop the dispatched prefix at a gripper transition and re-observe.
        switches = np.flatnonzero(closed != closed[0])
        count = int(switches[0]) if len(switches) else len(selected)
        points = []
        for index, velocity in enumerate(velocities[:count]):
            position = position + velocity * np.asarray(self.scales) * self.step_s
            if any(not lo <= q <= hi for q, (lo, hi) in zip(position, self.limits)):
                raise Rejected("pi05_integrated_joint_limit_exceeded")
            points.append({"positions": position.tolist(), "time_from_start_s": (index + 1) * self.step_s})
        return {"members": {self.arm: {"points": points}, self.gripper: {
            "position_m": self.closed_m if closed[0] else self.open_m, "max_effort_n": self.effort}}}


class Pi05Policy:
    def __init__(self, client, mapping):
        self.client, self.mapping = client, mapping

    async def predict(self, context):
        value = await self.client.infer(self.mapping.observation(context))
        # Pi 0.5 has no task-success/done head. This ends a declared chunk budget.
        return {"actions": value["actions"], "done": context["round"] + 1 >= context["node"]["max_rounds"]}


class DroidMappingRouter:
    def __init__(self, mappings):
        self.mappings = {name: DroidMapping(**cfg) for name, cfg in mappings.items()}

    def observation(self, context):
        mapping = self.mappings.get(context["node"]["capability"])
        if mapping is None:
            raise Rejected("pi05_robot_mapping_not_configured")
        return mapping.observation(context)

    def encode(self, actions, observation):
        names = set(observation.get("members", {}))
        mappings = [m for m in self.mappings.values() if names == {m.arm, m.gripper}]
        if len(mappings) != 1:
            raise Rejected("pi05_ambiguous_robot_mapping")
        return mappings[0].encode(actions, observation)


def create_pi05_app(config):
    from .vla import create_vla_app
    mapping = DroidMappingRouter(config["mappings"]) if "mappings" in config else DroidMapping(**config["mapping"])
    uri = config.get("uri") or os.environ[config.get("uri_env", "OPENPI_URI")]
    client = OpenPiClient(uri, timeout_s=config.get("timeout_s", 30),
                          api_key=os.environ.get(config.get("key_env", "OPENPI_API_KEY")))
    return create_vla_app(Pi05Policy(client, mapping), mapping, model=config.get("model", "pi05_droid"),
                          token=os.environ.get(config.get("bridge_key_env", "PI05_BRIDGE_TOKEN")),
                          max_concurrency=config.get("max_concurrency", 4))


def main():
    import argparse
    import json
    from pathlib import Path
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.host not in ("localhost", "127.0.0.1", "::1") and not os.environ.get(config.get("bridge_key_env", "PI05_BRIDGE_TOKEN")):
        parser.error("non-loopback bridge requires PI05_BRIDGE_TOKEN (or configured key environment)")
    uvicorn.run(create_pi05_app(config), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
