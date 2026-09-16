"""Analyze actual visual-cache counters and paired wall times, retaining uncertainty."""
import argparse
import json
import statistics
from pathlib import Path


def analyze(summary):
    rows = summary['records']
    valid = [r for r in rows if r.get('status') == 'completed']
    pairs = []
    for pair in sorted({r['pair'] for r in valid if r.get('pair') is not None}):
        group = {r['cache_enabled']: r for r in valid if r.get('pair') == pair}
        if set(group) != {True,False}:
            continue
        on, off = group[True], group[False]
        pairs.append({'pair':pair, 'on_s':on['wall_time_s'],'off_s':off['wall_time_s'],
            'reduction':1-on['wall_time_s']/off['wall_time_s'],
            'same_output':json.loads(on['output'])==json.loads(off['output']),
            'correct':on['correct'] and off['correct'],
            'cached_on':on['usage'].get('input_tokens_details',{}).get('cached_tokens'),
            'cached_off':off['usage'].get('input_tokens_details',{}).get('cached_tokens'),
            'retried':bool(on.get('rate_limit_retries') or off.get('rate_limit_retries')),
            'input_on':on['usage'].get('input_tokens'),'input_off':off['usage'].get('input_tokens')})
    return {'completed':len(valid),'correct':sum(r.get('correct',False) for r in valid),
        'infra':len(rows)-len(valid),'pairs':pairs,
        'median_paired_reduction':statistics.median(p['reduction'] for p in pairs) if pairs else None,
        'faster_pairs':sum(p['reduction']>0 for p in pairs),
        'note':'Small synthetic image-reading experiment; aggregate cache counts, not isolated vision-token counts; no robot task verdict.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',type=Path)
    args=parser.parse_args()
    report=analyze(json.loads((args.root/'summary.json').read_text()))
    with (args.root/'analysis.json').open('x') as file:
        json.dump(report,file,indent=2)
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
