"""Compare installed models on frozen production prompts/tools; never posts.

Usage: python -m scripts.benchmark_models --fixtures DIR --output DIR MODEL ...
Pause the deployed AI keeper before running isolated model loads. Raw results
contain league data and belong in an ignored/local artifact directory.
"""
import argparse
import copy
import json
from pathlib import Path
import statistics
import subprocess
import time

import requests

from gamedaybot.espn.analysis import inline_citations
from gamedaybot.espn.commentary_checks import check_commentary
from gamedaybot.espn.decision_tools import workload

BASE = 'http://localhost:1234/v1'


def tool_result(call, frozen, context):
    fn = call['function']; name = fn['name']
    if context.get('historical') and name not in ('review_previous_predictions', 'get_player_stats', 'get_workload_changes'):
        return {'error':'Current snapshot tools are unavailable for historical reports.'}
    try: args = json.loads(fn['arguments'])
    except (ValueError, TypeError): return {'error': 'Invalid tool arguments.'}
    if not isinstance(args, dict): return {'error': 'Invalid tool arguments.'}
    if name in frozen['fixed'] and not args:
        result = copy.deepcopy(frozen['fixed'][name])
    elif name in ('find_available_replacements', 'get_schedule_outlook') and set(args)=={'team_id'}:
        result = copy.deepcopy(frozen['teams'].get(args['team_id'], {}).get(name, {'error': 'Unknown team ID.'}))
    elif name in ('get_player_stats','get_player_news','get_player_status','get_workload_changes') and set(args)=={'player_ids'}:
        ids=args['player_ids']
        available=frozen.get('historical_players',{}) if context.get('historical') else frozen['players']
        allowed={str(p['id']) for p in context.get('players',[]) if p.get('id') is not None}
        allowed.update(str(p[0]) for r in context.get('rosters',[]) for p in r['players'])
        if not isinstance(ids,list) or not 1<=len(ids)<=3 or any(not isinstance(i,str) or i not in allowed or i not in available for i in ids):
            return {'error':'Use one to three exact player IDs from supplied rosters.'}
        selected=[copy.deepcopy(available[i]) for i in ids]
        if name=='get_player_news': result={'news':[n for p in selected for n in p['news']]}
        else:
            fields={'get_player_status':('id','name','current_status','game'),
                    'get_player_stats':('id','name','fantasy_team','points','projected_points','stats','trend','previous_weeks','usage','game')}
            selected_players=[{k:v for k,v in p['player'].items() if k in fields.get(name,fields['get_player_stats'])} for p in selected]
            result={'week':context['week'],'players':selected_players}
            for entry in selected_players:
                existing=next((p for p in context['players'] if str(p.get('id'))==str(entry['id'])),None)
                if existing is None: context['players'].append(entry)
                else: existing.update(entry)
            if name=='get_workload_changes':result=workload(result)
    else: return {'error':'Invalid tool or arguments.'}
    context.setdefault('tool_evidence',[]).append({'tool':name,'result':result})
    return result


