"""Run on the TV or development PC. Secrets are entered privately, never printed."""
import getpass
import json
import os
from pathlib import Path

path = Path(os.environ.get('AIROS_ASSISTANT_CONFIG', Path.home() / '.config/airos/assistant.json'))
try:
    cfg = json.loads(path.read_text(encoding='utf-8'))
except (OSError, ValueError):
    cfg = {}
print('Air voice assistant setup. OpenAI API usage is billed separately from ChatGPT.')
print('Held microphone audio and general questions go to OpenAI. The voice may be AI-generated.')
key = getpass.getpass('Paste your OpenAI API key (hidden; Enter keeps the current key): ').strip()
if key:
    cfg['api_key'] = key
cfg.setdefault('model', 'gpt-4.1-mini')
cfg.setdefault('transcription_model', 'gpt-4o-mini-transcribe')
cfg.setdefault('speech_model', 'tts-1')
cfg.setdefault('voice', 'alloy')
cfg.setdefault('microphone', 'default')
path.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, indent=2)
if os.name != 'nt':
    path.chmod(0o600)
print('Saved privately. Restart the Air OS service to load the new code.')
