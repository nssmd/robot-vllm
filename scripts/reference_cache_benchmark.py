"""GPT automatic-cache probe: fixed visual references, fresh current view each trial.

Compares reference-first and current-first layouts, not a cache-disabled API.
All views are synthetic fixtures. No robot performance claim.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import statistics
import time
import uuid

from PIL import Image, ImageDraw


def picture(path, colors, trial=None):
    im = Image.new('RGB', (1536, 1536), 'white')
    draw = ImageDraw.Draw(im)
    for i, color in enumerate(colors):
        x, y = 50 + (i % 4)*374, 50 + (i // 4)*374
        draw.ellipse((x+35, y+35, x+270, y+270), fill=color)
    if trial is not None:
        draw.rectangle((0,1490,1535,1535),fill=(20+trial*11,40,90))
        draw.text((15,1500),f'CURRENT TRIAL {trial}',fill='white')
    im.save(path)
    return base64.b64encode(path.read_bytes()).decode()


def request(model, refs, current, trial, layout, key):
    def image(data):
        return {'type':'input_image','image_url':'data:image/png;base64,'+data,'detail':'high'}
    reference = [{'type':'input_text','text':'REFERENCE A: target pattern.'},image(refs[0]),
                 {'type':'input_text','text':'REFERENCE B: alternative pattern.'},image(refs[1])]
    live = [{'type':'input_text','text':f'CURRENT camera observation, trial {trial}.'},image(current)]
    content = reference+live if layout=='reference_first' else live+reference
    content.append({'type':'input_text','text':'Return JSON with current_color (top-left circle in CURRENT), reference_a_color (top-left in A), reference_b_color (top-left in B), and matches (A, B, or neither). Compare the top-left colors only.'})
    return {'model':model,'instructions':'Use the labeled images. References are fixed targets, not current observations. Read the CURRENT image anew. Return JSON only.',
            'input':[{'role':'user','content':content}], 'store':False,'max_output_tokens':256,
            'reasoning':{'effort':'low'},'prompt_cache_key':key,
            'text':{'format':{'type':'json_object'}}}


def main():
    import requests
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint',default=os.environ.get('GP6_ENDPOINT'))
    parser.add_argument('--model',default='gpt-5.5')
    parser.add_argument('--azure-cli',action='store_true')
    parser.add_argument('--key-env',default='GP6_API_KEY')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--trials',type=int,default=6)
    parser.add_argument('--interval',type=float,default=8)
    args=parser.parse_args()
    if not args.endpoint or not 2<=args.trials<=12:
        parser.error('endpoint and 2..12 trials required')
    if args.azure_cli:
        from azure.identity import AzureCliCredential
        token=AzureCliCredential().get_token('https://cognitiveservices.azure.com/.default').token
    else:
        token=os.environ[args.key_env]
    root=args.output
    root.mkdir(parents=True,exist_ok=False)
    colors=['red','green','blue','orange']*4
    alternate=['purple',*colors[1:]]
    refs=[picture(root/'reference-a.png',colors),picture(root/'reference-b.png',alternate)]
    namespace='ref-'+uuid.uuid4().hex[:12]
    manifest={'model':args.model,'trials':args.trials,'scope':'synthetic visual reference matching, no motion',
              'layouts':['reference_first','current_first'],'cache':'automatic, no explicit breakpoints',
              'warmup_trial':0,'order':'alternating','fixed_views':2,'fresh_views_per_trial':1,
              'text_padding':False,'namespace':namespace}
    (root/'manifest.json').write_text(json.dumps(manifest,indent=2))
    rows=[]
    stopped=False
    for trial in range(args.trials):
        color=['red','purple','blue'][trial%3]
        live=picture(root/f'current-{trial}.png',[color,*colors[1:]],trial)
        expected={'current_color':color,'reference_a_color':'red','reference_b_color':'purple',
                  'matches':['A','B','neither'][trial%3]}
        layouts=['reference_first','current_first']
        if trial%2:
            layouts.reverse()
        for layout in layouts:
            body=request(args.model,refs,live,trial,layout,namespace+'-'+layout)
            (root/f'{trial}-{layout}-request.json').write_text(json.dumps(body))
            start=time.monotonic()
            try:
                response=requests.post(args.endpoint,json=body,headers={'Authorization':'Bearer '+token},timeout=120)
                elapsed=time.monotonic()-start
                (root/f'{trial}-{layout}-response.txt').write_text(response.text)
                data=response.json()
                row={'trial':trial,'layout':layout,'wall_time_s':elapsed,'http_status':response.status_code,
                     'model':data.get('model'),'usage':data.get('usage'),'status':data.get('status')}
                if response.ok and data.get('status')=='completed':
                    text=''.join(p.get('text','') for x in data.get('output',[]) for p in x.get('content',[]) if p.get('type')=='output_text')
                    row['output']=text
                    row['correct']=json.loads(text)==expected
                else:
                    row['error']=data.get('error')
                    stopped=True
            except Exception as exc:
                row={'trial':trial,'layout':layout,'error_type':type(exc).__name__,'status':'infra'}
                stopped=True
            rows.append(row)
            (root/f'{trial}-{layout}-result.json').write_text(json.dumps(row,indent=2))
            print(json.dumps(row),flush=True)
            if stopped:
                break
            time.sleep(args.interval)
        if stopped:
            break
    summary={'complete':not stopped,'records':rows,'layouts':{},'paired':[]}
    for layout in layouts:
        valid=[r for r in rows if r['layout']==layout and r.get('status')=='completed' and r['trial']>0]
        measured=[r for r in valid if isinstance((r.get('usage') or {}).get('input_tokens_details',{}).get('cached_tokens'),int)]
        inputs=sum(r['usage']['input_tokens'] for r in measured)
        cached=sum(r['usage']['input_tokens_details']['cached_tokens'] for r in measured)
        summary['layouts'][layout]={'nonwarmup_completed':len(valid),'correct':sum(r.get('correct',False) for r in valid),
            'cache_metered_calls':len(measured),'input_tokens':inputs,'cached_tokens':cached,
            'cached_ratio':cached/inputs if inputs else None,
            'median_wall_s':statistics.median(r['wall_time_s'] for r in valid) if valid else None}
    for trial in range(1,args.trials):
        pair={r['layout']:r for r in rows if r['trial']==trial and r.get('status')=='completed'}
        if len(pair)==2:
            summary['paired'].append({'trial':trial,'reduction':1-pair['reference_first']['wall_time_s']/pair['current_first']['wall_time_s'],
                                      'both_correct':all(r.get('correct') for r in pair.values())})
    summary['median_paired_reduction']=statistics.median(p['reduction'] for p in summary['paired']) if summary['paired'] else None
    (root/'summary.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps({k:v for k,v in summary.items() if k!='records'},indent=2))


if __name__=='__main__':
    main()
