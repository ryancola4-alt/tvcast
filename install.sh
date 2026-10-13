#!/usr/bin/env bash
# TV Cast — Raspberry Pi installer.
#   curl -fsSL https://YOU.github.io/tvcast/pi/install.sh | bash -s -- https://YOU.github.io/tvcast/
# Safe to run again: it updates the software and keeps your code and settings.
set -euo pipefail

APP_URL="${1:-}"
APP_URL="${APP_URL%/}"
GO2RTC_VERSION="v1.9.14"
BIN="$HOME/.local/share/tvcast"
CONF="$HOME/.config/tvcast"
say()  { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
ask()  { local q="$1" d="${2:-}" a; read -r -p "  $q${d:+ [$d]}: " a </dev/tty || true; echo "${a:-$d}"; }

[ "$(id -u)" -eq 0 ] && { echo "Run this as your normal user (not with sudo)."; exit 1; }
case "$(uname -m)" in aarch64|arm64) ARCH=arm64 ;; x86_64) ARCH=amd64 ;; armv7l) ARCH=arm ;; *) echo "Unsupported CPU: $(uname -m)"; exit 1 ;; esac

say "Installing system packages (a few minutes the first time)"
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg v4l-utils alsa-utils adb python3-aiohttp python3-cryptography qrencode curl >/dev/null
sudo usermod -aG video,audio "$USER" || true

say "Downloading go2rtc (streaming) and cloudflared (encrypted tunnel)"
mkdir -p "$BIN" "$CONF"
G2="go2rtc_linux_$ARCH"; [ "$ARCH" = arm ] && G2="go2rtc_linux_arm"
curl -fsSL -o "$BIN/go2rtc.new" "https://github.com/AlexxIT/go2rtc/releases/download/$GO2RTC_VERSION/$G2" && mv "$BIN/go2rtc.new" "$BIN/go2rtc"
curl -fsSL -o "$BIN/cloudflared.new" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH" && mv "$BIN/cloudflared.new" "$BIN/cloudflared"
chmod +x "$BIN/go2rtc" "$BIN/cloudflared"

say "Installing TV Cast"
if [ -n "$APP_URL" ]; then
  curl -fsSL -o "$BIN/tvcast.py" "$APP_URL/pi/tvcast.py"
elif [ -f "$(dirname "$0")/tvcast.py" ]; then
  cp "$(dirname "$0")/tvcast.py" "$BIN/tvcast.py"
else
  echo "Give your app address, e.g.: bash install.sh https://YOU.github.io/tvcast/"; exit 1
fi
chmod +x "$BIN/tvcast.py"
sudo ln -sf "$BIN/tvcast.py" /usr/local/bin/tvcast
[ -n "$APP_URL" ] && tvcast set app_url "$APP_URL/" >/dev/null

say "Checking the capture card"
if tvcast detect | grep -q '"error"'; then
  warn "No capture card found yet. Plug it into a blue USB 3 port — TV Cast picks it up automatically."
else
  tvcast detect | grep -E '"(name|w|h|fps|audio)"' | sed 's/^/   /'
fi

say "Finding your TV box"
CUR_BOX="$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.config/tvcast/config.json'))).get('box',''))" 2>/dev/null || true)"
if [ -z "$CUR_BOX" ]; then
  FOUND="$(tvcast findbox | grep -oE '[0-9.]+:5555' | head -1 || true)"
  if [ -n "$FOUND" ]; then echo "   Found a box with debugging on: $FOUND"; fi
  BOX="$(ask "TV box IP address (Settings → Network on the box)" "$FOUND")"
  [ -n "$BOX" ] && tvcast set box "$BOX" >/dev/null
  CUR_BOX="$BOX"
fi
if [ -n "$CUR_BOX" ]; then
  echo "   Connecting to $CUR_BOX — if the TV asks 'Allow USB debugging?', tick Always allow and press OK."
  adb connect "$CUR_BOX" >/dev/null 2>&1 || true
  for i in $(seq 1 30); do
    STATE="$(adb -s "$CUR_BOX" get-state 2>/dev/null || true)"
    [ "$STATE" = device ] && break
    sleep 2
  done
  if [ "${STATE:-}" = device ]; then echo "   ✓ TV box control works"; adb -s "$CUR_BOX" shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1 || true
  else warn "Couldn't control the box yet (state: ${STATE:-none}). Check USB debugging is on, then run: tvcast set box IP"; fi
fi

say "Starting TV Cast on boot"
sudo tee /etc/systemd/system/tvcast.service >/dev/null <<EOF
[Unit]
Description=TV Cast (stream + remote for the TV box)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
Environment=HOME=$HOME
ExecStart=/usr/bin/python3 $BIN/tvcast.py
Restart=always
RestartSec=3
Nice=-5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable tvcast >/dev/null 2>&1
sudo systemctl restart tvcast

say "Waiting for the encrypted tunnel"
for i in $(seq 1 30); do
  if journalctl -u tvcast --since "-2min" --no-pager 2>/dev/null | grep -q "published address"; then echo "   ✓ Online and reachable from anywhere"; break; fi
  sleep 2
done

tvcast code
cat <<'EOF'
  Open the link on your phone (or scan the QR), then Add to Home Screen.

  Handy commands:
    tvcast code        show code / link / QR again
    tvcast status      health check
    tvcast newcode     new code (old invites stop working)
    journalctl -u tvcast -f    live log
EOF
