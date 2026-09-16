import importlib.util
from pathlib import Path

import pytest

from robot_vllm.control import Rejected

spec = importlib.util.spec_from_file_location('ros_shared_control', Path(__file__).parents[1]/'scripts/ros_shared_control.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('value', [[], {'arm':[True,0]}, {'arm':[float('nan'),0]},
    {'arm':[float('inf'),0]}, {'arm':[.9,0]}, {'arm':[0]}, {'other':[0,0]},
    {'arm':[0,0],'extra':[0,0]}])
def test_invalid_model_targets_rejected_before_dispatch(value):
    with pytest.raises(Rejected):
        module.validate_targets(value,['arm'])


def test_current_round_changes_targets_without_changing_pair_initialization():
    names = ['a','b','c','d']
    for round_id in range(10):
        targets = module.initial_targets(names,round_id)
        assert targets == module.initial_targets(names,round_id)
        module.validate_targets(targets,names)
        if round_id:
            assert targets != module.initial_targets(names,round_id-1)
