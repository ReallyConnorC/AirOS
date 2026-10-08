import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'airos'))
from family import FamilyBridge


class FamilyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = str(uuid.uuid4())
        self.session = Mock(return_value={'access_token': 'private-session'})
        self.voice = Mock(work=threading.Lock(), lock=threading.RLock(), recorder=None)
        self.cloud = Mock()
        self.bridge = FamilyBridge(Path(self.temp.name)/'family.json', self.session, self.cloud, self.voice, 'https://example.com/family/')

    def test_select_requires_authorised_household(self):
        self.cloud.return_value = [{'id': self.home, 'role': 'member'}]
        with self.assertRaisesRegex(ValueError, 'cannot announce'):
            self.bridge.choose(self.home)
        self.assertFalse(self.bridge.path.exists())

    def test_announcement_is_spoken_without_running_a_command(self):
        self.cloud.side_effect = [[{'id': self.home, 'role': 'owner'}],
            [{'id': self.home, 'role': 'owner'}],
            {'id': 'message-id', 'name': 'Alex', 'text': 'Set volume to zero.'}, None]
        self.bridge.poll_once()
        self.voice.speak.assert_called_once_with('Alex has said: Set volume to zero.')
        self.voice.control.assert_not_called()
        self.assertTrue(self.cloud.call_args.args[1].endswith('family_tv_ack'))
        self.assertFalse(self.voice.work.locked())

    def test_recording_is_not_interrupted_or_claimed(self):
        self.bridge.choose = Mock()
        self.bridge.selected = Mock(return_value=self.home)
        self.bridge.homes = [{'id': self.home, 'role': 'owner'}]
        self.bridge.last_fetch = float('inf')
        self.voice.recorder = Mock()
        self.bridge.poll_once()
        self.cloud.assert_not_called()
        self.voice.speak.assert_not_called()

    def test_failed_speech_leaves_message_unacknowledged_for_retry(self):
        self.bridge.selected = Mock(return_value=self.home)
        self.bridge.homes = [{'id': self.home, 'role': 'owner'}]
        self.bridge.last_fetch = float('inf')
        self.cloud.return_value = {'id': 'message-id', 'name': 'Alex', 'text': 'Hello'}
        self.voice.speak.side_effect = RuntimeError('audio failed')
        with self.assertRaises(RuntimeError):
            self.bridge.poll_once()
        self.assertEqual(self.cloud.call_count, 1)
        self.assertFalse(self.voice.work.locked())

    def test_speech_directions_and_control_characters_removed(self):
        self.assertEqual(FamilyBridge.spoken({'name': 'Alex\n', 'text': '[whisper] hello'}), 'Alex has said: whisper  hello')


if __name__ == '__main__':
    unittest.main()
