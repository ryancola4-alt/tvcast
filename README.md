# TV Cast

Watch your TV box on your phone or computer from anywhere, with a floating glass remote.

```
TV box ──HDMI──► capture card ──USB──► Raspberry Pi ──► encrypted tunnel ──► your phone / PC
   ▲                  └─HDMI OUT──► TV                    │
   └────────── remote keys over Wi-Fi (ADB) ◄─────────────┘
```

- **At home:** a direct connection gives the lowest delay.
- **Anywhere else:** a free Cloudflare quick tunnel (encrypted, no account, no ports opened on your router).
- **Finding your Pi:** the Pi posts its current address to ntfy.sh, encrypted with your 8-character code. The app looks it up with the same code.
- **No secrets in the web page.** Everything lives in `~/.config/tvcast/config.json` on the Pi.

## Files

| File | What it is |
|---|---|
| `index.html` | The app (phone + Windows). Installable from the browser. |
| `manifest.webmanifest`, `sw.js`, `icons/` | What makes it installable and open instantly. |
| `pi/install.sh` | One-line installer for the Pi. |
| `pi/tvcast.py` | The Pi service: capture, stream, box control, tunnel. |

## 1. Put the app on GitHub Pages (once, about 5 minutes)

1. Sign in at github.com → **+** → **New repository** → name it `tvcast` → **Public** → **Create**.
2. Click **uploading an existing file**. Drag in **everything from this folder**, including the `pi` and `icons` folders → **Commit changes**.
3. Go to **Settings → Pages** → under *Branch* pick `main` and `/ (root)` → **Save**.
4. After about a minute your app is at `https://YOURNAME.github.io/tvcast/`.

## 2. Set up the Pi

1. Use **Raspberry Pi Imager** to flash **Raspberry Pi OS Lite (64-bit)**. Under **Edit settings**:
   - hostname `tvcast`
   - a username and password
   - your Wi-Fi
   - **Services → Enable SSH**
2. Wire it up:
   - TV box HDMI → capture card **IN**
   - capture card **OUT** → TV
   - capture card USB → the Pi's **blue** USB port
   - Ethernet if you can, then power.
3. On the TV box: **Developer options → USB debugging ON** (plus *network/wireless debugging* if listed). Keep it on your main Wi-Fi, not the guest network.
4. On your computer, open Command Prompt:
   ```
   ssh YOURUSER@tvcast.local
   ```
   Then paste (with your own address):
   ```
   curl -fsSL https://YOURNAME.github.io/tvcast/pi/install.sh | bash -s -- https://YOURNAME.github.io/tvcast/
   ```
5. When the TV shows **Allow USB debugging?**, tick **Always allow** → **OK**.
6. The installer ends by showing your **code, link and QR**.

## 3. Install the app

- **iPhone:** open the link in **Safari** → Share → **Add to Home Screen**.
- **Windows:** open the link in **Edge** → **⋯ → Apps → Install this site as an app**.
- **Android:** open it in Chrome → **Install app**.

## Using it

- **Remote:**
  - Drag it by the top bar; resize it with the corner handle.
  - **–** minimizes it, **⋮⋮** shows the number pad.
  - Hold arrows or volume to repeat. Hold OK or Home for a long press.
- **Edit keys (✎):** tap any key, then:
  - **Learn:** press that button on the box's own remote.
  - **Test:** send it now.
  - **Clear / Default:** remove or restore the key.
- **Keyboard (Windows):**
  - arrows, Enter = OK, Esc/Backspace = Back
  - H home, M menu, +/− volume, PgUp/PgDn channel, Space play
  - S sound, F full screen, R remote
- **Check connection** (pulse icon): tests every link (lookup, tunnel, capture card, sound, box control, video path, remote delay) and says what to fix.
- **Settings** (gear):
  - quality, including 480p to save mobile data
  - lip-sync slider
  - rename, restart stream, custom server address
- **Invite** (share icon): the code, QR and copy-link button. Anyone with the code can watch and control, so make a new one to revoke access.

## Pi commands

```
tvcast code            show code, link and QR
tvcast newcode         new code (old invites stop working), then: sudo systemctl restart tvcast
tvcast status          health check
tvcast findbox         find TV boxes with debugging on
tvcast set box 192.168.1.45
tvcast set quality 1080p30        (480p30, 720p30, 720p60, 1080p30)
tvcast detect          show which capture card and audio input will be used
journalctl -u tvcast -f           live log
sudo systemctl restart tvcast
```

## Troubleshooting

- **No picture:**
  - run `tvcast detect`
  - use a USB 3 (blue) port
  - try `tvcast set quality 720p30`
- **Black screen on some apps:** those apps block capture. Live TV usually works.
- **Remote says "unauthorized":** accept the *Allow USB debugging* prompt on the TV.
- **Remote says "offline":**
  - the box's IP changed: run `tvcast findbox`, then `tvcast set box NEW_IP`
  - reserve the box's IP in your router so this doesn't happen again
- **Sound out of sync:** use Settings → Lip-sync.
- **Laggy away from home:**
  - pick 480p or 720p
  - with a router VPN, exclude the Pi under ASUS **VPN Director** (WAN rule)
