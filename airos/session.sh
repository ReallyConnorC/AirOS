#!/bin/sh
# Air OS session. Runs on tty1 right after autologin. Starts the display (cage), and inside it the
# system service and the fullscreen TV interface. If the interface exits, logout -> autologin restarts it.
AIROS_DIR=/opt/airos
URL=http://127.0.0.1:8080/
STATE="$HOME/.local/state/airos"
mkdir -p "$STATE"

if [ "$1" != "--inside" ]; then
  # -m last: on a laptop plugged into a TV, show Air OS on the TV rather than the laptop's own screen.
  # Without 3D graphics (some PCs and virtual machines) the display can't start; then draw in software instead.
  cage -s -m last -- "$0" --inside >>"$STATE/session.log" 2>&1 && exit 0
  exec env WLR_RENDERER=pixman cage -s -m last -- "$0" --inside >>"$STATE/session.log" 2>&1
fi

# Inside the display: the service inherits WAYLAND_DISPLAY so it can open YouTube/Netflix windows.
pkill -f "$AIROS_DIR/server.py" 2>/dev/null
python3 "$AIROS_DIR/server.py" >>"$STATE/server.log" 2>&1 &

i=0
until curl -fs -o /dev/null "$URL" || [ $i -ge 50 ]; do sleep 0.2; i=$((i + 1)); done

# Google Chrome (PCs) includes Widevine for Netflix; Chromium is used on the Raspberry Pi.
BROWSER=$(command -v google-chrome-stable || command -v chromium || command -v chromium-browser)
exec "$BROWSER" \
  --kiosk --app="$URL" \
  --ozone-platform=wayland \
  --user-data-dir="$HOME/.local/share/airos/$(basename "$BROWSER")" \
  --ignore-gpu-blocklist --enable-gpu-rasterization --enable-zero-copy \
  --enable-features=AcceleratedVideoDecodeLinuxGL,VaapiVideoDecoder,VaapiIgnoreDriverChecks \
  --remote-debugging-port=9222 \
  --autoplay-policy=no-user-gesture-required \
  --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --disable-features=Translate \
  --use-fake-ui-for-media-stream --deny-permission-prompts --password-store=basic \
  --check-for-update-interval=31536000
