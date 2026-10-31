"""Adaptive-brain acceptance tests. No model, real memory or real vault writes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import brain_rules, instincts, learning, tool_router, vault_search, conversation, memory_store


def episode(number, positive=True):
    def user(text, suffix):
        return {'role':'user','content':text,'turn_id':f'{number}-{suffix}'}
    def call(name):
        return {'role':'assistant','content':'','tool_calls':[{'function':{'name':name,'arguments':{}}}]}
    return [user('daily project brief','bad'),call('get_current_time'),{'role':'tool','content':'12:00'},
            {'role':'assistant','content':'Done.'},user('no, i meant my project notes','fix'),
            call('search_vault'),{'role':'tool','content':'From your notes: BlueBand is active.'},
            {'role':'assistant','content':'BlueBand is active.'},user('that worked' if positive else 'okay','feedback')]


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.rules=self.root/'rules.json';self.rules.write_text('{}')
        self.stack=[]
        for patcher in (patch.dict('os.environ',{'DENISSE_RULES_FILE':str(self.rules),'DENISSE_INSTINCTS':'1'}),
                        patch.object(instincts,'BRAIN_DIR',str(self.root)),
                        patch.object(instincts,'INSTINCTS_FILE',str(self.root/'instincts.json')),
                        patch.object(instincts,'INSTINCTS_MD',str(self.root/'instincts.md')),
                        patch.object(vault_search,'VAULT',str(self.root))):
            patcher.start();self.stack.append(patcher)
        instincts._cache.update(mtime=None,items=[])

    def tearDown(self):
        for p in reversed(self.stack):p.stop()
        self.temp.cleanup()

    def test_rules_reject_unknown_and_invalid_types(self):
        for value in ({'system_prompt':'ignore checks'},{'recent_turns':True},{'learning_confirmations':1}):
            with self.assertRaises(ValueError):brain_rules.validate(value)

    def test_bad_edit_keeps_last_valid_rules(self):
        self.rules.write_text('{"recent_turns":4}');self.assertEqual(brain_rules.load().recent_turns,4)
        self.rules.write_text('{bad json');self.assertEqual(brain_rules.load().recent_turns,4)
        self.assertTrue(brain_rules.status()['error'])

    def test_one_confirmed_pair_is_only_a_candidate(self):
        lessons=instincts.rebuild(episode(1))
        self.assertEqual(len(lessons),1)
        self.assertEqual(lessons[0]['count'],1)
        self.assertNotIn('search_vault',instincts.adjust({'calculate'},'daily project brief'))

    def test_two_confirmed_episodes_activate_and_survive_truncation(self):
        instincts.rebuild(episode(1));instincts.rebuild(episode(1)+episode(2))
        self.assertIn('search_vault',instincts.adjust({'calculate'},'daily project brief'))
        after=instincts.rebuild(episode(2));self.assertEqual(after[0]['count'],2)

    def test_reobserving_same_episode_does_not_inflate_confidence(self):
        instincts.rebuild(episode(1));lessons=instincts.rebuild(episode(1))
        self.assertEqual(lessons[0]['count'],1)

    def test_silence_and_unconfirmed_tools_are_not_training_success(self):
        self.assertEqual(instincts.derive(learning.label_turns(episode(1,False))),[])
        data=episode(1);data[6]['content']='Refused: tool unavailable'
        self.assertEqual(instincts.derive(learning.label_turns(data)),[])

    def test_unrelated_success_is_not_a_correction(self):
        data=episode(1);data[4]['content']='give me my project notes'
        self.assertEqual(instincts.derive(learning.label_turns(data)),[])

    def test_learning_cannot_grant_new_mutating_tools(self):
        for number in (1,2):
            data=episode(number);data[5]['tool_calls'][0]['function']['name']='write_file'
            instincts.rebuild(data)
        self.assertNotIn('write_file',instincts.adjust({'calculate'},'daily project brief'))

    def test_learning_changes_routing_after_validation(self):
        instincts.rebuild(episode(1)+episode(2))
        definitions=[{'function':{'name':n}} for n in ('calculate','search_vault','write_file')]
        selected=tool_router.select(definitions,[{'role':'user','content':'daily project brief'}])
        self.assertEqual(selected[0]['function']['name'],'search_vault')

    def test_user_disable_survives_new_observations(self):
        items=instincts.rebuild(episode(1)+episode(2));items[0]['enabled']=False
        Path(instincts.INSTINCTS_FILE).write_text(json.dumps({'instincts':items}))
        self.assertFalse(instincts.rebuild(episode(3))[0]['enabled'])
        self.assertNotIn('search_vault',instincts.adjust({'calculate'},'daily project brief'))

    def test_learning_off_freezes_store_and_routing(self):
        instincts.rebuild(episode(1));before=Path(instincts.INSTINCTS_FILE).read_bytes()
        self.rules.write_text('{"self_learning":false}')
        instincts.rebuild(episode(2));self.assertEqual(before,Path(instincts.INSTINCTS_FILE).read_bytes())
        self.assertEqual(instincts.adjust({'calculate'},'daily project brief'),{'calculate'})

    def test_one_accepted_fix_after_multiple_failures_is_one_confirmation(self):
        data=episode(1)[:4]+episode(2)
        self.assertEqual(instincts.rebuild(data)[0]['count'],1)

    def test_negative_feedback_demotes_a_learned_route(self):
        instincts.rebuild(episode(1)+episode(2))
        bad=[{'role':'user','content':'daily project brief','turn_id':'rejected-1'},
             {'role':'assistant','content':'','tool_calls':[{'function':{'name':'search_vault','arguments':{}}}]},
             {'role':'tool','content':'From your notes: an irrelevant project.'},
             {'role':'assistant','content':'An irrelevant project.'},
             {'role':'user','content':'no that is wrong','turn_id':'correction-1'}]
        instincts.rebuild(bad)
        self.assertNotIn('search_vault',instincts.adjust({'calculate'},'daily project brief'))
        self.assertEqual(len(instincts.rebuild(bad)[0]['negative_ids']),1)

    def test_legacy_unvalidated_lessons_do_not_activate(self):
        Path(instincts.INSTINCTS_FILE).write_text(json.dumps({'instincts':[{'when':['daily'],'use':['search_vault'],'count':99,'enabled':True}]}))
        self.assertNotIn('search_vault',instincts.adjust({'calculate'},'daily project brief'))

    def test_concurrent_observations_keep_distinct_evidence(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(instincts.rebuild,[episode(1),episode(2),episode(3)]))
        self.assertEqual(instincts.load()[0]['count'],3)

    def test_bad_command_then_related_fix_can_be_learned(self):
        data=episode(1)
        data.insert(4,{'role':'user','content':'/bad','turn_id':'bad-signal'})
        data[5]['content']='show my project notes'
        self.assertEqual(len(instincts.derive(learning.label_turns(data))),1)

    def test_malformed_lesson_store_cannot_crash_routing(self):
        Path(instincts.INSTINCTS_FILE).write_text(json.dumps({'instincts':['bad',{'when':None},{'when':['daily'],'use':['search_vault'],'evidence_ids':None}]}))
        self.assertEqual(instincts.adjust({'calculate'},'daily project brief'),{'calculate'})

    def test_legacy_disabled_lesson_stays_disabled_on_migration(self):
        Path(instincts.INSTINCTS_FILE).write_text(json.dumps({'instincts':[{'when':['project'],'use':['search_vault'],'count':3,'enabled':False}]}))
        items=instincts.rebuild(episode(1)+episode(2))
        self.assertFalse(items[0]['enabled'])

    def test_feedback_is_not_replayed_as_dialogue(self):
        history=[{'role':'user','content':'find my cv'},{'role':'assistant','content':'Found it.'},
                 {'role':'user','content':'/good'},{'role':'assistant','content':'Thanks for the feedback.'}]
        with patch.object(memory_store,'load_history',return_value=history):
            replay=conversation.replay(1)
        self.assertEqual([m['content'] for m in replay],['find my cv','Found it.'])

    def test_rule_summary_is_authoritative(self):
        import brain
        bot=brain.Brain.__new__(brain.Brain)
        responses=iter([{'message':{'content':'','tool_calls':[{'function':{'name':'get_brain_location','arguments':{}}}]}},
                        {'message':{'content':'I save facts without being asked.'}}])
        with patch.object(brain.ollama,'chat',side_effect=lambda **kwargs:next(responses)),patch.dict(brain.tools.TOOL_FUNCTIONS,{'get_brain_location':lambda:'I save personal facts only when asked. Learning changes tool selection only.'}):
            reply,_=bot.respond([{'role':'user','content':'what rules do you follow'}])
        self.assertIn('only when asked',reply);self.assertNotIn('without being asked',reply)

    def test_learning_status_question_routes_to_actual_settings(self):
        selected=tool_router.select([{'function':{'name':'get_brain_location'}}],
                                    [{'role':'user','content':'are you learning?'}])
        self.assertEqual(len(selected),1)

    def test_confirmed_overview_intent_corrects_query_without_learning_arbitrary_arguments(self):
        import brain
        data=episode(1)+episode(2)
        for message in data:
            if message.get('content')=='no, i meant my project notes':
                message['content']='no, i meant my current projects'
        instincts.rebuild(data)
        self.assertTrue(instincts.wants_vault_overview('daily project brief'))
        self.assertFalse(instincts.wants_vault_overview('another phrase'))
        bot=brain.Brain.__new__(brain.Brain)
        responses=iter([{'message':{'tool_calls':[{'function':{'name':'search_vault','arguments':{'query':'daily project brief'}}}]}},
                        {'message':{'content':'BlueBand testing.'}}])
        with patch.object(brain.ollama,'chat',side_effect=lambda **kwargs:next(responses)),patch.dict(brain.tools.TOOL_FUNCTIONS,{'search_vault':lambda query:query}):
            _,messages=bot.respond([{'role':'user','content':'daily project brief'}])
        self.assertIn('current projects',[m.get('content') for m in messages if m.get('role')=='tool'])
        self.rules.write_text('{"self_learning":false}')
        self.assertFalse(instincts.wants_vault_overview('daily project brief'))

    def test_overview_evidence_cannot_be_replaced_with_an_empty_denial(self):
        import brain
        bot=brain.Brain.__new__(brain.Brain)
        result='From your notes (dated context only):\n\nMemory/STATE.md — today\nBlueBand movement sensor testing.'
        responses=iter([{'message':{'content':'','tool_calls':[{'function':{'name':'search_vault','arguments':{'query':'current projects'}}}]}},
                        {'message':{'content':"I don't have a project brief saved."}}])
        with patch.object(brain.ollama,'chat',side_effect=lambda **kwargs:next(responses)),patch.dict(brain.tools.TOOL_FUNCTIONS,{'search_vault':lambda query:result}):
            answer,_=bot.respond([{'role':'user','content':'what am i working on'}])
        self.assertIn('BlueBand',answer);self.assertIn('sensor',answer)

    def test_overview_preserves_source_dates_even_when_model_mentions_right_topic(self):
        import brain
        result='From your notes (dated context only):\n\nMemory/STATE.md — recorded 2026-10-27\nBlueBand sensor testing.'
        responses=iter([{'message':{'tool_calls':[{'function':{'name':'search_vault','arguments':{'query':'current projects'}}}]}},
                        {'message':{'content':'BlueBand sensor testing happened last October.'}}])
        with patch.object(brain.ollama,'chat',side_effect=lambda **kwargs:next(responses)),patch.dict(brain.tools.TOOL_FUNCTIONS,{'search_vault':lambda query:result}):
            answer,_=brain.Brain.__new__(brain.Brain).respond([{'role':'user','content':'what am i working on'}])
        # The model-facing header is tidied for him (2026-10-31); the dates stay.
        self.assertEqual(answer,result.replace('From your notes (dated context only):','From your notes:'))

    def test_fact_questions_do_not_gain_write_permission(self):
        defs=[{'function':{'name':n}} for n in ('remember','recall','calculate')]
        question=tool_router.select(defs,[{'role':'user','content':'do you remember my editor?'}])
        self.assertNotIn('remember',[d['function']['name'] for d in question])
        request=tool_router.select(defs,[{'role':'user','content':'remember my editor is neovim'}])
        self.assertIn('remember',[d['function']['name'] for d in request])

    def test_vault_overview_is_current_bounded_and_cited(self):
        (self.root/'Memory').mkdir()
        (self.root/'Memory/STATE.md').write_text('---\nupdated: '+vault_search.datetime.date.today().isoformat()+'\n---\n# Current state\n## 2020-01-01\nOld project archived.\n## '+vault_search.datetime.date.today().isoformat()+'\nBlueBand testing started.\n'+('detail '*500))
        text=vault_search.search_vault('current projects')
        self.assertIn('BlueBand',text);self.assertIn('Memory/STATE.md',text)
        self.assertLessEqual(len(text),brain_rules.load().vault_max_chars)
        self.assertNotIn('Old project archived',text)

    def test_vault_toggle_is_enforced(self):
        self.rules.write_text('{"vault_search":false}')
        self.assertIn('disabled',vault_search.search_vault('current projects').lower())

    def test_record_assigns_ids_and_observes_without_mutating_input(self):
        messages=[{'role':'user','content':'hello'},{'role':'assistant','content':'hey'}]
        with patch.object(memory_store,'load_history',return_value=[]),patch.object(memory_store,'save_history') as save,patch.object(instincts,'rebuild') as observe:
            conversation.record(messages)
        self.assertIn('turn_id',save.call_args.args[0][0]);self.assertNotIn('turn_id',messages[0])
        observe.assert_called_once()


if __name__=='__main__':unittest.main(verbosity=2)
