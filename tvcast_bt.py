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
def main():
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
    state = {"subscribed": False, "registered": False, "advertising": False, "error": ""}

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
        def __init__(self, idx, uuid):
            self.path, self.uuid, self.chars = f"{ROOT}/service{idx}", uuid, []
            super().__init__(bus, self.path)

        def get_path(self):
            return dbus.ObjectPath(self.path)

        def props(self):
            return {SVC_I: {"UUID": self.uuid, "Primary": True,
                            "Characteristics": dbus.Array([c.get_path() for c in self.chars], signature="o")}}

        @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, iface):
            return self.props()[SVC_I]

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
            if self.notifying:
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
    hid = Service(0, "1812")
    Chrc(hid, "2a4a", ["read"], [0x11, 0x01, 0x00, 0x02])                 # HID Information
    Chrc(hid, "2a4b", ["encrypt-read"], REPORT_MAP)                        # Report Map (asks the box to pair)
    Chrc(hid, "2a4c", ["write-without-response"], [0])                    # HID Control Point
    Chrc(hid, "2a4e", ["read", "write-without-response"], [1])            # Protocol Mode = report
    kb = Chrc(hid, "2a4d", ["encrypt-read", "notify"], bytes(8))           # Input report 1: keyboard
    Desc(kb, "2908", ["read"], [1, 1])
    cc = Chrc(hid, "2a4d", ["encrypt-read", "notify"], bytes(2))           # Input report 2: consumer keys
    Desc(cc, "2908", ["read"], [2, 1])
    dis = Service(1, "180a")
    Chrc(dis, "2a29", ["read"], b"TV Cast")                                # Manufacturer
    Chrc(dis, "2a50", ["read"], [0x02, 0x6B, 0x1D, 0x46, 0x02, 0x00, 0x01])  # PnP ID (USB, Linux Foundation)
    bat = Service(2, "180f")
    Chrc(bat, "2a19", ["read", "notify"], [100])                           # Battery 100%
    app.services = [hid, dis, bat]
    reports = {"kb": kb, "cc": cc}
    advert, agent = Advertisement(), Agent()

    def on_subscription():
        state["subscribed"] = kb.notifying or cc.notifying
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

    def devices():
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

    srv = Server(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("TV Cast Bluetooth remote ready on 127.0.0.1:%d", PORT)
    try:
        loop.run()
    finally:
        srv.shutdown()
    if state["error"]:
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
