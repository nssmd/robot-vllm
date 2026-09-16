import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location('reference_cache', Path(__file__).parents[1]/'scripts/reference_cache_benchmark.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_reference_prefix_survives_new_view_but_current_first_does_not():
    refs = ['target-a', 'target-b']
    a = module.request('gpt-5.5', refs, 'current-a', 0, 'reference_first', 'key')
    b = module.request('gpt-5.5', refs, 'current-b', 1, 'reference_first', 'key')
    ca, cb = a['input'][0]['content'], b['input'][0]['content']
    assert ca[:4] == cb[:4]
    assert ca[4:6] != cb[4:6]
    assert 'prompt_cache_options' not in a
    assert all('prompt_cache_breakpoint' not in block for block in ca)
    control = module.request('gpt-5.5', refs, 'current-b', 1, 'current_first', 'key')
    blocks = control['input'][0]['content']
    assert blocks[:2] == cb[4:6] and blocks[2:6] == cb[:4]
    assert blocks[-1] == cb[-1]
