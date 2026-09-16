"""Bounded exact-request cache diagnostic, not a robot-performance benchmark."""
import argparse
import copy
import json
import os
from pathlib import Path
import time
import uuid


def main():
    import requests
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint',required=True)
    parser.add_argument('--model',default='gpt-5.5')
    parser.add_argument('--azure-cli',action='store_true')
    parser.add_argument('--key-env',default='GP6_API_KEY')
    parser.add_argument('--image-request',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--interval',type=float,default=10)
    parser.add_argument('--suffix-trials',type=int,default=0,choices=range(4),
                        help='After exact repeats, append changing text to otherwise identical inputs')
    args=parser.parse_args()
    if not 2<=args.repeats<=5 or not 0<=args.interval<=60:
        parser.error('repeats 2..5; interval 0..60')
    if args.azure_cli:
        from azure.identity import AzureCliCredential
        token=AzureCliCredential().get_token('https://cognitiveservices.azure.com/.default').token
    else:
        token=os.environ[args.key_env]
    root=args.output
    root.mkdir(parents=True,exist_ok=False)
    namespace=uuid.uuid4().hex
    # Real existing docs, used as a text-only control for the visual-cache issue.
    docs='\n'.join(Path(p).read_text() for p in ['docs/architecture.md','docs/api.md','docs/PROMPT_CACHE.md'])
    image=copy.deepcopy(json.loads(args.image_request.read_text()))
    image.update(model=args.model,prompt_cache_key='diagnostic-image-'+namespace[:12])
    image.pop('prompt_cache_options',None)
    for item in image.get('input',[]):
        for block in item.get('content',[]):
            block.pop('prompt_cache_breakpoint',None)
    text={'model':args.model,'store':False,'max_output_tokens':256,'reasoning':{'effort':'low'},
          'prompt_cache_key':'diagnostic-text-'+namespace[:12],
          'instructions':'Answer from the supplied documentation. Return JSON with supports_native_cancel: boolean.',
          'input':[{'role':'user','content':[{'type':'input_text','text':docs+'\nDoes the runtime support native cancellation? Return JSON.'}]}],
          'text':{'format':{'type':'json_object'}}}
    manifest={'model':args.model,'repeats':args.repeats,'interval':args.interval,
              'exact_request_repetition':True,'connection':'one requests.Session','store':False,
              'affinity_header':'fixed per diagnostic','scope':'cache diagnostics, no robot commands'}
    manifest['suffix_trials']=args.suffix_trials
    (root/'manifest.json').write_text(json.dumps(manifest,indent=2))
    rows=[]
    with requests.Session() as session:
        session.headers.update({'Authorization':'Bearer '+token,'x-session-affinity':namespace})
        for condition,body in [('text',text),('image',image)]:
            (root/f'{condition}-request.json').write_text(json.dumps(body))
            for repeat in range(args.repeats + args.suffix_trials):
                current=copy.deepcopy(body)
                suffix=repeat-args.repeats
                if suffix>=0:
                    current['input'][0]['content'][-1]['text'] += f'\nAnswer the same question for appended trial {suffix}.'
                (root/f'{condition}-{repeat}-request.json').write_text(json.dumps(current))
                tick=time.monotonic()
                try:
                    response=session.post(args.endpoint,json=current,timeout=120)
                    elapsed=time.monotonic()-tick
                    (root/f'{condition}-{repeat}-response.txt').write_text(response.text)
                    data=response.json()
                    headers={k:v for k,v in response.headers.items() if any(s in k.lower() for s in ('request-id','ratelimit','retry','region','cache'))}
                    row={'condition':condition,'repeat':repeat,'http':response.status_code,'wall_s':elapsed,
                         'changed_suffix':suffix>=0,
                         'usage':data.get('usage'),'model':data.get('model'),'status':data.get('status'),'headers':headers}
                    if not response.ok:
                        row['error']=data.get('error')
                    else:
                        row['output']=''.join(p.get('text','') for item in data.get('output',[]) for p in item.get('content',[]) if p.get('type')=='output_text')
                    rows.append(row)
                    (root/f'{condition}-{repeat}-result.json').write_text(json.dumps(row,indent=2))
                    print(json.dumps({k:v for k,v in row.items() if k!='headers'}),flush=True)
                    if not response.ok:
                        return
                finally:
                    (root/'summary.json').write_text(json.dumps({'manifest':manifest,'records':rows},indent=2))
                time.sleep(args.interval)


if __name__=='__main__':
    main()
