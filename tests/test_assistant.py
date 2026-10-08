import json
import io
import urllib.error
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'airos'))
import assistant
import server


class AssistantTests(unittest.TestCase):
    def test_groq_request_uses_only_groq_endpoint(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{}'
        with patch.object(assistant, 'settings', return_value={'provider': 'groq', 'api_key': 'test-groq'}), patch.object(assistant.urllib.request, 'urlopen', return_value=response) as fetch:
            assistant.request('chat/completions', {})
            self.assertEqual(fetch.call_args.args[0].full_url, 'https://api.groq.com/openai/v1/chat/completions')

    def test_groq_never_calls_openai_for_voice(self):
        with patch.object(assistant, 'settings', return_value={'provider': 'groq', 'api_key': 'test-groq'}), patch.object(assistant.shutil, 'which', return_value=None), patch.object(assistant, 'request') as api:
            assistant.Assistant(Mock(), Mock()).speak('Hello')
            api.assert_not_called()

    def test_groq_natural_voice_preserves_long_replies(self):
        text = 'This is a spoken reply. ' * 25
        cfg = {'provider': 'groq', 'api_key': 'test', 'speech_model': 'tts-1', 'voice': 'alloy'}
        player = Mock(); player.wait.return_value = 0
        with patch.object(assistant, 'settings', return_value=cfg), patch.object(assistant.shutil, 'which', return_value='player'), patch.object(assistant, 'request', return_value=b'RIFF') as api, patch.object(assistant.subprocess, 'Popen', return_value=player):
            assistant.Assistant(Mock(), Mock()).speak(text)
        chunks = [call.args[1]['input'] for call in api.call_args_list]
        self.assertEqual(' '.join(chunks), text.strip())
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))
        self.assertTrue(all(call.args[1]['voice'] == 'hannah' and call.args[1]['model'] == 'canopylabs/orpheus-v1-english' for call in api.call_args_list))

    def test_voice_api_failure_falls_back_to_local_speech(self):
        cfg = {'provider': 'groq', 'api_key': 'test'}
        with patch.object(assistant, 'settings', return_value=cfg), patch.object(assistant.shutil, 'which', return_value='available'), patch.object(assistant, 'request', side_effect=RuntimeError('limit')), patch.object(assistant.subprocess, 'Popen') as play:
            assistant.Assistant(Mock(), Mock()).speak('Timer finished.')
            self.assertEqual(play.call_args.args[0][0], 'espeak-ng')

    def test_exhausted_credit_has_clear_private_error(self):
        error = urllib.error.HTTPError('https://api.openai.com', 429, 'quota', {},
            io.BytesIO(json.dumps({'error': {'code': 'credit_balance_exhausted'}}).encode()))
        with patch.object(assistant, 'settings', return_value={'api_key': 'test-secret'}), patch.object(assistant.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'credit is exhausted'):
                assistant.request('chat/completions', {})

    def setUp(self):
        self.control = Mock(return_value='Done.')
        self.air = assistant.Assistant(Mock(), self.control)
        self.air.speak = Mock()

    def test_duration_words_and_multiple_units(self):
        self.assertEqual(assistant.duration('set a timer for twenty five minutes'), 1500)
        self.assertEqual(assistant.duration('one hour and thirty minutes'), 5400)
        self.assertEqual(assistant.duration('half an hour'), 1800)

    def test_invalid_duration(self):
        for text in ('0 seconds', '-5 minutes', '25 hours', 'timer'):
            with self.assertRaises(ValueError):
                assistant.duration(text)

    def test_read_volume_does_not_change_it(self):
        self.air.local('what is the current volume')
        self.control.assert_called_once_with('volume_read', None)
        self.control.reset_mock()
        self.air.local('unmute')
        self.control.assert_called_once_with('volume_set', 30)

    def test_timers_do_not_use_cloud_and_multiple_work(self):
        with patch.object(assistant, 'request') as cloud, patch.object(assistant.time, 'monotonic', return_value=100):
            self.air.answer('set a timer for five minutes')
            self.air.answer('set a timer for ten seconds')
            self.assertEqual([t['end'] for t in self.air.timers], [400, 110])
            self.assertIn('remaining', self.air.local('read my timers'))
            cloud.assert_not_called()
            self.air.local('cancel timers')
            self.assertEqual(self.air.timers, [])

    def test_stopwatch_pause_resume(self):
        with patch.object(assistant.time, 'monotonic', return_value=100):
            self.air.local('start stopwatch')
        with patch.object(assistant.time, 'monotonic', return_value=115):
            self.assertEqual(self.air.local('pause stopwatch'), 'Stopwatch: 15 seconds.')
        with patch.object(assistant.time, 'monotonic', return_value=200):
            self.air.local('resume stopwatch')
        with patch.object(assistant.time, 'monotonic', return_value=205):
            self.assertEqual(self.air.local('read stopwatch'), 'Stopwatch: 20 seconds.')
        self.air.local('reset stopwatch')
        self.assertEqual(self.air.watch_elapsed, 0)

    def test_general_answer_uses_model_and_short_history(self):
        response = {'choices': [{'message': {'content': json.dumps({'reply': 'Hello.', 'action': 'none'})}}]}
        with patch.object(assistant, 'request', return_value=json.dumps(response).encode()) as cloud:
            self.air.answer('hello')
            self.assertEqual(self.air.state['text'], 'Hello.')
            self.assertEqual(cloud.call_args.args[0], 'chat/completions')
            self.assertEqual(len(self.air.history), 2)

    def test_ai_unknown_actions_never_execute(self):
        response = {'choices': [{'message': {'content': json.dumps({'reply': 'Cannot do that.', 'action': 'shell', 'value': 'anything'})}}]}
        with patch.object(assistant, 'request', return_value=json.dumps(response).encode()):
            self.air.answer('run a command')
        self.control.assert_not_called()

    def test_voice_press_does_not_close_app(self):
        with patch.object(server.voice_assistant, 'start') as start, patch.object(server.voice_assistant, 'stop') as stop, patch.object(server, 'go_home') as home:
            server.voice_key(True)
            server.voice_key(False)
            start.assert_called_once()
            stop.assert_called_once()
            home.assert_not_called()

    def test_assistant_cannot_bypass_app_pin(self):
        with patch.object(server, 'read_config', return_value={'pin': '1234', 'locks': {'youtube': True}}), patch.object(server, 'launch_app') as launch:
            self.assertIn('locked', server.assistant_control('open', 'youtube'))
            launch.assert_not_called()

    def test_action_validation(self):
        for action, value in [('open', 'shell'), ('volume_set', 101), ('volume_set', True), ('volume', 999), ('playback', 'delete')]:
            with self.assertRaises(ValueError):
                server.assistant_control(action, value)

    def test_linux_volume_falls_back_to_pulse(self):
        result = Mock(returncode=0, stdout='Volume: front-left: 32768 / 50% / -18.00 dB')
        with patch.object(server.sys, 'platform', 'linux'), patch.object(server.shutil, 'which', side_effect=lambda cmd: cmd if cmd in ('wpctl', 'pactl') else None), patch.object(server, 'run', side_effect=[Mock(returncode=1, stdout=''), result]):
            self.assertEqual(server.get_volume(), 50)

    def test_named_light_requires_exact_device(self):
        with patch.object(server.home, 'load', return_value={'devices': [{'id': '1', 'name': 'Desk light'}]}), patch.object(server.home, 'set_device') as change:
            self.assertIn('exact name', server.assistant_control('device', {'name': 'lights', 'on': False}))
            change.assert_not_called()
            server.assistant_control('device', {'name': 'Desk light', 'on': False})
            change.assert_called_once_with('1', on=False)


if __name__ == '__main__':
    unittest.main()
