"""Probe provider reuse of an identical image prefix with changed trial questions.

Synthetic visual grid, actual model/usage, no robot motion or task-success claim.
Cache counters are aggregate; an image-change control tests visual contribution.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import time
import uuid


INSTRUCTIONS = 'Read the provided 4 by 4 visual grids. Return JSON with color for the requested corner in the first image. If there is a second image also report its same corner as reference_color.'


def visual_request(model, cache_key, images, question, *, cache=True):
    """Exact image content precedes changing questions; no previous answer supplied."""
    image_blocks = [{'type': 'input_image', 'image_url': 'data:image/png;base64,'+pixels,
                     'detail': 'high'} for pixels in images]
    if not image_blocks:
        raise ValueError('at_least_one_image_required')
    if cache:
        image_blocks[-1]['prompt_cache_breakpoint'] = {'mode': 'explicit'}
    return {'model': model, 'store': False, 'max_output_tokens': 96,
            'reasoning': {'effort': 'low'}, 'prompt_cache_key': cache_key,
            # Explicit with no breakpoints disables reads AND writes on this endpoint.
            'prompt_cache_options': {'mode': 'explicit', 'ttl': '30m'},
            'instructions': INSTRUCTIONS, 'text': {'format': {'type': 'json_object'}},
            'input': [{'role': 'user', 'content': [*image_blocks,
                {'type': 'input_text', 'text': question}]}]}


def make_image(path, changed=False):
    from PIL import Image, ImageDraw
    im = Image.new('RGB', (1536, 1536), 'white')
    draw = ImageDraw.Draw(im)
    colors = ['red', 'green', 'blue', 'orange']
    for row in range(4):
        for col in range(4):
            x, y = 40 + col * 374, 40 + row * 374
            draw.rectangle((x, y, x+330, y+330), outline='black', width=3)
            color = 'purple' if changed and row == col == 0 else colors[(row+col) % 4]
            draw.ellipse((x+70, y+70, x+260, y+260), fill=color)
    im.save(path)
    return base64.b64encode(path.read_bytes()).decode()


def main():
    import requests
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default=os.environ.get('GP6_ENDPOINT'))
    parser.add_argument('--model', default='gpt-6-astra')
    parser.add_argument('--key-env', default='GP6_API_KEY')
    parser.add_argument('--azure-cli', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--views', type=int, choices=(1, 2), default=1)
    parser.add_argument('--pairs', type=int, default=0, help='Append alternating disabled/enabled cache comparisons')
    parser.add_argument('--resume-from', type=Path, help='Preserve cache namespace and skip successful cases from this run')
    parser.add_argument('--rate-limit-retries', type=int, choices=range(4), default=0)
    args = parser.parse_args()
    if not args.endpoint:
        parser.error('endpoint required')
    if args.azure_cli:
        from azure.identity import AzureCliCredential
        token = AzureCliCredential().get_token('https://cognitiveservices.azure.com/.default').token
    else:
        token = os.environ[args.key_env]
    args.output.mkdir(parents=True, exist_ok=False)
    a = make_image(args.output/'image-a.png')
    b = make_image(args.output/'image-b.png', True)
    cases = [('cold_a', a, 'top-left', 'red'), ('repeat_a', a, 'top-left', 'red'),
             ('new_question_a', a, 'top-right', 'orange'),
             ('changed_b', b, 'top-left', 'purple'), ('repeat_b', b, 'top-left', 'purple'),
             ('return_a', a, 'top-left', 'red')]
    cases = [(name, pixels, cell, expected, True, None) for name, pixels, cell, expected in cases]
    for pair in range(args.pairs):
        cell = 'top-left' if pair % 2 == 0 else 'top-right'
        expected = 'red' if pair % 2 == 0 else 'orange'
        order = (False, True) if pair % 2 == 0 else (True, False)
        for enabled in order:
            cases.append((f'pair-{pair}-' + ('on' if enabled else 'off'), a, cell, expected, enabled, pair))
    namespace = 'visual-' + uuid.uuid4().hex[:16]
    prior_rows = []
    if args.resume_from:
        prior = json.loads((args.resume_from/'summary.json').read_text())
        if prior['manifest']['model'] != args.model or prior['manifest']['views'] != args.views:
            raise ValueError('resume_model_or_views_mismatch')
        namespace = prior['manifest']['cache_key']
        prior_rows = [r for r in prior['records'] if r.get('status') == 'completed']
        # This is a continuation of identical image inputs, never an arbitrary cache identity.
        for name in ('image-a.png', 'image-b.png'):
            if (args.resume_from/name).read_bytes() != (args.output/name).read_bytes():
                raise ValueError('resume_images_changed')
    manifest = {'model': args.model, 'image_size': [1536,1536], 'detail': 'high',
                'cache_key': namespace, 'cases': [c[0] for c in cases],
                'scope': 'synthetic grid / actual API / no motion', 'text_padding': False,
                'breakpoint': 'after last image', 'interval_s': args.interval, 'views': args.views,
                'pairs': args.pairs, 'pair_order': 'alternating', 'disabled': 'explicit mode without breakpoints',
                'rate_limit_retries': args.rate_limit_retries,
                'resume_from': str(args.resume_from) if args.resume_from else None}
    (args.output/'manifest.json').write_text(json.dumps(manifest, indent=2))
    rows = list(prior_rows)
    for index, (name, pixels, cell, expected, enabled, pair) in enumerate(cases):
        if any(r['case'] == name for r in prior_rows):
            continue
        label = f'Pair {pair}' if pair is not None else f'Trial {index}'
        request = visual_request(args.model, namespace, [pixels, b] if args.views == 2 else [pixels],
                                 f'{label}. Return JSON: what color is the {cell} circle?', cache=enabled)
        (args.output/f'{index}-request.json').write_text(json.dumps(request))
        start = time.monotonic()
        try:
            for attempt in range(args.rate_limit_retries + 1):
                start = time.monotonic()
                response = requests.post(args.endpoint, json=request,
                    headers={'Authorization': 'Bearer '+token}, timeout=120)
                if response.status_code != 429 or attempt == args.rate_limit_retries:
                    break
                (args.output/f'{index}-rate-limit-{attempt}.json').write_text(json.dumps({
                    'status_code':429,'response':response.text,'wall_time_s':time.monotonic()-start,
                    'wait_s':60},indent=2))
                print(f'{name}: rate limited; waiting 60s before bounded retry {attempt+1}',flush=True)
                time.sleep(60)
            elapsed = time.monotonic()-start
            (args.output/f'{index}-response.txt').write_text(response.text)
            data = response.json()
            row = {'case': name, 'pair': pair, 'cache_enabled': enabled,
                   'rate_limit_retries': attempt,
                   'status_code': response.status_code, 'wall_time_s': elapsed,
                   'model': data.get('model'), 'usage': data.get('usage'), 'status': data.get('status')}
            if response.ok and data.get('status') == 'completed':
                output = ''.join(p.get('text','') for item in data.get('output',[]) for p in item.get('content',[]) if p.get('type')=='output_text')
                row['output'] = output
                expected_output = {'color': expected}
                if args.views == 2:
                    expected_output['reference_color'] = 'purple' if cell == 'top-left' else 'orange'
                row['correct'] = json.loads(output) == expected_output
            else:
                row['error'] = data.get('error')
                row['retry_after'] = response.headers.get('Retry-After')
            rows.append(row)
            (args.output/f'{index}-result.json').write_text(json.dumps(row, indent=2))
            print(json.dumps(row), flush=True)
            if not response.ok:
                break
        except Exception as exc:
            rows.append({'case': name, 'error_type': type(exc).__name__, 'status': 'infra'})
            break
        time.sleep(args.interval)
    summary = {'manifest': manifest, 'records': rows, 'complete': len(rows)==len(cases) and all(r.get('status')=='completed' for r in rows),
               'note': 'Aggregate cache counters do not directly label vision tokens. No latency significance or task success inferred.'}
    (args.output/'summary.json').write_text(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
