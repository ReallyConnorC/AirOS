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
provider = input('Provider: groq (free tier) or openai [groq]: ').strip().lower() or 'groq'
if provider not in ('groq', 'openai'):
    raise SystemExit('Choose groq or openai.')
print('Held microphone audio and general questions go to your chosen provider.')
print('Groq has free usage limits. OpenAI API usage requires separate billing.')
key = getpass.getpass('Paste your API key (hidden; Enter keeps the current key): ').strip()
if key:
    cfg['api_key'] = key
changed = cfg.get('provider', 'openai') != provider
cfg['provider'] = provider
models = ('openai/gpt-oss-20b', 'whisper-large-v3-turbo') if provider == 'groq' else ('gpt-4.1-mini', 'gpt-4o-mini-transcribe')
for field, model in zip(('model', 'transcription_model'), models):
    if changed or not cfg.get(field):
        cfg[field] = model
if changed or not cfg.get('speech_model'):
    cfg['speech_model'] = 'canopylabs/orpheus-v1-english' if provider == 'groq' else 'tts-1'
if changed or not cfg.get('voice'):
    cfg['voice'] = 'hannah' if provider == 'groq' else 'alloy'
cfg.setdefault('reasoning_effort', 'low')
cfg.setdefault('speech_mode', 'cloud')
cfg.setdefault('microphone', 'default')
path.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, indent=2)
if os.name != 'nt':
    path.chmod(0o600)
print('Saved privately. Restart the Air OS service to load the new code.')
