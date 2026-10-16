#!/usr/bin/env bash
# TV Cast — switch the Bluetooth remote to classic keyboard mode (survives restarts and reboots).
#   curl -fsSL https://YOU.github.io/tvcast/bt-classic.sh | bash
set -uo pipefail
BIN="$HOME/.local/share/tvcast"
BASE="$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.config/tvcast/config.json'))).get('app_url',''))" 2>/dev/null)"
BASE="${1:-$BASE}"; BASE="${BASE%/}"
[ -z "$BASE" ] && { echo "Usage: bash bt-classic.sh https://YOU.github.io/tvcast/"; exit 1; }

echo "▸ Getting the new Bluetooth helper"
curl -fsSL "$BASE/tvcast_bt.py" -o /tmp/tvcast_bt.py 2>/dev/null || curl -fsSL "$BASE/pi/tvcast_bt.py" -o /tmp/tvcast_bt.py
grep -q main_classic /tmp/tvcast_bt.py || { echo "GitHub hasn't published the new tvcast_bt.py yet — wait 30 s and run again."; exit 1; }
cp /tmp/tvcast_bt.py "$BIN/tvcast_bt.py"

echo "▸ Forgetting old pairings"
tvcast btforget >/dev/null 2>&1 || true

echo "▸ Setting Bluetooth to classic keyboard mode"
# classic only, shows up as a keyboard, re-pairing allowed without extra prompts
sudo sed -i '/^#\?ControllerMode/d; /^#\?Privacy *=/d; /^#\?Class *=/d; /^#\?JustWorksRepairing *=/d; /^\[General\]/a ControllerMode = bredr\nClass = 0x002540\nJustWorksRepairing = always' /etc/bluetooth/main.conf
# Bluetooth's own keyboard *host* plugin would grab the keyboard channels; the Pi is the keyboard here.
BTD="$(ls /usr/libexec/bluetooth/bluetoothd /usr/lib/bluetooth/bluetoothd 2>/dev/null | head -1)"
[ -z "$BTD" ] && { echo "Can't find bluetoothd"; exit 1; }
sudo mkdir -p /etc/systemd/system/bluetooth.service.d
printf '[Service]\nExecStart=\nExecStart=%s -P input\n' "$BTD" | sudo tee /etc/systemd/system/bluetooth.service.d/tvcast.conf >/dev/null
sudo rm -rf /etc/systemd/system/tvcast-bt.service.d
sudo systemctl daemon-reload
sudo systemctl restart bluetooth
sleep 3
sudo systemctl restart tvcast-bt
sleep 5
tvcast bt
echo
echo "  Now on the TV box: Settings → Bluetooth → forget any old 'TV Cast Remote',"
echo "  then Pair new device → 'TV Cast Remote' (keyboard icon)."
