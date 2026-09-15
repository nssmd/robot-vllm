"""Offline configuration validation, before drivers, providers or ROS are opened."""
from __future__ import annotations

import json
import math
from pathlib import Path


def keys(value, allowed, required=(), where="config"):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError("invalid_configuration_fields:" + where)


def number(value, low, high, where, integer=False):
    if (type(value) not in ((int,) if integer else (int, float))
            or not math.isfinite(value) or not low <= value <= high):
        raise ValueError("invalid_configuration_limit:" + where)


def validate_config(config):
    keys(config, {"schema", "devices", "groups", "models", "planner", "scheduler", "runtime", "api"}, ["devices"])
    if config.get("schema", "robot_runtime.config.v1") != "robot_runtime.config.v1":
        raise ValueError("unsupported_configuration_schema")
    devices = config["devices"]
    if not isinstance(devices, list) or not 1 <= len(devices) <= 64:
        raise ValueError("devices_must_contain_1_to_64_entries")
    names, actions, capabilities = set(), set(), set()
    for device in devices:
        common = {"name", "backend", "robot_id", "kind", "frame_id"}
        backend = device.get("backend", "ros2") if isinstance(device, dict) else None
        fields = {"mock": {"tick_s", "stop_delay_s"}, "ros2": {
            "action_name", "joint_state_topic", "joint_names", "limits", "state_max_age_s",
            "camera_topics", "lease_topic", "lease_period_s"}, "plugin": {"factory", "options"}}
        if backend not in fields:
            raise ValueError("unknown_device_backend")
        keys(device, common | fields[backend], ["name"], "device")
        name = device["name"]
        if not isinstance(name, str) or not name or len(name) > 128 or name in names:
            raise ValueError("invalid_or_duplicate_device_name")
        names.add(name)
        if backend == "ros2":
            keys(device, common | fields[backend], ["action_name", "joint_state_topic", "joint_names", "limits"], name)
            for field in ("action_name", "joint_state_topic"):
                if not isinstance(device[field], str) or not device[field].startswith("/"):
                    raise ValueError("absolute_ros_name_required:" + field)
            if device["action_name"] in actions:
                raise ValueError("duplicate_physical_action_endpoint")
            actions.add(device["action_name"])
            joints, limits = device["joint_names"], device["limits"]
            if (not isinstance(joints, list) or not joints or any(not isinstance(j, str) or not j for j in joints)
                    or len(set(joints)) != len(joints) or not isinstance(limits, list) or len(limits) != len(joints)):
                raise ValueError("invalid_joint_mapping")
            for bound in limits:
                if not isinstance(bound, list) or len(bound) != 2:
                    raise ValueError("invalid_joint_limits")
                for v in bound:
                    number(v, -1e6, 1e6, "joint_limit")
                if bound[0] >= bound[1]:
                    raise ValueError("invalid_joint_limits")
            number(device.get("state_max_age_s", 2), 0.01, 3600, "state_max_age_s")
            number(device.get("lease_period_s", 0.1), 0.01, 60, "lease_period_s")
            cameras = device.get("camera_topics", {})
            if not isinstance(cameras, dict) or any(not isinstance(v, str) or not v.startswith("/") for v in cameras.values()):
                raise ValueError("invalid_camera_topics")
            if device.get("lease_topic") is not None and (
                    not isinstance(device["lease_topic"], str) or not device["lease_topic"].startswith("/")):
                raise ValueError("invalid_lease_topic")
            capabilities.add(name + ".trajectory")
        elif backend == "mock":
            number(device.get("tick_s", 0.01), 0.0001, 10, "tick_s")
            number(device.get("stop_delay_s", 0), 0, 60, "stop_delay_s")
            capabilities.add(name + ".move")
        elif not isinstance(device.get("factory"), str) or ":" not in device["factory"] or not isinstance(device.get("options", {}), dict):
            raise ValueError("invalid_plugin_factory")
    groups = config.get("groups", [])
    if not isinstance(groups, list):
        raise ValueError("invalid_groups")
    for group in groups:
        keys(group, {"name", "members", "shared_resources"}, ["name", "members"], "group")
        name, members = group["name"], group["members"]
        if (not isinstance(name, str) or not name or name in capabilities or not isinstance(members, list)
                or not members or any(not isinstance(n, str) or not n for n in members)
                or len(members) != len(set(members))):
            raise ValueError("invalid_group")
        capabilities.add(name)
        resources = group.get("shared_resources", [])
        if not isinstance(resources, list) or any(not isinstance(v, str) or not v for v in resources):
            raise ValueError("invalid_shared_resources")
    models = config.get("models", {})
    if not isinstance(models, dict):
        raise ValueError("invalid_models")
    for alias, model in models.items():
        keys(model, {"kind", "model", "endpoint", "endpoint_env", "key_env", "timeout_s",
            "reasoning_effort", "stream", "structured_output", "verify_model"}, ["kind", "model"], "model")
        if alias == "code" or model["kind"] not in ("openai_chat", "openai_responses", "vla_json"):
            raise ValueError("invalid_model_kind_or_alias")
        if not isinstance(model["model"], str) or not model["model"]:
            raise ValueError("invalid_model_name")
        if not (model.get("endpoint") or model.get("endpoint_env")):
            raise ValueError("model_endpoint_configuration_required")
        if model.get("endpoint") and not model["endpoint"].startswith(("http://", "https://")):
            raise ValueError("invalid_model_endpoint")
        number(model.get("timeout_s", 30), 0.01, 120, "model_timeout_s")
        for flag in ("stream", "structured_output", "verify_model"):
            if flag in model and type(model[flag]) is not bool:
                raise ValueError("invalid_model_flag:" + flag)
    scheduler = config.get("scheduler", {})
    keys(scheduler, {"max_parallel", "max_replans", "max_active_tasks"}, where="scheduler")
    for name, low, high in [("max_parallel", 1, 64), ("max_replans", 0, 3), ("max_active_tasks", 1, 256)]:
        if name in scheduler:
            number(scheduler[name], low, high, name, integer=True)
    runtime = config.get("runtime", {})
    keys(runtime, {"observation_ttl_s", "cancel_timeout_s"}, where="runtime")
    for name, maximum in [("observation_ttl_s", 3600), ("cancel_timeout_s", 60)]:
        if name in runtime:
            number(runtime[name], 0.01, maximum, name)
    api = config.get("api", {})
    keys(api, {"max_body_bytes"}, where="api")
    number(api.get("max_body_bytes", 2_097_152), 1024, 16_777_216, "max_body_bytes", integer=True)
    return config


def load_config(path):
    return validate_config(json.loads(Path(path).read_text()))
