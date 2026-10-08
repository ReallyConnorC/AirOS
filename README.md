# Air OS

A TV operating system you install on a Raspberry Pi or a spare PC. It boots straight into a
fullscreen TV interface that you control with a remote or a keyboard.

Air OS is built on Linux, the same way most smart‑TV systems are: a minimal Debian base, a kiosk
display (`cage` + Chromium), and the Air OS interface and system service on top.

## Features

- **Air voice assistant (2.1)**: hold the microphone button or F8 and release to ask.
  Answers appear over the current app. Includes timers, stopwatch, spoken time,
  volume, playback and named Home lights/plugs, plus short AI answers.
  Set up the private API key using [the assistant guide](docs/assistant.md).

- **Unboxing experience**: a new TV greets you with a handwritten "hello" in ten languages and a picture of the remote,
  shows how the remote works, explains privacy, then connects Wi‑Fi, signs in, checks the sound, names the TV,
  picks a look and sets the screen saver.
- **Apple TV‑style home screen**: a big top shelf picture for whatever is selected, and a grid of apps that reads
  left to right, then down, with Continue Watching below. No ads.
- **App Store**: developers upload `.atv` apps on the [Air OS Developer](https://reallyconnorc.github.io/AirOS/developer/)
  website and they appear on every TV at once. Installed apps update themselves within a few hours of a new upload.
  Apps run in a sandbox (their own web origin), so they can't reach Wi‑Fi, settings, the account or files.
- **Home** (with the Home app from the App Store): TP‑Link Tapo lights and plugs are controlled directly on your
  network; RTSP cameras (Tapo indoor/outdoor or other brands) show live; motion pops up a card with the camera's
  picture in the top‑right corner, even over YouTube. Apps only get Home access if you choose Allow when installing.
- **Volume anywhere**: the + and − keys, and volume keys on keyboards and remotes, work inside YouTube and other apps too,
  with an on‑screen volume bar.
- **Factory reset**: Settings → System → Factory reset erases settings, sign‑in, history and saved Wi‑Fi.
  It runs only once; you can repeat it from Settings → Run setup again.
- **Air OS accounts**: sign in or create an account with an email and password. Scan the QR code on the TV with
  your phone to do it on your phone's keyboard, or type it with the remote. Your theme, Live TV playlist and watch
  history follow you to every Air OS TV you sign in on. Signing in and creating accounts happen only on your phone,
  by scanning the QR code on the TV. Needs a free Supabase project (see below).
- **Screen saver**: random scenery photos and the time after 5, 10 or 30 idle minutes (or off).
- **Automatic updates**: every night the TV installs the newest Air OS release from GitHub by itself, and
  Settings → Updates checks right away. Needs the project on GitHub (see below).
- **YouTube**: opens YouTube's own TV interface, which works with the remote, with the mouse pointer hidden. Press **Home** on the remote to come back.
- **Netflix**: on PCs and laptops, the installer uses Google Chrome, which includes the Widevine DRM Netflix needs.
  On a Raspberry Pi it adds Widevine from Raspberry Pi OS. Netflix plays at up to 720p in a browser on Linux.
- **Laptops**: battery level on screen, screen brightness, a choice of sound output (TV over HDMI or the laptop's
  speakers), keeps running with the lid closed, and shows on the TV as soon as HDMI is plugged in.
- **Live TV**: free live news and sports channels, plus any IPTV playlist (M3U) you add. Press ↑/↓ to change channel.
- **My Videos / Music**: plays files from `~/Videos`, `~/Music`, and any USB drive you plug in.
  Seeking works, and videos resume where you left off from **Continue watching**.
- **Settings**: system volume, Wi‑Fi (scan and connect), account, updates, device name, theme, restart and shut down.
- An on‑screen keyboard for passwords and URLs, so you never need a real keyboard.

## Install on a TV box

What you need: a Raspberry Pi 4/5, or any 64‑bit PC, with **Raspberry Pi OS Lite** or **Debian 12+** freshly installed.

1. Copy this folder onto the device (USB stick, `scp`, or `git clone`).
2. Run:
   ```bash
   sudo ./install.sh
   ```
3. Reboot. The device starts Air OS on its own from now on.

The installer creates an `air` user that logs in automatically, installs the packages Air OS needs, puts Air OS in
`/opt/airos`, and hides boot messages. Your settings are kept in `/home/air/.config/airos/config.json`. Logs are in
`/home/air/.local/state/airos/`.

Wi‑Fi management uses NetworkManager. On Debian, interfaces already set up in `/etc/network/interfaces`
stay managed there.

## Set up accounts (once)

1. Create a free project at [supabase.com](https://supabase.com).
2. In the project, open **SQL Editor**, paste in [supabase/setup.sql](supabase/setup.sql) and click **Run**.
3. Open **Project Settings → API** and copy the **Project URL** and the **anon public** key into
   [airos/services.json](airos/services.json) as `supabase_url` and `supabase_anon_key`. The anon key is meant to be
   public; the database rules in `setup.sql` stop accounts from seeing each other's data.
4. Optional: under **Authentication → Sign In / Providers → Email**, turn off **Confirm email** if you don't want new
   accounts to click an email link before they can sign in.

Signing in with a phone: the TV shows a QR code for the sign‑in page at
`https://reallyconnorc.github.io/AirOS/link/` (the [docs/link](docs/link/index.html) folder, served by GitHub Pages).
The phone signs in there and hands its session to the TV through Supabase using a one‑time code that expires after
10 minutes, so it works however the TV is connected. Run [supabase/pairing.sql](supabase/pairing.sql) once to set this up.

## Set up automatic updates (once)

1. Put this folder on GitHub (for example with GitHub Desktop: **File → Add local repository**, then **Publish**).
   The repository must be public so TVs can download it.
2. Set `update_repo` in [airos/services.json](airos/services.json) to `yourname/AirTV`, then install on your TVs.

To ship an update: raise the number in [airos/VERSION](airos/VERSION) (e.g. `1.1` → `1.2`), push, and on GitHub
create a **Release** with the tag `v1.2`. Each TV installs it during the night (between 4 and 5 am) and restarts
Air OS on the new version. Settings → Updates installs it straight away.

## Make apps for the App Store

An `.atv` file is a zip with a `manifest.json`, an icon and the app's web pages (or a `url` for a TV website):

```json
{"id": "com.yourname.weather", "name": "Weather", "version": "1.0.0", "description": "Today's forecast.",
 "icon": "icon.png", "color": "#1a6bd6", "start": "index.html"}
```

Upload it at https://reallyconnorc.github.io/AirOS/developer/ after signing in with an Air OS account. See
[examples/hello-world](examples/hello-world) for a complete app. The remote's arrows, OK (Enter) and Back (Escape) arrive as
normal key presses; Home always returns to Air OS. Run [supabase/store.sql](supabase/store.sql) once to set up the store.
An app that adds `"permissions": ["home"]` to its manifest asks the viewer, at install, for access to the Home service
(`/_home/…` on its own origin, with the one‑time key Air OS passes in the address as `#token=`).
TV websites can also be listed in [store/apps.json](store/apps.json).

## Controls

| Remote / keyboard | Action |
|---|---|
| Arrow keys | Move between items |
| OK / Enter | Select, or pause/play in the player |
| Back / Esc | Go back, or exit the player |
| ← → in the player | Seek 10 s |
| ↑ ↓ in the player | Volume (in Live TV: change channel) |
| H / Home | Go to the home screen (also closes YouTube/Netflix) |

USB, Bluetooth and "air mouse" TV remotes that send these keys work without any setup. Media keys
(play/pause, volume) are also supported.

## Try it on Windows or macOS

```bash
python airos/server.py --open
```

This opens Air OS fullscreen in Chrome or Edge, just like on the TV box. YouTube and Netflix open inside the
same window; press **Home** to come back. Without `--open`, you can visit http://localhost:8080 in any browser,
but apps then open in a separate window. Videos play from your own Videos and Music folders. Wi‑Fi, system volume and
power controls only work on the Linux device.

## Project layout

```
install.sh          turns a Debian / Raspberry Pi OS machine into Air OS
airos/server.py     system service: UI, media files, volume, Wi‑Fi, power (Python standard library only)
airos/session.sh    boot session: starts the service and the fullscreen interface
airos/updater.py    installs the newest GitHub release (runs nightly as root)
airos/services.json addresses of the online services: GitHub repo for updates, Supabase for accounts
airos/VERSION       this version's number, compared with the newest release
supabase/setup.sql  database setup for accounts
airos/ui/index.html the TV interface
airos/ui/fonts/     the handwriting font for the welcome screen (Sacramento, SIL Open Font License)
```

## Limitations

- Netflix works through its website at up to 720p. Full‑quality Netflix and Disney+ apps are only given to certified TV makers.
- YouTube and Netflix take over the screen while open, so only the Home key brings Air OS back. Netflix may stop working if it
  changes which browsers it accepts.
- Video formats are whatever Chromium supports: H.264, VP9 and AV1 in MP4, WebM or MKV. Files in other
  codecs, such as HEVC or AC‑3 audio, may not play.
- Remote control over HDMI‑CEC (your TV's own remote) isn't wired up yet.
- Hyper‑V virtual machines have no sound card and no Wi‑Fi, so Air OS shows "No speakers found" and uses the
  virtual network cable there. Both work on real hardware, and the Windows version plays through Windows' own sound
  output (speakers or headset).
