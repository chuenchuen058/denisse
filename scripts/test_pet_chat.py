"""Regression checks from the pet chat audit; no model calls or real writes."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain
import speech_control
import tool_router
import tray
import voice_output
import wake_word


class PetChatTests(unittest.TestCase):
    def test_observed_stop_requests_are_silent_without_model_or_tools(self):
        for text in ('shush', 'okay stop talkinhg', 'okay its too long stop talking',
                     'you are still talkjing', 'no you are still talking',
                     'Please stop speaking.', 'could you stop talking please?'):
            with self.subTest(text=text), patch.object(brain.ollama, 'chat') as model:
                old = speech_control.capture()
                answer, messages = brain.Brain.__new__(brain.Brain).respond([
                    {'role': 'user', 'content': text}])
                self.assertEqual(answer, '')
                self.assertEqual(messages[-1]['content'], '')
                self.assertTrue(speech_control.cancelled(old))
                model.assert_not_called()

    def test_stop_timer_music_and_quoted_speech_are_not_speech_controls(self):
        for text in ('stop the timer', 'stop listening', 'stop Spotify',
                     "don't stop talking", 'what does shush mean?',
                     'tell me why she said stop talking', 'no, stop my study session'):
            self.assertFalse(speech_control.is_stop_request(text), text)

    def test_pending_audio_cannot_start_after_stop(self):
        audio = voice_output.PreparedAudio.__new__(voice_output.PreparedAudio)
        audio._alias, audio._duration_ms = 'test_audio', 1000
        epoch = speech_control.capture()
        speech_control.stop()
        with patch.object(voice_output, '_mci') as mci:
            audio.play(speech_epoch=epoch)
        mci.assert_not_called()

    def test_playback_stops_on_owner_thread(self):
        audio = voice_output.PreparedAudio.__new__(voice_output.PreparedAudio)
        audio._alias, audio._duration_ms = 'test_audio', 1000
        commands = []
        owner = threading.get_ident()
        def mci(command):
            commands.append((command, threading.get_ident()))
            if command.startswith('play '):
                speech_control.stop()
        with patch.object(voice_output, '_mci', side_effect=mci):
            audio.play()
        self.assertEqual([c for c, _ in commands],
                         ['play test_audio from 0 to 1000', 'stop test_audio'])
        self.assertTrue(all(tid == owner for _, tid in commands))

    def test_local_fallback_also_stops(self):
        engine = Mock()
        engine.isBusy.return_value = True
        engine.iterate.side_effect = speech_control.stop
        voice_output.speak_local(engine, 'A lengthy reply')
        engine.stop.assert_called_once()
        engine.endLoop.assert_called_once()

    def test_typed_stop_works_while_busy_and_without_brain(self):
        popup = tray.ChatPopup.__new__(tray.ChatPopup)
        popup.entry = Mock()
        popup.entry.get.return_value = 'shush'
        popup.busy, popup.brain = True, None
        with patch.object(tray, '_stop_speaking') as stop:
            self.assertEqual(popup._send(), 'break')
        stop.assert_called_once()

    def test_stop_button_cancels_inflight_voice_generation(self):
        gen = wake_word._start_new_request()
        with patch.object(tray, '_chat_popup', None):
            tray._stop_speaking()
        self.assertFalse(wake_word._is_current(gen))

    def test_busy_stop_square_preserves_follow_up_draft(self):
        popup = tray.ChatPopup.__new__(tray.ChatPopup)
        popup.busy, popup.entry = True, Mock()
        with patch.object(tray, '_stop_speaking') as stop:
            popup._send_clicked()
        stop.assert_called_once()
        popup.entry.delete.assert_not_called()

    def test_stop_during_synthesis_prevents_late_playback(self):
        popup = tray.ChatPopup.__new__(tray.ChatPopup)
        epoch = speech_control.capture()
        audio = voice_output.PreparedAudio.__new__(voice_output.PreparedAudio)
        audio._alias, audio._duration_ms = 'test_pending', 1000
        audio.close = Mock()
        popup._reveal_reply = lambda reply, audio, ready: ready.set()
        def synth(text):
            speech_control.stop()
            return 'isolated.mp3'
        root = Mock()
        root.after.side_effect = lambda delay, callback: callback()
        with patch.object(tray, 'root', root, create=True), \
             patch.object(tray, 'synthesize_inworld', side_effect=synth), \
             patch.object(tray, 'prepare_audio', return_value=audio), \
             patch.object(voice_output, '_mci') as mci:
            popup._synthesize_then_reveal('A reply', epoch)
        mci.assert_not_called()
        audio.close.assert_called_once()

    def test_closed_popup_delivers_late_reply_to_reopened_chat(self):
        old = tray.ChatPopup.__new__(tray.ChatPopup)
        old.typing_label = Mock()
        old.typing_label.config.side_effect = tray.tk.TclError('window closed')
        current, orb = Mock(), Mock()
        ready = threading.Event()
        with patch.object(tray, '_chat_popup', current), patch.object(tray, '_floating_orb', orb), \
             patch.object(tray, '_set_thinking'):
            old._reveal_reply('The full reply', None, ready)
        current.receive_reply.assert_called_once_with('The full reply')
        self.assertTrue(ready.is_set())

    def test_open_chat_receives_voice_reply_without_second_bubble(self):
        popup, orb = Mock(), Mock()
        with patch.object(tray, '_chat_popup', popup), patch.object(tray, '_floating_orb', orb), \
             patch.object(tray, 'ThinkingBubble') as bubble:
            tray._show_reply_main('A voice answer')
            popup.receive_reply.assert_called_once_with('A voice answer')
            popup.receive_reply.reset_mock()
            tray._show_reply_main('A typed answer', source='chat')
            popup.receive_reply.assert_not_called()
        bubble.assert_not_called()

    def test_timer_alert_is_visible_without_fake_conversation_record(self):
        popup = tray.ChatPopup.__new__(tray.ChatPopup)
        popup.busy, popup.brain = False, Mock()
        popup._add_bubble, popup._render_history = Mock(), Mock()
        with patch.object(tray.conversation, 'replay', return_value=[]), \
             patch.object(tray.conversation, 'record') as record:
            popup.receive_reply('Your timer is done.')
        popup._add_bubble.assert_called_once_with('denisse', 'Your timer is done.')
        popup._render_history.assert_not_called()
        record.assert_not_called()

    def test_follow_up_keeps_file_request_context(self):
        groups = tool_router.matched_groups([
            {'role': 'user', 'content': 'could you pull up my passport file'},
            {'role': 'assistant', 'content': "I couldn't complete the lookup."},
            {'role': 'user', 'content': 'why cant you?'}])
        self.assertIn('files', groups)

    def test_file_reply_cannot_invent_paths_after_reading_a_skill(self):
        # The 2026-10-28 fabrication: read a skill, then answer with paths
        # that exist nowhere. Since 2026-10-29 the brain no longer stops at
        # "I don't have verified results" -- it runs the search she skipped
        # (brain._file_query / search_forced) and answers from that. The
        # guarantee is unchanged and stricter: the invented path never
        # reaches him, and the real one does, even when the model invents
        # again after the search.
        # One verified match plus "pull up" also triggers the existing
        # single-match auto-open, so the chain is: invented -> searched ->
        # the real file opened. The invented one must never be opened.
        found = 'Found 1 match(es):\nC:\\Real\\Passport ctc..pdf'
        searched, opened = [], []
        responses = iter([
            {'message': {'tool_calls': [{'function': {'name': 'read_skill',
                                                    'arguments': {'name': 'organize_files'}}}]}},
            {'message': {'content': 'Matching files:\nC:\\MadeUp\\Passport.pdf'}},
            {'message': {'content': 'Matching files:\nC:\\MadeUp\\Passport.pdf'}},
            {'message': {'content': 'Opened C:\\MadeUp\\Passport.pdf'}}])
        with patch.object(brain.ollama, 'chat', side_effect=lambda **kw: next(responses)), \
             patch.dict(brain.tools.TOOL_FUNCTIONS,
                        {'read_skill': lambda name: 'Use find_files to locate documents.',
                         'find_files': lambda query, location='': searched.append(query) or found,
                         'open_application': lambda app_name: opened.append(app_name) or f'Opened {app_name}'},
                        clear=True):
            answer, _ = brain.Brain.__new__(brain.Brain).respond([
                {'role': 'user', 'content': 'pull up my passport file'},
                {'role': 'assistant', 'content': "I couldn't complete the lookup."},
                {'role': 'user', 'content': 'why cant you?'}])
        self.assertEqual(searched, ['passport'])
        self.assertEqual(opened, ['C:\\Real\\Passport ctc..pdf'])
        self.assertNotIn('MadeUp', answer)

    def test_file_reply_uses_actual_search_when_model_invents_extra_paths(self):
        found = 'Found 2 matches:\nC:\\Test\\Passport.pdf\nC:\\Test\\Cover.pdf'
        responses = iter([
            {'message': {'tool_calls': [{'function': {'name': 'find_files',
                                                    'arguments': {'query': 'passport'}}}]}},
            {'message': {'content': 'I found C:\\MadeUp\\Passport.pdf'}}])
        with patch.object(brain.ollama, 'chat', side_effect=lambda **kw: next(responses)), \
             patch.dict(brain.tools.TOOL_FUNCTIONS, {'find_files': lambda query: found}, clear=True):
            answer, _ = brain.Brain.__new__(brain.Brain).respond([
                {'role': 'user', 'content': 'find my passport file'}])
        self.assertNotIn('MadeUp', answer)
        self.assertIn('C:\\Test\\Passport.pdf', answer)

    def test_actual_read_result_is_not_replaced_by_search_status(self):
        responses = iter([
            {'message': {'tool_calls': [{'function': {'name': 'read_file',
                                                    'arguments': {'path': 'C:/Test/notes.txt'}}}]}},
            {'message': {'content': 'C:/Test/notes.txt contains the project notes.'}}])
        with patch.object(brain.ollama, 'chat', side_effect=lambda **kw: next(responses)), \
             patch.dict(brain.tools.TOOL_FUNCTIONS, {'read_file': lambda path: 'Project notes.'}, clear=True):
            answer, _ = brain.Brain.__new__(brain.Brain).respond([
                {'role': 'user', 'content': 'read file C:/Test/notes.txt'}])
        self.assertIn('contains the project notes', answer)


if __name__ == '__main__':
    unittest.main(verbosity=2)
