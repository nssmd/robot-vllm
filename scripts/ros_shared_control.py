"""Real ROS 2 + actual model: four-arm state sharing and token comparison.

Controllers are synthetic, joint feedback and actions use native ROS transport.
No hardware/manipulation success or provider-side cache speedup is claimed.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time

from robot_vllm.control import DeviceRuntime, Rejected
from robot_vllm.deployment import Deployment
from robot_vllm.models import ModelEndpoint, ModelMeter
from robot_vllm.ros_validation import until
from robot_vllm.runtime import Journal, write_json
from robot_vllm.sensing import SenseKey, SharedPerception


def validate_targets(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise Rejected('model_target_names_mismatch')
    for target in value.values():
        if not isinstance(target, list) or len(target) != 2 or any(type(x) not in (int, float) or not -.8 <= x <= .8 for x in target):
            raise Rejected('invalid_model_joint_target')
    return value


def initial_targets(names, round_id):
    sign = 1 if round_id % 2 == 0 else -1
    return {name:[round(sign*(.1*(i+1)+.01*round_id),2),
                  round(-sign*(.1*(i+1)+.01*round_id),2)] for i,name in enumerate(names)}


async def run(args):
    if args.azure_cli:
        from azure.identity import AzureCliCredential
        os.environ['ROS_SHARED_MODEL_KEY'] = AzureCliCredential().get_token('https://cognitiveservices.azure.com/.default').token
    args.output.mkdir(parents=True, exist_ok=False)
    runtime = DeviceRuntime(args.output/'runtime', observation_ttl_s=120)
    logs, processes, deployment = [], [], None
    service = SharedPerception(max_age_s=120)
    journal = Journal(args.output/'model-events.jsonl')
    endpoint = ModelEndpoint('model', {'kind':'openai_responses','model':args.model,'endpoint':args.endpoint,
        'key_env':'ROS_SHARED_MODEL_KEY' if args.azure_cli else args.key_env,
        'max_output_tokens':256,'reasoning_effort':'low','timeout_s':90,'inference':{'max_concurrency':4}})
    rows = []
    try:
        devices = []
        prefix = '/shared_' + runtime.runtime_id[:8]
        for i in range(4):
            name = f'arm_{i+1}'
            ns = prefix+'/'+name
            log = (args.output/f'{name}.log').open('x')
            logs.append(log)
            processes.append(subprocess.Popen([sys.executable,'-m','robot_vllm.ros_validation','controller',
                '--namespace',ns,'--ready',str(args.output/f'{name}.ready.json')],stdout=log,stderr=log))
            devices.append({'name':name,'backend':'ros2','action_name':ns+'/follow_joint_trajectory',
                'joint_state_topic':ns+'/joint_states','joint_names':['joint_1','joint_2'],
                'limits':[[-1,1],[-1,1]],'state_max_age_s':3})
        await until(lambda: all((args.output/f'arm_{i+1}.ready.json').exists() for i in range(4)),timeout=20)
        deployment = Deployment(runtime, {'devices':devices})
        await deployment.ready(15)
        names = [d['name']+'.trajectory' for d in devices]
        write_json(args.output/'manifest.json', {'model':args.model,'rounds':args.rounds,
            'variants':['independent','shared'],'arms':4,'joints_per_arm':2,
            'controllers':'synthetic','transport':'native ROS2','input':'joint-state snapshot, no images',
            'same_snapshot_within_pair':True,'order':'alternates per round','provider_cache':'not forced',
            'rule':'negate both measured joints of each requested arm', 'tolerance_rad':.001,
            'initial_state_schedule':'sign alternates per round; magnitude = 0.1*(arm_index+1)+0.01*round'})
        async def dispatch(targets, observations, identity):
            operations = await asyncio.gather(*(runtime.submit(owner='shared-control',request_id=identity+'-'+name,
                capability=name, observation_id=observations[name]['observation_id'],
                arguments={'points':[{'positions':targets[name],'time_from_start_s':.3}]}) for name in names))
            peak = runtime.health()['active_executions']
            results = await asyncio.gather(*(runtime.wait(op['execution_id'],timeout_s=10) for op in operations))
            if not all(r['status']=='completed' and r['settled'] for r in results):
                raise RuntimeError('native_action_not_completed')
            async def feedback_ready():
                for _ in range(100):
                    measured = {name:(await runtime.observe(name))['data']['positions_rad'] for name in names}
                    error = max(abs(a-b) for name in names for a,b in zip(measured[name],targets[name]))
                    if error <= .001:
                        return measured,error
                    await asyncio.sleep(.02)
                raise RuntimeError('joint_feedback_did_not_reach_target')
            measured,error = await feedback_ready()
            return {'peak_active_executions':peak,'measured_positions':measured,'max_target_error_rad':error,
                    'native_completed':len(results)}
        for round_id in range(args.rounds):
            initial = initial_targets(names, round_id)
            # One captured observation payload is used by both model conditions.
            obs = {name:await runtime.observe(name) for name in names}
            await dispatch(initial,obs,f'reset-{round_id}-initial')
            snapshot = {name:(await runtime.observe(name))['data']['positions_rad'] for name in names}
            write_json(args.output/f'snapshot-{round_id}.json',snapshot)
            variants = ['independent','shared'] if round_id%2==0 else ['shared','independent']
            for variant in variants:
                obs = {name:await runtime.observe(name) for name in names}
                await dispatch(initial,obs,f'reset-{round_id}-{variant}')
                tickets = {name:await runtime.observe(name) for name in names}
                if any(abs(a-b)>.001 for name in names for a,b in zip(tickets[name]['data']['positions_rad'],snapshot[name])):
                    raise RuntimeError('paired_initial_state_mismatch')
                captured = time.monotonic()
                service.invalidate('joint-snapshot')
                cache_key = SenseKey('joint-snapshot',f'round-{round_id}-{variant}','radians-v1',args.model,
                                    'negate-joints',service.generation('joint-snapshot'))
                meter = ModelMeter(journal)
                async def predict(requested):
                    context = {'joint_positions_rad':snapshot,'requested_arms':requested,
                        'joint_order':['joint_1','joint_2'],'limits_rad':[-.8,.8]}
                    value = await endpoint.generate(instruction='Return JSON mapping exactly the requested arm names to two target joint positions in radians. Negate each currently measured joint position. Use the supplied state, without explanation.',
                        context=context,role='state-control',meter=meter)
                    return validate_targets(value,requested)
                async def consume(name):
                    start=time.monotonic()
                    values = (await service.get(cache_key,captured,lambda:predict(names))) if variant=='shared' else await predict([name])
                    return name,values[name],time.monotonic()-start
                start=time.monotonic()
                responses = await asyncio.gather(*(consume(name) for name in names),return_exceptions=True)
                failed=[r for r in responses if isinstance(r,BaseException)]
                if failed:
                    write_json(args.output/f'infra-{round_id}-{variant}.json',{'error':str(failed[0]),'metrics':meter.summary()})
                    raise RuntimeError('model_request_failed')
                targets = {name:positions for name,positions,_ in responses}
                correct = all(abs(a+b)<.001 for name in names for a,b in zip(targets[name],snapshot[name]))
                service.validate(cache_key,captured)
                # Reject incorrect model outputs before controller dispatch.
                if not correct:
                    write_json(args.output/f'incorrect-{round_id}-{variant}.json',{'targets':targets,'metrics':meter.summary()})
                    raise RuntimeError('model_output_failed_control_rule')
                waiting=time.monotonic()-start
                action=await dispatch(targets,tickets,f'round-{round_id}-{variant}')
                row={'round':round_id,'variant':variant,'status':'passed','targets':targets,'control':action,
                     'model_metrics':meter.summary(),'sense_wait_s':waiting,
                     'individual_wait_sum_s':sum(r[2] for r in responses),'sense_and_action_s':time.monotonic()-start}
                rows.append(row)
                write_json(args.output/f'result-{round_id}-{variant}.json',row)
                print(round_id,variant,'passed',meter.summary()['total_tokens'],round(waiting,3),flush=True)
        summary={'status':'passed','scope':'native ROS synthetic controllers + actual model, no manipulation verdict',
                 'rows':rows,'shared_perception':service.snapshot(),'variants':{}}
        for variant in ['independent','shared']:
            records=[r for r in rows if r['variant']==variant]
            summary['variants'][variant]={'rounds':len(records),'model_calls':sum(r['model_metrics']['model_calls'] for r in records),
                'tokens':{k:sum(r['model_metrics'][k] for r in records) for k in ['prompt_tokens','completion_tokens','total_tokens','cached_input_tokens']},
                'unmetered_calls':sum(r['model_metrics']['unmetered_call_count'] for r in records),
                'median_sense_wait_s':statistics.median(r['sense_wait_s'] for r in records),
                'median_sense_and_action_s':statistics.median(r['sense_and_action_s'] for r in records)}
        write_json(args.output/'result.json',summary)
        print(json.dumps(summary['variants'],indent=2),flush=True)
    except Exception as exc:
        write_json(args.output/'failure.json',{'exception':type(exc).__name__,'message':str(exc),'completed_conditions':len(rows)})
        raise
    finally:
        endpoint.pool.close()
        await service.close()
        journal.close()
        if deployment:
            await deployment.close()
        else:
            await runtime.close()
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
        for process in processes:
            try:
                await asyncio.to_thread(process.wait,timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                await asyncio.to_thread(process.wait,timeout=5)
        for log in logs:
            log.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint',default=os.environ.get('GP6_ENDPOINT'))
    parser.add_argument('--model',default='gpt-5.5')
    parser.add_argument('--azure-cli',action='store_true')
    parser.add_argument('--key-env',default='GP6_API_KEY')
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not args.endpoint or not 1<=args.rounds<=10:
        parser.error('endpoint and 1..10 rounds required')
    asyncio.run(run(args))


if __name__=='__main__':
    main()
