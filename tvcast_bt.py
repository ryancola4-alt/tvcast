#!/usr/bin/env python3
"""
TV Cast — Bluetooth remote helper.

Makes the Raspberry Pi show up as a Bluetooth LE keyboard/remote ("TV Cast Remote") that the
TV box pairs with like any remote. Used when the box doesn't allow USB debugging.

  * HID over GATT (the standard every Android box supports): arrows, OK, Back, Home, Menu,
    volume, channel, media keys, numbers.
  * Pairing is "Just Works" — no code to type.
  * tvcast.py sends key presses here on 127.0.0.1:8789 (one JSON object per line).

Runs as root under systemd (tvcast-bt.service). Needs: bluez, python3-dbus, python3-gi.
"""
import json, logging, os, socketserver, sys, threading, time
from collections import deque

NAME = os.environ.get("TVCAST_BT_NAME", "TV Cast Remote")
PORT = int(os.environ.get("TVCAST_BT_PORT", "8789"))
log = logging.getLogger("tvcast-bt")

# ---------------------------------------------------------------- key tables (Android KEYCODE -> HID usage)
KB = {   # Keyboard page (0x07)
    "KEYCODE_DPAD_UP": 0x52, "KEYCODE_DPAD_DOWN": 0x51, "KEYCODE_DPAD_LEFT": 0x50, "KEYCODE_DPAD_RIGHT": 0x4F,
    "KEYCODE_DPAD_CENTER": 0x28, "KEYCODE_ENTER": 0x28, "KEYCODE_MENU": 0x65, "KEYCODE_DEL": 0x2A,
    "KEYCODE_PAGE_UP": 0x4B, "KEYCODE_PAGE_DOWN": 0x4E, "KEYCODE_ESCAPE": 0x29, "KEYCODE_TAB": 0x2B,
    "KEYCODE_SPACE": 0x2C, "KEYCODE_0": 0x27,
    **{f"KEYCODE_{i}": 0x1D + i for i in range(1, 10)},          # 1..9 = 0x1E..0x26
}
CC = {   # Consumer page (0x0C)
    "KEYCODE_HOME": 0x0223, "KEYCODE_BACK": 0x0224, "KEYCODE_SEARCH": 0x0221,
    "KEYCODE_VOLUME_UP": 0x00E9, "KEYCODE_VOLUME_DOWN": 0x00EA, "KEYCODE_VOLUME_MUTE": 0x00E2,
    "KEYCODE_MEDIA_PLAY_PAUSE": 0x00CD, "KEYCODE_MEDIA_PLAY": 0x00B0, "KEYCODE_MEDIA_PAUSE": 0x00B1,
    "KEYCODE_MEDIA_STOP": 0x00B7, "KEYCODE_MEDIA_FAST_FORWARD": 0x00B3, "KEYCODE_MEDIA_REWIND": 0x00B4,
    "KEYCODE_MEDIA_NEXT": 0x00B5, "KEYCODE_MEDIA_PREVIOUS": 0x00B6, "KEYCODE_MEDIA_RECORD": 0x00B2,
    "KEYCODE_CHANNEL_UP": 0x009C, "KEYCODE_CHANNEL_DOWN": 0x009D, "KEYCODE_GUIDE": 0x008D,
    "KEYCODE_POWER": 0x0030, "KEYCODE_SLEEP": 0x0032, "KEYCODE_LAST_CHANNEL": 0x0083,
    "KEYCODE_CAPTIONS": 0x0061, "KEYCODE_INFO": 0x0060, "KEYCODE_SETTINGS": 0x0196,
}

REPORT_MAP = bytes([
    # Keyboard, report 1: modifiers, reserved, 6 keys
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,
    0xC0,
    # Consumer control, report 2: one 16-bit usage
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x02,
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00,
    0xC0,
])


def plan(key, mode="press"):
    """-> (report_name, down_bytes, up_bytes, hold_seconds) or None if the key can't be sent over Bluetooth."""
    hold = 0.75 if mode == "long" else 0.04
    if key in KB:
        return "kb", bytes([0, 0, KB[key], 0, 0, 0, 0, 0]), bytes(8), hold
    if key in CC:
        u = CC[key]
        return "cc", bytes([u & 0xFF, u >> 8]), bytes(2), hold
    return None


