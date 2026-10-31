"""Regressions from live conversations; model and tools are isolated, no real writes."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain
import tool_router


def user(text):
    return {'role':'user','content':text}


def reply(text):
    return {'message':{'content':text}}


def call(name, args=None):
    return {'message':{'content':'','tool_calls':[{'function':{'name':name,'arguments':args or {}}}]}}


class DialogueTests(unittest.TestCase):
    def run_turn(self, prompt, responses, functions=None, before=None):
        bot=brain.Brain.__new__(brain.Brain)
        response_iter=iter(responses)
        def chat(**kwargs):
            return next(response_iter, reply('Done.'))
        isolated={name: (lambda **kwargs: 'Refused: isolated tool') for name in brain.tools.TOOL_FUNCTIONS}
        isolated.update(functions or {})
        with patch.object(brain.ollama,'chat',side_effect=chat), patch.dict(brain.tools.TOOL_FUNCTIONS, isolated, clear=True):
            return bot.respond(list(before or [])+[user(prompt)])[0]

    def test_repeated_done_without_action(self):
        result=self.run_turn('pull up my latest cv',[reply('Done.')])
        self.assertFalse(brain._CLAIMS_DONE.search(result), result)

    def test_read_only_success_is_not_action_confirmation(self):
        result=self.run_turn('find my latest cv', [call('find_files',{'query':'cv'}),reply('Done.')],
                             {'find_files':lambda query: 'Found 1 match: C:/test/cv.pdf'})
        self.assertFalse(brain._CLAIMS_DONE.search(result), result)

    def test_wrong_domain_success_does_not_confirm_timer(self):
        result=self.run_turn('start timer', [call('calculate',{'expression':'60*5'}),reply('The timer has been started.')],
                             {'calculate':lambda expression:'300'})
        self.assertFalse(brain._CLAIMS_DONE.search(result), result)

    def test_failed_attempt_does_not_confirm_same_tool(self):
        result=self.run_turn('stop timer', [call('cancel_timer',{'label':'study'}),reply('The timer has been stopped.')],
                             {'cancel_timer':lambda label:'Error: timer service unavailable'})
        self.assertNotIn('has been stopped', result)

    def test_unoffered_tool_cannot_write(self):
        write=Mock(return_value='Added: nothing')
        self.run_turn('hey',[call('add_task',{'task':'nothing'}),reply('No worries.')],{'add_task':write})
        write.assert_not_called()

    def test_feedback_cannot_create_a_task_or_fact(self):
        write=Mock(return_value='Added: nothing')
        remember=Mock(return_value='Saved')
        for name,args,funcs in [('add_task',{'task':'nothing'},{'add_task':write}),
                                ('remember',{'key':'last_message','value':'nothing'},{'remember':remember})]:
            self.run_turn('your done is nothing',[call(name,args),reply('You are right.')],funcs,
                          [user('open a file called passport'),{'role':'assistant','content':'Done.'}])
        write.assert_not_called();remember.assert_not_called()

    def test_fake_json_call_also_obeys_tool_policy(self):
        write=Mock(return_value='Added: nothing')
        self.run_turn('that did not work',[reply('Try this: {"name":"add_task","arguments":{"task":"nothing"}}'),reply('You are right.')],{'add_task':write})
        write.assert_not_called()

    def test_real_task_request_remains_allowed(self):
        write=Mock(return_value='Added: call the dentist')
        result=self.run_turn('remind me to call the dentist',[call('add_task',{'task':'call the dentist'}),reply("I've added that reminder.")],{'add_task':write})
        write.assert_called_once();self.assertIn('added',result.lower())

    def test_successful_vault_search_cannot_be_denied(self):
        result=self.run_turn('could you access my vault',[call('search_vault',{'query':'vault'}),reply("I don't have access to your vault.")],
                             {'search_vault':lambda query:'From your notes (quote these):\n\nMemory/STATE.md\n- Your project is active.'})
        self.assertNotIn("don't have access",result)
        self.assertIn('project',result)

    def test_task_list_must_reach_the_user(self):
        result=self.run_turn('pull up a list i need to do today',[call('list_tasks'),reply('You have a lot on your plate. What first?')],
                             {'list_tasks':lambda:'2 open:\n- call the dentist\n- file the paperwork'})
        self.assertIn('dentist',result)

    def test_raw_json_call_is_recovered_when_allowed(self):
        search=Mock(return_value='Found 1 match: C:/test/cv.pdf')
        self.run_turn('find my cv',[reply('{"name":"find_files","arguments":{"query":"cv"}}'),reply('Found your CV.')],{'find_files':search})
        search.assert_called_once_with(query='cv')

    def test_document_open_requires_an_observed_path(self):
        open_app=Mock(return_value='Opened C:/test/cv.pdf')
        self.run_turn('pull up my latest cv',[call('open_application',{'app_name':'C:/test/cv.pdf'}),reply('I cannot open that yet.')],{'open_application':open_app})
        open_app.assert_not_called()

    def test_document_opens_after_verified_search(self):
        open_app=Mock(return_value='Opened C:/test/cv.pdf')
        self.run_turn('pull up my latest cv',[call('find_files',{'query':'cv'}),call('open_application',{'app_name':'C:/test/cv.pdf'}),reply('Your CV is now open.')],
                      {'find_files':lambda query:'Found 1 match: C:/test/cv.pdf','open_application':open_app})
        open_app.assert_called_once()

    def test_explicit_app_request_after_document_is_not_blocked(self):
        open_app=Mock(return_value='Opened Spotify')
        self.run_turn('open Spotify',[call('open_application',{'app_name':'Spotify'}),reply("I've opened Spotify.")],{'open_application':open_app},[user('find my invoice file')])
        open_app.assert_called_once()

    def test_verified_path_does_not_authorize_suffix_or_failed_lookup(self):
        self.assertFalse(brain._known_document_path('C:/test/cv.pdf',[],{'find_files':'Found 1 match: C:/test/cv.pdf.exe'}))
        self.assertFalse(brain._known_document_path('C:/test/cv.pdf',[],{'find_files':'Error: C:/test/cv.pdf'}))
        self.assertTrue(brain._known_document_path('C:/test/cv.pdf',[user('open C:/test/cv.pdf')],{}))

    def test_feedback_filter_survives_disabled_router(self):
        with patch.dict('os.environ',{'DENISSE_TOOL_ROUTER':'0'}):
            names={d['function']['name'] for d in tool_router.select(brain.TOOL_DEFINITIONS,[user('that did not work')])}
        self.assertFalse(names & tool_router.MUTATING_TOOLS)

    def test_explicit_open_is_completed_after_single_search_match(self):
        open_app=Mock(return_value='Opened C:/test/cv.pdf')
        self.run_turn('pull up my latest cv',[call('find_files',{'query':'cv'}),reply('I found it. Shall I open it?'),reply('Your CV is open.')],
                      {'find_files':lambda query:'Found 1 match: C:/test/cv.pdf','open_application':open_app})
        open_app.assert_called_once_with(app_name='C:/test/cv.pdf')

    def test_search_or_howto_is_not_permission_to_open(self):
        for prompt in ('find my cv','show me how to open my cv'):
            open_app=Mock(return_value='Opened C:/test/cv.pdf')
            self.run_turn(prompt,[call('find_files',{'query':'cv'}),reply('I found your CV.')],
                          {'find_files':lambda query:'Found 1 match: C:/test/cv.pdf','open_application':open_app})
            open_app.assert_not_called()

    def test_multiple_matches_are_not_auto_opened(self):
        open_app=Mock()
        self.run_turn('open my cv',[call('find_files',{'query':'cv'}),reply('Which CV do you want?')],
                      {'find_files':lambda query:'Found 2 matches:\nC:/test/cv.pdf\nC:/test/old_cv.pdf','open_application':open_app})
        open_app.assert_not_called()

    def test_no_running_timer_is_not_a_completed_cancellation(self):
        result=self.run_turn('stop timer',[call('cancel_timer'),reply('Done, Chuen.')],
                             {'cancel_timer':lambda:"There's no timer running, so nothing was stopped."})
        self.assertFalse(brain._CLAIMS_DONE.search(result),result)

    def test_failed_lookup_cannot_prove_file_nonexistence(self):
        result=self.run_turn('find my cv',[call('find_files',{'query':'cv'}),reply('The file does not exist.')],
                             {'find_files':lambda query:'Error: missing query'})
        self.assertNotIn('does not exist',result)
        self.assertIn("can't tell",result)

    def test_empty_search_is_scoped_to_searched_folders(self):
        result=self.run_turn('find my cv',[call('find_files',{'query':'cv'}),reply('The file does not exist.')],
                             {'find_files':lambda query:"No files matching 'cv' found in documents."})
        self.assertRegex(result,'folders (?:searched|I search)')  # short since 2026-10-31

    def test_unrelated_write_does_not_confirm_file_open(self):
        result=self.run_turn('open my cv',[call('remember',{'key':'request','value':'cv'}),reply('Done.')],
                             {'remember':lambda key,value:'Saved request=cv'})
        self.assertFalse(brain._CLAIMS_DONE.search(result),result)

    def test_last_round_does_not_run_a_tool(self):
        write=Mock()
        with patch.object(brain,'MAX_TOOL_ROUNDS',0):
            self.run_turn('add a task to call the dentist',[call('add_task',{'task':'call the dentist'})],{'add_task':write})
        write.assert_not_called()

    def test_file_search_relay_preserves_the_actual_path(self):
        result=self.run_turn('find my cv',[call('find_files',{'query':'cv'}),reply('It is in your Test folder.')],
                             {'find_files':lambda query:'Found 1 match: C:/test/cv.pdf'})
        # 2026-10-31: he asked for names, not paths, with an offer of the path.
        self.assertIn('cv.pdf',result);self.assertTrue(result.endswith(brain._PATH_ASK),result)

    def test_completed_task_is_named(self):
        result=self.run_turn('tick off the paperwork',[call('complete_task',{'task':'paperwork'}),reply('All set.')],
                             {'complete_task':lambda task:'Marked done: file the Spritzer paperwork. 2 other tasks still open; use list_tasks only if asked.'})
        self.assertIn('Spritzer',result)
        self.assertNotIn('list_tasks',result)

    def test_deadline_year_is_preserved_from_retrieved_evidence(self):
        result=self.run_turn('when is my coursework due',[call('search_vault',{'query':'coursework'}),reply('It is due 28 January.')],
                             {'search_vault':lambda query:'From your notes:\n\nCoursework.md\n  (Coursework due 28 Jan 2027)'})
        self.assertIn('2027',result)

    def test_task_no_match_cannot_confirm_completion(self):
        result=self.run_turn('tick off the paperwork',[call('complete_task',{'task':'paperwork'}),reply('Done.')],
                             {'complete_task':lambda task:'No single open task matched "paperwork".'})
        self.assertFalse(brain._CLAIMS_DONE.search(result),result)

    def test_cv_routes_to_file_tools(self):
        self.assertIn('files',tool_router.matched_groups([user('pull up my latest cv')]))

    def test_retry_keeps_file_goal_through_multiple_clarifications(self):
        messages=[user('open a file called passport'),user('just passport then'),user('im pretty sure its in my laptop try again'),user("whats done?")]
        groups=tool_router.matched_groups(messages)
        self.assertIn('files',groups);self.assertNotIn('tasks',groups)

    def test_retry_does_not_become_notes_search(self):
        groups=tool_router.matched_groups([user('find my file'),user('try again')])
        self.assertIn('files',groups);self.assertNotIn('notes',groups)

    def test_explicit_timer_correction_does_not_select_music(self):
        groups=tool_router.matched_groups([user('pause Spotify'),user('i meant the timer turn it off i finished studying')])
        self.assertIn('timer',groups);self.assertNotIn('music',groups)

    def test_fresh_topic_does_not_inherit_file_context(self):
        self.assertEqual(tool_router.matched_groups([user('find my file'),user('hey'),user('and again')]),[])


if __name__=='__main__':
    unittest.main(verbosity=2)