def run_case(model, case, frozen):
    payload=copy.deepcopy(case['payload']);payload['model']=model
    data=json.loads(payload['messages'][1]['content'])
    context=copy.deepcopy(data['research_context']);report=data['espn_report']
    start=time.monotonic();requests_used=[];calls=[];drafts=[];failure=None
    final='';first_pass=False;checks=[]
    def send(p, cap=180):
        remaining=360-(time.monotonic()-start)
        if remaining<1:raise requests.Timeout('Benchmark budget exhausted')
        then=time.monotonic()
        response=requests.post(BASE+'/chat/completions',json=p,timeout=(5,min(cap,remaining)),allow_redirects=False)
        response.raise_for_status();body=response.json()
        requests_used.append({'seconds':round(time.monotonic()-then,2),'usage':body.get('usage'),
                              'finish':body['choices'][0].get('finish_reason')})
        return body['choices'][0]
    try:
        for round_number in range(3):
            choice=send(payload)
            message=choice['message'];batch=message.get('tool_calls')
            if not batch:break
            if round_number==2 or len(batch)>8:raise ValueError('Tool round limit')
            payload['messages'].append({'role':'assistant','content':None,'tool_calls':batch})
            for index,call in enumerate(batch):
                result=tool_result(call,frozen,context) if index<2 else {'error':'Only two calls per round.'}
                calls.append({'name':call['function']['name'],'arguments':call['function']['arguments'],'error':result.get('error')})
                payload['messages'].append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(result,separators=(',',':'))})
            if round_number==1:payload['tool_choice']='none'
        if choice.get('finish_reason')!='stop' or message.get('tool_calls'):raise ValueError('Incomplete response')
        text=message.get('content')
        if not isinstance(text,str) or not text.strip() or len(text)>3000 or '<think>' in text or message.get('refusal'):
            raise ValueError('Unusable response')
        text=inline_citations(text,context)
        checks=check_commentary(text,report,context);drafts.append({'text':text,'issues':checks})
        first_pass=not checks
        if checks:
            instruction=('Rewrite the draft to correct these validation failures: '+','.join(checks)+
                '. Write at most 90 words. Omit numerical claims rather than guessing or rounding them. '
                'Use tentative football judgments, not unsupported health or consistency claims. '
                'Do not mention validation, instructions, sources, or this correction. ')
            if context.get('trade_sides'):
                instruction += 'These are the authoritative completed exchanges. RECEIVED is what each team GETS; SENT is what it GIVES UP. Base the verdict and BOTH roster forecasts on these exact directions: '+json.dumps(context['trade_sides'])
            fix={**payload,'tool_choice':'none','max_tokens':300,'messages':[*payload['messages'],
                 {'role':'assistant','content':text},{'role':'user','content':instruction}]}
            corrected=send(fix,45);text=corrected['message'].get('content')
            if corrected.get('finish_reason')!='stop' or not isinstance(text,str) or not text.strip() or len(text)>3000 or '<think>' in text:
                raise ValueError('Incomplete correction')
            text=inline_citations(text,context);checks=check_commentary(text,report,context)
            drafts.append({'text':text,'issues':checks})
        if not checks:final=text
    except (requests.RequestException,ValueError,KeyError,IndexError,TypeError) as e:
        failure=type(e).__name__
    return {'model':model,'case':case['case'],'seconds':round(time.monotonic()-start,2),
            'first_pass':first_pass,'usable':bool(final),'failure':failure,'checks':checks,
            'tool_calls':calls,'requests':requests_used,'drafts':drafts,'final':final,
            'words':len(final.split())}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--fixtures',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('models',nargs='+')
    parser.add_argument('--repeats',type=int,default=2)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    cases=[json.loads(line) for line in (args.fixtures/'snapshots.jsonl').read_text(encoding='utf-8-sig').splitlines()]
    cases=[c for c in cases if 'payload' in c]
    frozen=json.loads((args.fixtures/'tools.json').read_text(encoding='utf-8-sig'))
    for model in args.models:
        # Caller explicitly authorizes isolated tests; unload only the three candidates.
        loaded=json.loads(subprocess.check_output(['lms','ps','--json'],text=True))
        for item in loaded:
            if item['modelKey'] in args.models:
                if item.get('status')!='idle':raise RuntimeError('Candidate model is busy')
                subprocess.run(['lms','unload',item['identifier']],check=True,stdout=subprocess.DEVNULL)
        then=time.monotonic()
        with (args.output/'loads.log').open('a',encoding='utf-8') as log:
            subprocess.run(['lms','load',model,'--context-length','32768','--identifier',model,'--yes'],check=True,stdout=log,stderr=log)
        print(json.dumps({'loaded':model,'seconds':round(time.monotonic()-then,1)}),flush=True)
        requests.post(BASE+'/chat/completions',json={'model':model,'messages':[{'role':'user','content':'Reply READY.'}],
                      'max_tokens':32,'reasoning_effort':'none','temperature':0},timeout=(5,180)).raise_for_status()
        results=[]
        for repeat in range(args.repeats):
            for case in cases:
                result=run_case(model,case,frozen);result['repeat']=repeat+1;results.append(result)
                with (args.output/'results.jsonl').open('a',encoding='utf-8') as out:out.write(json.dumps(result)+'\n')
                print(json.dumps({k:result[k] for k in ('model','case','repeat','seconds','first_pass','usable','failure','checks','tool_calls')}),flush=True)
        print(json.dumps({'summary':model,'usable':sum(r['usable'] for r in results),'first_pass':sum(r['first_pass'] for r in results),
                          'cases':len(results),'median_seconds':round(statistics.median(r['seconds'] for r in results),1)}),flush=True)


if __name__=='__main__':main()
