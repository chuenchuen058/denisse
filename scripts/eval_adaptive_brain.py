"""Real-model ON/OFF comparison using synthetic lessons and a scratch vault.
No real conversation, memory, lessons, tasks or applications are modified.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import brain, brain_rules, instincts, vault_search


def episode(n):
    def user(text, kind):return {'role':'user','content':text,'turn_id':f'{n}-{kind}'}
    def tool(name):return {'role':'assistant','content':'','tool_calls':[{'function':{'name':name,'arguments':{}}}]}
    return [user('daily project brief','bad'),tool('get_current_time'),{'role':'tool','content':'12:00'},
            {'role':'assistant','content':'Done.'},user('no, i meant my current projects','fix'),
            tool('search_vault'),{'role':'tool','content':'From your notes: BlueBand sensor testing.'},
            {'role':'assistant','content':'BlueBand sensor testing.'},user('/good','accepted')]


def main():
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    results=[]
    with tempfile.TemporaryDirectory(prefix='denisse-learning-eval-') as tmp:
        root=Path(tmp);(root/'Memory').mkdir()
        today=vault_search.datetime.date.today().isoformat()
        (root/'Memory/STATE.md').write_text(f'---\nupdated: {today}\n---\n# State\n## {today} - BlueBand\nBlueBand: test the movement sensor prototype.\n',encoding='utf-8')
        rules=root/'rules.json';rules.write_text('{}')
        with patch.dict(os.environ,{'DENISSE_RULES_FILE':str(rules),'DENISSE_INSTINCTS':'1','DENISSE_VAULT_SEARCH':'1'}), \
             patch.object(instincts,'BRAIN_DIR',str(root)), \
             patch.object(instincts,'INSTINCTS_FILE',str(root/'instincts.json')), \
             patch.object(instincts,'INSTINCTS_MD',str(root/'instincts.md')), \
             patch.object(vault_search,'VAULT',str(root)):
            bot=brain.Brain();instincts.rebuild(episode(1)+episode(2))
            for trial in range(3):
                for enabled in (False,True):
                    rules.write_text(json.dumps({'self_learning':enabled}))
                    called=[]
                    def stub(name):
                        def run(**kwargs):
                            called.append(name)
                            if name=='search_vault':return vault_search.search_vault(**kwargs)
                            if name=='get_current_time':return f'{today} 12:00'
                            if name=='get_brain_location':return original_location()
                            return 'Refused: outside this isolated test.'
                        return run
                    original_location=brain.tools.get_brain_location
                    funcs={name:stub(name) for name in brain.tools.TOOL_FUNCTIONS}
                    for prompt in ('daily project brief','what am i working on','what rules do you follow'):
                        called.clear()
                        with patch.dict(brain.tools.TOOL_FUNCTIONS,funcs,clear=True):
                            answer,_=bot.respond([{'role':'user','content':prompt}])
                        passed=('search_vault' in called and 'blueband' in answer.lower()) if prompt!='what rules do you follow' else ('get_brain_location' in called and 'only when asked' in answer.lower() and 'tool selection' in answer.lower())
                        row={'trial':trial+1,'learning':enabled,'prompt':prompt,'tools':list(called),'reply':answer,'passed':passed}
                        results.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
    Path('logs/adaptive_comparison.json').write_text(json.dumps(results,indent=2,ensure_ascii=False),encoding='utf-8')
    return 0 if all(r['passed'] for r in results if r['learning']) else 1

if __name__=='__main__':raise SystemExit(main())
