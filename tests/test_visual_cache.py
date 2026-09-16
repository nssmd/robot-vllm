import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location('visual_cache_benchmark', Path(__file__).parents[1]/'scripts/visual_cache_benchmark.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_only_image_block_has_breakpoint_and_question_remains_fresh():
    a = module.visual_request('test','key',['image-a','image-b'],'trial 1')
    b = module.visual_request('test','key',['image-a','image-b'],'trial 2')
    blocks_a = a['input'][0]['content']
    blocks_b = b['input'][0]['content']
    assert blocks_a[:-1] == blocks_b[:-1]
    assert blocks_a[-1] != blocks_b[-1]
    assert 'prompt_cache_breakpoint' not in blocks_a[0]
    assert blocks_a[1]['prompt_cache_breakpoint'] == {'mode':'explicit'}
    assert 'prompt_cache_breakpoint' not in blocks_a[-1]


def test_disabled_cache_preserves_model_inputs_and_changed_pixels_change_prefix():
    a = module.visual_request('test','key',['image-a'],'question')
    b = module.visual_request('test','key',['image-a'],'question',cache=False)
    assert b['prompt_cache_options']['mode'] == 'explicit'
    del a['input'][0]['content'][0]['prompt_cache_breakpoint']
    assert a == b
    changed = module.visual_request('test','key',['new-image'],'question',cache=False)
    assert changed['input'][0]['content'][0] != b['input'][0]['content'][0]
