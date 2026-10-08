# Air voice assistant

Air OS 2.1 reuses the microphone button. Hold it to record and release to ask.
F8 does the same on a keyboard. The assistant displays over the existing browser
page; it does not go Home or close YouTube. It listens only during the button hold,
with a 20-second limit. Recorded clips are deleted after processing.

## Setup

1. Create an OpenAI API account at https://platform.openai.com/ and create an API key.
   API usage needs its own billing/credit; a ChatGPT subscription does not supply it.
2. On the TV, as the `air` user, run `python3 /path/to/AirTV/tools/setup-assistant.py`.
   Paste the key at the hidden prompt. Do not put it in services.json or an app package.
   You can also set OPENAI_API_KEY in the system service environment.
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

- Brain: gpt-4.1-mini, short responses with limited conversation history.
- Audio recognition: gpt-4o-mini-transcribe.
- Cloud voice: tts-1, chosen for lower latency. Linux uses local espeak-ng for
  quicker speech, including timer alerts. Windows uses cloud voice and ffplay.
- Time, timers, stopwatch, volume and named light/plug commands run locally
  after transcription and do not need an additional AI reasoning call.

These model fields can be changed in the private settings file. No secret is sent
to YouTube, installed app pages, the App Store, or the normal configuration API.
Audio sent for transcription and general questions are processed by OpenAI.
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
