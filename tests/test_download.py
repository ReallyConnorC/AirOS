import io
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'airos'))
import server

class DownloadTests(unittest.TestCase):
    def test_chunked_download_progress_and_installed_files(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('manifest.json', json.dumps({'id': 'com.test.app', 'name': 'Test'}))
            z.writestr('index.html', 'x' * 150000)
        body = buf.getvalue()
        response = io.BytesIO(body)
        response.headers = {'Content-Length': str(len(body))}
        seen = []
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'APPS_DIR', Path(directory)), patch.object(server, 'SERVICES', {'supabase_url': 'https://store.test'}), patch.object(server.urllib.request, 'urlopen', return_value=response), patch.object(server, 'launch_info', return_value={'name': 'Test'}):
            server.install_package('com.test.app', 'https://store.test/storage/v1/object/public/apps/test.atv', lambda state, percent: seen.append((state, percent)))
            self.assertEqual((Path(directory) / 'com.test.app/index.html').stat().st_size, 150000)
        self.assertGreater(len(seen), 2)
        self.assertEqual(seen[-1], ('installing', 100))
        self.assertTrue(any(0 < percent < 99 for state, percent in seen if state == 'downloading'))

    def test_download_failure_is_reported_and_retryable(self):
        event = threading.Event()
        def fail(*args):
            event.set()
            raise ValueError('Network unavailable')
        server.DOWNLOADS.clear()
        with patch.object(server, 'install_package', side_effect=fail):
            server.start_download('com.test.fail', 'test')
            self.assertTrue(event.wait(1))
            # Join actual worker via polling its public status, bounded to one second.
            import time
            deadline = time.monotonic() + 1
            while server.DOWNLOADS['com.test.fail']['state'] != 'error' and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual(server.DOWNLOADS['com.test.fail']['error'], 'Network unavailable')
        server.DOWNLOADS.clear()

    def test_untrusted_package_cannot_download(self):
        with patch.object(server.urllib.request, 'urlopen') as fetch:
            with self.assertRaises(ValueError):
                server.install_package('com.test.app', 'https://untrusted.test/app.atv')
            fetch.assert_not_called()
