#!/usr/bin/env bash
# TV Cast — switch the remote to Bluetooth (for TV boxes that don't allow USB debugging).
# The Pi becomes a Bluetooth remote called "TV Cast Remote" that you pair with the box once.
#   curl -fsSL https://YOU.github.io/tvcast/bt-setup.sh | bash
set -euo pipefail

BIN="$HOME/.local/share/tvcast"
BASE="${1:-}"
if [ -z "$BASE" ]; then
  BASE="$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.config/tvcast/config.json'))).get('app_url',''))" 2>/dev/null || true)"
fi
BASE="${BASE%/}"
[ -z "$BASE" ] && { echo "Usage: bash bt-setup.sh https://YOU.github.io/tvcast/"; exit 1; }
# Works whether the files were uploaded in a pi/ folder or all at the top level.
fetch() { curl -fsSL "$BASE/pi/$1" -o "$2.new" 2>/dev/null || curl -fsSL "$BASE/$1" -o "$2.new"; mv "$2.new" "$2"; }

printf '\n\033[1;36m▸ Installing Bluetooth support\033[0m\n'
sudo apt-get install -y -qq bluez python3-dbus python3-gi >/dev/null
mkdir -p "$BIN"
fetch tvcast.py "$BIN/tvcast.py"
fetch tvcast_bt.py "$BIN/tvcast_bt.py"
chmod +x "$BIN/tvcast.py" "$BIN/tvcast_bt.py"
sudo ln -sf "$BIN/tvcast.py" /usr/local/bin/tvcast
sudo rfkill unblock bluetooth 2>/dev/null || true
# LE-only: otherwise the box can pair over classic Bluetooth, which has no remote (HID) service.
# Private (random) address: a box that ever saw the Pi over classic Bluetooth would otherwise keep trying that way.
sudo sed -i '/^#\?ControllerMode/d; /^#\?Privacy *=/d; /^\[General\]/a ControllerMode = le\nPrivacy = device' /etc/bluetooth/main.conf

sudo tee /etc/systemd/system/tvcast-bt.service >/dev/null <<EOF
[Unit]
Description=TV Cast Bluetooth remote
After=bluetooth.service
Requires=bluetooth.service

[Service]
ExecStart=/usr/bin/python3 $BIN/tvcast_bt.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable tvcast-bt >/dev/null 2>&1
sudo systemctl restart bluetooth
sleep 2
sudo systemctl restart tvcast-bt
tvcast set box bt >/dev/null
sudo systemctl restart tvcast
sleep 4

printf '\n\033[1;36m▸ Bluetooth remote status\033[0m\n'
tvcast bt || { echo; echo "  The helper didn't start. Send this to Claude:"; sudo journalctl -u tvcast-bt -n 20 --no-pager; exit 1; }
cat <<'EOF'

  Now pair it (once) — watch the box in the TV Cast app and use the box's own remote:
    1. Settings → Remotes & Accessories (or Bluetooth) → Add accessory / Pair remote
    2. Pick "TV Cast Remote" and confirm.
    3. In a few seconds the remote in the app works. Check with:  tvcast bt
EOF
