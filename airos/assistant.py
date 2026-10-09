"""System-owned push-to-talk assistant. No credentials or control API in app pages."""
import datetime
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path


def settings():
    path = Path(os.environ.get('AIROS_ASSISTANT_CONFIG', Path.home() / '.config/airos/assistant.json'))
    try:
        cfg = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cfg = {}
    key = cfg.get('api_key', '')
    provider = cfg.get('provider') or ('groq' if key.startswith('gsk_') or os.environ.get('GROQ_API_KEY') else 'openai')
    if provider not in ('groq', 'openai'):
        raise ValueError('Choose groq or openai as the assistant provider')
    defaults = ({'model': 'openai/gpt-oss-20b', 'transcription_model': 'whisper-large-v3-turbo'}
                if provider == 'groq' else {'model': 'gpt-4.1-mini', 'transcription_model': 'gpt-4o-mini-transcribe'})
    return {**defaults, 'reasoning_effort': 'low', 'speech_model': 'tts-1', 'voice': 'alloy', **cfg, 'provider': provider,
            'api_key': os.environ.get('GROQ_API_KEY' if provider == 'groq' else 'OPENAI_API_KEY') or key}


def request(path, data, content_type='application/json', sink=None):
    cfg = settings()
    if not cfg['api_key']:
        raise RuntimeError('Set up the AI key on this TV first. See docs/assistant.md.')
    body = json.dumps(data).encode() if content_type == 'application/json' else data
    provider = cfg.get('provider', 'openai')
    base = 'https://api.groq.com/openai/v1/' if provider == 'groq' else 'https://api.openai.com/v1/'
    req = urllib.request.Request(base + path, body,
                                 {'Authorization': 'Bearer ' + cfg['api_key'], 'Content-Type': content_type, 'User-Agent': 'AirOS/2.1'})
    try:
        with urllib.request.urlopen(req, timeout=8 if sink else 20) as reply:
            if sink:
                while True:
                    chunk = reply.read1(4096)
                    if not chunk:
                        break
                    sink(chunk)
                return b''
            return reply.read()
    except urllib.error.HTTPError as e:
        try:
            code = json.loads(e.read()).get('error', {}).get('code', '')
        except (ValueError, AttributeError):
            code = ''
        if code in ('credit_balance_exhausted', 'insufficient_quota'):
            message = ('Your Groq allowance is exhausted. Check your Groq account limits.' if provider == 'groq'
                       else 'Your OpenAI API credit is exhausted. Add credit in OpenAI Platform billing.')
        elif e.code == 401:
            message = 'The AI API key was rejected. Update the private key on this TV.'
        elif e.code == 429:
            message = 'The AI service is busy. Wait a moment and try again.'
        else:
            message = f'The AI service returned error {e.code}. Check the model and API account access.'
        raise RuntimeError(message) from None
    except Exception:
        raise RuntimeError('The AI service could not respond. Check the internet, API key and API credit.') from None


def transcribe(path):
    boundary = 'AirOS' + os.urandom(12).hex()
    parts = []
    for key, value in {'model': settings()['transcription_model'], 'response_format': 'json'}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="voice.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
    parts.extend([path.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()])
    return json.loads(request('audio/transcriptions', b''.join(parts), 'multipart/form-data; boundary=' + boundary)).get('text', '').strip()


NUMBERS = dict(zip('one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty'.split(),
                   list(range(1, 21)) + [30, 40, 50, 60]))


def duration(text):
    if re.search(r'-\s*\d|\bminus\b', text):
        raise ValueError('Timer duration must be positive.')
    text = text.lower().replace('half an hour', '30 minutes').replace('a minute', '1 minute').replace('an hour', '1 hour')
    words = '|'.join(NUMBERS)
    text = re.sub(r'\b(' + words + r')[- ](' + words + r')\b', lambda m: str(NUMBERS[m[1]] + NUMBERS[m[2]]), text)
    text = re.sub(r'\b(' + words + r')\b', lambda m: str(NUMBERS[m[1]]), text)
    total = sum(float(n) * {'second': 1, 'minute': 60, 'hour': 3600}[unit]
                for n, unit in re.findall(r'(\d+(?:\.\d+)?)\s*(second|minute|hour)s?\b', text))
    if not 1 <= total <= 86400:
        raise ValueError('Say a timer duration between one second and 24 hours.')
    return int(total)


