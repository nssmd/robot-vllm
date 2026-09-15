"""LIBERO short adapter with explicit observation allowlist and post-hoc verdict."""

from __future__ import annotations

import importlib
from ctypes.util import find_library
import json
import os
from pathlib import Path
import sys
import time

from .protocol import Observation


PROPRIO_KEYS = ("robot0_joint_pos", "robot0_joint_vel", "robot0_eef_pos",
                "robot0_eef_quat", "robot0_gripper_qpos", "robot0_gripper_qvel")
CAMERAS = ("agentview", "robot0_eye_in_hand")
SHORT_SUITES = {"libero_" + subset + suffix for subset in ("spatial", "object", "goal")
                for suffix in ("", "_task", "_object", "_swap", "_lan")}


def filter_visible(raw: dict) -> tuple[dict, dict]:
    """Positive allowlist, never a prefix match or pass-through of info/reward."""
    import numpy as np
    images = {}
    for camera in CAMERAS:
        value = raw.get(camera + "_image")
        if value is not None:
            image = np.asarray(value)
            if image.ndim != 3 or image.shape[-1] != 3 or image.dtype != np.uint8:
                raise ValueError("invalid_camera_frame")
            images[camera] = np.ascontiguousarray(image[::-1])
    proprio = {}
    for key in PROPRIO_KEYS:
        if key in raw:
            values = np.asarray(raw[key], dtype=float).reshape(-1)
            if not np.isfinite(values).all():
                raise ValueError("invalid_proprioception")
            proprio[key] = values.tolist()
    return images, proprio


class LiberoEnvironment:
    backend_name = "libero"

    def __init__(self, output: Path, *, root: Path, task: str, init_index: int | None = None,
                 camera_size=128, settle_steps=10):
        suite, number = task.split("/")
        if suite not in SHORT_SUITES:
            raise ValueError("only LIBERO spatial/object/goal short suites are supported")
        self.root, self.output, self.task = Path(root).resolve(), output.resolve(), task
        self.suite, self.task_id, self.init_index = suite, int(number), init_index
        self.camera_size, self.settle_steps = camera_size, settle_steps
        self._env = None
        self.perception_time_s = 0.0
        self.action_time_s = 0.0
        self._version = 0

    def _activate(self):
        os.environ.setdefault("MUJOCO_GL", "egl")
        os.environ.setdefault("PYOPENGL_PLATFORM", os.environ["MUJOCO_GL"])
        if (os.environ["MUJOCO_GL"] == "egl"
                and not os.environ.get("__EGL_VENDOR_LIBRARY_FILENAMES")
                and find_library("EGL_nvidia")):
            vendor = self.output / "egl-vendor.json"
            vendor.write_text(json.dumps({"file_format_version": "1.0.0",
                                         "ICD": {"library_path": "libEGL_nvidia.so.0"}}))
            os.environ["__EGL_VENDOR_LIBRARY_FILENAMES"] = str(vendor)
        config_dir = self.output / "libero-config"
        config_dir.mkdir()
        upstream = self.root / "libero" / "libero"
        datasets = self.output / "datasets"
        datasets.mkdir()
        paths = {"benchmark_root": str(upstream), "bddl_files": str(upstream / "bddl_files"),
                 "init_states": str(upstream / "init_files"), "assets": str(upstream / "assets"),
                 "datasets": str(datasets)}
        (config_dir / "config.yaml").write_text(json.dumps(paths, indent=2))
        os.environ["LIBERO_CONFIG_PATH"] = str(config_dir)
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        existing = sys.modules.get("libero.libero")
        if existing is not None:
            if not Path(existing.__file__).resolve().is_relative_to(self.root):
                raise RuntimeError("different_libero_checkout_already_imported")
            existing.config_file = str(config_dir / "config.yaml")
            existing.libero_config_path = str(config_dir)
        importlib.invalidate_caches()

    def _capture(self, raw):
        from PIL import Image
        tick = time.monotonic()
        images, proprio = filter_visible(raw)
        if set(images) != set(CAMERAS) or not proprio:
            raise RuntimeError("incomplete_visible_observation")
        files = {}
        for name, pixels in images.items():
            path = self.output / "frames" / f"{self._version:06d}-{name}.png"
            with path.open("xb") as f:
                Image.fromarray(pixels).save(f, format="PNG")
            files[name] = str(path)
        self.perception_time_s += time.monotonic() - tick
        return Observation(self._episode_id, self._version, tick, self._instruction, files, proprio)

    def reset(self, episode_id: str, seed: int):
        self._activate()
        import numpy as np
        from libero.libero import benchmark
        from libero.libero.envs import OffScreenRenderEnv
        self._episode_id = episode_id
        suites = benchmark.get_benchmark_dict()
        if self.suite not in suites:
            raise ValueError("suite_not_available")
        bench = suites[self.suite]()
        if self.task_id < 0 or self.task_id >= bench.get_num_tasks():
            raise ValueError("invalid_task_index")
        bddl = bench.get_task_bddl_file_path(self.task_id)
        self._env = OffScreenRenderEnv(
            bddl_file_name=bddl, controller="OSC_POSE", use_object_obs=True,
            ignore_done=True, camera_heights=self.camera_size, camera_widths=self.camera_size,
            control_freq=20, camera_names=list(CAMERAS), camera_depths=False)
        self._env.seed(seed)
        raw = self._env.reset()
        if self.init_index is not None:
            import torch
            spec = bench.get_task(self.task_id)
            path = self.root / "libero/libero/init_files" / spec.problem_folder / spec.init_states_file
            states = torch.load(path, weights_only=False)
            if not 0 <= self.init_index < len(states):
                raise ValueError("invalid_init_index")
            raw = self._env.set_init_state(states[self.init_index])
        # Reset seed and explicit init index have separate manifest semantics; no seed modulo.
        for _ in range(self.settle_steps):
            raw, _, _, _ = self._env.step(np.array([0, 0, 0, 0, 0, 0, -1], dtype=float))
        # Private reproducibility evidence, never included in model payloads or visible journals.
        private = self.output / "private-evaluator"
        private.mkdir()
        with (private / "initial-state.npy").open("xb") as f:
            np.save(f, self._env.get_sim_state(), allow_pickle=False)
        self._instruction = self._env.language_instruction
        self._version = 0
        (self.output / "frames").mkdir()
        return self._capture(raw)

    def step(self, action):
        import numpy as np
        tick = time.monotonic()
        raw, _reward, _done, _info = self._env.step(np.asarray(action, dtype=float))
        self.action_time_s += time.monotonic() - tick
        self._version += 1
        return self._capture(raw)

    def final_verdict(self):
        return bool(self._env.check_success())

    def close(self):
        if self._env is not None:
            self._env.close()
            self._env = None