# ---------------------------------------------------------------- BlueZ GATT peripheral
def main_le():
    import dbus, dbus.service, dbus.mainloop.glib
    from gi.repository import GLib

    BUS = "org.bluez"
    OM, PROPS = "org.freedesktop.DBus.ObjectManager", "org.freedesktop.DBus.Properties"
    SVC_I, CHR_I, DSC_I = "org.bluez.GattService1", "org.bluez.GattCharacteristic1", "org.bluez.GattDescriptor1"
    AD_I, AGENT_I, DEV_I, ADP_I = "org.bluez.LEAdvertisement1", "org.bluez.Agent1", "org.bluez.Device1", "org.bluez.Adapter1"
    ROOT = "/org/tvcast"

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus() if os.environ.get("TVCAST_BT_BUS") == "session" else dbus.SystemBus()   # session = tests
    loop = GLib.MainLoop()
    state = {"subscribed": False, "linked": False, "registered": False, "advertising": False, "error": ""}

    def ay(b):
        return dbus.Array([dbus.Byte(x) for x in b], signature="y")

    class Application(dbus.service.Object):
        def __init__(self):
            self.path, self.services = ROOT, []
            super().__init__(bus, ROOT)

        @dbus.service.method(OM, out_signature="a{oa{sa{sv}}}")
        def GetManagedObjects(self):
            out = {}
            for s in self.services:
                out[s.get_path()] = s.props()
                for c in s.chars:
                    out[c.get_path()] = c.props()
                    for d in c.descs:
                        out[d.get_path()] = d.props()
            return out

    class Service(dbus.service.Object):
        # Fixed start handle: the box's saved "send me key presses" setting is stored by handle,
        # so keeping handles identical across restarts/reboots means it keeps working without re-pairing.
        def __init__(self, idx, uuid, handle):
            self.path, self.uuid, self.chars, self.handle = f"{ROOT}/service{idx}", uuid, [], handle
            super().__init__(bus, self.path)

        def get_path(self):
            return dbus.ObjectPath(self.path)

        def props(self):
            return {SVC_I: {"UUID": self.uuid, "Primary": True, "Handle": dbus.UInt16(self.handle),
                            "Characteristics": dbus.Array([c.get_path() for c in self.chars], signature="o")}}

        @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, iface):
            return self.props()[SVC_I]

        @dbus.service.method(PROPS, in_signature="ssv")
        def Set(self, iface, name, value):
            if name == "Handle":
                if int(value) != self.handle:
                    log.warning("service %s got handle 0x%04x (wanted 0x%04x)", self.uuid, int(value), self.handle)
                self.handle = int(value)

    class Chrc(dbus.service.Object):
        def __init__(self, svc, uuid, flags, value=b""):
            self.path = f"{svc.path}/char{len(svc.chars)}"
            self.svc, self.uuid, self.flags, self.value, self.descs, self.notifying = svc, uuid, flags, bytes(value), [], False
            svc.chars.append(self)
            super().__init__(bus, self.path)

        def get_path(self):
            return dbus.ObjectPath(self.path)

        def props(self):
            return {CHR_I: {"Service": self.svc.get_path(), "UUID": self.uuid,
                            "Flags": dbus.Array(self.flags, signature="s"),
                            "Descriptors": dbus.Array([d.get_path() for d in self.descs], signature="o")}}

        @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, iface):
            return self.props()[CHR_I]

        @dbus.service.method(CHR_I, in_signature="a{sv}", out_signature="ay")
        def ReadValue(self, options):
            return ay(self.value[int(options.get("offset", 0)):])

        @dbus.service.method(PROPS, in_signature="ssv")
        def Set(self, iface, name, value):
            pass

        @dbus.service.method(CHR_I, in_signature="aya{sv}")
        def WriteValue(self, value, options):
            self.value = bytes(value)

        @dbus.service.method(CHR_I)
        def StartNotify(self):
            self.notifying = True
            on_subscription()

        @dbus.service.method(CHR_I)
        def StopNotify(self):
            self.notifying = False
            on_subscription()

        @dbus.service.signal(PROPS, signature="sa{sv}as")
        def PropertiesChanged(self, interface, changed, invalidated):
            pass

        def send(self, data):
            self.value = bytes(data)
            if self.notifying or state["linked"]:
                self.PropertiesChanged(CHR_I, {"Value": ay(self.value)}, [])
                return True
            return False

    class Desc(dbus.service.Object):
        def __init__(self, chrc, uuid, flags, value):
            self.path = f"{chrc.path}/desc{len(chrc.descs)}"
            self.chrc, self.uuid, self.flags, self.value = chrc, uuid, flags, bytes(value)
            chrc.descs.append(self)
            super().__init__(bus, self.path)

        def get_path(self):
            return dbus.ObjectPath(self.path)

        def props(self):
            return {DSC_I: {"Characteristic": self.chrc.get_path(), "UUID": self.uuid,
                            "Flags": dbus.Array(self.flags, signature="s")}}

        @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, iface):
            return self.props()[DSC_I]

        @dbus.service.method(DSC_I, in_signature="a{sv}", out_signature="ay")
        def ReadValue(self, options):
            return ay(self.value[int(options.get("offset", 0)):])

        @dbus.service.method(PROPS, in_signature="ssv")
        def Set(self, iface, name, value):
            pass

    class Advertisement(dbus.service.Object):
        PATH = f"{ROOT}/advert0"

        def __init__(self):
            super().__init__(bus, self.PATH)

        def props(self):
            return {"Type": "peripheral", "ServiceUUIDs": dbus.Array(["1812"], signature="s"),
                    "Appearance": dbus.UInt16(0x03C1), "LocalName": NAME, "Discoverable": True}

        @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, iface):
            return self.props()

        @dbus.service.method(AD_I)
        def Release(self):
            state["advertising"] = False

    class Agent(dbus.service.Object):
        """Accept pairing without a code ("Just Works") and trust the box so it reconnects by itself."""
        PATH = f"{ROOT}/agent"

        def __init__(self):
            super().__init__(bus, self.PATH)

        def trust(self, device):
            try:
                dbus.Interface(bus.get_object(BUS, device), PROPS).Set(DEV_I, "Trusted", True)
            except Exception as e:
                log.warning("trust failed: %s", e)

        @dbus.service.method(AGENT_I)
        def Release(self): pass

        @dbus.service.method(AGENT_I, in_signature="os")
        def AuthorizeService(self, device, uuid): self.trust(device)

        @dbus.service.method(AGENT_I, in_signature="o", out_signature="s")
        def RequestPinCode(self, device): self.trust(device); return "0000"

        @dbus.service.method(AGENT_I, in_signature="o", out_signature="u")
        def RequestPasskey(self, device): self.trust(device); return dbus.UInt32(0)

        @dbus.service.method(AGENT_I, in_signature="ouq")
        def DisplayPasskey(self, device, passkey, entered): log.info("passkey %06d", passkey)

        @dbus.service.method(AGENT_I, in_signature="os")
        def DisplayPinCode(self, device, pincode): log.info("pin %s", pincode)

        @dbus.service.method(AGENT_I, in_signature="ou")
        def RequestConfirmation(self, device, passkey): self.trust(device)

        @dbus.service.method(AGENT_I, in_signature="o")
        def RequestAuthorization(self, device): self.trust(device)

        @dbus.service.method(AGENT_I)
        def Cancel(self): pass

    # ---- build the HID device
    app = Application()
    hid = Service(0, "1812", 0x0100)
    Chrc(hid, "2a4a", ["read"], [0x11, 0x01, 0x00, 0x02])                 # HID Information
    Chrc(hid, "2a4b", ["encrypt-read"], REPORT_MAP)                        # Report Map (asks the box to pair)
    Chrc(hid, "2a4c", ["write-without-response"], [0])                    # HID Control Point
    Chrc(hid, "2a4e", ["read", "write-without-response"], [1])            # Protocol Mode = report
    kb = Chrc(hid, "2a4d", ["encrypt-read", "notify"], bytes(8))           # Input report 1: keyboard
    Desc(kb, "2908", ["read"], [1, 1])
    cc = Chrc(hid, "2a4d", ["encrypt-read", "notify"], bytes(2))           # Input report 2: consumer keys
    Desc(cc, "2908", ["read"], [2, 1])
    dis = Service(1, "180a", 0x0140)
    Chrc(dis, "2a29", ["read"], b"TV Cast")                                # Manufacturer
    Chrc(dis, "2a50", ["read"], [0x02, 0x6B, 0x1D, 0x46, 0x02, 0x00, 0x01])  # PnP ID (USB, Linux Foundation)
    bat = Service(2, "180f", 0x0160)
    Chrc(bat, "2a19", ["read", "notify"], [100])                           # Battery 100%
    app.services = [hid, dis, bat]
    reports = {"kb": kb, "cc": cc}
    advert, agent = Advertisement(), Agent()

    def on_subscription():
        state["subscribed"] = kb.notifying or cc.notifying or state["linked"]
        log.info("box %s", "connected — remote ready" if state["subscribed"] else "disconnected")

    # ---- adapter setup
    def find_adapter():
        objs = dbus.Interface(bus.get_object(BUS, "/"), OM).GetManagedObjects()
        for path, ifaces in objs.items():
            if "org.bluez.GattManager1" in ifaces and "org.bluez.LEAdvertisingManager1" in ifaces:
                return path
        return None

    adapter_path = find_adapter()
    if not adapter_path:
        log.error("No Bluetooth adapter found (is bluetooth.service running? try: sudo rfkill unblock bluetooth)")
        sys.exit(1)
    adapter = bus.get_object(BUS, adapter_path)
    aprops = dbus.Interface(adapter, PROPS)
    for k, v in (("Powered", True), ("Alias", NAME), ("Pairable", True), ("PairableTimeout", dbus.UInt32(0)),
                 ("Discoverable", False)):   # stay hidden on classic Bluetooth; the box must find us over LE (HID needs LE)
        try:
            aprops.Set(ADP_I, k, v)
        except Exception as e:
            log.warning("adapter %s: %s", k, e)

    am = dbus.Interface(bus.get_object(BUS, "/org/bluez"), "org.bluez.AgentManager1")
    try:
        am.RegisterAgent(dbus.ObjectPath(Agent.PATH), "NoInputNoOutput")
        am.RequestDefaultAgent(dbus.ObjectPath(Agent.PATH))
    except Exception as e:
        log.warning("agent: %s", e)

    def ok_app():
        state["registered"] = True; log.info("HID service registered")

    def ok_ad():
        state["advertising"] = True; log.info("advertising as '%s'", NAME)

    def fail(what):
        def h(err):
            state["error"] = f"{what}: {err}"; log.error(state["error"]); loop.quit()
        return h

    dbus.Interface(adapter, "org.bluez.GattManager1").RegisterApplication(
        dbus.ObjectPath(ROOT), {}, reply_handler=ok_app, error_handler=fail("GATT"))
    dbus.Interface(adapter, "org.bluez.LEAdvertisingManager1").RegisterAdvertisement(
        dbus.ObjectPath(Advertisement.PATH), {}, reply_handler=ok_ad, error_handler=fail("advertising"))

    def devices(objs=None):
        if objs is None:
            objs = dbus.Interface(bus.get_object(BUS, "/"), OM).GetManagedObjects()
        out = []
        for path, ifaces in objs.items():
            d = ifaces.get(DEV_I)
            if d and str(d.get("Adapter")) == adapter_path and (d.get("Paired") or d.get("Bonded") or d.get("Connected")):
                out.append({"name": str(d.get("Alias", d.get("Address", "?"))), "address": str(d.get("Address")),
                            "connected": bool(d.get("Connected")), "paired": bool(d.get("Paired") or d.get("Bonded")),
                            "path": str(path)})
        return out

    # ---- key queue (runs on the GLib loop; keeps presses in order)
    queue, busy = deque(), [False]

    def pump():
        if busy[0] or not queue:
            return False
        busy[0] = True
        item = queue.popleft()
        p = plan(item["key"], item.get("mode", "press"))
        if not p:
            item["res"] = {"ok": False, "err": f"{item['key']} isn't available over Bluetooth"}
            busy[0] = False; item["ev"].set(); GLib.idle_add(pump); return False
        name, down, up, hold = p
        t0 = time.monotonic()
        if not reports[name].send(down):
            item["res"] = {"ok": False, "err": "The TV box isn't connected to the Pi's Bluetooth"}
            busy[0] = False; item["ev"].set(); GLib.idle_add(pump); return False

        def next_one():
            busy[0] = False
            pump()
            return False

        def release():
            reports[name].send(up)
            item["res"] = {"ok": True, "ms": int((time.monotonic() - t0) * 1000)}
            item["ev"].set()
            GLib.timeout_add(12, next_one)          # small gap so the box sees separate presses
            return False
        GLib.timeout_add(int(hold * 1000), release)
        return False

    def submit(item):
        queue.append(item); pump(); return False

    def forget():
        for d in devices():
            try:
                dbus.Interface(adapter, ADP_I).RemoveDevice(d["path"])
            except Exception as e:
                log.warning("remove %s: %s", d["name"], e)
        return False

    # ---- local control socket for tvcast.py
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            for raw in self.rfile:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                cmd = msg.get("cmd")
                if cmd == "key":
                    item = {"key": str(msg.get("key", "")), "mode": msg.get("mode", "press"), "ev": threading.Event()}
                    GLib.idle_add(submit, item)
                    res = item["res"] if item["ev"].wait(5) else {"ok": False, "err": "timeout"}
                elif cmd == "status":
                    try:
                        devs = devices()
                    except Exception as e:
                        devs = []; log.warning("devices: %s", e)
                    res = {"name": NAME, "subscribed": state["subscribed"], "advertising": state["advertising"],
                           "registered": state["registered"], "error": state["error"],
                           "connected": [d["name"] for d in devs if d["connected"]],
                           "paired": [d["name"] for d in devs if d["paired"]]}
                elif cmd == "forget":
                    GLib.idle_add(forget); res = {"ok": True}
                else:
                    res = {"ok": False, "err": "unknown command"}
                self.wfile.write((json.dumps(res) + "\n").encode()); self.wfile.flush()

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    # ---- keep the box connected: if it drops (box slept, Pi restarted), the Pi calls it back
    pending = set()

    def reconnect():
        if state["subscribed"]:
            return True
        try:
            devs = devices()
        except Exception:
            return True
        for d in devs:
            if d["paired"] and not d["connected"] and d["path"] not in pending:
                pending.add(d["path"])
                log.info("reconnecting to %s", d["name"])

                def done(path=d["path"], name=d["name"]):
                    pending.discard(path); log.info("reconnected to %s", name)

                def err(e, path=d["path"]):
                    pending.discard(path)
                    log.info("box not reachable yet (%s)", e.get_dbus_name() if hasattr(e, "get_dbus_name") else e)

                dbus.Interface(bus.get_object(BUS, d["path"]), DEV_I).Connect(
                    reply_handler=done, error_handler=err, timeout=20)
        return True

    def refresh_link():
        # asynchronous, so it can never stall the helper while bluetoothd is busy
        def ok(objs):
            linked = any(d["connected"] and d["paired"] for d in devices(objs))
            if linked != state["linked"]:
                state["linked"] = linked
                on_subscription()
        dbus.Interface(bus.get_object(BUS, "/"), OM).GetManagedObjects(
            reply_handler=ok, error_handler=lambda e: log.warning("link check: %s", e))
        return False

    def on_dev_change(iface, changed, invalidated, path=None):
        if iface == DEV_I and ("Connected" in changed or "Paired" in changed or "Bonded" in changed):
            refresh_link()
        if iface == DEV_I and "Connected" in changed:
            log.info("box %s", "linked" if changed["Connected"] else "unlinked")
            if not changed["Connected"]:
                GLib.timeout_add_seconds(3, lambda: (reconnect(), False)[1])

    bus.add_signal_receiver(on_dev_change, dbus_interface=PROPS, signal_name="PropertiesChanged",
                            bus_name=BUS, path_keyword="path")
    GLib.timeout_add_seconds(15, reconnect)
    GLib.timeout_add_seconds(5, lambda: (refresh_link(), True)[1])   # safety net: re-check the link every 5 s
    refresh_link()

    srv = Server(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("TV Cast Bluetooth remote ready on 127.0.0.1:%d", PORT)
    try:
        loop.run()
    finally:
        srv.shutdown()
    if state["error"]:
        sys.exit(1)


# ================================================================ classic Bluetooth keyboard (default)
# A classic (BR/EDR) HID keyboard has no per-connection "subscribe" step, so it keeps working
# after the Pi or the helper restarts: the Pi simply reconnects to the box like any keyboard does.
SDP_RECORD = """<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001"><sequence><uuid value="0x1124" /></sequence></attribute>
  <attribute id="0x0004"><sequence>
    <sequence><uuid value="0x0100" /><uint16 value="0x0011" /></sequence>
    <sequence><uuid value="0x0011" /></sequence>
  </sequence></attribute>
  <attribute id="0x0005"><sequence><uuid value="0x1002" /></sequence></attribute>
  <attribute id="0x0006"><sequence><uint16 value="0x656e" /><uint16 value="0x006a" /><uint16 value="0x0100" /></sequence></attribute>
  <attribute id="0x0009"><sequence><sequence><uuid value="0x1124" /><uint16 value="0x0101" /></sequence></sequence></attribute>
  <attribute id="0x000d"><sequence><sequence>
    <sequence><uuid value="0x0100" /><uint16 value="0x0013" /></sequence>
    <sequence><uuid value="0x0011" /></sequence>
  </sequence></sequence></attribute>
  <attribute id="0x0100"><text value="%(name)s" /></attribute>
  <attribute id="0x0101"><text value="Remote keyboard" /></attribute>
  <attribute id="0x0102"><text value="TV Cast" /></attribute>
  <attribute id="0x0200"><uint16 value="0x0100" /></attribute>
  <attribute id="0x0201"><uint16 value="0x0111" /></attribute>
  <attribute id="0x0202"><uint8 value="0x40" /></attribute>
  <attribute id="0x0203"><uint8 value="0x00" /></attribute>
  <attribute id="0x0204"><boolean value="true" /></attribute>
  <attribute id="0x0205"><boolean value="true" /></attribute>
  <attribute id="0x0206"><sequence><sequence><uint8 value="0x22" /><text encoding="hex" value="%(map)s" /></sequence></sequence></attribute>
  <attribute id="0x0207"><sequence><sequence><uint16 value="0x0409" /><uint16 value="0x0100" /></sequence></sequence></attribute>
  <attribute id="0x020b"><uint16 value="0x0100" /></attribute>
  <attribute id="0x020c"><uint16 value="0x0c80" /></attribute>
  <attribute id="0x020d"><boolean value="false" /></attribute>
  <attribute id="0x020e"><boolean value="false" /></attribute>
  <attribute id="0x020f"><uint16 value="0x0640" /></attribute>
  <attribute id="0x0210"><uint16 value="0x0320" /></attribute>
</record>
"""
PSM_CTRL, PSM_INTR = 0x11, 0x13


def hidp_reply(msg):
    """Answer the box's HID control messages (HIDP). Returns bytes to send back, or None."""
    if not msg:
        return None
    kind, param = msg[0] >> 4, msg[0] & 0x0F
    if kind in (0x5, 0x7, 0x9):          # SET_REPORT / SET_PROTOCOL / SET_IDLE -> "OK"
        return b"\x00"
    if kind == 0x6:                      # GET_PROTOCOL -> report protocol
        return b"\xA0\x01"
    if kind == 0x8:                      # GET_IDLE
        return b"\xA0\x00"
    if kind == 0x4:                      # GET_REPORT -> current (empty) report
        rid = msg[1] if len(msg) > 1 else 1
        return b"\xA1" + bytes([rid]) + (bytes(8) if rid == 1 else bytes(2))
    return None


def frame(name, data):
    """Interrupt-channel input report: DATA|Input, report id, payload."""
    return b"\xA1" + (b"\x01" if name == "kb" else b"\x02") + bytes(data)


def main_classic():
    import socket
    import dbus, dbus.service, dbus.mainloop.glib
    from gi.repository import GLib

    BUS, OM, PROPS = "org.bluez", "org.freedesktop.DBus.ObjectManager", "org.freedesktop.DBus.Properties"
    ADP_I, DEV_I, AGENT_I = "org.bluez.Adapter1", "org.bluez.Device1", "org.bluez.Agent1"
    ROOT = "/org/tvcast"
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    loop = GLib.MainLoop()
    st = {"ctrl": None, "intr": None, "host": "", "error": "", "advertising": False, "busy": False}

    # ---- adapter
    objs = dbus.Interface(bus.get_object(BUS, "/"), OM).GetManagedObjects()
    adapter_path = next((p for p, i in objs.items() if ADP_I in i), None)
    if not adapter_path:
        log.error("No Bluetooth adapter found"); sys.exit(1)
    adapter = bus.get_object(BUS, adapter_path)
    aprops = dbus.Interface(adapter, PROPS)
    for k, v in (("Powered", True), ("Alias", NAME), ("Pairable", True), ("PairableTimeout", dbus.UInt32(0)),
                 ("DiscoverableTimeout", dbus.UInt32(0)), ("Discoverable", True)):
        try:
            aprops.Set(ADP_I, k, v)
        except Exception as e:
            log.warning("adapter %s: %s", k, e)
    st["advertising"] = True

    # ---- pairing agent: no code to type; trust the box so it can reconnect by itself
    class Agent(dbus.service.Object):
        PATH = ROOT + "/agent"
        def trust(self, device):
            try:
                dbus.Interface(bus.get_object(BUS, device), PROPS).Set(DEV_I, "Trusted", True)
            except Exception as e:
                log.warning("trust: %s", e)
        @dbus.service.method(AGENT_I)
        def Release(self): pass
        @dbus.service.method(AGENT_I, in_signature="os")
        def AuthorizeService(self, d, u): self.trust(d)
        @dbus.service.method(AGENT_I, in_signature="o", out_signature="s")
        def RequestPinCode(self, d): self.trust(d); return "0000"
        @dbus.service.method(AGENT_I, in_signature="o", out_signature="u")
        def RequestPasskey(self, d): self.trust(d); return dbus.UInt32(0)
        @dbus.service.method(AGENT_I, in_signature="ouq")
        def DisplayPasskey(self, d, p, e): log.info("passkey %06d", p)
        @dbus.service.method(AGENT_I, in_signature="os")
        def DisplayPinCode(self, d, p): log.info("pin %s", p)
        @dbus.service.method(AGENT_I, in_signature="ou")
        def RequestConfirmation(self, d, p): self.trust(d)
        @dbus.service.method(AGENT_I, in_signature="o")
        def RequestAuthorization(self, d): self.trust(d)
        @dbus.service.method(AGENT_I)
        def Cancel(self): pass

    class Profile(dbus.service.Object):
        PATH = ROOT + "/hid"
        @dbus.service.method("org.bluez.Profile1")
        def Release(self): pass
        @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}")
        def NewConnection(self, device, fd, props): pass
        @dbus.service.method("org.bluez.Profile1", in_signature="o")
        def RequestDisconnection(self, device): close_all("host asked")

    agent, profile = Agent(bus, Agent.PATH), Profile(bus, Profile.PATH)
    am = dbus.Interface(bus.get_object(BUS, "/org/bluez"), "org.bluez.AgentManager1")
    try:
        am.RegisterAgent(dbus.ObjectPath(Agent.PATH), "NoInputNoOutput"); am.RequestDefaultAgent(dbus.ObjectPath(Agent.PATH))
    except Exception as e:
        log.warning("agent: %s", e)
    pm = dbus.Interface(bus.get_object(BUS, "/org/bluez"), "org.bluez.ProfileManager1")
    try:
        pm.RegisterProfile(dbus.ObjectPath(Profile.PATH), "00001124-0000-1000-8000-00805f9b34fb", {
            "ServiceRecord": SDP_RECORD % {"name": NAME, "map": REPORT_MAP.hex()},
            "Role": "server", "RequireAuthentication": True, "RequireAuthorization": False})
    except Exception as e:
        st["error"] = f"HID profile: {e}"; log.error(st["error"])

    # ---- L2CAP channels (control 17, interrupt 19)
    def listener(psm):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((socket.BDADDR_ANY, psm))
        s.listen(1)
        return s
    try:
        srv_ctrl, srv_intr = listener(PSM_CTRL), listener(PSM_INTR)
    except OSError as e:
        st["error"] = (f"can't open the keyboard channels ({e}). Bluetooth's own input plugin must be off: "
                       "run bt-classic.sh again")
        log.error(st["error"]); srv_ctrl = srv_intr = None

    def close_all(why):
        for k in ("intr", "ctrl"):
            if st[k]:
                try: st[k].close()
                except OSError: pass
                st[k] = None
        if why:
            log.info("box disconnected (%s)", why)

    def watch(sock, name):
        def on_data(fd, cond):
            if cond & (GLib.IO_HUP | GLib.IO_ERR):
                close_all(f"{name} closed"); return False
            try:
                data = sock.recv(64)
            except OSError:
                close_all(f"{name} error"); return False
            if not data:
                close_all(f"{name} closed"); return False
            if name == "ctrl":
                if data[0] == 0x15:          # HID_CONTROL virtual cable unplug
                    close_all("unplugged"); return False
                r = hidp_reply(data)
                if r is not None:
                    try: sock.send(r)
                    except OSError: pass
            return True
        GLib.io_add_watch(sock.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, on_data)

    def adopt(name, sock, addr):
        if st[name]:
            try: st[name].close()
            except OSError: pass
        st[name] = sock; st["host"] = addr
        watch(sock, name)
        if name == "intr":
            log.info("box connected (%s) — remote ready", addr)

    def on_accept(srv, name):
        def cb(fd, cond):
            try:
                sock, (addr, _) = srv.accept()
            except OSError as e:
                log.warning("accept %s: %s", name, e); return True
            adopt(name, sock, addr)
            return True
        return cb
    if srv_ctrl:
        GLib.io_add_watch(srv_ctrl.fileno(), GLib.IO_IN, on_accept(srv_ctrl, "ctrl"))
        GLib.io_add_watch(srv_intr.fileno(), GLib.IO_IN, on_accept(srv_intr, "intr"))

    # ---- reconnect: like any keyboard, the Pi calls the box back after restarts / sleep
    def paired_hosts():
        objs = dbus.Interface(bus.get_object(BUS, "/"), OM).GetManagedObjects()
        out = []
        for p, i in objs.items():
            d = i.get(DEV_I)
            if d and str(d.get("Adapter")) == adapter_path and d.get("Paired"):
                out.append({"addr": str(d.get("Address")), "name": str(d.get("Alias", d.get("Address"))),
                            "connected": bool(d.get("Connected")), "path": str(p)})
        return out

    attempt = {"n": 0}
    def reconnect():
        if st["intr"] or st["busy"] or not srv_ctrl:
            return True
        try:
            hosts = paired_hosts()
        except Exception:
            return True
        if not hosts:
            return True
        host = hosts[0]
        st["busy"] = True
        def work():
            c = i = None
            try:
                c = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
                c.settimeout(8); c.connect((host["addr"], PSM_CTRL))
                i = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
                i.settimeout(8); i.connect((host["addr"], PSM_INTR))
                c.settimeout(None); i.settimeout(None)
                def done():
                    if st["intr"]:          # the box connected to us meanwhile
                        c.close(); i.close()
                    else:
                        adopt("ctrl", c, host["addr"]); adopt("intr", i, host["addr"])
                    st["busy"] = False; attempt["n"] = 0
                    return False
                GLib.idle_add(done)
            except OSError as e:
                for s_ in (c, i):
                    if s_:
                        try: s_.close()
                        except OSError: pass
                attempt["n"] += 1
                if attempt["n"] in (1, 5) or attempt["n"] % 30 == 0:
                    log.info("box %s not answering yet (%s)", host["name"], e.strerror or e)
                GLib.idle_add(lambda: (st.__setitem__("busy", False), False)[1])
        threading.Thread(target=work, daemon=True).start()
        return True
    GLib.timeout_add_seconds(5, reconnect)
    GLib.timeout_add_seconds(1, lambda: (reconnect(), False)[1])

    # ---- key queue (GLib thread)
    queue, busy = deque(), [False]
    def pump():
        if busy[0] or not queue:
            return False
        busy[0] = True
        item = queue.popleft()
        p = plan(item["key"], item.get("mode", "press"))
        def finish(res, gap=12):
            item["res"] = res; item["ev"].set()
            GLib.timeout_add(gap, lambda: (busy.__setitem__(0, False), pump(), False)[2])
        if not p:
            finish({"ok": False, "err": f"{item['key']} isn't available over Bluetooth"}, 0); return False
        if not st["intr"]:
            finish({"ok": False, "err": "The TV box isn't connected to the Pi's Bluetooth"}, 0); return False
        name, down, up, hold = p
        t0 = time.monotonic()
        try:
            st["intr"].send(frame(name, down))
        except OSError as e:
            close_all(f"send failed: {e}")
            finish({"ok": False, "err": "Lost the Bluetooth link — reconnecting"}, 0); return False
        def release():
            try:
                if st["intr"]: st["intr"].send(frame(name, up))
            except OSError:
                close_all("send failed")
            finish({"ok": True, "ms": int((time.monotonic() - t0) * 1000)})
            return False
        GLib.timeout_add(int(hold * 1000), release)
        return False

    def forget():
        close_all("forgetting")
        for h in paired_hosts():
            try: dbus.Interface(adapter, ADP_I).RemoveDevice(h["path"])
            except Exception as e: log.warning("remove: %s", e)
        return False

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            for raw in self.rfile:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                cmd = msg.get("cmd")
                if cmd == "key":
                    item = {"key": str(msg.get("key", "")), "mode": msg.get("mode", "press"), "ev": threading.Event()}
                    GLib.idle_add(lambda it=item: (queue.append(it), pump(), False)[2])
                    res = item["res"] if item["ev"].wait(5) else {"ok": False, "err": "timeout"}
                elif cmd == "status":
                    try:
                        hosts = paired_hosts()
                    except Exception:
                        hosts = []
                    res = {"name": NAME, "mode": "classic", "subscribed": bool(st["intr"]),
                           "advertising": st["advertising"], "registered": not st["error"], "error": st["error"],
                           "connected": [h["name"] for h in hosts if h["connected"]],
                           "paired": [h["name"] for h in hosts]}
                elif cmd == "forget":
                    GLib.idle_add(forget); res = {"ok": True}
                else:
                    res = {"ok": False, "err": "unknown command"}
                self.wfile.write((json.dumps(res) + "\n").encode()); self.wfile.flush()

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    srv = Server(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("TV Cast Bluetooth remote (classic keyboard) ready on 127.0.0.1:%d", PORT)
    try:
        loop.run()
    finally:
        close_all("")
        srv.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    (main_le if os.environ.get("TVCAST_BT_MODE", "classic") == "le" else main_classic)()
