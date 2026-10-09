# Air voice assistant

Air OS 2.1 reuses the microphone button. Hold it to record and release to ask.
F8 does the same on a keyboard. The assistant displays over the existing browser
page; it does not go Home or close YouTube. It listens only during the button hold,
with a 20-second limit. Recorded clips are deleted after processing.

## Setup

1. Create a Groq API key at https://console.groq.com/keys for its rate-limited free tier.
   Alternatively, use OpenAI at https://platform.openai.com/ with separate API credit.
2. On the TV, as the `air` user, run `python3 /path/to/AirTV/tools/setup-assistant.py`.
   Choose groq and paste the key at the hidden prompt. Do not put it in services.json or an app package.
   You can also set GROQ_API_KEY or OPENAI_API_KEY in the system service environment.
3. On Linux, run the updated installer to get ffmpeg and espeak-ng, or install
   those packages separately. Restart Air OS after deploying the changed airos files.
4. On a Windows development PC, install ffmpeg (including ffplay) from a reputable
   package source and make it available on PATH. Run the setup script as your
   normal user, then restart `server.py --open`.
5. Connect the remote's USB microphone or another microphone. Hold the voice
   button or F8, ask “set a timer for five minutes”, and release.

Private settings live in `~/.config/airos/assistant.json` (the Linux TV user's
home, or C:\Users\Admin on this development PC). To choose a specific microphone,
set `microphone` to its PulseAudio source name on Linux, or its DirectShow device
name on Windows. Linux's default source follows the existing remote-mic preference.
The assistant reads its settings on each request. All cloud calls use HTTPS.

## Fast defaults

- Groq brain: openai/gpt-oss-20b, short responses with limited conversation history.
- Groq audio recognition: whisper-large-v3-turbo.
- Natural voice: Groq Orpheus English, Hannah, played through ffplay.
  Replies over 200 characters are split to meet the speech API limit.
  Local espeak-ng provides a fallback when cloud speech is unavailable.
  Set speech_mode to offline in private settings to use only local speech.
- Optional OpenAI defaults: gpt-4.1-mini, gpt-4o-mini-transcribe and tts-1.
- Time, timers, stopwatch, volume and named light/plug commands run locally
  after transcription and do not need an additional AI reasoning call.

These model fields can be changed in the private settings file. No secret is sent
to YouTube, installed app pages, the App Store, or the normal configuration API.
Audio sent for transcription and general questions are processed by the selected provider (Groq or OpenAI).
Spoken replies on Windows may be AI-generated.

## Try saying

- “What time is it?”
- “Set a timer for twenty five minutes.” Multiple timers can run together.
- “Read my timers.” / “Cancel timers.” (Cancels all timers.)
- “Start stopwatch.” / “Pause stopwatch.” / “Resume stopwatch.” / “Reset stopwatch.”
- “Read stopwatch.”
- “Volume up.” / “Volume down.” / “Set volume to 40.” / “Mute.”
- “Open YouTube.” / “Open Netflix.” / “Go home.”
- “Pause video.” / “Resume video.”
- “Turn Desk light off.” Use the exact name of a device already added in Home.
- General questions, explanations, jokes and short conversation.

The assistant honours existing app locks. It cannot bypass a PIN. It has no shell,
purchase, account-change, or arbitrary web navigation tool. It does not yet offer
live weather/news, calendars, messaging, wake-word listening, or the full Alexa
feature set. Timers and stopwatch run in the system service across apps, but reset
when the service/device restarts. Playback control requires accessible HTML media
in the current page; some iframe/native players will not be controllable.

## Volume

Windows now reads/writes the actual default audio endpoint rather than guessing
its level. Linux tries PipeWire, PulseAudio, then ALSA and unmutes when raising the
volume. The physical volume keys and +/− can change output inside apps. A volume
overlay is injected when a debug-enabled app page is available.

## Verification

Run `python -m unittest discover -s tests -v`. Tests mock cloud and device actions;
they do not spend API credit or record the microphone. Verify actual microphone,
remote, YouTube playback and audio output on the target TV before releasing.

## Fast replies in Air OS 2.4

Groq GPT OSS 20B uses low reasoning effort with short spoken responses, alongside Whisper Large V3 Turbo transcription. Natural Orpheus audio is piped to ffplay as bytes arrive instead of downloading the whole recording first. A small in-memory cache reuses repeated phrases, and tap-to-talk ends after about 0.65 seconds of quiet. Timers, volume and time commands still run locally after transcription.

The private setup script saves `reasoning_effort: "low"`; existing Groq GPT OSS settings also default to low. `/api/assistant` reports the last transcription, answer and first-audio timings in milliseconds, and whether playback used cloud or offline speech. First-audio timing measures bytes delivered to the player, not an acoustic measurement. Internet speed and microphone detection still affect the total delay.