def elapsed(seconds):
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return ', '.join(f'{v} {u}{"s" if v != 1 else ""}' for v, u in ((h, 'hour'), (m, 'minute'), (s, 'second')) if v) or '0 seconds'


def clean_utterance(text):
    return re.sub(r"\b(?:u+h+|u+m+|e+r+m+|h+m+m+)\b[,.!?]*", "", text, flags=re.I).strip(" ,.!?")


def speech_heard(log):
    # A positive silence_start means audio was present before the first pause.
    # A silence_end event indicates speech resumed after an initial quiet period.
    return any(
        float(value) > .15 for value in re.findall(r"silence_start: ([0-9.]+)", log)
    ) or bool(re.search(r"silence_end: [0-9.]+", log))


def audio_active(wav):
    try:
        with open(wav, 'rb') as audio:
            size = audio.seek(0, 2)
            if size < 3244:
                return False
            audio.seek(-3200, 2)
            samples = struct.unpack('<1600h', audio.read(3200))
        return sum(value * value for value in samples) / len(samples) > 600 ** 2
    except (OSError, struct.error):
        return False


class Assistant:
    def __init__(self, notify, control):
        self.notify, self.control = notify, control
        self.lock = threading.RLock()
        self.work = threading.Lock()
        self.recorder = None
        self.recording = None
        self.record_started = 0
        self.handsfree = None
        self.hesitations = 0
        self.generation = 0
        self.history = []
        self.timers = []
        self.watch_started = None
        self.watch_elapsed = 0
        self.player = None
        self.state = {'phase': 'idle', 'text': '', 'until': 0}
        self.last_timing = {}
        self.speech_cache = {}  # bounded in-memory cache; never stores private speech on disk
        self.speech_backend = ''

    def show(self, phase, text, seconds=10):
        with self.lock:
            self.state = {'phase': phase, 'text': text[:1000], 'until': time.monotonic() + seconds}
        self.notify(self.state)

    def status(self):
        with self.lock:
            return {'configured': bool(settings()['api_key']), 'recording': self.recorder is not None,
                    'last_timing_ms': dict(self.last_timing),
                    'speech_backend': self.speech_backend,
                    'timers': [{'id': t['id'], 'remaining': max(0, int(t['end'] - time.monotonic()))} for t in self.timers],
                    'stopwatch': elapsed(self.watch_elapsed + (time.monotonic() - self.watch_started if self.watch_started is not None else 0))}

    def start(self, continuing=False):
        with self.lock:
            if self.recorder or self.work.locked():
                return
            if not continuing:
                self.hesitations = 0
            if not settings()['api_key']:
                self.show('error', 'AI voice needs setup. Add your private API key using docs/assistant.md.')
                return
            if not shutil.which('ffmpeg'):
                self.show('error', 'Microphone recording needs ffmpeg installed on this device.')
                return
            if self.player and self.player.poll() is None:
                self.player.terminate()
            temp = tempfile.TemporaryDirectory(prefix='airos-voice-')
            wav = Path(temp.name) / 'voice.wav'
            device = settings().get('microphone', 'default')
            if os.name == 'nt':
                if device == 'default':
                    probe = subprocess.run(['ffmpeg', '-hide_banner', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy'], capture_output=True, text=True, timeout=5)
                    microphones = re.findall(r'"([^"]+)" \(audio\)', probe.stderr)
                    if not microphones:
                        temp.cleanup()
                        self.show('error', 'No microphone found. Connect the remote microphone or set microphone in assistant.json.')
                        return
                    device = microphones[0]
                source = ['-f', 'dshow', '-i', 'audio=' + device]
            else:
                source = ['-f', 'pulse', '-i', device]
            log = Path(temp.name) / 'ffmpeg.log'
            try:
                # End a turn on silence for both tapped and held microphone buttons.
                with open(log, 'wb') as err:
                    self.recorder = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'info', '-nostats', *source,
                        '-t', '20', '-af', 'silencedetect=noise=-35dB:d=1.1', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-y', str(wav)],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
            except OSError:
                temp.cleanup()
                self.show('error', 'Could not start microphone recording.')
                return
            self.recording, self.record_started = (temp, wav), time.monotonic()
            self.generation += 1
            generation = self.generation
            self.show('listening', 'Listening… speak now.', 22)
            threading.Thread(target=self._limit, args=(generation,), daemon=True).start()
            threading.Thread(target=self._quiet, args=(generation, log), daemon=True).start()

    def _quiet(self, generation, log):
        """Finish a spoken turn automatically, including while the button is held."""
        started, heard = time.monotonic(), False
        while True:
            time.sleep(.2)
            with self.lock:
                if self.generation != generation or not self.recorder:
                    return
                wav = self.recording[1]
            try:
                text = log.read_text(errors='replace')
            except OSError:
                text = ''
            heard = heard or speech_heard(text) or audio_active(wav)
            quiet_after_speech = heard and text.rfind('silence_start') > text.rfind('silence_end')
            if quiet_after_speech or (not heard and time.monotonic() - started > 7):
                self.stop(generation, discard=not heard)
                return

    def _limit(self, generation):
        # Polling also reports microphone failures promptly, without waiting for the full limit.
        for _ in range(200):
            time.sleep(.1)
            with self.lock:
                if self.generation != generation or not self.recorder:
                    return
                done = self.recorder.poll() is not None
            if done:
                break
        self.stop(generation)

    def stop(self, generation=None, discard=False):
        with self.lock:
            if not self.recorder or (generation is not None and generation != self.generation):
                return
            if generation is None:
                self.handsfree = self.generation  # release keeps listening until speech ends
                return
            proc, (temp, wav) = self.recorder, self.recording
            self.recorder = self.recording = None
            self.work.acquire()
        def finish():
            started = time.monotonic()
            continue_listening = False
            try:
                if proc.poll() is None:
                    try:
                        proc.communicate(b'q\n', timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        proc.kill()
                        proc.wait()
                if discard:
                    self.show('idle', 'No speech heard. Press the microphone button to try again.')
                    return
                if not wav.exists() or wav.stat().st_size < 1000:
                    raise RuntimeError('No microphone audio captured. Check the selected input device.')
                self.show('thinking', 'Understanding…', 45)
                raw = transcribe(wav)
                text = clean_utterance(raw)
                if raw.strip() and not text and self.hesitations < 3:
                    self.hesitations += 1
                    continue_listening = True
                    return
                self.last_timing = {'transcription': round((time.monotonic() - started) * 1000)}
                if not text:
                    raise RuntimeError('I did not catch that. Press the microphone button and try again.')
                self.answer(text)
            except Exception as e:
                self.show('error', str(e))
            finally:
                temp.cleanup()
                self.work.release()
                if continue_listening:
                    self.start(continuing=True)
        threading.Thread(target=finish, daemon=True).start()

    def submit(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('Use a short spoken request.')
        if not self.work.acquire(blocking=False):
            return
        def respond():
            try:
                self.show('thinking', 'Thinking…', 45)
                self.answer(text.strip())
            except Exception as e:
                self.show('error', str(e))
            finally:
                self.work.release()
        threading.Thread(target=respond, daemon=True).start()

    def local(self, text):
        t = text.lower().strip().rstrip('.?!')
        if re.search(r'\b(what.*time|tell.*time|read.*time|time is it)\b', t):
            return 'It is ' + datetime.datetime.now().strftime('%H:%M') + '.'
        if 'stopwatch' in t or 'stop watch' in t:
            with self.lock:
                if re.search(r'\b(reset|clear)\b', t):
                    self.watch_started, self.watch_elapsed = None, 0
                    return 'Stopwatch reset.'
                if re.search(r'\b(start|resume)\b', t):
                    if self.watch_started is None:
                        self.watch_started = time.monotonic()
                    return 'Stopwatch running.'
                if re.search(r'\b(stop|pause)\b', t) and self.watch_started is not None:
                    self.watch_elapsed += time.monotonic() - self.watch_started
                    self.watch_started = None
                return 'Stopwatch: ' + self.status()['stopwatch'] + '.'
        if 'timer' in t:
            with self.lock:
                if re.search(r'\b(cancel|stop|clear|delete)\b', t):
                    self.timers.clear()
                    return 'All timers cancelled.'
                if re.search(r'\b(start|set|create)\b', t):
                    seconds = duration(t)
                    if len(self.timers) >= 20:
                        return 'There are already 20 timers. Cancel timers first.'
                    self.timers.append({'id': (max((x['id'] for x in self.timers), default=0) + 1), 'end': time.monotonic() + seconds})
                    return 'Timer set for ' + elapsed(seconds) + '.'
                return '; '.join(f"Timer {x['id']}: {elapsed(x['end'] - time.monotonic())} remaining" for x in self.timers) or 'No timers running.'
        if t in ('unmute', 'unmute volume', 'unmute the volume'):
            return self.control('volume_set', 30)
        if t in ('mute', 'mute volume', 'mute the volume'):
            return self.control('volume_set', 0)
        if re.search(r'\bvolume\b', t) or t in ('louder', 'quieter'):
            value = re.search(r'\b(\d{1,3})\b', t)
            if value and re.search(r'\b(set|change|turn|volume to)\b', t):
                return self.control('volume_set', int(value[1]))
            if re.search(r'\b(down|lower|quieter|decrease)\b', t):
                return self.control('volume', -5)
            if re.search(r'\b(up|raise|louder|increase)\b', t):
                return self.control('volume', 5)
            if re.search(r'\b(what|read|tell|current)\b', t):
                return self.control('volume_read', None)
        if re.fullmatch(r'(?:please )?(?:open|launch|start)(?: the)? (youtube|you tube|netflix)(?: app)?', t):
            return self.control('open', 'youtube' if 'tube' in t else 'netflix')
        if t in ('go home', 'home screen', 'open home'):
            return self.control('home', None)
        light = re.fullmatch(r'(?:please )?turn (?:the )?(.+?) (on|off)', t)
        if not light:
            light = re.fullmatch(r'(?:please )?turn (on|off) (?:the )?(.+)', t)
            if light:
                return self.control('device', {'name': light[2], 'on': light[1] == 'on'})
        elif light:
            return self.control('device', {'name': light[1], 'on': light[2] == 'on'})
        if t in ('pause', 'pause video', 'pause playback', 'resume', 'resume video', 'resume playback', 'play'):
            return self.control('playback', 'pause' if t.startswith('pause') else 'play')
        return None

    def answer(self, text):
        started = time.monotonic()
        reply = self.local(text)
        if reply is None:
            messages = [{'role': 'system', 'content': 'You are Air, a friendly TV voice assistant. Always return a JSON object with reply, action and value. Speak naturally in one concise sentence, under 180 characters unless more detail is requested. Answer questions directly without introductions. You have no live web access. Do not claim to perform actions. Allowed actions: none, volume (-5 or 5), volume_set (0-100), open (youtube or netflix), home, playback (play or pause), command (a plain timer/stopwatch/time command). No other actions. Never promise unsupported Alexa features or current weather/news.'},
                        *self.history[-8:], {'role': 'user', 'content': text}]
            cfg = settings()
            payload = {'model': cfg['model'], 'messages': messages,
                       'response_format': {'type': 'json_object'}, 'max_completion_tokens': 512, 'temperature': .3}
            if cfg['provider'] == 'groq' and cfg['model'].startswith('openai/gpt-oss-'):
                payload.update(reasoning_effort=cfg.get('reasoning_effort', 'low'), include_reasoning=False)
            result = json.loads(request('chat/completions', payload))
            output = json.loads(result['choices'][0]['message']['content'])
            action, value = output.get('action', 'none'), output.get('value')
            if action == 'command':
                reply = self.local(str(value)) or 'I cannot perform that action yet.'
            elif action in ('volume', 'volume_set', 'open', 'home', 'playback'):
                reply = self.control(action, value)
            else:
                reply = str(output.get('reply', 'Please try asking another way.'))[:900]
            self.history.extend([{'role': 'user', 'content': text}, {'role': 'assistant', 'content': reply}])
            self.history = self.history[-8:]
        self.last_timing['answer'] = round((time.monotonic() - started) * 1000)
        self.show('answer', reply, 15)
        self.speak(reply)

    def speak(self, text):
        cfg = settings()
        if cfg.get('speech_mode', 'cloud') != 'offline' and cfg['api_key'] and shutil.which('ffplay'):
            try:
                groq = cfg['provider'] == 'groq'
                model = cfg.get('speech_model', 'tts-1')
                voice = cfg.get('voice', 'alloy')
                if groq:
                    if model == 'tts-1':
                        model = 'canopylabs/orpheus-v1-english'
                    if voice == 'alloy':
                        voice = 'hannah'
                # Groq accepts at most 200 characters per speech request.
                chunks = []
                remaining = text.strip()
                while remaining:
                    end = min(len(remaining), 200 if groq else 4000)
                    if end < len(remaining):
                        boundary = remaining.rfind(' ', 0, end + 1)
                        if boundary > 0:
                            end = boundary
                    chunks.append(remaining[:end])
                    remaining = remaining[end:].lstrip()
                started = time.monotonic()
                for index, chunk in enumerate(chunks):
                    cache_key = (cfg['provider'], model, voice, chunk)
                    self.player = subprocess.Popen(['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet',
                        '-probesize', '4096', '-analyzeduration', '0', '-f', 'wav', '-i', 'pipe:0'],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    player = self.player
                    audio = bytearray()
                    def feed(data):
                        if not audio and index == 0:
                            self.last_timing['voice_first_audio'] = round((time.monotonic() - started) * 1000)
                        audio.extend(data)
                        player.stdin.write(data)
                        player.stdin.flush()
                    try:
                        if cache_key in self.speech_cache:
                            feed(self.speech_cache[cache_key])
                        else:
                            request('audio/speech', {'model': model, 'voice': voice, 'input': chunk, 'response_format': 'wav'}, sink=feed)
                        player.stdin.close()
                        if player.wait(timeout=45) != 0:
                            raise RuntimeError('Voice playback failed')
                        self.speech_backend = 'cloud'
                        if audio and len(audio) <= 2_000_000:
                            while len(self.speech_cache) >= 16:
                                self.speech_cache.pop(next(iter(self.speech_cache)))
                            self.speech_cache[cache_key] = bytes(audio)
                    finally:
                        if not player.stdin.closed:
                            player.stdin.close()
                self.last_timing['voice_playback_complete'] = round((time.monotonic() - started) * 1000)
                return
            except Exception:
                if self.player and self.player.poll() is None:
                    self.player.terminate()
        # Keep timer alerts and replies audible if the cloud voice is unavailable.
        if shutil.which('espeak-ng'):
            self.speech_backend = 'offline'
            self.player = subprocess.Popen(['espeak-ng', '-v', 'en-gb', '-s', '175', text],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def tick(self):
        refreshed = 0
        while True:
            time.sleep(.2)
            with self.lock:
                due = [t for t in self.timers if t['end'] <= time.monotonic()]
                self.timers = [t for t in self.timers if t not in due]
            if due:
                text = 'Timer finished.' if len(due) == 1 else f'{len(due)} timers finished.'
                self.show('alarm', text, 30)
                threading.Thread(target=self.speak, args=(text,), daemon=True).start()
            # An app may navigate or enter fullscreen while the assistant is speaking.
            if time.monotonic() - refreshed >= 1:
                refreshed = time.monotonic()
                with self.lock:
                    state = dict(self.state)
                if state['until'] > refreshed and state['phase'] != 'idle':
                    self.notify(state)
