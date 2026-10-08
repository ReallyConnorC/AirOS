"""Outbound-only Air Family connection. The TV never opens a public control port."""
import json
import re
import threading
import time
import uuid
from pathlib import Path


class FamilyBridge:
    def __init__(self, path, session, cloud, voice, page):
        self.path, self.session, self.cloud, self.voice, self.page = Path(path), session, cloud, voice, page
        self.lock = threading.RLock()
        self.homes = []
        self.error = ''
        self.connected = False
        self.last_fetch = 0

    def selected(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8')).get('home_id')
        except (OSError, ValueError):
            return None

    def rpc(self, name, body=None):
        session = self.session()
        if not session:
            raise ValueError('Sign in to your Air OS account on the TV first.')
        return self.cloud('POST', '/rest/v1/rpc/family_' + name, body or {}, token=session['access_token'])

    def refresh(self):
        homes = self.rpc('me')
        # TVs announce messages only for households granting kitchen/owner access.
        with self.lock:
            self.homes = [h for h in homes if h.get('role') in ('owner', 'kitchen')]
            self.last_fetch = time.monotonic()
        return self.homes

    def choose(self, home_id):
        home_id = str(uuid.UUID(str(home_id)))
        homes = self.refresh()
        if not any(h['id'] == home_id for h in homes):
            raise ValueError('This account cannot announce messages for that household.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps({'home_id': home_id}), encoding='utf-8')
        temp.replace(self.path)

    def status(self):
        try:
            signed_in = bool(self.session())
        except Exception:
            signed_in = False
        if signed_in:
            try:
                self.refresh()
                self.error = ''
            except Exception:
                self.error = 'The household service is unavailable. Check your connection and database setup.'
        else:
            with self.lock:
                self.homes = []
            self.error = 'Sign in to Air OS on this TV, then create a household on your phone.'
        with self.lock:
            return {'page': self.page, 'homes': list(self.homes), 'selected': self.selected(),
                    'connected': self.connected, 'error': self.error}

    @staticmethod
    def spoken(message):
        # These are announcements, never assistant commands or model instructions.
        name = str(message.get('name', 'Someone'))[:32]
        text = str(message.get('text', ''))[:350]
        clean = lambda s: re.sub(r'[\x00-\x1f\[\]]', ' ', s).strip()
        return clean(name) + ' has said: ' + clean(text)

    def poll_once(self):
        if not self.session():
            self.connected = False
            return
        if time.monotonic() - self.last_fetch > 60:
            self.refresh()
        selected = self.selected()
        if not selected:
            if len(self.homes) != 1:
                return
            self.choose(self.homes[0]['id'])
            selected = self.selected()
        if not any(h['id'] == selected for h in self.homes):
            self.connected = False
            return
        # Do not claim a message while a spoken question is being processed.
        with self.voice.lock:
            if self.voice.recorder or not self.voice.work.acquire(blocking=False):
                return
        try:
            message = self.rpc('tv_next', {'p_home': selected})
            self.connected = True
            self.error = ''
            if not message:
                return
            text = self.spoken(message)
            self.voice.show('answer', text, 30)
            self.voice.speak(text)
            self.rpc('tv_ack', {'p_id': message['id']})
        finally:
            self.voice.work.release()

    def run(self):
        while True:
            try:
                self.poll_once()
            except Exception:
                self.connected = False
                self.error = 'Waiting for the online household service.'
            time.sleep(3)

    def start(self):
        threading.Thread(target=self.run, daemon=True, name='air-family').start()
