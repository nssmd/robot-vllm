"""Actual GPT cache measurement on retained, changing MuJoCo observations.

Same images and question in both layouts; ordered history vs newest-first.
No answers are replayed. This is observation replay, not closed-loop evaluation.
"""
import argparse
import base64
import io
import json
import os
from pathlib import Path
import statistics
import time
import uuid

from PIL import Image
from robot_vllm.observation_history import ObservationHistory
from robot_vllm.runtime import write_json


def main():
    import requests
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default=os.environ.get('GP6_ENDPOINT'))
    parser.add_argument('--model', default='gpt-5.5')
    parser.add_argument('--azure-cli', action='store_true')
    parser.add_argument('--key-env', default='GP6_API_KEY')
    parser.add_argument('--episode', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=8)
    args = parser.parse_args()
    if not args.endpoint:
        parser.error('endpoint required')
    if args.azure_cli:
        from azure.identity import AzureCliCredential
        key = AzureCliCredential().get_token('https://cognitiveservices.azure.com/.default').token
    else:
        key = os.environ[args.key_env]
    args.output.mkdir(parents=True, exist_ok=False)
    files = ['phase-0.png', 'phase-0-after.png', 'phase-1.png', 'phase-1-after.png']
    # Grounding labels are used only after the API returns, never added to input.
    labels = [json.loads((args.episode/f'phase-{phase}-verdict.json').read_text())['prediction']['red'] for phase in (0, 1)]
    history = ObservationHistory('replay-' + uuid.uuid4().hex[:12])
    namespace = 'hist-' + uuid.uuid4().hex[:12]
    write_json(args.output/'manifest.json', {'model': args.model, 'images': files,
        'source_episode': str(args.episode), 'resized': [1536, 1536],
        'layouts': ['chronological', 'newest_first'], 'max_frames': 8,
        'scope': 'actual model / MuJoCo image replay / no robot motion',
        'text_padding': False, 'cache': 'automatic', 'order': 'alternating'})
    rows = []
    stopped = False
    previous_png = None
    for index, name in enumerate(files):
        with Image.open(args.episode/name) as im:
            im = im.convert('RGB').resize((1536, 1536))
            buf = io.BytesIO()
            im.save(buf, format='PNG')
        png = buf.getvalue()
        if png == previous_png:
            raise ValueError('consecutive camera frames unexpectedly identical')
        previous_png = png
        (args.output/name).write_bytes(png)
        history.append(sequence=index, captured_at=float(index), png_base64=base64.b64encode(png).decode())
        messages = history.messages('Return JSON with current_red_column (1..5) and changed_since_previous (boolean). Read the red target disk, not the black slider. Compare with the preceding observation; false if none.')
        expected = {'current_red_column': labels[index//2], 'changed_since_previous': index == 2 and labels[0] != labels[1]}
        layouts = ['chronological', 'newest_first']
        if index % 2:
            layouts.reverse()
        for layout in layouts:
            inputs = messages if layout == 'chronological' else list(reversed(messages[:-1])) + messages[-1:]
            body = {'model': args.model, 'store': False, 'max_output_tokens': 256,
                'reasoning': {'effort': 'low'}, 'text': {'format': {'type': 'json_object'}},
                'prompt_cache_key': namespace + '-' + layout,
                'instructions': 'Read labeled camera observations. Sequence numbers determine time. Historical images are not current state. Return JSON only.', 'input': inputs}
            write_json(args.output/f'{index}-{layout}-request.json', body)
            tick = time.monotonic()
            try:
                response = requests.post(args.endpoint, json=body, headers={'Authorization': 'Bearer '+key}, timeout=120)
                elapsed = time.monotonic()-tick
                (args.output/f'{index}-{layout}-response.txt').write_text(response.text)
                data = response.json()
                row = {'index': index, 'layout': layout, 'http_status': response.status_code,
                       'status': data.get('status'), 'model': data.get('model'), 'usage': data.get('usage'), 'wall_time_s': elapsed}
                if response.ok and data.get('status') == 'completed':
                    output = ''.join(p.get('text', '') for item in data.get('output', []) for p in item.get('content', []) if p.get('type') == 'output_text')
                    row.update(output=output, expected=expected, correct=json.loads(output)==expected)
                else:
                    row['error'] = data.get('error')
                    stopped = True
            except Exception as exc:
                row = {'index': index, 'layout': layout, 'status': 'infra', 'error_type': type(exc).__name__}
                stopped = True
            rows.append(row)
            write_json(args.output/f'{index}-{layout}-result.json', row)
            print(json.dumps(row), flush=True)
            if stopped:
                break
            time.sleep(args.interval)
        if stopped:
            break
    summary = {'complete': not stopped, 'records': rows, 'layouts': {}}
    for layout in ['chronological', 'newest_first']:
        valid = [r for r in rows if r['layout']==layout and r.get('status')=='completed']
        measured = [r for r in valid if type((r.get('usage') or {}).get('input_tokens_details', {}).get('cached_tokens')) is int]
        summary['layouts'][layout] = {'completed': len(valid), 'correct': sum(r['correct'] for r in valid),
            'cached_tokens': sum(r['usage']['input_tokens_details']['cached_tokens'] for r in measured),
            'input_tokens': sum(r['usage']['input_tokens'] for r in measured),
            'cache_metered_calls': len(measured),
            'median_wall_s': statistics.median(r['wall_time_s'] for r in valid) if valid else None}
    write_json(args.output/'summary.json', summary)
    print(json.dumps(summary['layouts'], indent=2))


if __name__ == '__main__':
    main()
