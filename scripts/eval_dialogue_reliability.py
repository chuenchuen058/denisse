"""Real-model dialogue checks with every machine tool replaced by a fixture.
Never writes conversation/facts/tasks, opens files, executes shell or plays music.
"""
import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import brain
import tool_router

CASES=[
 {'id':'open_cv','prompt':'pull up my latest cv','need':{'find_files','open_application'}},
 {'id':'retry_file','before':['find my invoice file', 'just invoice then'], 'prompt':'im pretty sure its in my laptop try again','need':{'find_files'},'words':['invoice.pdf']},
 {'id':'feedback','before':['open my invoice file'],'prompt':'your done is nothing','forbid':tool_router.CORE|{'add_task','write_file','run_shell_command','open_application','complete_task','remove_task','set_timer','cancel_timer'}},
 {'id':'timer_correction','before':['pause Spotify'],'prompt':'i meant the timer turn it off i finished studying','need':{'cancel_timer'},'forbid':{'pause_music','add_task'}},
 {'id':'task_list','prompt':'pull up a list i need to do today','need':{'list_tasks'},'words':['dentist','paperwork']},
 {'id':'vault_access','prompt':'could you access my vault','need':{'search_vault'},'deny':["don't have access","do not have access","disabled since","it's been disabled"]},
 {'id':'greeting','prompt':'hey','no_tools':True,'deny':['no response needed','no reply needed',"didn't catch",'rephrase']},
]


def main():
 p=argparse.ArgumentParser();p.add_argument('--rounds',type=int,default=1);p.add_argument('--out');args=p.parse_args()
 sys.stdout.reconfigure(encoding='utf-8',errors='replace')
 bot=brain.Brain();results=[]
 for trial in range(args.rounds):
  for case in CASES:
   ran=[]
   document='invoice.pdf' if case['id'] in ('retry_file','feedback') else 'latest_cv.pdf'
   document_path='C:\\Test\\'+document
   def fixture(name):
    def run(**kwargs):
     ran.append(name)
     values={
      'find_files':'Found 1 match(es), showing 1 (newest first):\n'+document_path,
      'open_application':('Opened '+document_path if kwargs.get('app_name','').replace('/','\\').lower()==document_path.lower() else 'Error: file path was not found. Use find_files before opening a document.'),
      'list_tasks':'2 open:\n- call the dentist\n- file the paperwork',
      'cancel_timer':'Cancelled: study timer.',
      'list_timers':'study timer: 4 minutes remaining',
      'search_vault':'From your notes (quote these):\n\nDenisse/Project Log/Vault Search.md\n- Vault read-back was tried on 2026-09-02 and disabled the same day.\nMemory/STATE.md\n- Current project: BlueBand movement tracker.',
      'get_current_time':'2026-10-26 18:00',
      'list_skills':'math: calculations\ntake_notes: save a note',
      'read_skill':'Use verified data and report what happened.',
     }
     return values.get(name, f'Refused: {name} is not part of this test scenario.')
    return run
   funcs={name:fixture(name) for name in brain.tools.TOOL_FUNCTIONS}
   messages=[]
   for text in case.get('before',[]):
    messages.extend([{'role':'user','content':text},{'role':'assistant','content':'Done.'}])
   messages.append({'role':'user','content':case['prompt']})
   with patch.dict(brain.tools.TOOL_FUNCTIONS,funcs,clear=True):
    reply,_=bot.respond(messages)
   fails=[]
   if not case.get('need',set()) <= set(ran):fails.append('missing tools '+str(case.get('need',set())-set(ran)))
   if set(ran)&case.get('forbid',set()):fails.append('forbidden writes/tools '+str(set(ran)&case['forbid']))
   if case.get('no_tools') and ran:fails.append('greeting called tools')
   if any(w.lower() not in reply.lower() for w in case.get('words',[])):fails.append('missing answer content')
   if any(w in reply.lower() for w in case.get('deny',[])):fails.append('unwanted reply text')
   if 'unknown tool' in reply.lower():fails.append('leaked unknown tool error')
   if brain._CLAIMS_DONE.search(reply) and not set(ran)&{'open_application','cancel_timer'}:fails.append('unsupported success claim')
   r={'trial':trial+1,'id':case['id'],'reply':reply,'tools':ran,'failures':fails,'passed':not fails}
   results.append(r);print(json.dumps(r,ensure_ascii=False),flush=True)
 if args.out:Path(args.out).write_text(json.dumps(results,indent=2,ensure_ascii=False),encoding='utf-8')
 print(f"PASS {sum(r['passed'] for r in results)}/{len(results)}",flush=True)
 return 0 if all(r['passed'] for r in results) else 1

if __name__=='__main__':raise SystemExit(main())
