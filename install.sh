#!/usr/bin/env bash
# Installs Air OS on a fresh Raspberry Pi OS Lite or Debian 12+ system.
# The machine will boot straight into the Air OS TV interface.
#   sudo ./install.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo ./install.sh"; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)/airos"
DEST=/opt/airos
TV_USER="${AIROS_USER:-air}"
HLS_VERSION=1.5.13
QUIET_BOOT="quiet loglevel=3 logo.nologo vt.global_cursor_default=0"

echo "==> Installing packages"
apt-get update
if apt-cache show chromium >/dev/null 2>&1; then BROWSER_PKG=chromium; else BROWSER_PKG=chromium-browser; fi
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  cage "$BROWSER_PKG" python3 curl ca-certificates procps dbus-user-session \
  network-manager polkitd brightnessctl bluez ffmpeg python3-cryptography \
  pipewire pipewire-pulse wireplumber alsa-utils pulseaudio-utils \
  fonts-noto-core fonts-noto-color-emoji
systemctl enable NetworkManager
# Netflix needs Widevine DRM. Raspberry Pi OS packages it for Chromium; on PCs, Google Chrome includes it.
if apt-cache show libwidevinecdm0 >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y libwidevinecdm0
elif [ "$(dpkg --print-architecture)" = amd64 ]; then
  echo "==> Installing Google Chrome (for Netflix) and hardware video decoding"
  if curl -fsSL -o /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y /tmp/google-chrome.deb  # also adds Google's repo, so Chrome updates itself
    rm -f /tmp/google-chrome.deb
  else
    echo "note: couldn't download Google Chrome, so Netflix won't be offered"
  fi
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends intel-media-va-driver i965-va-driver mesa-va-drivers || true
else
  echo "note: Widevine isn't available for this system, so Netflix won't be offered"
fi

echo "==> Creating the '$TV_USER' user"
id "$TV_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$TV_USER"
for g in video audio render input netdev bluetooth; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$TV_USER"
done
TV_HOME=$(getent passwd "$TV_USER" | cut -d: -f6)
install -d -o "$TV_USER" -g "$TV_USER" "$TV_HOME/Videos" "$TV_HOME/Music"

echo "==> Installing Air OS to $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -r "$SRC"/. "$DEST"/
chmod -R u=rwX,go=rX "$DEST"  # the TV user must read it; copies from Windows (scp) can arrive as owner-only
chmod +x "$DEST/session.sh" "$DEST/server.py" "$DEST/updater.py"
mkdir -p "$DEST/ui/vendor"
curl -fsSL "https://cdnjs.cloudflare.com/ajax/libs/hls.js/$HLS_VERSION/hls.min.js" -o "$DEST/ui/vendor/hls.min.js" \
  || echo "warning: couldn't download hls.js; Live TV will fetch it online at runtime"

echo "==> Booting straight into Air OS"
systemctl set-default multi-user.target  # no desktop environment
mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/airos.conf <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $TV_USER --noclear --skip-login --nonewline --noissue %I \$TERM
EOF
PROFILE="$TV_HOME/.bash_profile"
if ! grep -q "airos-session" "$PROFILE" 2>/dev/null; then
  cat >> "$PROFILE" <<'EOF'
# airos-session
[ -f ~/.profile ] && . ~/.profile
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ "$(tty)" = /dev/tty1 ]; then exec /opt/airos/session.sh; fi
EOF
  chown "$TV_USER:$TV_USER" "$PROFILE"
fi

echo "==> Letting Air OS manage Wi-Fi, Bluetooth, the time zone and updates"
systemctl enable bluetooth 2>/dev/null || true
# The TV user may change these without a password prompt.
cat > /etc/polkit-1/rules.d/50-airos.rules <<EOF
polkit.addRule(function(action, subject) {
  if (subject.user != "$TV_USER") return;
  if (action.id.indexOf("org.freedesktop.NetworkManager.") == 0) return polkit.Result.YES;
  if (action.id == "org.freedesktop.timedate1.set-timezone") return polkit.Result.YES;
  if (action.id == "org.freedesktop.systemd1.manage-units" && action.lookup("unit") == "airos-update.service" &&
      action.lookup("verb") == "start") return polkit.Result.YES;
});
EOF

echo "==> Turning on automatic updates"
mkdir -p /var/lib/airos
cat > /etc/systemd/system/airos-update.service <<EOF
[Unit]
Description=Air OS update
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $DEST/updater.py
EOF
cat > /etc/systemd/system/airos-update.timer <<EOF
[Unit]
Description=Check for Air OS updates every night

[Timer]
OnCalendar=*-*-* 04:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable airos-update.timer

echo "==> Keeping laptops running with the lid closed"
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/airos.conf <<EOF
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF

echo "==> Hiding boot messages"
CMDLINE=""
for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do [ -f "$f" ] && { CMDLINE=$f; break; }; done
if [ -n "$CMDLINE" ]; then
  grep -q "logo.nologo" "$CMDLINE" || sed -i "1 s/\$/ $QUIET_BOOT/" "$CMDLINE"
elif [ -f /etc/default/grub ] && ! grep -q "logo.nologo" /etc/default/grub; then
  sed -i "s/^GRUB_CMDLINE_LINUX_DEFAULT=\"/&$QUIET_BOOT /" /etc/default/grub
  update-grub || true
fi

echo
echo "Air OS is installed. Reboot to start it:  sudo reboot"
echo "Add videos to $TV_HOME/Videos and music to $TV_HOME/Music, or plug in a USB drive."
