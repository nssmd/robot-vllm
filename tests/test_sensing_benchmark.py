import importlib.util
from pathlib import Path

import pytest

from robot_vllm.control import Rejected
from robot_vllm.testing.sensing_scene import COLORS


spec = importlib.util.spec_from_file_location("sensing_benchmark", Path(__file__).parents[1] / "scripts/sensing_benchmark.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_compact_and_explicit_decoders_preserve_same_target_decisions():
    columns = dict(zip(COLORS, [1, 3, 5, 2]))
    explicit = {"targets": [{"color": c, "column": v} for c, v in columns.items()]}
    assert benchmark.decode(explicit, COLORS, False) == benchmark.decode(columns, COLORS, True)


@pytest.mark.parametrize("value,compact", [
    ({"red": 0, "green": 1, "blue": 2, "yellow": 3}, True),
    ({"red": True, "green": 1, "blue": 2, "yellow": 3}, True),
    ({"red": 1, "green": 1, "blue": 2}, True),
    ({"targets": [{"color": "red", "column": 1}, {"color": "red", "column": 2}]}, False),
    ({"targets": [{"color": "red", "column": 1, "hidden": 1}]}, False),
])
def test_invalid_model_targets_cannot_become_motor_commands(value, compact):
    with pytest.raises(Rejected):
        benchmark.decode(value, COLORS, compact)
