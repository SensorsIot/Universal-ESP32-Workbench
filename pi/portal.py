#!/usr/bin/env python3
"""
RFC2217 Portal v4 — Proxy Supervisor with Serial Services

HTTP server that tracks USB serial device hotplug events and manages
plain_rfc2217_server.py lifecycle.  On hotplug add → start proxy; on remove → stop it.
Hardware config loaded from testbench.json (GPIO pins, debug probes).
"""

import http.server
import collections
import json
import os
import base64
import hashlib
import shutil
import signal
import socket
import subprocess
import sys
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import debug_controller
import gpiod
import mqtt_controller
import wifi_controller
try:
    import ble_controller
except ImportError:
    ble_controller = None

PORT = 8080
def _config_path() -> str:
    """Where the hardware config lives, tolerating the pre-rename name.

    The file used to be `workbench.json`, and it is *operator-written* — slot
    labels, GPIO pins, an ESP-Prog declaration. An upgrade that silently
    stopped reading it would not fail loudly: the portal would auto-detect
    slots instead and come up looking healthy with the operator's pin
    assignments gone. Read the old name when the new one is absent, and say
    so, rather than making a rename cost somebody their wiring.
    """
    env = os.environ.get("RFC2217_CONFIG")
    if env:
        return env
    new, old = "/etc/rfc2217/testbench.json", "/etc/rfc2217/workbench.json"
    if not os.path.exists(new) and os.path.exists(old):
        print(f"[config] using {old}; rename it to {new}", flush=True)
        return old
    return new


CONFIG_FILE = _config_path()
PROXY_EXE = "/usr/local/bin/plain_rfc2217_server.py"

# Auto-assignment port ranges
TCP_PORT_BASE = int(os.environ.get("TCP_PORT_BASE", "4001"))
GDB_PORT_BASE = int(os.environ.get("GDB_PORT_BASE", "3333"))
TELNET_PORT_BASE = int(os.environ.get("TELNET_PORT_BASE", "4444"))
_auto_label_counter = 0

# Flap detection — suppress proxy restarts during USB connect/disconnect storms
FLAP_WINDOW_S = 30       # Look at events within this window
FLAP_THRESHOLD = 10       # 10 events in 30s — allows dual-USB devices (2 events per plug)
FLAP_COOLDOWN_S = 10      # After flapping, wait before recovery attempt
FLAP_MAX_RETRIES = 2      # Max no-GPIO recovery attempts before manual intervention

# Native USB (ttyACM) boot delay — let ESP32-C3 boot past download-mode window
# before opening the port (Linux cdc_acm asserts DTR+RTS on open, which triggers
# the USB-Serial/JTAG controller's auto-download if the chip is still in early boot)
NATIVE_USB_BOOT_DELAY_S = 2

# Slot states (per-slot lifecycle, exposed in /api/devices)
STATE_ABSENT     = "absent"
STATE_IDLE       = "idle"
STATE_RESETTING  = "resetting"
STATE_MONITORING = "monitoring"
STATE_FLAPPING      = "flapping"
STATE_RECOVERING    = "recovering"
STATE_DOWNLOAD_MODE = "download_mode"
STATE_DEBUGGING     = "debugging"
STATE_FLASHING      = "flashing"

# Module-level state
slots: dict[str, dict] = {}
seq_counter: int = 0
host_ip: str = "127.0.0.1"  # refreshed periodically; see _refresh_host_ip()
hostname: str = "localhost"

# Activity log — recent operations visible in UI
activity_log: collections.deque = collections.deque(maxlen=200)
_enter_portal_running: bool = False

# Human interaction — test scripts block on POST /api/human-interaction
# until the operator clicks Done/Cancel on the web UI.
_human_event: threading.Event | None = None
_human_confirmed: bool = False
_human_message: str | None = None
_human_lock = threading.Lock()

# Test progress — test scripts push updates via POST /api/test/update,
# UI polls via GET /api/test/progress.
_test_lock = threading.Lock()
_test_session = None  # dict or None; see _handle_test_update for schema

# GPIO control — drive Pi GPIO pins from test scripts (e.g. hold DUT GPIO low)
_gpio_lock = threading.Lock()
_gpio_chip = None       # gpiod.Chip, opened lazily
_gpio_requests = {}     # pin -> gpiod.LineRequest
_gpio_directions = {}   # pin -> "output" | "input"
GPIO_ALLOWED = {16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27}  # BCM GPIOs safe for DUT control
# Reserved: GPIO 2/3 = I2C (Si5351), GPIO 5/6 = GPCLK, GPIO 6/12/13 = PE4302 LE/CLK/DATA

# Unified signal generator (Si5351 / PE4302 with GPCLK fallback)
try:
    from signal_generator import SignalGenerator
    _siggen: "SignalGenerator | None" = SignalGenerator()
    print(f"[siggen] hardware: {_siggen.hardware_status()}, "
          f"backends: {_siggen.available_backends()}", flush=True)
except Exception as _siggen_exc:  # pragma: no cover
    print(f"[siggen] disabled: {_siggen_exc}", flush=True)
    _siggen = None

# RTL-SDR receiver (rtl_433 decode + pulse analyzer) — receive-side of siggen
try:
    from sdr_controller import SdrReceiver
    _sdr: "SdrReceiver | None" = SdrReceiver()
    print(f"[sdr] hardware: {_sdr.hardware_status()}", flush=True)
except Exception as _sdr_exc:  # pragma: no cover
    print(f"[sdr] disabled: {_sdr_exc}", flush=True)
    _sdr = None

# UDP log receiver — ESP32 devices send debug logs over UDP to port 5555
UDP_LOG_PORT = int(os.environ.get("UDP_LOG_PORT", "5555"))
UDP_LOG_MAX_LINES = 2000
_udp_log: collections.deque = collections.deque(maxlen=UDP_LOG_MAX_LINES)
_udp_thread: threading.Thread | None = None
_udp_shutdown = threading.Event()

# UDP discovery beacon — responds to DISCOVER probes so containers can find us
BEACON_PORT = int(os.environ.get("BEACON_PORT", "5888"))
_beacon_thread: threading.Thread | None = None
_beacon_shutdown = threading.Event()

# OTA firmware repository — serve .bin files for ESP32 OTA updates
FIRMWARE_DIR = os.environ.get("FIRMWARE_DIR", "/var/lib/rfc2217/firmware")
ESPOTA_PATH = os.environ.get("ESPOTA_PATH", "/usr/local/bin/espota.py")

# Serial buffer size — how many lines each slot's ring buffer keeps
SERIAL_BUF_MAXLEN = 1000



def _gpio_set(pin, value):
    """Set a GPIO pin: value=0 (low), 1 (high), or "z" (input with pull-up)."""
    global _gpio_chip
    with _gpio_lock:
        if _gpio_chip is None:
            _gpio_chip = gpiod.Chip("/dev/gpiochip0")

        if value == "z":
            # Switch to input with pull-up (not floating)
            if pin in _gpio_requests:
                _gpio_requests[pin].release()
                del _gpio_requests[pin]
            _gpio_requests[pin] = _gpio_chip.request_lines(
                consumer="serial-portal",
                config={pin: gpiod.LineSettings(
                    direction=gpiod.line.Direction.INPUT,
                    bias=gpiod.line.Bias.PULL_UP,
                )},
            )
            _gpio_directions[pin] = "input"
            return

        gval = gpiod.line.Value.ACTIVE if value else gpiod.line.Value.INACTIVE

        # Request as output if not already, or reconfigure if switching from input
        if pin not in _gpio_requests or _gpio_directions.get(pin) == "input":
            if pin in _gpio_requests:
                _gpio_requests[pin].release()
                del _gpio_requests[pin]
            _gpio_requests[pin] = _gpio_chip.request_lines(
                consumer="serial-portal",
                config={pin: gpiod.LineSettings(
                    direction=gpiod.line.Direction.OUTPUT,
                    output_value=gval,
                )},
            )
        else:
            _gpio_requests[pin].set_value(pin, gval)
        _gpio_directions[pin] = "output"


# ---------------------------------------------------------------------------
# USB Unbind / Rebind — stop kernel-level USB event storms
# ---------------------------------------------------------------------------

def _slot_key_to_usb_device(slot_key: str) -> str | None:
    """Parse a slot_key like 'platform-3f980000.usb-usb-0:1.1.2:1.0' → '1-1.1.2'.

    The USB device address is the bus-port portion before the interface suffix.
    The slot_key format is: platform-<controller>-usb-<bus>:<port_path>:<interface>
    We need the last 'usb-' to skip the controller name which also contains 'usb'.
    """
    # Find the last 'usb-' which precedes '<bus>:<port>:<iface>'
    idx = slot_key.rfind("usb-")
    if idx < 0:
        return None
    tail = slot_key[idx + 4:]  # '0:1.1.2:1.0'
    parts = tail.split(":")
    if len(parts) < 2:
        return None
    bus = parts[0]       # '0'
    port_path = parts[1] # '1.1.2'
    # Linux sysfs USB device name: <roothub>-<port_path>
    # Pi: bus 0 → roothub '1'
    try:
        bus_num = int(bus) + 1
    except ValueError:
        return None
    return f"{bus_num}-{port_path}"


def _usb_device_from_devnode(devnode: str) -> str | None:
    """'/dev/ttyACM2' → '1-1.3', by asking the kernel where the node lives.

    DEVPATH ends '.../usb1/1-1/1-1.3/1-1.3:1.0/tty/ttyACM2'; the USB *device*
    is the last path element without an interface suffix.
    """
    if not devnode:
        return None
    try:
        out = subprocess.check_output(
            ["udevadm", "info", "-q", "path", "-n", devnode],
            text=True, timeout=5).strip()
    except Exception:
        return None
    for part in reversed(out.split("/")):
        # '1-1.3' yes; '1-1.3:1.0' is an interface; 'usb1', 'tty' neither
        if re.fullmatch(r"\d+-[\d.]+", part):
            return part
    return None


def _devnode_from_usb_device(usb_device: str, timeout: float = 15.0) -> str | None:
    """'1-1.3' → '/dev/ttyACM0', waiting for the kernel to recreate the node.

    A rebind is a re-enumeration, and the kernel hands out the next free
    ttyACM number rather than the one the device had before: SLOT2 left as
    ttyACM2 and came back as ttyACM0. Waiting on the recorded path therefore
    reported "no devnode after rebind" for a device that was present and
    healthy under a different name — the recovery check inventing its own
    failure. Ask sysfs which tty belongs to this USB device instead.
    """
    import glob
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for path in glob.glob(f"/sys/bus/usb/devices/{usb_device}:*/tty/*"):
            name = os.path.basename(path)
            node = f"/dev/{name}"
            if os.path.exists(node):
                return node
        time.sleep(0.5)
    return None


def _usb_device_for_slot(slot: dict) -> str | None:
    """The USB device to unbind/rebind for *slot*, however the slot was made.

    `slot_key` is a udev ID_PATH only for slots pinned in the config file. On
    an auto-detected bench — which the manual calls the normal case, "slots
    are auto-detected, no config file needed" — the key is a synthetic
    `_fixed_SLOT2` with no USB path in it at all, so parsing it returned None
    and every recovery aborted at its first guard with "cannot determine USB
    device from slot_key". The whole flap-recovery feature was therefore
    inoperative on the default configuration, while the endpoint went on
    answering `ok: true`.

    The devnode knows the answer, so ask it when the key cannot say.
    """
    usb_device = _slot_key_to_usb_device(slot.get("slot_key") or "")
    if usb_device:
        return usb_device
    return _usb_device_from_devnode(slot.get("devnode") or "")


def _usb_unbind(usb_device: str) -> bool:
    """Unbind a USB device from its driver to stop enumeration storms."""
    path = "/sys/bus/usb/drivers/usb/unbind"
    try:
        with open(path, "w") as f:
            f.write(usb_device)
        print(f"[portal] USB unbind: {usb_device}", flush=True)
        return True
    except OSError as e:
        print(f"[portal] USB unbind failed for {usb_device}: {e}", flush=True)
        return False


def _usb_rebind(usb_device: str) -> bool:
    """Rebind a USB device so the kernel re-enumerates it."""
    path = "/sys/bus/usb/drivers/usb/bind"
    try:
        with open(path, "w") as f:
            f.write(usb_device)
        print(f"[portal] USB rebind: {usb_device}", flush=True)
        return True
    except OSError as e:
        print(f"[portal] USB rebind failed for {usb_device}: {e}", flush=True)
        return False


def log_activity(msg: str, cat: str = "info"):
    """Append a timestamped entry to the activity log."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "msg": msg,
        "cat": cat,  # info, ok, error, step
    }
    activity_log.append(entry)
    print(f"[activity] [{cat}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# UDP Log Receiver
# ---------------------------------------------------------------------------

def _udp_log_thread():
    """Background thread: listen for UDP log packets on port 5555."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", UDP_LOG_PORT))
    sock.settimeout(1.0)
    print(f"[udplog] listening on UDP :{UDP_LOG_PORT}", flush=True)
    while not _udp_shutdown.is_set():
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        source_ip = addr[0]
        try:
            text = data.decode("utf-8", errors="replace").rstrip("\r\n")
        except Exception:
            continue
        ts = time.time()
        for line in text.split("\n"):
            line = line.rstrip("\r")
            if line:
                _udp_log.append({"ts": ts, "source": source_ip, "line": line})
                log_activity(f"[{source_ip}] {line}", "info")
    sock.close()
    print("[udplog] stopped", flush=True)


def start_udp_log():
    """Start the UDP log receiver thread."""
    global _udp_thread
    _udp_shutdown.clear()
    _udp_thread = threading.Thread(target=_udp_log_thread, daemon=True, name="udp-log")
    _udp_thread.start()


# ---------------------------------------------------------------------------
# Discovery beacon — respond to UDP DISCOVER probes
# ---------------------------------------------------------------------------

def _beacon_responder_thread():
    """Background thread: listen for DISCOVER probes and respond with portal info."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", BEACON_PORT))
    sock.settimeout(1.0)
    print(f"[beacon] listening for DISCOVER probes on UDP :{BEACON_PORT}", flush=True)
    while not _beacon_shutdown.is_set():
        try:
            data, addr = sock.recvfrom(1024)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            text = data.decode("utf-8", errors="replace").strip()
        except Exception:
            continue
        if text == "DISCOVER":
            response = json.dumps({
                "service": "testbench",
                "hostname": hostname,
                "ip": host_ip,
                "port": PORT,
            })
            sock.sendto(response.encode(), addr)
    sock.close()
    print("[beacon] stopped", flush=True)


def start_beacon():
    """Start the discovery beacon responder thread."""
    global _beacon_thread
    _beacon_shutdown.clear()
    _beacon_thread = threading.Thread(
        target=_beacon_responder_thread, daemon=True, name="beacon"
    )
    _beacon_thread.start()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Global config (loaded from testbench.json, optional)
_global_config: dict = {}


def _port_is_serial_usable(hub_dev_path: str, port_num: int) -> bool:
    """Return True if the given hub port is empty OR has a serial device.

    Non-serial devices (ethernet adapters, storage, input) mean the port
    is used for something else and should NOT be exposed as a slot.
    Empty ports are kept — user may plug an ESP32 in later.
    """
    import glob as _glob
    # A device on this port would be at e.g. /sys/bus/usb/devices/1-1.2
    hub_name = os.path.basename(hub_dev_path)
    child_path = os.path.join("/sys/bus/usb/devices", f"{hub_name}.{port_num}")
    if not os.path.exists(child_path):
        return True  # empty port — keep as potential slot
    # Check the device's interface classes. Serial/CDC = 0x02 or 0x0a,
    # FTDI/vendor = 0xff. Ethernet = 0x02 with subclass 0x06 (but class 0x02
    # also covers CDC ACM, so discriminate by kernel driver).
    drivers = set()
    for iface in _glob.glob(os.path.join(child_path, f"{hub_name}.{port_num}:*")):
        driver_link = os.path.join(iface, "driver")
        if os.path.islink(driver_link):
            drivers.add(os.path.basename(os.readlink(driver_link)))
    # Keep ports where a serial-capable driver is bound
    serial_drivers = {"cdc_acm", "ftdi_sio", "cp210x", "ch341", "usbserial",
                      "pl2303", "ch343"}
    return bool(drivers & serial_drivers) or not drivers


# Per-model USB ports that hubs advertise but the board never wires to a
# connector. An unwired port is indistinguishable from an empty wired jack
# via sysfs alone (both have no child device), so the only reliable way to
# exclude them is a lookup keyed on /proc/device-tree/model.
_PHANTOM_PORTS_BY_MODEL: dict[str, set[str]] = {
    "Raspberry Pi 3 Model B Plus": {"0:1.4"},
}


def _phantom_ports_for_pi() -> set[str]:
    try:
        with open("/proc/device-tree/model") as f:
            model = f.read().rstrip("\x00").strip()
    except OSError:
        return set()
    for prefix, phantoms in _PHANTOM_PORTS_BY_MODEL.items():
        if model.startswith(prefix):
            return phantoms
    return set()


def _detect_usb_hub_ports() -> list[str]:
    """Enumerate external-facing USB hub ports on the Pi.

    Works on any Raspberry Pi (Zero 2 W, 3, 4, 5, CM4, etc.) by walking
    /sys/bus/usb and listing every port on every non-root hub.  Ports
    occupied by non-serial devices (USB Ethernet, storage, etc.) are
    skipped so only ESP32-usable ports become slots.

    Returns a sorted list of usb_prefix strings (e.g. "0:1.1", "0:2.1").
    """
    import glob as _glob
    phantoms = _phantom_ports_for_pi()
    ports: set[str] = set()
    for dev_path in _glob.glob("/sys/bus/usb/devices/*"):
        # Skip root hubs (usb1, usb2) — we want downstream hubs only
        name = os.path.basename(dev_path)
        if name.startswith("usb"):
            continue
        try:
            with open(os.path.join(dev_path, "bDeviceClass")) as f:
                if f.read().strip() != "09":  # 0x09 = Hub class
                    continue
            with open(os.path.join(dev_path, "maxchild")) as f:
                nports = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if nports <= 0:
            continue
        # Derive the bus:port prefix from the hub's own devpath, e.g. "1-1" -> "0:1"
        bus_dash_port = name
        try:
            bus, port = bus_dash_port.split("-", 1)
        except ValueError:
            continue
        bus_prefix = f"{int(bus) - 1}:{port}"  # kernel busnum is 1-based; udev uses 0-based
        for p in range(1, nports + 1):
            prefix = f"{bus_prefix}.{p}"
            if prefix in phantoms:
                continue
            if _port_is_serial_usable(dev_path, p):
                ports.add(prefix)
    return sorted(ports)


def _autogenerate_config() -> dict:
    """Create a default multi-slot config matching the Pi's USB topology."""
    ports = _detect_usb_hub_ports()
    try:
        with open("/proc/device-tree/model") as f:
            model = f.read().rstrip("\x00").strip()
    except OSError:
        model = "Unknown"

    slots = []
    for i, prefix in enumerate(ports, start=1):
        slots.append({
            "label": f"SLOT{i}",
            "usb_prefix": prefix,
            "tcp_port": 4000 + i,
            "gdb_port": 3332 + i,
            "openocd_telnet_port": 4443 + i,
        })
    print(f"[portal] auto-detected Pi model: {model}", flush=True)
    print(f"[portal] auto-detected {len(slots)} USB hub port(s): {ports}",
          flush=True)
    return {
        "gpio_boot": 18,
        "gpio_en": 17,
        "slots": slots,
        "debug_probes": [],
    }


def load_config(path: str) -> dict[str, dict]:
    """Load testbench.json hardware config. All fields default to None/empty.

    If the config file is missing, auto-detect the Pi's USB hub topology
    and generate a default config on the fly (no file written).  Users
    who want custom labels/ports/GPIO pins can write testbench.json.

    testbench.json provides:
      - gpio_boot: Pi BCM GPIO pin wired to DUT BOOT (None if not wired)
      - gpio_en: Pi BCM GPIO pin wired to DUT EN/RST (None if not wired)
      - debug_probes: ESP-Prog probe definitions (empty if none)
      - slots: Fixed slot definitions with usb_prefix for physical port mapping
    """
    global _global_config
    _global_config = {"gpio_boot": None, "gpio_en": None, "debug_probes": [],
                      "slot_prefixes": []}
    result: dict[str, dict] = {}
    cfg = None
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        print(f"[portal] no {path} — auto-detecting USB topology", flush=True)
        cfg = _autogenerate_config()
    except Exception as exc:
        print(f"[portal] config error: {exc} — auto-detecting", flush=True)
        cfg = _autogenerate_config()

    try:
        if cfg.get("gpio_boot"):
            _global_config["gpio_boot"] = cfg["gpio_boot"]
        if cfg.get("gpio_en"):
            _global_config["gpio_en"] = cfg["gpio_en"]
        if cfg.get("debug_probes"):
            _global_config["debug_probes"] = cfg["debug_probes"]
        # Fixed slot definitions — each has label, optional usb_prefix
        for sdef in cfg.get("slots", []):
            label = sdef["label"]
            prefix = sdef.get("usb_prefix")  # None = catch-all
            placeholder_key = f"_fixed_{label}"
            slot = _make_slot(
                slot_key=placeholder_key,
                label=label,
                tcp_port=sdef.get("tcp_port"),
                gdb_port=sdef.get("gdb_port"),
                openocd_telnet_port=sdef.get("openocd_telnet_port"),
            )
            slot["_usb_prefix"] = prefix
            result[placeholder_key] = slot
            _global_config["slot_prefixes"].append(
                {"label": label, "prefix": prefix,
                 "placeholder_key": placeholder_key})
        gb = _global_config["gpio_boot"]
        ge = _global_config["gpio_en"]
        probes = len(_global_config["debug_probes"])
        n_slots = len(cfg.get("slots", []))
        print(f"[portal] config: gpio_boot={gb}, gpio_en={ge}, "
              f"probes={probes}, fixed_slots={n_slots}", flush=True)
    except Exception as exc:
        print(f"[portal] config parse error: {exc}", flush=True)
    return result


def _find_fixed_slot_for_key(slot_key: str) -> dict | None:
    """Match a USB slot_key against configured prefix patterns.

    Returns the fixed slot dict whose prefix matches, or None.
    Longer prefixes checked first. The fixed slot always stays in the
    dict under its placeholder key — multiple slot_keys can map to it.
    """
    candidates = sorted(_global_config.get("slot_prefixes", []),
                        key=lambda sp: len(sp.get("prefix", "")),
                        reverse=True)
    for sp in candidates:
        prefix = sp.get("prefix")
        if prefix and prefix in slot_key:
            pk = sp["placeholder_key"]
            if pk in slots:
                return slots[pk]
    return None


def _next_available_port(base: int, used_attr: str) -> int:
    """Find the next available port starting from base."""
    used = {s.get(used_attr) for s in slots.values() if s.get(used_attr)}
    port = base
    while port in used:
        port += 1
    return port


def _next_label() -> str:
    """Generate the next auto-label."""
    global _auto_label_counter
    _auto_label_counter += 1
    return f"AUTO-{_auto_label_counter}"


def _make_slot(slot_key: str, label: str = None, tcp_port: int = None,
               gdb_port: int = None, openocd_telnet_port: int = None,
               group: str = None, role: str = None) -> dict:
    """Create a fully populated slot dict with auto-assigned ports if needed."""
    if not tcp_port:
        tcp_port = _next_available_port(TCP_PORT_BASE, "tcp_port")
    if not gdb_port:
        gdb_port = _next_available_port(GDB_PORT_BASE, "gdb_port")
    if not openocd_telnet_port:
        openocd_telnet_port = _next_available_port(TELNET_PORT_BASE,
                                                    "openocd_telnet_port")
    if not label:
        label = _next_label()

    return {
        "label": label,
        "slot_key": slot_key,
        "tcp_port": tcp_port,
        "gdb_port": gdb_port,
        "openocd_telnet_port": openocd_telnet_port,
        "group": group,
        "role": role,
        "gpio_boot": _global_config.get("gpio_boot"),
        "gpio_en": _global_config.get("gpio_en"),
        "present": False,
        "running": False,
        "pid": None,
        "devnode": None,
        "_devnodes": {},  # slot_key → devnode for all active devices on this slot
        "seq": 0,
        "last_action": None,
        "last_event_ts": None,
        "url": None,
        "last_error": None,
        "flapping": False,
        "state": STATE_ABSENT,
        "_event_times": [],
        "_recovering": False,
        "_recover_retries": 0,
        "_auto_debug_chip": None,
        "_jtag_slot": None,  # slot label providing JTAG (own or probe)
        "_serial_buf": collections.deque(maxlen=SERIAL_BUF_MAXLEN),
        "_lock": threading.Lock(),
    }


def get_host_ip() -> str:
    """Detect host IP, preferring eth0 (wired management interface)."""
    # Prefer eth0 — the wired management interface
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "eth0"],
            timeout=2, stderr=subprocess.DEVNULL,
        ).decode()
        for part in out.split():
            if "/" in part:
                ip = part.split("/")[0]
                if ip and not ip.startswith("127."):
                    return ip
    except Exception:
        pass
    # Fallback: UDP socket trick (picks default-route interface)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _refresh_host_ip():
    """Re-resolve host IP and hostname; update slot URLs if the IP changed."""
    global host_ip, hostname
    # The hostname is read once at startup and the UI heading is drawn from
    # it, so a rename left every open portal page naming the machine by what
    # it used to be called until somebody restarted the service. The IP was
    # already refreshed here for the same reason.
    new_name = get_hostname()
    if new_name != hostname:
        print(f"[portal] hostname changed: {hostname} -> {new_name}", flush=True)
        hostname = new_name
    new_ip = get_host_ip()
    if new_ip != host_ip:
        old = host_ip
        host_ip = new_ip
        for slot in slots.values():
            if slot["running"] and slot["tcp_port"]:
                slot["url"] = f"rfc2217://{host_ip}:{slot['tcp_port']}"
        print(f"[portal] host_ip changed: {old} -> {host_ip}", flush=True)


def get_hostname() -> str:
    """Get the system hostname (used for mDNS / display)."""
    return socket.gethostname()


def wait_for_device(devnode: str, timeout: float = 5.0) -> bool:
    """Wait until the device node exists and is accessible.

    For ttyACM (native USB CDC) devices, only check file existence —
    os.open() asserts DTR+RTS via the cdc_acm driver, which resets
    ESP32-C3 into download mode during the boot window.
    """
    is_native_usb = devnode and "ttyACM" in devnode
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(devnode):
            if is_native_usb:
                return True  # Don't open — avoids DTR reset
            try:
                fd = os.open(devnode, os.O_RDWR | os.O_NONBLOCK)
                os.close(fd)
                return True
            except OSError:
                pass
        time.sleep(0.1)
    return False


def is_port_listening(port: int) -> bool:
    """Quick TCP connect check on localhost."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        result = s.connect_ex(("127.0.0.1", port))
        s.close()
        return result == 0
    except Exception:
        return False


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pick_best_devnode(slot: dict) -> str:
    """For dual-USB boards, prefer the non-JTAG devnode for the serial proxy.

    When a slot has both a JTAG/serial combo device (Espressif 303a:1001)
    and a dedicated serial chip (CH343, CP2102, etc.), the proxy should
    use the dedicated serial chip so it doesn't conflict with OpenOCD.
    """
    devnodes = list(slot.get("_devnodes", {}).values())
    if len(devnodes) <= 1:
        return slot["devnode"]

    # Check each devnode via udevadm — skip JTAG interfaces
    non_jtag = []
    for dn in devnodes:
        try:
            out = subprocess.check_output(
                ["udevadm", "info", "-q", "property", "-n", dn],
                text=True, timeout=3)
            if "JTAG" in out:
                continue
        except Exception:
            pass
        non_jtag.append(dn)

    if non_jtag and non_jtag[0] != slot["devnode"]:
        label = slot.get("label", "?")
        print(f"[portal] {label}: proxy on {non_jtag[0]} "
              f"(non-JTAG, skip {slot['devnode']})", flush=True)
        slot["devnode"] = non_jtag[0]
        return non_jtag[0]
    return slot["devnode"]


def start_proxy(slot: dict) -> bool:
    """Start plain_rfc2217_server for *slot*.  Returns True on success."""
    devnode = slot["devnode"]
    tcp_port = slot["tcp_port"]
    label = slot["label"]

    if not os.path.exists(PROXY_EXE):
        slot["last_error"] = f"Proxy executable not found: {PROXY_EXE}"
        print(f"[portal] {label}: {slot['last_error']}", flush=True)
        return False

    # Settle — done *before* acquiring lock (caller holds lock already)
    if not wait_for_device(devnode):
        slot["last_error"] = f"Device {devnode} not ready after settle timeout"
        print(f"[portal] {label}: {slot['last_error']}", flush=True)
        return False

    # The monitor port is the RFC2217 port + 1000: a plain read-only fan-out
    # that any number of observers may attach to without negotiating, and so
    # without any way to assert DTR/RTS on the device they are watching.
    monitor_port = tcp_port + 1000
    slot["monitor_port"] = monitor_port
    cmd = ["python3", PROXY_EXE, "-p", str(tcp_port),
           "-m", str(monitor_port), devnode]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        slot["last_error"] = str(exc)
        print(f"[portal] {label}: popen failed: {exc}", flush=True)
        return False

    # Brief pause then check it didn't die immediately
    time.sleep(0.5)
    if proc.poll() is not None:
        slot["last_error"] = f"Proxy exited immediately (code {proc.returncode})"
        print(f"[portal] {label}: {slot['last_error']}", flush=True)
        return False

    # Wait for BOTH ports. Marking the slot running while only the RFC2217
    # port listens leaves a window where a monitor connects to a fan-out that
    # is not up yet: it gets a live socket and no data, which is
    # indistinguishable from a silent device.
    for _ in range(20):
        if is_port_listening(tcp_port) and is_port_listening(monitor_port):
            slot["running"] = True
            slot["pid"] = proc.pid
            slot["last_error"] = None
            slot["url"] = f"rfc2217://{host_ip}:{tcp_port}"
            slot["state"] = STATE_IDLE
            _start_recorder(slot)
            print(
                f"[portal] {label}: proxy started (pid {proc.pid}, port {tcp_port})",
                flush=True,
            )
            return True
        time.sleep(0.1)

    # Port never came up — kill the process
    _stop_pid(proc.pid)
    slot["last_error"] = "Proxy started but port not listening"
    print(f"[portal] {label}: {slot['last_error']}", flush=True)
    return False


def _stop_pid(pid: int, timeout: float = 5.0):
    """SIGTERM, wait, SIGKILL fallback."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_process_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def stop_proxy(slot: dict) -> bool:
    """Stop proxy for *slot*.  Returns True if stopped (or already stopped)."""
    label = slot["label"]
    pid = slot["pid"]
    if pid and _is_process_alive(pid):
        print(f"[portal] {label}: stopping proxy (pid {pid})", flush=True)
        _stop_pid(pid)
    slot["running"] = False
    slot["pid"] = None
    slot["url"] = None
    slot["last_error"] = None
    return True


def flash_slot(slot: dict, images: list, chip: str = "auto",
               baud: int = 460800, erase: bool = False) -> dict:
    """Flash *slot*'s device locally with esptool, then restart the proxy.

    A separate flash method for classic ESP32 boards behind a USB-serial bridge
    (CH340/CP2102/CH9102), whose DTR/RTS auto-reset cannot be driven reliably
    over the RFC2217 proxy.  esptool runs on the Pi against the local devnode,
    where the reset works natively.  The native-USB RFC2217 path is unchanged.

    images: [{"offset": "0x1000", "data": b"<raw bytes>"}, ...]
    """
    label = slot["label"]
    if not images:
        return {"ok": False, "error": "no images provided"}

    with slot["_lock"]:
        if not slot.get("present"):
            return {"ok": False, "error": f"{label}: no device present"}
        if slot["state"] in (STATE_DEBUGGING, STATE_FLASHING):
            return {"ok": False, "error": f"{label}: busy (state={slot['state']})"}
        devnode = slot.get("devnode")
        if not devnode:
            return {"ok": False, "error": f"{label}: no devnode"}

        slot["state"] = STATE_FLASHING
        stop_proxy(slot)

        tmpdir = tempfile.mkdtemp(prefix="wbflash_")
        try:
            flash_args: list = []
            for i, img in enumerate(images):
                off = img.get("offset")
                data = img.get("data")
                if off is None or data is None:
                    raise ValueError(f"image {i}: missing 'offset' or 'data'")
                fpath = os.path.join(tmpdir, f"img{i}.bin")
                with open(fpath, "wb") as fh:
                    fh.write(data)
                flash_args += [str(off), fpath]

            cmd = ["python3", "-m", "esptool",
                   "--chip", chip or "auto", "--port", devnode,
                   "--baud", str(baud),
                   "--before", "default-reset", "--after", "hard-reset",
                   "--connect-attempts", "5", "write-flash"]
            if erase:
                cmd.append("--erase-all")
            cmd += flash_args

            log_activity(f"flash({label}): esptool write-flash {len(images)} image(s)", "step")
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            out = (proc.stdout or "") + (proc.stderr or "")
            ok = proc.returncode == 0
            result = {"ok": ok, "returncode": proc.returncode, "log": out[-4000:]}
            if not ok:
                result["error"] = f"esptool exited {proc.returncode}"
        except subprocess.TimeoutExpired:
            result = {"ok": False, "error": "esptool timed out"}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            slot["state"] = STATE_IDLE if slot.get("present") else STATE_ABSENT
            start_proxy(slot)

    if result.get("ok"):
        log_activity(f"flash({label}) — done", "ok")
    else:
        log_activity(f"flash({label}) — {result.get('error', 'failed')}", "error")
    return result


def _make_dynamic_slot(slot_key: str) -> dict:
    """Create a fully-configured slot for a newly discovered device."""
    return _make_slot(slot_key=slot_key)


def scan_existing_devices():
    """Scan for already-plugged-in USB serial devices and start proxies.

    Called once at startup so devices present at boot are recognized
    without requiring a hotplug event.
    """
    import glob as _glob
    import subprocess as _sp

    print(f"[portal] boot scan: {len(slots)} fixed slot(s) from config",
          flush=True)

    devnodes = sorted(_glob.glob("/dev/ttyACM*") + _glob.glob("/dev/ttyUSB*"))
    if not devnodes:
        print("[portal] boot scan: no USB serial devices found", flush=True)
        return

    print(f"[portal] boot scan: found {len(devnodes)} device(s)", flush=True)
    for devnode in devnodes:
        # Get ID_PATH from udevadm
        try:
            out = _sp.check_output(
                ["udevadm", "info", "-q", "property", "-n", devnode],
                text=True, timeout=5,
            )
        except Exception as exc:
            print(f"[portal] boot scan: udevadm failed for {devnode}: {exc}", flush=True)
            continue

        props = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v

        id_path = props.get("ID_PATH", "")
        devpath = props.get("DEVPATH", "")
        slot_key = id_path if id_path else devpath
        if not slot_key:
            print(f"[portal] boot scan: no slot_key for {devnode}, skipping", flush=True)
            continue

        fixed = _find_fixed_slot_for_key(slot_key)
        if fixed:
            slot = fixed
        elif slot_key not in slots:
            slots[slot_key] = _make_dynamic_slot(slot_key)
            slot = slots[slot_key]
        else:
            slot = slots[slot_key]

        slot["_devnodes"][slot_key] = devnode
        slot["present"] = True
        if not slot["devnode"]:
            slot["devnode"] = devnode  # first devnode becomes primary
        slot["state"] = STATE_IDLE
        if slot["tcp_port"]:
            slot["url"] = f"rfc2217://{host_ip}:{slot['tcp_port']}"

        if not slot["running"]:
            print(f"[portal] boot scan: starting {slot['label']} ({devnode}) "
                  f"on port {slot['tcp_port']}", flush=True)
            with slot["_lock"]:
                start_proxy(slot)
            # Auto-start debug in background (detection is slow)
            if (slot["running"] and slot.get("gdb_port")
                    and slot.get("label")):
                def _bg_boot_debug(s=slot):
                    time.sleep(3)  # let proxy stabilize
                    # Check after sleep — _usb_devices is populated by now
                    if _is_probe_slot(s):
                        return  # probe-only slot, not a DUT
                    try:
                        # Detection runs outside lock — no deadlock possible
                        usb_devs = list(s.get("_usb_devices", []))
                        psm = _build_probe_slot_map()
                        info = debug_controller.detect_slot_jtag(
                            s["label"], usb_devs, psm,
                            usb_prefix=s.get("_usb_prefix"))
                        chip = info["chip"]
                        jtag_slot = info["jtag_slot"]
                        if chip and jtag_slot:
                            # Ensure proxy is running (brief lock)
                            with s["_lock"]:
                                if s["present"]:
                                    if not s["running"] or not _is_process_alive(s.get("pid")):
                                        start_proxy(s)
                                        time.sleep(1)
                            # Detection is the whole job. Identity is what a
                            # caller wants from a freshly plugged board, and
                            # detect_slot_jtag has already established it with
                            # transient OpenOCD runs that release the chip.
                            #
                            # This used to leave a session *running* on every
                            # JTAG-capable slot. OpenOCD halts the target when
                            # it attaches and holds the JTAG interface, so a
                            # board nobody was debugging sat there halted: a
                            # flash met a busy chip, chip/info exited 1, and a
                            # device that had just been programmed correctly
                            # printed nothing. Debugging is a mode a caller
                            # asks for with /api/debug/start — never the
                            # resting state of a slot.
                            s["_auto_debug_chip"] = chip
                            s["_jtag_slot"] = jtag_slot
                            log_activity(
                                f"Detected {s['label']}: {chip} "
                                f"(JTAG:{jtag_slot}) — chip released, no "
                                f"debug session held", "ok")
                        else:
                            with s["_lock"]:
                                if (s["present"]
                                        and (not s["running"]
                                             or not _is_process_alive(
                                                 s.get("pid")))):
                                    start_proxy(s)
                    except Exception as e:
                        print(f"[portal] boot auto-debug failed: {e}",
                              flush=True)
                        with s["_lock"]:
                            if (s["present"]
                                    and (not s["running"]
                                         or not _is_process_alive(
                                             s.get("pid")))):
                                start_proxy(s)
                threading.Thread(target=_bg_boot_debug, daemon=True).start()


def _refresh_slot_health(slot: dict):
    """Check that a slot's proxy is still alive; mark dead if not."""
    if slot["running"] and slot["pid"]:
        if not _is_process_alive(slot["pid"]):
            slot["running"] = False
            slot["pid"] = None
            slot["url"] = None
            slot["last_error"] = "Process died"
            slot["state"] = STATE_IDLE if slot["present"] else STATE_ABSENT


_PROBE_VIDS = {"0403"}  # FTDI (ESP-Prog, etc.)
# JTAG-capable FTDI chips used by ESP-Prog and clones.
# FT232R (6001) is a single-channel USB-UART and is NOT a JTAG probe.
_PROBE_PIDS = {"6010", "6011", "6014", "6015"}  # FT2232H, FT4232H, FT232H, FT230X(H)


def _build_probe_slot_map() -> dict[str, str]:
    """Map probe labels to the slot labels where the probe hardware lives.

    Finds which present slot is a probe-only slot (FTDI device, no DUT)
    and maps each configured probe to it.
    """
    probes = debug_controller.get_probes()
    if not probes:
        return {}
    # Find all probe-only slots
    probe_slots = [
        s["label"] for s in slots.values()
        if s.get("present") and _is_probe_slot(s)
    ]
    if not probe_slots:
        return {}
    # Map each probe to the first available probe slot
    # (with one probe and one FTDI slot this is unambiguous)
    result: dict[str, str] = {}
    for i, p in enumerate(probes):
        if i < len(probe_slots):
            result[p["label"]] = probe_slots[i]
    return result


def _is_probe_slot(slot: dict) -> bool:
    """True if slot contains only a JTAG-capable debug probe (no DUT).

    Must be an FTDI multi-interface chip (FT2232H, FT4232H, FT232H).
    Single-channel USB-UART chips like FT232R are not probes.
    """
    usb_devs = slot.get("_usb_devices", [])
    if not usb_devs:
        return False
    probe_devs = []
    for d in usb_devs:
        vid_pid = d.get("vid_pid", "").split(":")
        if len(vid_pid) != 2:
            continue
        vid, pid = vid_pid
        if vid in _PROBE_VIDS and pid in _PROBE_PIDS:
            probe_devs.append(d)
    return bool(probe_devs) and not any(
        d for d in usb_devs if d not in probe_devs)


def _scan_usb_devices(slot: dict) -> list[dict]:
    """Scan sysfs for USB devices matching this slot's USB prefix.

    Returns list of {product, vid_pid} for non-serial USB devices (e.g. HID).
    Slot prefix "0:1.1" maps to sysfs path "1-1.1*".
    """
    prefix = slot.get("_usb_prefix")
    if not prefix:
        return []
    # Convert slot prefix "0:1.1" → sysfs glob "1-1.1*"
    # prefix format: "0:1.X" or "0:1.X.Y"
    parts = prefix.split(":")  # ["0", "1.1"]
    if len(parts) != 2:
        return []
    sysfs_pattern = f"1-{parts[1]}"
    sysfs_base = "/sys/bus/usb/devices"
    devices = []
    import glob as _glob
    for path in sorted(_glob.glob(f"{sysfs_base}/{sysfs_pattern}*")):
        name = os.path.basename(path)
        # Skip interface entries (contain ':')
        if ":" in name:
            continue
        prod_file = os.path.join(path, "product")
        vid_file = os.path.join(path, "idVendor")
        pid_file = os.path.join(path, "idProduct")
        if not os.path.isfile(prod_file):
            continue
        try:
            product = open(prod_file).read().strip()
            vid = open(vid_file).read().strip() if os.path.isfile(vid_file) else "?"
            pid = open(pid_file).read().strip() if os.path.isfile(pid_file) else "?"
            # Skip hubs
            if "hub" in product.lower():
                continue
            devices.append({"product": product, "vid_pid": f"{vid}:{pid}"})
        except OSError:
            continue
    return devices


def _refresh_all_usb_devices():
    """Rescan sysfs USB devices for every fixed slot and cache on the slot dict."""
    for slot in slots.values():
        slot["_usb_devices"] = _scan_usb_devices(slot)


def _slot_info(slot: dict) -> dict:
    """Return a JSON-safe copy of a slot (excludes _lock, promotes _recovering/_recover_retries)."""
    # Clear stale flapping: if no events in the last FLAP_WINDOW_S, the device
    # has stabilised. This handles the case where the device stops cycling and
    # no new hotplug event arrives to trigger the in-handler quiet-period check.
    if slot["flapping"] and not slot["_recovering"]:
        now = time.time()
        recent = [t for t in slot["_event_times"] if now - t < FLAP_WINDOW_S]
        slot["_event_times"] = recent
        if len(recent) < FLAP_THRESHOLD:
            label = slot["label"] or slot["slot_key"][-20:]
            slot["flapping"] = False
            slot["last_error"] = None
            slot["state"] = STATE_IDLE if slot["present"] else STATE_ABSENT
            print(f'[portal] {label}: flapping cleared (events aged out during poll)', flush=True)
            log_activity(f"{label}: device stabilised — flapping cleared", "ok")
            # Restart proxy if device is present but proxy died
            if slot["present"] and not slot["running"]:
                def _bg_restart(s=slot):
                    with s["_lock"]:
                        start_proxy(s)
                threading.Thread(target=_bg_restart, daemon=True).start()

    info = {k: v for k, v in slot.items() if not k.startswith("_")}
    info["recovering"] = slot["_recovering"]
    info["recover_retries"] = slot["_recover_retries"]
    info["devnodes"] = list(slot.get("_devnodes", {}).values())
    # Two different facts, deliberately separate: `has_gpio` says pins are
    # configured, `gpio_wired` says a measurement found them connected to
    # this board. Collapsing them is what let recovery claim a capability
    # nothing had checked.
    info["has_gpio"] = slot.get("gpio_boot") is not None
    info["gpio_wired"] = slot.get("gpio_wired")          # True/False/None
    info["gpio_wired_detail"] = slot.get("gpio_wired_detail")
    # Debug status
    label = slot.get("label") or slot.get("slot_key", "")[-20:]
    if label and debug_controller.is_debugging(label):
        sessions = debug_controller.status()
        sess = sessions.get(label, {})
        info["debugging"] = True
        info["debug_chip"] = sess.get("chip")
        info["debug_gdb_port"] = sess.get("gdb_port")
    else:
        info["debugging"] = False
    # Always expose detected chip and JTAG source (persist after debug stop)
    info["detected_chip"] = slot.get("_auto_debug_chip")
    info["monitor_port"] = slot.get("monitor_port")
    info["jtag_slot"] = slot.get("_jtag_slot")
    # Cached USB device info (updated on hotplug and boot)
    usb_devs = slot.get("_usb_devices", [])
    info["usb_devices"] = usb_devs
    # Detect debug probes and HID devices
    usb_warning = None
    is_probe = _is_probe_slot(slot)
    if usb_devs:
        hid_devs = [d for d in usb_devs
                     if "hid" in d.get("product", "").lower()
                     or "keyboard" in d.get("product", "").lower()
                     or "mouse" in d.get("product", "").lower()]
        if hid_devs:
            names = ", ".join(d["product"] for d in hid_devs)
            usb_warning = f"Not flashable — USB in HID mode: {names}"
    info["usb_warning"] = usb_warning
    info["is_probe"] = is_probe
    return info


# ---------------------------------------------------------------------------
# Serial Services — reset and monitor (FR-008, FR-009)
# ---------------------------------------------------------------------------

def _resolve_slot(body: dict) -> dict | None:
    """Find the slot a request means, by label or by slot_key.

    Every other endpoint takes {"slot": "SLOT3"}; /api/start and /api/stop
    historically took slot_key only, so a caller who stopped a slot with the
    obvious payload got "missing slot_key" and could leave the bench with a
    dead proxy. Both spellings work now.
    """
    label = body.get("slot")
    if label:
        return _find_slot_by_label(label)
    key = body.get("slot_key")
    if key:
        return slots.get(key) or _find_slot_by_label(key)
    return None


def _find_slot_by_label(label: str) -> dict | None:
    """Find a slot by label or truncated slot_key."""
    for s in slots.values():
        if s["label"] == label:
            return s
    # Fallback: match truncated slot_key (for dynamic/unconfigured slots)
    for s in slots.values():
        if s.get("slot_key", "")[-20:] == label:
            return s
    return None


def _read_serial_lines(ser, pattern: str | None, timeout: float) -> tuple[list[str], str | None]:
    """Read serial lines until pattern matched or timeout.

    Returns (lines, matched_line) where matched_line is None if no match.
    """
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        chunk = ser.read(512)
        if chunk:
            buf += chunk
            text = buf.decode("utf-8", errors="replace")
            new_lines = text.split("\n")
            # Last element may be incomplete — keep in buf
            if not text.endswith("\n"):
                buf = new_lines.pop().encode("utf-8", errors="replace")
            else:
                buf = b""
            for line in new_lines:
                stripped = line.strip()
                if stripped:
                    lines.append(stripped)
                    if pattern and pattern in stripped:
                        return lines, stripped
    # Process any remaining buffer
    if buf:
        stripped = buf.decode("utf-8", errors="replace").strip()
        if stripped:
            lines.append(stripped)
            if pattern and pattern in stripped:
                return lines, stripped
    return lines, None


def serial_reset(slot: dict) -> dict:
    """FR-008: Reset device via DTR/RTS.  Stops proxy, opens direct serial,
    sends reset pulse, reads initial boot output, restarts proxy.

    Returns {"ok": True/False, "output": [...], "error": "..."}.
    """
    import serial as pyserial

    label = slot["label"]
    devnode = slot.get("devnode")

    if not devnode:
        return {"ok": False, "error": f"{label}: no device node"}
    if not slot.get("present"):
        return {"ok": False, "error": f"{label}: device not present"}

    # Stop the proxy so we can open direct serial
    with slot["_lock"]:
        stop_proxy(slot)
        slot["state"] = STATE_RESETTING

    # Open direct serial with DTR/RTS safe
    try:
        ser = pyserial.Serial(devnode, 115200, timeout=0.1)
        ser.dtr = False
        ser.rts = False
        time.sleep(0.1)
        ser.read(8192)  # drain
    except Exception as e:
        slot["state"] = STATE_IDLE if slot["present"] else STATE_ABSENT
        return {"ok": False, "error": f"Cannot open {devnode}: {e}"}

    # Send DTR/RTS reset pulse
    ser.dtr = True
    time.sleep(0.05)
    ser.dtr = False
    time.sleep(0.05)
    ser.rts = True
    time.sleep(0.05)
    ser.rts = False

    # Read boot output (up to 5s)
    lines, _ = _read_serial_lines(ser, None, timeout=5.0)
    ser.close()

    # Push boot output into the ring buffer for /api/serial/output
    now = time.time()
    buf = slot["_serial_buf"]
    for line in lines:
        buf.append({"ts": now, "text": line})

    # Restart the proxy — DTR/RTS resets don't cause USB re-enumeration
    # (the chip reboots but ttyACM stays), so hotplug won't restart it.
    time.sleep(NATIVE_USB_BOOT_DELAY_S)
    with slot["_lock"]:
        if not slot["running"]:
            start_proxy(slot)
        # start_proxy sets STATE_IDLE on success; set it here if proxy failed
        if slot["state"] == STATE_RESETTING:
            slot["state"] = STATE_IDLE if slot["present"] else STATE_ABSENT

    return {"ok": True, "output": lines}



def jtag_reset_capturing(slot: dict, effective_label: str,
                         timeout: float = 5.0) -> dict:
    """JTAG reset that also returns the device's boot output.

    A JTAG reset does not re-enumerate USB, so the proxy stays up and the boot
    output can be read *through* it — unlike the DTR/RTS path, which must stop
    the proxy and take the device.

    The listener attaches **before** the reset is issued. A banner printed
    while nothing is reading is gone, and a monitor opened afterwards reports a
    silence indistinguishable from a device that never started.

    `output` carries the device's boot lines in both reset paths; OpenOCD's own
    reply is returned separately as `openocd`.
    """
    sock = None
    monitor_port = slot.get("monitor_port")
    if monitor_port and slot.get("running"):
        try:
            # The fan-out, not RFC2217: an observer must not be able to touch
            # the control lines of the part it is about to watch boot.
            sock = socket.create_connection(("127.0.0.1", monitor_port), timeout=5)
            sock.settimeout(0.2)
        except OSError as e:
            log_activity(f"jtag reset: cannot attach to monitor port ({e})", "info")
            sock = None

    result = debug_controller.jtag_reset(effective_label)
    if not result.get("ok"):
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        return result

    lines: list[str] = []
    if sock is not None:
        buf = b""
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    text = raw.decode("utf-8", errors="replace").rstrip("\r")
                    if text:
                        lines.append(text)
        except Exception as e:
            log_activity(f"jtag reset: boot capture failed ({e})", "info")
        finally:
            try:
                sock.close()
            except OSError:
                pass

        now = time.time()
        buf = slot["_serial_buf"]
        for line in lines:
            buf.append({"ts": now, "text": line})

    result["openocd"] = result.pop("output", "")
    result["output"] = lines
    return result





def _start_recorder(slot: dict) -> None:
    """Keep a slot's ring buffer filled whenever its proxy is up.

    The fan-out drains the device continuously, but nothing was *recording*
    unless a monitor happened to be attached — so a reply that arrived between
    two calls was gone. That makes a write-then-read sequence unreliable for
    exactly the reason it looks reliable: it usually works, and fails when the
    device answers quickly.

    One reader per slot, reconnecting on its own, so /api/serial/output can
    answer for a window nobody was watching.
    """
    label = slot["label"]
    # Liveness, not a flag. `_recorder_running` was set once and never
    # cleared, so a thread that died took the slot's recording with it
    # permanently: every later start_proxy() saw the flag and returned.
    thread = slot.get("_recorder_thread")
    if thread is not None and thread.is_alive():
        return
    slot["_recorder_running"] = True

    def run():
        buf = b""
        while slot.get("_recorder_running"):
            port = slot.get("monitor_port")
            if not port or not slot.get("running"):
                time.sleep(1.0)
                continue
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                sock.settimeout(1.0)
            except OSError:
                time.sleep(1.0)
                continue
            try:
                while slot.get("_recorder_running") and slot.get("running"):
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        text = raw.decode("utf-8", errors="replace").rstrip("\r")
                        if text:
                            slot["_serial_buf"].append(
                                {"ts": time.time(), "text": text})
            finally:
                try:
                    sock.close()
                except OSError:
                    pass

    def supervised():
        # A recorder that dies silently is worse than one that never started:
        # /api/serial/output keeps answering, with a buffer that simply stops
        # advancing.
        try:
            run()
        except Exception as exc:
            log_activity(f"recorder for {label} died: {exc}", "error")
        finally:
            slot["_recorder_running"] = False

    thread = threading.Thread(target=supervised, name=f"recorder-{label}",
                              daemon=True)
    slot["_recorder_thread"] = thread
    thread.start()
    log_activity(f"recorder started for {label}", "info")

def serial_write(slot: dict, data: bytes, timeout: float = 5.0) -> dict:
    """FR-030: send bytes to a device.

    Three separate contributors wrote this endpoint before it existed, and a
    project without it has only one alternative: open the slot's RFC2217 port
    itself. That is how a test ends up asserting DTR and RTS by accident,
    which on a native-USB part means download mode or reset — so the absence
    of this call was actively dangerous, not merely inconvenient.

    Writes through the proxy, which owns the device. The control lines are
    driven low before the port opens, never after: serial_for_url() asserts
    them on construction.
    """
    import serial as pyserial

    label = slot["label"]
    tcp_port = slot.get("tcp_port")
    if not tcp_port:
        return {"ok": False, "error": f"{label}: no tcp_port configured"}
    if not slot.get("running"):
        return {"ok": False, "error": f"{label}: proxy not running"}
    if not data:
        return {"ok": False, "error": "nothing to write"}

    # RFC2217 is single-client, and the holder is often transient: a flash
    # that has just finished, a proxy restarted moments ago by a bench reset.
    # Failing the write outright makes every caller implement this wait, and
    # the error it would report — "Remote does not seem to support RFC2217"
    # — describes a protocol mismatch rather than a slot that is busy for
    # another second.
    ser = None
    last_err = None
    for attempt in range(4):
        try:
            ser = pyserial.serial_for_url(f"rfc2217://127.0.0.1:{tcp_port}",
                                          do_not_open=True)
            ser.baudrate = 115200
            ser.timeout = timeout
            ser.dtr = False
            ser.rts = False
            ser.open()
            break
        except Exception as e:
            last_err = e
            ser = None
            if attempt < 3:
                time.sleep(1.0)
    if ser is None:
        return {"ok": False,
                "error": f"cannot reach the proxy on {tcp_port}: {last_err}"}
    try:
        written = ser.write(data)
        ser.flush()
        # RFC2217 write() is a socket send: flush() does not guarantee the
        # bytes have crossed the network, reached the proxy and been put on
        # the wire. Closing straight away discards them, and the call still
        # reports success because the local write did succeed. Hold the
        # connection while the device is given a chance to answer, which both
        # drains the write and proves it went out.
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            try:
                if not ser.read(256):
                    continue
            except Exception:
                break
    except Exception as e:
        return {"ok": False, "error": f"write failed: {e}"}
    finally:
        try:
            ser.close()
        except Exception:
            pass

    log_activity(f"serial.write({label}, {len(data)} bytes)", "step")
    return {"ok": True, "written": written or len(data)}

def bench_reset() -> dict:
    """Return the whole bench to its initial state.

    A test that starts from whatever the previous test left behind is
    measuring history. This is the one call that makes "before" mean the same
    thing every time, and it belongs at the start of every run.

    Initial state, per subsystem:
      * every present slot — proxy running, no access grant, no debug session
      * WiFi              — idle: no SoftAP, not joined to anything
      * SDR               — no live console, no logging, no capture
      * operator prompt   — none pending
      * test session      — ended
      * MQTT broker       — running (shared infrastructure: ensured, never
                            stopped, because other work may depend on it)

    Every step is attempted even if an earlier one fails: a reset that gives
    up halfway leaves a bench dirtier than one that was never reset, and the
    caller could not tell which. Returns what actually changed, so a caller
    can see whether the bench was already clean.
    """
    changed: list = []
    errors: list = []

    def attempt(what, fn_):
        try:
            if fn_():
                changed.append(what)
        except Exception as e:
            errors.append(f"{what}: {e}")

    with _grants_lock:
        if _grants:
            changed.append(f"released {len(_grants)} slot grant(s)")
            _grants.clear()

    for slot in list(slots.values()):
        label = slot.get("label")
        if not label:
            continue
        if debug_controller.is_debugging(label):
            attempt(f"stopped debug session on {label}",
                    lambda lbl=label: debug_controller.stop(lbl).get("ok"))
        if slot.get("present"):
            # Restart unconditionally, not only when the proxy has died.
            #
            # RFC2217 is single-client, and a client that goes away without
            # closing cleanly leaves the connection in CLOSE-WAIT: the proxy
            # is still running, still says `running: true`, and refuses every
            # later client with "port is busy". An esptool killed mid-flash
            # does exactly this. The slot is then wedged for every subsequent
            # run, and this reset — whose whole purpose is to make "before"
            # mean the same thing every time — skipped it precisely because
            # the proxy was alive.
            attempt(f"restarted proxy for {label}",
                    lambda sl=slot: (stop_proxy(sl), start_proxy(sl))[1])

    # These two return None, not a result dict — so ask the radio what state
    # it is in rather than trusting a return value that does not exist.
    was_ap = False
    try:
        was_ap = bool(wifi_controller.ap_status().get("active"))
    except Exception:
        pass
    attempt("stopped WiFi AP",
            lambda: (wifi_controller.ap_stop(), was_ap)[1])
    was_sta = False
    try:
        was_sta = wifi_controller.get_mode().get("mode") not in (None, "idle", "ap")
    except Exception:
        pass
    attempt("left WiFi network",
            lambda: (wifi_controller.sta_leave(), was_sta)[1])

    if _sdr is not None:
        attempt("stopped SDR live console", lambda: bool(_sdr.stop_live()))
        attempt("stopped SDR logging", lambda: bool(_sdr.stop_log()))
        attempt("stopped SDR capture", lambda: bool(_sdr.stop()))

    global _human_event, _human_confirmed, _human_message
    with _human_lock:
        if _human_message is not None:
            _human_message = None
            _human_confirmed = False
            if _human_event is not None:
                _human_event.set()
                _human_event = None
            changed.append("cleared pending operator prompt")

    global _test_session
    with _test_lock:
        if _test_session:
            _test_session = None
            changed.append("ended test session")

    attempt("started MQTT broker", lambda: mqtt_controller.start().get("ok"))

    log_activity(f"bench.reset — {len(changed)} change(s)", "ok")
    return {"ok": True, "changed": changed, "errors": errors}

# ---------------------------------------------------------------------------
# Slot access manager (FR-031 – FR-035)
#
# Arbitrates MODE, never the data path. Bytes still flow by esptool, OpenOCD
# and the RFC2217 proxy exactly as before; this only answers "who owns this
# slot, in what mode, until when", and refuses conflicting requests instead of
# letting one operation silently terminate another.
# ---------------------------------------------------------------------------

SLOT_MODES = ("idle", "flashing", "resetting", "monitoring",
              "flapping", "recovering", "download", "debugging")
GRANT_TTL_DEFAULT = 60.0
GRANT_TTL_MAX = 3600.0

_grants: dict = {}
_grants_lock = threading.Lock()


def _grant_expired(g, now=None) -> bool:
    return (now or time.time()) >= g["expires_at"]


def _live_grant(label: str):
    """Return the slot's live grant, dropping it if the lease has run out.

    Expiry is lazy: a holder that stopped renewing is reclaimed the next time
    anyone asks, which is when it matters. This is the bounded version of the
    failure where a dead client held a slot until the portal was restarted.
    """
    g = _grants.get(label)
    if g and _grant_expired(g):
        log_activity(
            f"slot.lease expired for {label} — reclaiming from {g['owner']}", "info")
        _grants.pop(label, None)
        return None
    return g


def _devnode_holders(devnode: str) -> list:
    """Processes holding *devnode*, as [(pid, cmdline)].

    FR-034. The manager is cooperative and cannot stop a process opening the
    device behind its back — but it can notice, and refuse while naming it.
    A named refusal is worth far more than the silence it replaces.
    """
    holders = []
    if not devnode:
        return holders
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            fd_dir = f"/proc/{entry}/fd"
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        if os.readlink(f"{fd_dir}/{fd}") == devnode:
                            try:
                                with open(f"/proc/{entry}/cmdline", "rb") as fh:
                                    cmd = fh.read().replace(b"\0", b" ").decode(
                                        "utf-8", "replace").strip()
                            except OSError:
                                cmd = ""
                            holders.append((int(entry), cmd))
                            break
                    except OSError:
                        continue
            except OSError:
                continue
    except OSError:
        pass
    return holders


def _unexpected_holders(slot: dict) -> list:
    """Holders of the slot's devnode that are not the slot's own proxy."""
    expected = {slot.get("pid")} - {None}
    return [(pid, cmd) for pid, cmd in _devnode_holders(slot.get("devnode"))
            if pid not in expected]


def slot_acquire(slot: dict, mode: str, owner: str, ttl: float) -> tuple:
    """Grant *mode* on *slot* to *owner*. Returns (body, http_status)."""
    label = slot["label"]
    if mode not in SLOT_MODES:
        return {"ok": False, "error": f"unknown mode '{mode}'",
                "modes": list(SLOT_MODES)}, 400
    if not owner:
        return {"ok": False, "error": "missing 'owner'"}, 400
    ttl = max(1.0, min(float(ttl or GRANT_TTL_DEFAULT), GRANT_TTL_MAX))

    with _grants_lock:
        held = _live_grant(label)
        if held:
            # FR-033: refuse, never pre-empt, and name the incumbent.
            return {"ok": False, "error": "held",
                    "mode": held["mode"], "owner": held["owner"],
                    "since": held["since"],
                    "expires_in": round(held["expires_at"] - time.time(), 1)}, 409

        # FR-035: OpenOCD claims a different USB interface, so devnode
        # inspection cannot see it — the manager's own record must.
        eff = slot.get("label") or ""
        if mode != "debugging" and debug_controller.is_debugging(eff):
            return {"ok": False, "error": "held", "mode": "debugging",
                    "owner": "debug-session", "since": None,
                    "expires_in": None}, 409

        strays = _unexpected_holders(slot)
        if strays:
            pid, cmd = strays[0]
            return {"ok": False, "error": "device held out of band",
                    "pid": pid, "cmdline": cmd}, 409

        now = time.time()
        token = uuid.uuid4().hex[:12]
        _grants[label] = {
            "token": token, "mode": mode, "owner": owner, "ttl": ttl,
            "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "expires_at": now + ttl,
        }
    log_activity(f"slot.acquire({label}, {mode}) granted to {owner}", "ok")
    return {"ok": True, "token": token, "mode": mode,
            "expires_in": round(ttl, 1)}, 200


def slot_by_token(token: str):
    for label, g in _grants.items():
        if g["token"] == token:
            return label, g
    return None, None


def slot_renew(token: str) -> tuple:
    with _grants_lock:
        label, g = slot_by_token(token)
        if not g or _grant_expired(g):
            return {"ok": False, "error": "unknown or expired token"}, 404
        # The holder's own ttl, not the default: a renewal that silently
        # extends an 8 s lease to 60 s is a lease nobody asked for.
        ttl = max(1.0, min(g["ttl"], GRANT_TTL_MAX))
        g["expires_at"] = time.time() + ttl
    return {"ok": True, "expires_in": round(ttl, 1)}, 200


def slot_release(token: str) -> tuple:
    with _grants_lock:
        label, g = slot_by_token(token)
        if not g:
            return {"ok": False, "error": "unknown token"}, 404
        _grants.pop(label, None)
    log_activity(f"slot.release({label}) by {g['owner']}", "ok")
    return {"ok": True, "mode": "idle"}, 200


def slot_mode(slot: dict) -> dict:
    label = slot["label"]
    with _grants_lock:
        g = _live_grant(label)
    if not g:
        eff = slot.get("label") or ""
        if debug_controller.is_debugging(eff):
            return {"ok": True, "mode": "debugging", "owner": "debug-session",
                    "since": None, "expires_in": None}
        return {"ok": True, "mode": "idle" if slot.get("present") else "absent",
                "owner": None, "since": None, "expires_in": None}
    return {"ok": True, "mode": g["mode"], "owner": g["owner"],
            "since": g["since"],
            "expires_in": round(g["expires_at"] - time.time(), 1)}

# esptool 5 renamed the console script (`esptool.py` -> `esptool`) and every
# subcommand and flag from underscores to hyphens; the old spellings still work
# but warn, and go away at the next major release. The module form is the one
# name that has never changed, so invoke through it rather than either script.
# Requires esptool >= 5 for the hyphenated spellings — install.sh pins that.
ESPTOOL = ["python3", "-m", "esptool"]

_CHIP_INFO_PATTERNS = {
    # esptool renamed several of these at v5 ("Chip is" -> "Chip type:",
    # "Crystal is" -> "Crystal frequency:"). Accept both so the endpoint keeps
    # working on a bench that has not been updated.
    "chip": r"^Chip (?:is|type:)\s+(.+?)(?:\s+\(revision|$)",
    "revision": r"revision (v?[\d.]+)",
    "features": r"^Features:\s+(.+)$",
    "crystal": r"^Crystal (?:is|frequency:)\s+(.+)$",
    "usb_mode": r"^USB mode:\s+(.+)$",
    "mac": r"^MAC:\s+([0-9a-fA-F:]+)$",
    "flash_manufacturer": r"^Manufacturer:\s+(\w+)$",
    "flash_device": r"^Device:\s+(\w+)$",
    "flash_size": r"^Detected flash size:\s+(.+)$",
}


def _parse_chip_info(text: str) -> dict:
    """Pull the identity fields out of `esptool flash-id` output.

    Every field is optional: esptool's wording varies by version, and a missing
    key is better than a wrong one. Callers that need certainty read `output`.
    """
    info: dict = {}
    for key, pattern in _CHIP_INFO_PATTERNS.items():
        m = re.search(pattern, text, re.M)
        if m:
            info[key] = m.group(1).strip()
    return info


def flash_device(slot: dict, files: dict, esptool_args: list[str],
                 timeout_s: float = 300.0) -> dict:
    """Flash a device on *slot* via esptool.

    Stops the RFC2217 proxy so esptool can claim the local devnode, writes
    the uploaded *files* (basename → bytes) into a temp dir, runs esptool
    with *esptool_args* (any token equal to a basename is rewritten to its
    temp path), then restarts the proxy.

    Returns {"ok": bool, "output": str, "returncode": int, "error": str?}.
    """
    import tempfile

    label = slot["label"]
    devnode = slot.get("devnode")

    if not devnode:
        return {"ok": False, "error": f"{label}: no device node"}
    if not slot.get("present"):
        return {"ok": False, "error": f"{label}: device not present"}

    # A debug session must not be holding the chip while esptool drives it.
    # OpenOCD halts the target when it connects, and auto-debug reattaches on
    # its own, so a flash could run against a halted CPU and hand back a
    # device that boots into nothing. Everything downstream then reads an
    # empty serial buffer and blames the flash, the firmware, or the cable.
    # Stop it here and put it back afterwards — the caller asked to flash a
    # board, not to think about who else had it.
    was_debugging = debug_controller.is_debugging(label)
    if was_debugging:
        log_activity(f"flash({label}) — stopping debug session first", "step")
        debug_controller.stop(label)

    # Stop the proxy so esptool can open the local serial port directly
    with slot["_lock"]:
        was_running = bool(slot.get("running"))
        if was_running:
            stop_proxy(slot)
        slot["state"] = STATE_RESETTING

    output_text = ""
    returncode = -1
    error = None
    try:
        with tempfile.TemporaryDirectory(prefix="wb-flash-") as tmpdir:
            paths = {}
            for name, data in files.items():
                if "/" in name or ".." in name:
                    error = f"invalid filename: {name}"
                    break
                p = os.path.join(tmpdir, name)
                with open(p, "wb") as f:
                    f.write(data)
                paths[name] = p
            if not error:
                resolved = [paths.get(a, a) for a in esptool_args]
                cmd = ESPTOOL + ["--port", devnode] + resolved
                log_activity(f"flash({label}) — {' '.join(cmd[:6])} ...", "step")
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout_s,
                )
                output_text = (proc.stdout or "") + (proc.stderr or "")
                returncode = proc.returncode
                if returncode != 0:
                    error = f"esptool exited {returncode}"
    except subprocess.TimeoutExpired:
        error = f"esptool timed out after {timeout_s}s"
    except Exception as e:
        error = f"flash failed: {e}"
    finally:
        # Restart the proxy on the same devnode (DTR/RTS reset doesn't cause
        # USB re-enumeration — same handling as serial_reset).
        time.sleep(NATIVE_USB_BOOT_DELAY_S)
        with slot["_lock"]:
            if was_running and not slot.get("running"):
                start_proxy(slot)
            if slot["state"] == STATE_RESETTING:
                slot["state"] = STATE_IDLE if slot["present"] else STATE_ABSENT

    # esptool's final hard reset leaves the device running. Auto-debug will
    # reattach shortly and halt it again, so a slot that was being debugged
    # gets its session back with the CPU explicitly released — debugging a
    # board and being able to watch it run are not alternatives.
    if was_debugging:
        _restore_debug_running(slot, label)

    if error:
        log_activity(f"flash({label}) — failed: {error}", "error")
        return {"ok": False, "error": error,
                "output": output_text, "returncode": returncode}
    log_activity(f"flash({label}) — ok ({len(output_text)} bytes output)", "ok")
    return {"ok": True, "output": output_text, "returncode": returncode}


def _restore_debug_running(slot: dict, label: str) -> None:
    """Reattach OpenOCD to *slot* and leave its CPU running, not halted."""
    chip = slot.get("_auto_debug_chip")
    r = debug_controller.start(label, slot, slot.get("gdb_port"),
                               slot.get("openocd_telnet_port"), chip, None)
    if not r.get("ok"):
        log_activity(f"flash({label}) — debug session not restored: "
                     f"{r.get('error')}", "info")
        return
    out = debug_controller.jtag_reset(label, halt=False)
    log_activity(f"flash({label}) — debug session restored, CPU running"
                 if out.get("ok") else
                 f"flash({label}) — debug restored but 'reset run' failed",
                 "ok" if out.get("ok") else "info")


def read_flash_device(slot: dict, offset: int, length: int,
                      chip: str = "auto", baud: str = "460800",
                      timeout_s: float = 120.0) -> dict:
    """Read *length* bytes of flash from *offset* on *slot*'s device via esptool.

    The counterpart to flash_device: the portal has always been able to WRITE
    flash but never read it back, so pulling a coredump, an NVS blob, or the
    running partition table meant dropping to OpenOCD. Same proxy lifecycle as a
    flash — stop the RFC2217 proxy, run `esptool read-flash`, restart the proxy.

    Returns {"ok": bool, "data": bytes, "sha256": str, "output": str,
    "returncode": int, "error": str?}. The HTTP layer base64-encodes `data`.
    """
    import tempfile

    label = slot["label"]
    devnode = slot.get("devnode")

    if not devnode:
        return {"ok": False, "error": f"{label}: no device node"}
    if not slot.get("present"):
        return {"ok": False, "error": f"{label}: device not present"}
    if length <= 0:
        return {"ok": False, "error": "length must be positive"}

    with slot["_lock"]:
        was_running = bool(slot.get("running"))
        if was_running:
            stop_proxy(slot)
        slot["state"] = STATE_RESETTING

    data = b""
    output_text = ""
    returncode = -1
    error = None
    try:
        with tempfile.TemporaryDirectory(prefix="wb-read-") as tmpdir:
            outfile = os.path.join(tmpdir, "readback.bin")
            cmd = ESPTOOL + ["--port", devnode]
            if chip and chip != "auto":
                cmd += ["--chip", chip]
            cmd += ["--baud", str(baud), "read-flash",
                    hex(offset), str(length), outfile]
            log_activity(
                f"read-flash({label}) — {hex(offset)} +{length}B", "step")
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s,
            )
            output_text = (proc.stdout or "") + (proc.stderr or "")
            returncode = proc.returncode
            if returncode != 0:
                error = f"esptool exited {returncode}"
            elif os.path.exists(outfile):
                with open(outfile, "rb") as f:
                    data = f.read()
            else:
                error = "esptool produced no output file"
    except subprocess.TimeoutExpired:
        error = f"esptool timed out after {timeout_s}s"
    except Exception as e:
        error = f"read-flash failed: {e}"
    finally:
        # Same as flash_device: read-flash does a DTR/RTS reset, not a USB
        # re-enum, so the same devnode is reused after a short boot delay.
        time.sleep(NATIVE_USB_BOOT_DELAY_S)
        with slot["_lock"]:
            if was_running and not slot.get("running"):
                start_proxy(slot)
            if slot["state"] == STATE_RESETTING:
                slot["state"] = STATE_IDLE if slot["present"] else STATE_ABSENT

    if error:
        log_activity(f"read-flash({label}) — failed: {error}", "error")
        return {"ok": False, "error": error,
                "output": output_text, "returncode": returncode}
    sha = hashlib.sha256(data).hexdigest()
    log_activity(f"read-flash({label}) — ok ({len(data)} bytes)", "ok")
    return {"ok": True, "data": data, "sha256": sha,
            "output": output_text, "returncode": returncode}


def serial_monitor(slot: dict, pattern: str | None = None,
                   timeout: float = 10.0) -> dict:
    """FR-009: Read serial output through the proxy's read-only fan-out.

    Not an RFC2217 client. RFC2217 is single-client and its negotiation
    applies control-line changes to the device — so monitoring used to take
    the one connection slot, could be starved by any other client, and put a
    caller one careless option away from asserting DTR or RTS. The fan-out
    port is plain bytes: any number of observers, none able to disturb what
    they are watching.

    Returns {"ok": True, "matched": True/False, "line": "...", "output": [...]}.
    """
    label = slot["label"]
    monitor_port = slot.get("monitor_port")

    if not monitor_port:
        return {"ok": False, "error": f"{label}: no monitor port configured"}
    if not slot.get("running"):
        return {"ok": False, "error": f"{label}: proxy not running"}

    try:
        sock = socket.create_connection(("127.0.0.1", monitor_port), timeout=5)
    except OSError as e:
        return {"ok": False,
                "error": f"Cannot connect to monitor port {monitor_port}: {e}"}

    slot["state"] = STATE_MONITORING
    lines: list[str] = []
    matched: str | None = None
    buf = b""
    deadline = time.monotonic() + timeout
    try:
        sock.settimeout(0.2)
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = raw.decode("utf-8", errors="replace").rstrip("\r")
                if not text:
                    continue
                lines.append(text)
                now = time.time()
                slot["_serial_buf"].append({"ts": now, "text": text})
                if pattern and pattern in text:
                    matched = text
                    break
            if matched:
                break
    finally:
        try:
            sock.close()
        except OSError:
            pass
        slot["state"] = STATE_IDLE if slot["present"] else STATE_ABSENT

    return {"ok": True, "matched": matched is not None,
            "line": matched, "output": lines}

def _start_flap_recovery(slot: dict):
    """Entry point when flapping is detected.  Unbinds USB to stop the storm,
    then dispatches to GPIO or no-GPIO recovery in a background thread."""
    label = slot["label"] or slot["slot_key"][-20:]

    if slot["_recovering"]:
        return  # Already in a recovery cycle

    slot["_recovering"] = True
    slot["state"] = STATE_RECOVERING

    # Stop proxy if still running
    with slot["_lock"]:
        if slot["running"] and slot["pid"]:
            stop_proxy(slot)

    # Unbind USB at kernel level — event storm stops immediately
    usb_device = _usb_device_for_slot(slot)
    if usb_device:
        _usb_unbind(usb_device)
        log_activity(f"{label}: USB unbound — flap storm stopped", "ok")
    else:
        log_activity(f"{label}: cannot determine USB device from slot_key", "error")
        slot["_recovering"] = False
        slot["state"] = STATE_FLAPPING
        return

    # Configured pins are not connected pins. Take the GPIO path only when
    # the wiring has actually been measured (POST /api/serial/gpio-test);
    # otherwise fall back to the unbind/rebind cycle, which needs no wires
    # and, unlike the GPIO path, has always been honest about its outcome.
    if slot.get("gpio_boot") is not None and not slot.get("gpio_wired"):
        log_activity(
            f"{label}: BOOT/EN wiring unconfirmed — using the no-GPIO "
            f"recovery. Run POST /api/serial/gpio-test to measure it.",
            "warn")
    has_gpio = (slot.get("gpio_boot") is not None
                and bool(slot.get("gpio_wired")))
    if has_gpio:
        t = threading.Thread(
            target=_recover_with_gpio, args=(slot, usb_device),
            daemon=True, name=f"recover-gpio-{label}",
        )
    else:
        t = threading.Thread(
            target=_recover_without_gpio, args=(slot, usb_device),
            daemon=True, name=f"recover-nogpio-{label}",
        )
    t.start()


def _download_mode_reached(devnode: str, timeout_s: float = 25.0) -> tuple:
    """Is the device actually in download mode? Returns (ok, detail).

    Asks the only question that matters to the caller: can a flasher talk to
    it. `--before no-reset` is deliberate — resetting here would be the check
    creating the condition it is supposed to observe, and a board that only
    reaches download mode because esptool toggled DTR/RTS is a board whose
    GPIO recovery did nothing.

    A failure is not proof the wiring is absent — a dead board looks the same
    from here — so the detail says what was seen and lets the caller judge.
    """
    if not devnode or not os.path.exists(devnode):
        return False, f"no devnode ({devnode or 'unknown'}) after rebind"
    try:
        proc = subprocess.run(
            ESPTOOL + ["--port", devnode, "--before", "no-reset",
                       "--after", "no-reset", "--connect-attempts", "1",
                       "chip-id"],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, "esptool timed out talking to the device"
    except Exception as exc:
        return False, f"could not run esptool: {exc}"

    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode == 0:
        return True, out[-300:]
    return False, out[-300:] or f"esptool exited {proc.returncode}"



# ── GPIO wiring: measured, not assumed ──────────────────────────────────
#
# `gpio_boot`/`gpio_en` are *configuration*: the portal fills in 18 and 17 by
# default, and nothing in software can see whether a wire exists. Recovery
# believed the configuration, drove two pins that went nowhere on most
# benches, and reported success — so the bench claimed a capability it did
# not have, which is the failure this whole file keeps trying to eliminate.
#
# The operator could be asked to confirm the wiring, but a person recalling
# what they soldered is an observation by eye, and this project's own rule is
# that observations belong to instruments. So measure it: pulsing EN on a
# wired board resets it and the ROM says so on serial; holding BOOT low
# across that reset puts a wired board in download mode, which esptool can
# confirm. Both are answerable in about fifteen seconds.
GPIO_WIRING_FILE = os.environ.get(
    "GPIO_WIRING_FILE", "/var/lib/rfc2217/gpio-wiring.json")

_ROM_BANNER = re.compile(r"ESP-ROM:|rst:0x[0-9a-fA-F]+|boot:0x[0-9a-fA-F]+")


def _load_gpio_wiring() -> dict:
    try:
        with open(GPIO_WIRING_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_gpio_wiring(label: str, record: dict):
    data = _load_gpio_wiring()
    data[label] = record
    try:
        os.makedirs(os.path.dirname(GPIO_WIRING_FILE), exist_ok=True)
        with open(GPIO_WIRING_FILE, "w") as fh:
            json.dump(data, fh, indent=1)
    except OSError as exc:
        print(f"[portal] could not persist GPIO wiring: {exc}", flush=True)


def _saw_rom_banner(slot: dict, since: float, timeout: float) -> bool:
    """Did the device print a ROM boot banner after *since*?"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for entry in list(slot.get("_serial_buf") or []):
            if entry.get("ts", 0) > since and _ROM_BANNER.search(entry.get("text", "")):
                return True
        time.sleep(0.3)
    return False


def test_gpio_wiring(slot: dict) -> dict:
    """Drive EN and BOOT and observe whether this board is actually wired.

    Returns {ok, en_wired, boot_wired, detail}. Leaves the board running.
    """
    label = slot["label"] or slot["slot_key"][-20:]
    gpio_boot = slot.get("gpio_boot")
    gpio_en = slot.get("gpio_en")
    if gpio_boot is None and gpio_en is None:
        return {"ok": False, "error": f"{label}: no GPIO pins configured"}

    result = {"ok": True, "en_wired": False, "boot_wired": False, "detail": ""}

    # ── EN: pulse it and listen for the ROM ──────────────────────────
    if gpio_en is None:
        result["detail"] = "no gpio_en configured, so EN cannot be tested; "
    else:
        log_activity(f"gpio-test({label}): pulsing EN (GPIO{gpio_en})", "step")
        since = time.time()
        try:
            _gpio_set(gpio_en, 0)
            time.sleep(0.15)
            _gpio_set(gpio_en, 1)
        except Exception as exc:
            return {"ok": False, "error": f"{label}: driving EN failed: {exc}"}
        result["en_wired"] = _saw_rom_banner(slot, since, timeout=8.0)
        result["detail"] = (
            "EN pulse produced a ROM boot banner; "
            if result["en_wired"] else
            "EN pulse produced no reset — the pin is not connected to this "
            "board (or the board is unresponsive); ")

    # ── BOOT: only meaningful if we can reset the board ──────────────
    if result["en_wired"] and gpio_boot is not None:
        log_activity(f"gpio-test({label}): holding BOOT (GPIO{gpio_boot}) "
                     f"across a reset", "step")
        was_running = slot.get("running")
        with slot["_lock"]:
            if was_running:
                stop_proxy(slot)
        try:
            _gpio_set(gpio_boot, 0)
            time.sleep(0.05)
            _gpio_set(gpio_en, 0)
            time.sleep(0.15)
            _gpio_set(gpio_en, 1)
            time.sleep(1.5)
            ok, detail = _download_mode_reached(slot.get("devnode") or "")
            result["boot_wired"] = ok
            result["detail"] += (
                "BOOT held across the reset reached download mode."
                if ok else
                f"BOOT held across the reset did not reach download mode "
                f"({detail}).")
        except Exception as exc:
            result["detail"] += f"BOOT test failed: {exc}"
        finally:
            # Always hand the board back running, whatever we learned.
            try:
                _release_slot_gpio(slot)
            except Exception:
                pass
            if was_running:
                with slot["_lock"]:
                    start_proxy(slot)
    elif result["en_wired"]:
        result["detail"] += "no gpio_boot configured, so BOOT was not tested."

    wired = bool(result["en_wired"] and result["boot_wired"])
    slot["gpio_wired"] = wired
    slot["gpio_wired_detail"] = result["detail"]
    _save_gpio_wiring(label, {
        "en_wired": result["en_wired"],
        "boot_wired": result["boot_wired"],
        "gpio_wired": wired,
        "detail": result["detail"],
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    log_activity(f"gpio-test({label}): EN={result['en_wired']} "
                 f"BOOT={result['boot_wired']} — {result['detail']}",
                 "ok" if wired else "warn")
    return result


def _recover_with_gpio(slot: dict, usb_device: str):
    """Recovery for boards WITH GPIO pins configured.

    1. Wait cooldown
    2. Hold BOOT/GPIO0 LOW (forces download mode on next boot)
    3. Pulse EN/RST if configured
    4. Rebind USB — device enumerates in download mode (stable)
    5. State → download_mode; BOOT stays held LOW until /api/serial/release
    """
    label = slot["label"] or slot["slot_key"][-20:]
    gpio_boot = slot["gpio_boot"]
    gpio_en = slot.get("gpio_en")

    log_activity(f"{label}: GPIO recovery — waiting {FLAP_COOLDOWN_S}s cooldown", "step")
    time.sleep(FLAP_COOLDOWN_S)

    # Hold BOOT/GPIO0 LOW → forces download mode
    try:
        _gpio_set(gpio_boot, 0)
        log_activity(f"{label}: GPIO{gpio_boot} (BOOT) held LOW", "step")
    except Exception as e:
        log_activity(f"{label}: GPIO set failed: {e}", "error")
        slot["_recovering"] = False
        slot["state"] = STATE_FLAPPING
        return

    # Pulse EN/RST if we have it — clean reset into download mode
    if gpio_en is not None:
        try:
            _gpio_set(gpio_en, 0)
            time.sleep(0.1)
            _gpio_set(gpio_en, 1)
            log_activity(f"{label}: GPIO{gpio_en} (EN) pulsed — reset", "step")
            time.sleep(0.5)
        except Exception as e:
            log_activity(f"{label}: EN pulse failed: {e}", "error")

    # Rebind USB — device should enumerate in download mode now
    _usb_rebind(usb_device)
    time.sleep(2)  # Let kernel enumerate

    slot["_recovering"] = False
    slot["flapping"] = False

    # Ask the device, do not assume. Driving BOOT low and pulsing EN is an
    # act, not an outcome: `has_gpio` means pins are *configured* — the portal
    # fills in defaults 18/17 — and no amount of configuration tells the bench
    # whether wires exist. On a board with nothing connected this drove two
    # pins into thin air, declared download_mode, and left a caller to
    # discover the truth from esptool failing on a port that reads healthy.
    # Re-resolve rather than trust the recorded path: a rebind re-enumerates
    # and the kernel picks the next free ttyACM number, so the slot's devnode
    # is stale by definition here.
    devnode = _devnode_from_usb_device(usb_device) or slot.get("devnode") or ""
    if devnode and devnode != slot.get("devnode"):
        log_activity(f"{label}: devnode moved to {devnode} after rebind", "info")
        slot["devnode"] = devnode
    ok, detail = _download_mode_reached(devnode)
    if ok:
        slot["_recover_retries"] = 0
        slot["state"] = STATE_DOWNLOAD_MODE
        slot["last_error"] = None
        log_activity(
            f"{label}: download mode confirmed — flash firmware, then "
            f"POST /api/serial/release", "ok")
    else:
        # Put the pins back. Holding BOOT low is only useful to a caller who
        # is about to flash, and there is no such caller when the device
        # never reached download mode — leaving it asserted stops the board
        # booting at all, turning a failed rescue into a worse state than the
        # one it was called for.
        try:
            _release_slot_gpio(slot)
        except Exception as exc:
            log_activity(f"{label}: could not release GPIO after failed "
                         f"recovery: {exc}", "error")
        slot["state"] = STATE_FLAPPING
        slot["last_error"] = (
            f"GPIO recovery did not reach download mode: {detail}. "
            f"Either BOOT (GPIO{gpio_boot}) and EN"
            f"{f' (GPIO{gpio_en})' if gpio_en is not None else ''} are not "
            f"wired to this board, or it is unresponsive. Re-flash over USB "
            f"or check the wiring."
        )
        log_activity(f"{label}: {slot['last_error']}", "error")


def _recover_without_gpio(slot: dict, usb_device: str):
    """Recovery for boards WITHOUT GPIO pins.

    Unbind, wait fixed cooldown, rebind, check if flapping resumes.
    After FLAP_MAX_RETRIES, gives up — corrupt flash won't self-heal.
    """
    label = slot["label"] or slot["slot_key"][-20:]
    retry = slot["_recover_retries"]

    if retry >= FLAP_MAX_RETRIES:
        slot["_recovering"] = False
        slot["state"] = STATE_FLAPPING
        slot["last_error"] = (
            f"Recovery failed after {FLAP_MAX_RETRIES} attempts — "
            "needs manual intervention (re-flash with USB cable or add GPIO wiring)"
        )
        log_activity(f"{label}: {slot['last_error']}", "error")
        return

    log_activity(f"{label}: no-GPIO recovery attempt {retry + 1}/{FLAP_MAX_RETRIES} — waiting {FLAP_COOLDOWN_S}s", "step")
    time.sleep(FLAP_COOLDOWN_S)

    slot["_recover_retries"] = retry + 1
    slot["_recovering"] = False  # Allow hotplug to detect if flapping resumes
    slot["flapping"] = False
    slot["_event_times"] = []
    slot["last_error"] = None
    slot["state"] = STATE_IDLE

    # Rebind USB — if firmware is OK, device boots normally.
    # If still corrupt, flapping resumes → _handle_hotplug detects → another cycle.
    _usb_rebind(usb_device)
    log_activity(f"{label}: USB rebound — monitoring for stability", "step")


def _release_slot_gpio(slot: dict) -> dict:
    """Release GPIO pins after flashing and reboot the device cleanly.

    Sets BOOT to high-Z, pulses EN if available.
    """
    label = slot["label"] or slot["slot_key"][-20:]
    gpio_boot = slot.get("gpio_boot")
    gpio_en = slot.get("gpio_en")

    if gpio_boot is None:
        return {"ok": False, "error": f"{label}: no gpio_boot configured"}

    # Deliberately not gated on state. Putting BOOT back to high-Z is
    # cleanup, and refusing it because the slot is not in download_mode
    # strands exactly the board that needs it most: a recovery that held BOOT
    # low and then failed leaves the pin asserted, and this was the only call
    # that lifts it. A release from an unexpected state is at worst a no-op.

    # Release BOOT pin → high-Z (input with pull-up)
    try:
        _gpio_set(gpio_boot, "z")
        log_activity(f"{label}: GPIO{gpio_boot} (BOOT) released to high-Z", "step")
    except Exception as e:
        return {"ok": False, "error": f"GPIO release failed: {e}"}

    # Pulse EN for clean reboot into normal firmware
    if gpio_en is not None:
        try:
            _gpio_set(gpio_en, 0)
            time.sleep(0.1)
            _gpio_set(gpio_en, 1)
            log_activity(f"{label}: GPIO{gpio_en} (EN) pulsed — rebooting into firmware", "step")
        except Exception as e:
            log_activity(f"{label}: EN pulse failed (non-fatal): {e}", "info")

    slot["state"] = STATE_IDLE
    slot["_recover_retries"] = 0
    log_activity(f"{label}: released — device should boot into firmware", "ok")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Enter-portal — composite serial operation (FR-008 + FR-009)
# ---------------------------------------------------------------------------

def _do_enter_portal(portal_ssid: str, wifi_ssid: str, wifi_password: str,
                     portal_ip: str = "192.168.4.1",
                     save_path: str = "/connect",
                     field_ssid: str = "ssid", field_password: str = "password",
                     extra: "dict | None" = None, method: str = "POST",
                     internet: bool = False):
    """Connect to a device's captive portal SoftAP and submit WiFi credentials.

    1. Join the device's SoftAP (portal_ssid, open network)
    2. Submit credentials to the device's captive-portal save form
    3. Disconnect from SoftAP
    4. Start our own AP with the submitted credentials so the device can connect

    The form contract is parameterized so different DUT portals are supported.
    Defaults target the WiFi-Tester DUT (`POST /connect`, ssid/password). For a
    WiFiManager DUT pass save_path="/wifisave", field_ssid="s",
    field_password="p", and any custom params via `extra`. With internet=True
    the step-4 AP is NAT-bridged to eth0 so the DUT reaches the LAN/internet.
    """
    import urllib.parse

    # -- Step 1: join the device's captive portal SoftAP --
    log_activity(f"Joining captive portal SoftAP '{portal_ssid}'...", "step")
    try:
        result = wifi_controller.sta_join(portal_ssid, password="", timeout=15)
        log_activity(f"Connected to '{portal_ssid}' — IP: {result.get('ip', '?')}", "ok")
    except Exception as e:
        log_activity(f"Failed to join '{portal_ssid}': {e}", "error")
        return

    # -- Step 2: submit WiFi credentials to the captive portal --
    log_activity(f"Submitting credentials (SSID: {wifi_ssid}) to captive portal...", "step")
    try:
        fields = {field_ssid: wifi_ssid, field_password: wifi_password}
        if extra:
            fields.update(extra)
        query = urllib.parse.urlencode(fields)
        url = f"http://{portal_ip}{save_path}"
        headers = {}
        body_b64 = None
        if method.upper() == "GET":
            url = f"{url}?{query}"
        else:
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            body_b64 = base64.b64encode(query.encode("utf-8")).decode("ascii")
        resp = wifi_controller.http_relay(
            method=method.upper(), url=url, headers=headers,
            body=body_b64, timeout=10)
        log_activity(f"Portal responded with status {resp.get('status', '?')}", "ok")
    except Exception as e:
        log_activity(f"Failed to submit credentials: {e}", "error")

    # -- Step 3: disconnect from the device's SoftAP --
    log_activity("Disconnecting from captive portal SoftAP...", "step")
    try:
        wifi_controller.sta_leave()
    except Exception as e:
        log_activity(f"sta_leave error (non-fatal): {e}", "info")

    # -- Step 4: start our AP so the device can connect to us --
    log_activity(f"Starting AP '{wifi_ssid}'{' (internet)' if internet else ''} "
                 f"for device to connect...", "step")
    try:
        result = wifi_controller.ap_start(wifi_ssid, password=wifi_password,
                                          internet=internet)
        log_activity(
            f"AP '{wifi_ssid}' running — IP: {result.get('ip', '?')}. "
            f"Waiting for device to connect...",
            "ok",
        )
    except Exception as e:
        log_activity(f"Failed to start AP '{wifi_ssid}': {e}", "error")


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[portal] {self.address_string()} {fmt % args}", flush=True)

    # -- helpers --

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass  # Client disconnected before reading response

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return None
        return json.loads(self.rfile.read(length))

    # -- routes --

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/devices":
            self._handle_get_devices()
        elif path == "/api/info":
            self._handle_get_info()
        elif path == "/api/wifi/ping":
            self._handle_wifi_ping()
        elif path == "/api/wifi/mode":
            self._handle_wifi_mode_get()
        elif path == "/api/wifi/ap_status":
            self._handle_wifi_ap_status()
        elif path == "/api/wifi/scan":
            self._handle_wifi_scan()
        elif path == "/api/wifi/events":
            qs = parse_qs(parsed.query)
            self._handle_wifi_events(qs)
        elif path == "/api/log":
            qs = parse_qs(parsed.query)
            self._handle_get_log(qs)
        elif path == "/api/human/status":
            self._handle_human_status()
        elif path == "/api/test/progress":
            self._handle_test_progress()
        elif path == "/api/gpio/status":
            self._handle_gpio_status()
        elif path == "/api/debug/status":
            self._handle_debug_status()
        elif path == "/api/debug/probes":
            self._handle_debug_probes()
        elif path == "/api/debug/group":
            self._handle_debug_group()
        elif path == "/api/siggen/status":
            self._handle_siggen_status()
        elif path == "/api/siggen/frequencies":
            qs = parse_qs(parsed.query)
            self._handle_siggen_frequencies(qs)
        elif path == "/api/sdr/status":
            self._handle_sdr_status()
        elif path == "/api/sdr/live/status":
            self._handle_sdr_live_status()
        elif path == "/api/sdr/live":
            qs = parse_qs(parsed.query)
            self._handle_sdr_live_events(qs)
        elif path == "/api/sdr/log":
            if _sdr is None:
                self._sdr_unavailable()
            else:
                self._send_json({"ok": True, **_sdr.get_log()})
        elif path == "/api/mqtt/status":
            self._send_json({"ok": True, **mqtt_controller.status()})
        elif path == "/api/udplog":
            qs = parse_qs(parsed.query)
            self._handle_get_udplog(qs)
        elif path == "/api/serial/output":
            qs = parse_qs(parsed.query)
            self._handle_serial_output(qs)
        elif path == "/api/slot/mode":
            self._handle_slot_mode(parse_qs(parsed.query))
        elif path == "/api/firmware/list":
            self._handle_firmware_list()
        elif path == "/api/ble/status":
            self._handle_ble_status()
        elif path.startswith("/firmware/"):
            self._handle_firmware_download(path)
        elif path in ("/", "/index.html"):
            self._serve_ui()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/hotplug":
            self._handle_hotplug()
        elif path == "/api/flash":
            self._handle_flash()
        elif path == "/api/flash/read":
            self._handle_flash_read()
        elif path == "/api/chip/info":
            self._handle_chip_info()
        elif path == "/api/ota":
            self._handle_ota()
        elif path == "/api/serial/write":
            self._handle_serial_write()
        elif path == "/api/bench/reset":
            self._handle_bench_reset()
        elif path == "/api/slot/acquire":
            self._handle_slot_acquire()
        elif path == "/api/slot/renew":
            self._handle_slot_renew()
        elif path == "/api/slot/release":
            self._handle_slot_release()
        elif path == "/api/serial/reset":
            self._handle_serial_reset()
        elif path == "/api/serial/monitor":
            self._handle_serial_monitor()
        elif path == "/api/serial/recover":
            self._handle_serial_recover()
        elif path == "/api/serial/gpio-test":
            self._handle_gpio_test()
        elif path == "/api/serial/release":
            self._handle_serial_release()
        elif path == "/api/enter-portal":
            self._handle_enter_portal()
        elif path == "/api/start":
            self._handle_start()
        elif path == "/api/stop":
            self._handle_stop()
        elif path == "/api/wifi/mode":
            self._handle_wifi_mode_post()
        elif path == "/api/wifi/ap_start":
            self._handle_wifi_ap_start()
        elif path == "/api/wifi/ap_stop":
            self._handle_wifi_ap_stop()
        elif path == "/api/wifi/sta_join":
            self._handle_wifi_sta_join()
        elif path == "/api/wifi/sta_leave":
            self._handle_wifi_sta_leave()
        elif path == "/api/wifi/http":
            self._handle_wifi_http()
        elif path == "/api/wifi/lease_event":
            self._handle_wifi_lease_event()
        elif path == "/api/human-interaction":
            self._handle_human_interaction()
        elif path == "/api/human/done":
            self._handle_human_done()
        elif path == "/api/human/cancel":
            self._handle_human_cancel()
        elif path == "/api/test/update":
            self._handle_test_update()
        elif path == "/api/gpio/set":
            self._handle_gpio_set()
        elif path == "/api/debug/start":
            self._handle_debug_start()
        elif path == "/api/debug/stop":
            self._handle_debug_stop()
        elif path == "/api/siggen/start":
            self._handle_siggen_start()
        elif path == "/api/siggen/stop":
            self._handle_siggen_stop()
        elif path == "/api/siggen/freq":
            self._handle_siggen_freq()
        elif path == "/api/siggen/atten":
            self._handle_siggen_atten()
        elif path == "/api/sdr/capture":
            self._handle_sdr_capture()
        elif path == "/api/sdr/analyze":
            self._handle_sdr_analyze()
        elif path == "/api/sdr/power":
            self._handle_sdr_power()
        elif path == "/api/sdr/acquire":
            self._handle_sdr_acquire()
        elif path == "/api/sdr/live/start":
            self._handle_sdr_live_start()
        elif path == "/api/sdr/live/stop":
            self._handle_sdr_live_stop()
        elif path == "/api/sdr/reset":
            self._handle_sdr_reset()
        elif path == "/api/sdr/log/start":
            if _sdr is None:
                self._sdr_unavailable()
            else:
                log_activity("sdr log ● recording for AI analysis", "step")
                self._send_json({"ok": True, **_sdr.start_log()})
        elif path == "/api/sdr/log/stop":
            if _sdr is None:
                self._sdr_unavailable()
            else:
                res = _sdr.stop_log()
                log_activity(f"sdr log ■ stopped ({res.get('count')} lines)", "ok")
                self._send_json({"ok": True, **res})
        elif path == "/api/sdr/stop":
            self._handle_sdr_stop()
        elif path == "/api/mqtt/start":
            try:
                self._send_json({"ok": True, **mqtt_controller.start()})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 500)
        elif path == "/api/mqtt/stop":
            mqtt_controller.stop()
            self._send_json({"ok": True})
        elif path == "/api/firmware/upload":
            self._handle_firmware_upload()
        elif path == "/api/flash":
            self._handle_flash()
        elif path == "/api/chip/info":
            self._handle_chip_info()
        elif path == "/api/ble/scan":
            self._handle_ble_scan()
        elif path == "/api/ble/connect":
            self._handle_ble_connect()
        elif path == "/api/ble/disconnect":
            self._handle_ble_disconnect()
        elif path == "/api/ble/write":
            self._handle_ble_write()
        elif path == "/api/ble/read":
            self._handle_ble_read()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path == "/api/udplog":
            _udp_log.clear()
            self._send_json({"ok": True})
        elif path == "/api/firmware/delete":
            self._handle_firmware_delete()
        else:
            self._send_json({"error": "not found"}, 404)

    # -- handlers --

    def _handle_get_devices(self):
        _refresh_host_ip()
        infos = []
        for slot in slots.values():
            _refresh_slot_health(slot)
            infos.append(_slot_info(slot))
        # Sort by label so SLOT1-4 always appear in order
        infos.sort(key=lambda s: s.get("label", ""))
        # Appendix D promises `ok` on every response; these two GETs are the
        # only endpoints that ever omitted it, which made a generic client
        # treat a perfectly good answer as a failure.
        self._send_json({"ok": True, "slots": infos,
                         "host_ip": host_ip, "hostname": hostname})

    def _handle_get_info(self):
        _refresh_host_ip()
        self._send_json({
            "ok": True,
            "host_ip": host_ip,
            "hostname": hostname,
            "slots_configured": sum(1 for s in slots.values() if s["tcp_port"] is not None),
            "slots_running": sum(1 for s in slots.values() if s["running"]),
        })

    def _handle_hotplug(self):
        global seq_counter

        body = self._read_json()
        if body is None:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return

        action = body.get("action")
        devnode = body.get("devnode")
        id_path = body.get("id_path", "")
        devpath = body.get("devpath", "")

        if not action:
            self._send_json({"ok": False, "error": "missing action"}, 400)
            return

        slot_key = id_path if id_path else devpath
        if not slot_key:
            self._send_json({"ok": False, "error": "missing id_path and devpath"}, 400)
            return

        # Look up slot — match against fixed slot prefixes first
        fixed = _find_fixed_slot_for_key(slot_key)
        if fixed:
            slot = fixed
        elif slot_key not in slots:
            slots[slot_key] = _make_dynamic_slot(slot_key)
            slot = slots[slot_key]
        else:
            slot = slots[slot_key]
        lock = slot["_lock"]

        # Update event bookkeeping (always, even for unknown slots)
        seq_counter += 1
        slot["seq"] = seq_counter
        slot["last_action"] = action
        slot["last_event_ts"] = datetime.now(timezone.utc).isoformat()

        label = slot["label"] or slot_key[-20:]
        configured = slot["tcp_port"] is not None

        # -- Early exit: if recovery is in progress, ignore all events --
        # The unbind/rebind cycle generates synthetic udev events; don't let
        # them interfere with recovery state.
        if slot["_recovering"]:
            print(
                f"[portal] hotplug: {action} {label} ignored (recovery in progress)",
                flush=True,
            )
            self._send_json({
                "ok": True, "slot_key": slot_key, "seq": seq_counter,
                "accepted": False, "flapping": True, "recovering": True,
            })
            return

        # -- Debugging: suppress ADD events (USB re-enum during JTAG reset)
        # but always process REMOVE (physical unplug must be handled)
        _dbg_label = slot.get("label")
        if action == "add" and (slot["state"] == STATE_DEBUGGING
                or (_dbg_label and debug_controller.is_debugging(_dbg_label))):
            print(
                f"[portal] hotplug: add {label} suppressed (debugging)",
                flush=True,
            )
            if devnode:
                slot["devnode"] = devnode
            self._send_json({
                "ok": True, "slot_key": slot_key, "seq": seq_counter,
                "accepted": False, "debugging": True,
            })
            return

        # -- Flap detection --
        now = time.time()
        slot["_event_times"].append(now)
        # Prune events older than window
        slot["_event_times"] = [t for t in slot["_event_times"] if now - t < FLAP_WINDOW_S]

        # Recovery: if already flapping but not recovering, check if quiet long enough
        if slot["flapping"] and not slot["_recovering"]:
            _cleared = False
            if len(slot["_event_times"]) < 2:
                _cleared = True
                print(f'[portal] {label}: USB flapping cleared (events aged out)', flush=True)
            else:
                gap = slot["_event_times"][-1] - slot["_event_times"][-2]
                if gap >= FLAP_COOLDOWN_S:
                    _cleared = True
                    print(f'[portal] {label}: USB flapping cleared (quiet for {gap:.0f}s)', flush=True)
            if _cleared:
                slot["flapping"] = False
                slot["last_error"] = None
                slot["state"] = STATE_IDLE if slot["present"] else STATE_ABSENT
                # Restart proxy if device is present but proxy died
                if slot["present"] and not slot["running"]:
                    def _bg_restart(s=slot, lk=lock):
                        with lk:
                            start_proxy(s)
                    threading.Thread(target=_bg_restart, daemon=True).start()

        # Detect new flapping → active recovery
        if not slot["flapping"] and len(slot["_event_times"]) >= FLAP_THRESHOLD:
            slot["flapping"] = True
            slot["state"] = STATE_FLAPPING
            slot["last_error"] = "USB flapping detected — starting recovery"
            print(f'[portal] {label}: USB flapping detected ({len(slot["_event_times"])} events in {FLAP_WINDOW_S}s) — starting recovery', flush=True)
            _start_flap_recovery(slot)

        if action == "add":
            slot["_devnodes"][slot_key] = devnode
            slot["present"] = True
            # Adopt the new node when the recorded one has gone. A native-USB
            # ESP32 re-enumerates on every reset and every flash, and the
            # kernel need not hand back the same ttyACM*. If the matching
            # 'remove' is missed or lands after this 'add' — which it does
            # when the device reappears in milliseconds — the slot kept its
            # old devnode, went on reporting `present`, and every operation
            # on it failed with "Could not open /dev/ttyACM0 … No such file"
            # against hardware that was sitting right there under a different
            # name. Only a portal restart cleared it.
            if not slot["devnode"] or not os.path.exists(slot["devnode"]):
                slot["devnode"] = devnode
            if slot["tcp_port"]:
                slot["url"] = f"rfc2217://{host_ip}:{slot['tcp_port']}"
            if not slot["flapping"]:
                slot["state"] = STATE_IDLE

            if slot["flapping"]:
                pass  # Recovery handles everything
            else:
                # Start proxy in a background thread so we don't block the
                # HTTP response for the settle + port-listen check.
                def _bg_start(s=slot, lk=lock, dn=devnode):
                    # Native USB (ttyACM): delay before opening port so the
                    # chip boots past the download-mode-sensitive phase.
                    if dn and "ttyACM" in dn:
                        time.sleep(NATIVE_USB_BOOT_DELAY_S)
                    with lk:
                        if s["flapping"] or s["_recovering"]:
                            return  # Recovery in progress
                        # If proxy is already running on a different devnode,
                        # don't restart — this hotplug is for the JTAG interface
                        # which doesn't need a proxy.
                        if (s["running"] and s["pid"]
                                and s["devnode"] and s["devnode"] != dn):
                            return
                        # Stop existing proxy first if still running
                        if s["running"] and s["pid"]:
                            stop_proxy(s)
                        # Compute URL
                        if s["tcp_port"]:
                            s["url"] = f"rfc2217://{host_ip}:{s['tcp_port']}"
                        start_proxy(s)
                        if s["flapping"]:
                            s["last_error"] = "USB flapping detected \u2014 device is connect/disconnect cycling"
                        # Capture values for auto-debug (run outside lock)
                        _should_debug = (
                            s["running"] and s.get("gdb_port")
                            and s.get("label")
                            and not s["flapping"]
                            and not _is_probe_slot(s)
                            and not debug_controller.is_debugging(s["label"]))
                        _gdb = s.get("gdb_port")
                        _tel = s.get("openocd_telnet_port")
                        _lbl = s.get("label")
                        _usb = list(s.get("_usb_devices", []))
                    # Auto-start OpenOCD (outside lock — detection is slow)
                    if _should_debug:
                        time.sleep(3)
                        try:
                            psm = _build_probe_slot_map()
                            info = debug_controller.detect_slot_jtag(
                                _lbl, _usb, psm,
                                usb_prefix=s.get("_usb_prefix"))
                            chip = info["chip"]
                            jtag_slot = info["jtag_slot"]
                            if chip and jtag_slot:
                                # Ensure proxy is running (brief lock)
                                with lk:
                                    if (s["present"] and not s["flapping"]):
                                        if not s["running"] or not _is_process_alive(s.get("pid")):
                                            start_proxy(s)
                                            time.sleep(1)
                                # Identity only — the chip is released. See
                                # the boot path above: a session left running
                                # on a slot nobody is debugging halts the
                                # target and blocks every other use of it.
                                s["_auto_debug_chip"] = chip
                                s["_jtag_slot"] = jtag_slot
                                log_activity(
                                    f"Detected {_lbl}: {chip} "
                                    f"(JTAG:{jtag_slot}) — chip released, "
                                    f"no debug session held", "ok")
                            else:
                                with lk:
                                    # Store chip even without JTAG
                                    if chip:
                                        s["_auto_debug_chip"] = chip
                                    if (s["present"] and not s["flapping"]
                                            and (not s["running"]
                                                 or not _is_process_alive(
                                                     s.get("pid")))):
                                        start_proxy(s)
                        except Exception as e:
                            print(f"[portal] auto-debug failed: {e}",
                                  flush=True)
                            with lk:
                                if (s["present"] and not s["flapping"]
                                        and (not s["running"]
                                             or not _is_process_alive(
                                                 s.get("pid")))):
                                    start_proxy(s)
                threading.Thread(target=_bg_start, daemon=True).start()

        elif action == "remove":
            # Remove this devnode from the slot's set
            slot["_devnodes"].pop(slot_key, None)
            # Only go absent if no devnodes remain
            if not slot["_devnodes"]:
                slot["present"] = False
                slot["devnode"] = None
                slot["_auto_debug_chip"] = None
                slot["_jtag_slot"] = None
                if not slot["flapping"]:
                    slot["state"] = STATE_ABSENT
            else:
                # Other devnodes still active — update primary if needed
                if slot["devnode"] == devnode:
                    slot["devnode"] = next(iter(slot["_devnodes"].values()))
            # Stop debug session if active (preserves _auto_debug_chip
            # so debug auto-restarts on next hotplug add after flash)
            _rm_label = slot.get("label")
            if _rm_label and debug_controller.is_debugging(_rm_label):
                debug_controller.stop(_rm_label)
            # Stop proxy if no devnodes left
            if slot["running"] and not slot["_devnodes"]:
                def _bg_stop(s=slot, lk=lock):
                    with lk:
                        stop_proxy(s)
                threading.Thread(target=_bg_stop, daemon=True).start()

        # Rescan USB devices for all slots (a change on one port can
        # affect what's visible — e.g. sub-hub appearing/disappearing)
        _refresh_all_usb_devices()

        log_activity(
            f"USB {action}: {label} ({devnode or '?'})",
            "ok" if action == "add" else "info",
        )
        print(
            f"[portal] hotplug: {action} slot_key={slot_key} "
            f"devnode={devnode} seq={seq_counter}",
            flush=True,
        )

        self._send_json({
            "ok": True,
            "slot_key": slot_key,
            "seq": seq_counter,
            "accepted": configured,
            "flapping": slot["flapping"],
            "recovering": slot["_recovering"],
        })

    def _handle_start(self):
        body = self._read_json()
        if body is None:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return

        slot = _resolve_slot(body)
        if slot is None:
            self._send_json(
                {"ok": False, "error": "missing or unknown 'slot' / 'slot_key'"}, 400)
            return

        # Optional: a client that stopped a slot should not have to have
        # remembered its devnode to start it again.
        devnode = body.get("devnode") or slot.get("devnode")
        if not devnode:
            self._send_json(
                {"ok": False,
                 "error": f"{slot['label']}: no devnode known — pass 'devnode'"}, 400)
            return

        slot_key = slot["slot_key"]
        with slot["_lock"]:
            if slot["running"] and slot["pid"]:
                stop_proxy(slot)
            slot["devnode"] = devnode
            slot["present"] = True
            ok = start_proxy(slot)
            # start_proxy sets STATE_IDLE on success; ensure idle on failure too
            if not ok and slot["state"] not in (STATE_IDLE, STATE_FLAPPING):
                slot["state"] = STATE_IDLE
        self._send_json({"ok": ok, "slot_key": slot_key, "running": slot["running"]})

    def _handle_stop(self):
        body = self._read_json()
        if body is None:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return

        slot = _resolve_slot(body)
        if slot is None:
            self._send_json(
                {"ok": False, "error": "missing or unknown 'slot' / 'slot_key'"}, 400)
            return
        slot_key = slot["slot_key"]
        with slot["_lock"]:
            stop_proxy(slot)
            slot["state"] = STATE_IDLE if slot["present"] else STATE_ABSENT
        self._send_json({"ok": True, "slot_key": slot_key, "running": False})

    # -- WiFi handlers --

    def _handle_wifi_ping(self):
        self._send_json({"ok": True, **wifi_controller.ping()})

    def _handle_wifi_mode_get(self):
        self._send_json({"ok": True, **wifi_controller.get_mode()})

    def _handle_wifi_mode_post(self):
        body = self._read_json()
        if body is None:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return
        mode = body.get("mode")
        if mode not in ("wifi-testing", "serial-interface"):
            self._send_json({"ok": False, "error": "mode must be 'wifi-testing' or 'serial-interface'"}, 400)
            return
        ssid = body.get("ssid", "")
        password = body.get("pass", "")
        try:
            result = wifi_controller.set_mode(mode, ssid, password)
            self._send_json({"ok": True, **result})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)})

    def _handle_wifi_ap_start(self):
        body = self._read_json()
        if body is None:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return
        ssid = body.get("ssid")
        if not ssid:
            self._send_json({"ok": False, "error": "missing ssid"}, 400)
            return
        password = body.get("pass", "")
        channel = body.get("channel", 6)
        internet = bool(body.get("internet", False))
        try:
            result = wifi_controller.ap_start(ssid, password, channel,
                                              internet=internet)
            self._send_json({"ok": True, **result})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)})

    def _handle_wifi_ap_stop(self):
        try:
            wifi_controller.ap_stop()
            self._send_json({"ok": True})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)})

    def _handle_wifi_ap_status(self):
        self._send_json({"ok": True, **wifi_controller.ap_status()})

    def _handle_wifi_sta_join(self):
        body = self._read_json()
        if body is None:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return
        ssid = body.get("ssid")
        if not ssid:
            self._send_json({"ok": False, "error": "missing ssid"}, 400)
            return
        password = body.get("pass", "")
        timeout = body.get("timeout", 15)
        log_activity(f"WiFi STA joining '{ssid}'...", "step")
        try:
            result = wifi_controller.sta_join(ssid, password, timeout)
            log_activity(f"WiFi STA connected to '{ssid}' — IP: {result.get('ip', '?')}", "ok")
            self._send_json({"ok": True, **result})
        except Exception as e:
            log_activity(f"WiFi STA join failed: {e}", "error")
            self._send_json({"ok": False, "error": str(e)})

    def _handle_wifi_sta_leave(self):
        log_activity("WiFi STA disconnecting", "step")
        try:
            wifi_controller.sta_leave()
            log_activity("WiFi STA disconnected", "ok")
            self._send_json({"ok": True})
        except Exception as e:
            log_activity(f"WiFi STA leave failed: {e}", "error")
            self._send_json({"ok": False, "error": str(e)})

    def _handle_wifi_http(self):
        body = self._read_json()
        if body is None:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return
        method = body.get("method", "GET")
        url = body.get("url")
        if not url:
            self._send_json({"ok": False, "error": "missing url"}, 400)
            return
        headers = body.get("headers")
        req_body = body.get("body")  # base64 encoded
        timeout = body.get("timeout", 10)
        log_activity(f"HTTP relay {method} {url}", "step")
        try:
            result = wifi_controller.http_relay(method, url, headers, req_body, timeout)
            log_activity(f"HTTP relay {method} {url} — {result.get('status', '?')}", "ok")
            self._send_json({"ok": True, **result})
        except Exception as e:
            log_activity(f"HTTP relay failed: {e}", "error")
            self._send_json({"ok": False, "error": str(e)})

    def _handle_wifi_scan(self):
        log_activity("WiFi scanning...", "step")
        try:
            result = wifi_controller.scan()
            if "error" in result:
                # A radio that could not be asked is not an empty sky.
                log_activity(f"WiFi scan failed: {result['error']}", "error")
                self._send_json({"ok": False, **result}, 503)
                return
            n = len(result.get("networks", []))
            log_activity(f"WiFi scan found {n} networks", "ok")
            self._send_json({"ok": True, **result})
        except Exception as e:
            log_activity(f"WiFi scan failed: {e}", "error")
            self._send_json({"ok": False, "error": str(e)})

    def _handle_wifi_events(self, qs):
        timeout = 0
        if "timeout" in qs:
            try:
                timeout = float(qs["timeout"][0])
            except (ValueError, IndexError):
                pass
        events = wifi_controller.get_events(timeout)
        self._send_json({"ok": True, "events": events})

    def _handle_wifi_lease_event(self):
        body = self._read_json()
        if body is None:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return
        action = body.get("action", "")
        mac = body.get("mac", "")
        ip = body.get("ip", "")
        hostname = body.get("hostname", "")
        if not action or not mac:
            self._send_json({"ok": False, "error": "missing action or mac"}, 400)
            return
        wifi_controller.handle_lease_event(action, mac, ip, hostname)
        self._send_json({"ok": True})

    # -- serial services (FR-008, FR-009) --

    def _handle_serial_write(self):
        body = self._read_json() or {}
        slot_label = body.get("slot")
        slot = _find_slot_by_label(slot_label) if slot_label else None
        if not slot:
            self._send_json({"ok": False, "error": f"slot '{slot_label}' not found"}, 404)
            return
        if "hex" in body:
            try:
                data = bytes.fromhex(body["hex"])
            except ValueError as e:
                self._send_json({"ok": False, "error": f"bad hex: {e}"}, 400)
                return
        elif "text" in body:
            # `newline` defaults true because a console command without one is
            # never executed, which reads as "the device ignored me".
            text = body["text"]
            if body.get("newline", True):
                text += "\r\n"
            data = text.encode("utf-8")
        else:
            self._send_json({"ok": False, "error": "need 'text' or 'hex'"}, 400)
            return
        self._send_json(serial_write(slot, data))

    def _handle_bench_reset(self):
        self._send_json(bench_reset(), 200)

    def _handle_slot_acquire(self):
        body = self._read_json() or {}
        slot_label = body.get("slot")
        slot = _find_slot_by_label(slot_label) if slot_label else None
        if not slot:
            self._send_json({"ok": False, "error": f"slot '{slot_label}' not found"}, 404)
            return
        result, status = slot_acquire(slot, body.get("mode", ""),
                                      body.get("owner", ""),
                                      body.get("ttl", GRANT_TTL_DEFAULT))
        self._send_json(result, status)

    def _handle_slot_renew(self):
        body = self._read_json() or {}
        result, status = slot_renew(body.get("token", ""))
        self._send_json(result, status)

    def _handle_slot_release(self):
        body = self._read_json() or {}
        result, status = slot_release(body.get("token", ""))
        self._send_json(result, status)

    def _handle_slot_mode(self, qs):
        slot_label = (qs.get("slot") or [None])[0]
        slot = _find_slot_by_label(slot_label) if slot_label else None
        if not slot:
            self._send_json({"ok": False, "error": f"slot '{slot_label}' not found"}, 404)
            return
        self._send_json(slot_mode(slot), 200)

    def _handle_serial_reset(self):
        body = self._read_json() or {}
        slot_label = body.get("slot")
        if not slot_label:
            self._send_json({"ok": False, "error": "missing 'slot' field"}, 400)
            return
        slot = _find_slot_by_label(slot_label)
        if not slot:
            self._send_json({"ok": False, "error": f"slot '{slot_label}' not found"})
            return

        # JTAG reset if debug session is active (no USB re-enumeration)
        effective_label = slot.get("label") or slot.get("slot_key", "")[-20:]
        if debug_controller.is_debugging(effective_label):
            log_activity(f"serial.reset({slot_label}) via JTAG", "step")
            result = jtag_reset_capturing(slot, effective_label)
            if result["ok"]:
                log_activity(f"serial.reset({slot_label}) — JTAG reset done", "ok")
            else:
                log_activity(f"serial.reset({slot_label}) — JTAG failed, falling back to DTR/RTS", "info")
                result = serial_reset(slot)
        else:
            log_activity(f"serial.reset({slot_label})", "step")
            result = serial_reset(slot)

        if result["ok"]:
            log_activity(f"serial.reset({slot_label}) — done", "ok")
        else:
            log_activity(f"serial.reset({slot_label}) — {result.get('error', 'failed')}", "error")
        self._send_json(result)


    def _parse_multipart(self):
        """Parse a multipart/form-data body. Returns (fields, files) where fields
        is {name: text} and files is [{name, filename, content(bytes)}], or
        (None, None) if the body is invalid (error response already sent)."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"ok": False, "error": "expected multipart/form-data"}, 400)
            return None, None
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
        if not boundary:
            self._send_json({"ok": False, "error": "missing boundary"}, 400)
            return None, None
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return None, None
        body = self.rfile.read(length)
        fields, files = {}, []
        for part in body.split(b"--" + boundary.encode()):
            part = part.strip()
            if not part or part == b"--":
                continue
            if b"\r\n\r\n" in part:
                header_section, content = part.split(b"\r\n\r\n", 1)
            elif b"\n\n" in part:
                header_section, content = part.split(b"\n\n", 1)
            else:
                continue
            if content.endswith(b"\r\n"):
                content = content[:-2]
            headers_text = header_section.decode("utf-8", errors="replace")
            name = filename = None
            for token in headers_text.replace("\r", "").split("\n"):
                if "content-disposition" in token.lower():
                    for kv in token.split(";"):
                        kv = kv.strip()
                        if kv.startswith('name="'):
                            name = kv[6:].rstrip('"')
                        elif kv.startswith('filename="'):
                            filename = kv[10:].rstrip('"')
            if name is None:
                continue
            if filename is not None:
                files.append({"name": name, "filename": filename, "content": content})
            else:
                fields[name] = content.decode("utf-8", errors="replace").strip()
        return fields, files

    def _handle_flash(self):
        # multipart/form-data: fields slot [chip] [baud] [erase]; one file part
        # per flash image, the part name being its hex offset (e.g. name="0x10000").
        fields, files = self._parse_multipart()
        if fields is None:
            return
        slot_label = fields.get("slot")
        if not slot_label:
            self._send_json({"ok": False, "error": "missing 'slot' field"}, 400)
            return
        slot = _find_slot_by_label(slot_label)
        if not slot:
            self._send_json({"ok": False, "error": f"slot '{slot_label}' not found"}, 404)
            return
        if not files:
            self._send_json({"ok": False, "error": "no firmware image parts"}, 400)
            return
        images = []
        for f in files:
            off = f["name"]
            if not off.lower().startswith("0x"):
                self._send_json({"ok": False, "error": f"image part name '{off}' is not a hex offset"}, 400)
                return
            images.append({"offset": off, "data": f["content"]})
        images.sort(key=lambda im: int(im["offset"], 16))
        chip = fields.get("chip", "esp32")
        try:
            baud = int(fields.get("baud", 460800))
        except ValueError:
            baud = 460800
        erase = fields.get("erase", "").lower() in ("1", "true", "yes")
        result = flash_slot(slot, images, chip=chip, baud=baud, erase=erase)
        self._send_json(result)


    def _handle_serial_monitor(self):
        body = self._read_json() or {}
        slot_label = body.get("slot")
        if not slot_label:
            self._send_json({"ok": False, "error": "missing 'slot' field"}, 400)
            return
        slot = _find_slot_by_label(slot_label)
        if not slot:
            self._send_json({"ok": False, "error": f"slot '{slot_label}' not found"})
            return
        pattern = body.get("pattern")
        timeout = float(body.get("timeout", 10))
        log_activity(f"serial.monitor({slot_label}, pattern={pattern!r}, timeout={timeout})", "step")
        result = serial_monitor(slot, pattern, timeout)
        if result["ok"]:
            if result.get("matched"):
                log_activity(f"serial.monitor({slot_label}) — matched: {result['line']}", "ok")
            else:
                log_activity(f"serial.monitor({slot_label}) — timeout, no match", "info")
        else:
            log_activity(f"serial.monitor({slot_label}) — {result.get('error', 'failed')}", "error")
        self._send_json(result)

    def _handle_serial_output(self, qs):
        """GET /api/serial/output?slot=SLOT1&lines=50&since=0 — passive buffer read."""
        slot_label = qs.get("slot", [""])[0]
        if not slot_label:
            self._send_json({"ok": False, "error": "missing 'slot' param"}, 400)
            return
        slot = _find_slot_by_label(slot_label)
        if not slot:
            self._send_json({"ok": False, "error": f"slot '{slot_label}' not found"})
            return
        max_lines = int(qs.get("lines", ["50"])[0])
        since = float(qs.get("since", ["0"])[0])

        # `lines=N` means the most recent N, which is what every caller means
        # by it. Taking them from the front instead returned the *oldest* N of
        # a 1000-line ring: once a slot had been up a few minutes the answer
        # was always ancient history, and the newest line's timestamp aged
        # steadily — which reads exactly like a recorder that has died. It
        # survived because a buffer shorter than `lines` is returned whole, so
        # the read right after a flash — when tests do most of their reading —
        # was correct, and only a long-running slot lied.
        fresh = [e for e in slot["_serial_buf"] if e["ts"] > since]
        self._send_json({"ok": True, "lines": fresh[-max_lines:]})

    # -- recovery handlers --

    def _handle_gpio_test(self):
        """POST /api/serial/gpio-test {"slot": "SLOT1"} — measure BOOT/EN.

        Synchronous: it takes about fifteen seconds and the answer is the
        whole point, so returning "started" would reproduce the defect this
        exists to remove.
        """
        body = self._read_json() or {}
        slot_label = body.get("slot")
        if not slot_label:
            self._send_json({"ok": False, "error": "missing 'slot' field"}, 400)
            return
        slot = _find_slot_by_label(slot_label)
        if not slot:
            self._send_json({"ok": False,
                             "error": f"slot '{slot_label}' not found"})
            return
        if not slot.get("present"):
            self._send_json({"ok": False,
                             "error": f"{slot_label}: no device present"})
            return
        self._send_json(test_gpio_wiring(slot))

    def _handle_serial_recover(self):
        """POST /api/serial/recover {"slot": "SLOT1"} — manual recovery trigger."""
        body = self._read_json() or {}
        slot_label = body.get("slot")
        if not slot_label:
            self._send_json({"ok": False, "error": "missing 'slot' field"}, 400)
            return
        slot = _find_slot_by_label(slot_label)
        if not slot:
            self._send_json({"ok": False, "error": f"slot '{slot_label}' not found"})
            return
        # Pre-flight: recovery unbinds and rebinds a USB device, so if we
        # cannot name that device there is nothing to attempt. Refusing here
        # is the difference between "we tried and it did not work" and "we
        # never tried" — the endpoint used to report the second as the first.
        if not _usb_device_for_slot(slot):
            self._send_json({
                "ok": False,
                "error": (f"cannot determine the USB device for {slot_label} "
                          f"(slot_key={slot.get('slot_key')!r}, "
                          f"devnode={slot.get('devnode')!r}) — nothing to "
                          f"unbind, so recovery cannot start"),
            })
            return

        # Reset retry counter for fresh attempt
        slot["_recover_retries"] = 0
        slot["flapping"] = True
        log_activity(f"serial.recover({slot_label}) — manual recovery triggered", "step")
        _start_flap_recovery(slot)
        # `ok` here means the attempt was launched, not that it worked — the
        # cycle takes a cooldown plus a probe and cannot be answered inside
        # this request. Say so, and name where the answer appears, so a
        # caller does not read this as "the device is now flashable".
        self._send_json({
            "ok": True,
            "started": True,
            "message": (f"recovery started for {slot_label} — this reports "
                        f"the attempt, not the outcome"),
            "outcome_at": "GET /api/devices",
            "outcome_field": ("state becomes 'download_mode' on success, or "
                              "'flapping' with last_error explaining why not"),
        })

    def _handle_serial_release(self):
        """POST /api/serial/release {"slot": "SLOT1"} — release GPIO after flashing."""
        body = self._read_json() or {}
        slot_label = body.get("slot")
        if not slot_label:
            self._send_json({"ok": False, "error": "missing 'slot' field"}, 400)
            return
        slot = _find_slot_by_label(slot_label)
        if not slot:
            self._send_json({"ok": False, "error": f"slot '{slot_label}' not found"})
            return
        log_activity(f"serial.release({slot_label})", "step")
        result = _release_slot_gpio(slot)
        if result["ok"]:
            log_activity(f"serial.release({slot_label}) — done", "ok")
        else:
            log_activity(f"serial.release({slot_label}) — {result.get('error', 'failed')}", "error")
        self._send_json(result)

    # -- activity log & enter-portal --

    def _handle_get_log(self, qs):
        since = qs.get("since", [None])[0]
        entries = list(activity_log)
        if since:
            entries = [e for e in entries if e["ts"] > since]
        self._send_json({"ok": True, "entries": entries})

    def _handle_enter_portal(self):
        global _enter_portal_running
        body = self._read_json() or {}
        portal_ssid = body.get("portal_ssid", "iOS-Keyboard-Setup")
        portal_ip = body.get("portal_ip", "192.168.4.1")
        wifi_ssid = body.get("ssid", "")
        wifi_password = body.get("password", "")
        # Portal form contract (defaults = WiFi-Tester DUT; override for
        # WiFiManager: save_path="/wifisave", field_ssid="s", field_password="p").
        save_path = body.get("save_path", "/connect")
        field_ssid = body.get("field_ssid", "ssid")
        field_password = body.get("field_password", "password")
        extra = body.get("extra")
        method = body.get("method", "POST")
        internet = bool(body.get("internet", False))

        if not wifi_ssid:
            self._send_json({"ok": False, "error": "ssid is required"})
            return

        if _enter_portal_running:
            self._send_json({"ok": False, "error": "enter-portal already running"})
            return

        _enter_portal_running = True
        log_activity(f"Enter-portal: joining '{portal_ssid}', provisioning with '{wifi_ssid}'", "step")

        def _bg_enter_portal():
            global _enter_portal_running
            try:
                _do_enter_portal(portal_ssid, wifi_ssid, wifi_password, portal_ip,
                                 save_path=save_path, field_ssid=field_ssid,
                                 field_password=field_password, extra=extra,
                                 method=method, internet=internet)
            except Exception as e:
                log_activity(f"Enter-portal error: {e}", "error")
            finally:
                _enter_portal_running = False

        threading.Thread(
            target=_bg_enter_portal,
            daemon=True,
        ).start()

        self._send_json({"ok": True, "message": "enter-portal started in background"})

    # -- human interaction handlers (event-driven, blocking) --

    def _handle_human_interaction(self):
        """Blocking endpoint — stays open until human clicks Done/Cancel or timeout."""
        global _human_event, _human_confirmed, _human_message

        body = self._read_json()
        if not body or not body.get("message"):
            self._send_json({"ok": False, "error": "missing message"}, 400)
            return
        timeout = float(body.get("timeout", 120))

        with _human_lock:
            if _human_event is not None:
                self._send_json({"ok": False, "error": "another request pending"}, 409)
                return
            _human_event = threading.Event()
            _human_confirmed = False
            _human_message = body["message"]

        log_activity(f"Human interaction: {body['message']}", "step")

        # Block here until Done/Cancel or timeout
        responded = _human_event.wait(timeout=timeout)

        with _human_lock:
            confirmed = _human_confirmed
            msg = _human_message
            _human_event = None
            _human_message = None

        if responded:
            cat = "ok" if confirmed else "info"
            log_activity(f"Human {'confirmed' if confirmed else 'cancelled'}: {msg}", cat)
            self._send_json({"ok": True, "confirmed": confirmed})
        else:
            log_activity(f"Human interaction timed out: {msg}", "error")
            self._send_json({"ok": True, "confirmed": False, "timeout": True})

    def _handle_human_status(self):
        """UI polls this to show/hide the modal."""
        with _human_lock:
            if _human_event is not None and not _human_event.is_set():
                self._send_json({"ok": True, "pending": True, "message": _human_message})
            else:
                self._send_json({"ok": True, "pending": False, "message": ""})

    def _handle_human_done(self):
        """UI Done button — wakes the blocking handler with confirmed=True."""
        global _human_confirmed
        with _human_lock:
            if _human_event is None or _human_event.is_set():
                self._send_json({"ok": False, "error": "no pending request"})
                return
            _human_confirmed = True
            _human_event.set()
        self._send_json({"ok": True})

    def _handle_human_cancel(self):
        """UI Cancel button — wakes the blocking handler with confirmed=False."""
        global _human_confirmed
        with _human_lock:
            if _human_event is None or _human_event.is_set():
                self._send_json({"ok": False, "error": "no pending request"})
                return
            _human_confirmed = False
            _human_event.set()
        self._send_json({"ok": True})

    # -- test progress handlers --

    def _handle_test_progress(self):
        """GET /api/test/progress — UI polls this for test session state."""
        with _test_lock:
            if _test_session is None:
                self._send_json({"ok": True, "active": False})
            else:
                self._send_json({"ok": True, "active": True, **_test_session})

    def _handle_test_update(self):
        """POST /api/test/update — test scripts push progress updates."""
        global _test_session

        body = self._read_json()
        if not body:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return

        with _test_lock:
            # End session
            if body.get("end"):
                _test_session = None
                self._send_json({"ok": True})
                return

            # Start session (spec field present)
            if "spec" in body:
                _test_session = {
                    "spec": body["spec"],
                    "phase": body.get("phase", ""),
                    "total": body.get("total", 0),
                    "completed": [],
                    "current": None,
                }

            if _test_session is None:
                self._send_json({"ok": False, "error": "no active session"}, 400)
                return

            # Update phase if provided
            if "phase" in body and "spec" not in body:
                _test_session["phase"] = body["phase"]

            # Update total if provided
            if "total" in body and "spec" not in body:
                _test_session["total"] = body["total"]

            # Update current test
            if "current" in body:
                _test_session["current"] = body["current"]

            # Record a result
            if "result" in body:
                _test_session["completed"].append(body["result"])
                _test_session["current"] = None

        self._send_json({"ok": True})

    # -- GPIO handlers --

    def _handle_gpio_set(self):
        body = self._read_json()
        if not body:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return
        pin = body.get("pin")
        value = body.get("value")
        if pin is None or value is None:
            self._send_json({"ok": False, "error": "missing pin or value"}, 400)
            return
        if not isinstance(pin, int) or pin not in GPIO_ALLOWED:
            self._send_json({"ok": False, "error": f"pin {pin} not in allowed set"}, 400)
            return
        if value not in (0, 1, "z"):
            self._send_json({"ok": False, "error": "value must be 0, 1, or 'z'"}, 400)
            return
        try:
            _gpio_set(pin, value)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)})
            return
        self._send_json({"ok": True, "pin": pin, "value": value})

    def _handle_gpio_status(self):
        pins = {}
        with _gpio_lock:
            for pin, req in _gpio_requests.items():
                try:
                    val = req.get_value(pin)
                    # Track direction from our own state
                    direction = _gpio_directions.get(pin, "unknown")
                    pins[str(pin)] = {"direction": direction, "value": val.value}
                except Exception:
                    pins[str(pin)] = {"direction": "unknown", "value": None}
        self._send_json({"ok": True, "pins": pins})

    # -- UDP log handlers --

    def _handle_get_udplog(self, qs):
        since = float(qs.get("since", ["0"])[0])
        source = qs.get("source", [""])[0]
        limit = int(qs.get("limit", ["200"])[0])
        lines = []
        for entry in _udp_log:
            if entry["ts"] <= since:
                continue
            if source and entry["source"] != source:
                continue
            lines.append(entry)
            if len(lines) >= limit:
                break
        self._send_json({"ok": True, "lines": lines})

    # -- firmware handlers --

    def _handle_firmware_list(self):
        files = []
        if os.path.isdir(FIRMWARE_DIR):
            for project in sorted(os.listdir(FIRMWARE_DIR)):
                proj_dir = os.path.join(FIRMWARE_DIR, project)
                if not os.path.isdir(proj_dir):
                    continue
                for fname in sorted(os.listdir(proj_dir)):
                    fpath = os.path.join(proj_dir, fname)
                    if not os.path.isfile(fpath):
                        continue
                    stat = os.stat(fpath)
                    files.append({
                        "project": project,
                        "filename": fname,
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    })
        self._send_json({"ok": True, "files": files})

    def _handle_firmware_download(self, path):
        # path = /firmware/<project>/<filename>
        parts = path.split("/")
        # ["", "firmware", project, filename]
        if len(parts) != 4:
            self._send_json({"error": "invalid path"}, 400)
            return
        project = parts[2]
        filename = parts[3]
        if ".." in project or ".." in filename or "/" in project or "/" in filename:
            self._send_json({"error": "path traversal not allowed"}, 400)
            return
        fpath = os.path.join(FIRMWARE_DIR, project, filename)
        if not os.path.isfile(fpath):
            self._send_json({"error": "not found"}, 404)
            return
        try:
            fsize = os.path.getsize(fpath)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", fsize)
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            with open(fpath, "rb") as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except BrokenPipeError:
            pass

    def _handle_firmware_upload(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"ok": False, "error": "expected multipart/form-data"}, 400)
            return
        # Parse boundary
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
        if not boundary:
            self._send_json({"ok": False, "error": "missing boundary"}, 400)
            return
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return
        body = self.rfile.read(length)
        boundary_bytes = boundary.encode()
        parts_raw = body.split(b"--" + boundary_bytes)
        project = None
        file_data = None
        file_name = None
        for part in parts_raw:
            part = part.strip()
            if not part or part == b"--":
                continue
            if b"\r\n\r\n" in part:
                header_section, content = part.split(b"\r\n\r\n", 1)
            elif b"\n\n" in part:
                header_section, content = part.split(b"\n\n", 1)
            else:
                continue
            headers_text = header_section.decode("utf-8", errors="replace")
            if content.endswith(b"\r\n"):
                content = content[:-2]
            if 'name="project"' in headers_text:
                project = content.decode("utf-8").strip()
            elif 'name="file"' in headers_text:
                file_data = content
                # Extract filename from Content-Disposition
                for line in headers_text.split("\n"):
                    if "filename=" in line:
                        idx = line.index("filename=")
                        file_name = line[idx + 9:].strip().strip('"').strip("'")
        if not project or file_data is None or not file_name:
            self._send_json({"ok": False, "error": "missing project or file"}, 400)
            return
        if ".." in project or "/" in project or ".." in file_name or "/" in file_name:
            self._send_json({"ok": False, "error": "path traversal not allowed"}, 400)
            return
        proj_dir = os.path.join(FIRMWARE_DIR, project)
        os.makedirs(proj_dir, exist_ok=True)
        fpath = os.path.join(proj_dir, file_name)
        with open(fpath, "wb") as f:
            f.write(file_data)
        log_activity(f"firmware.upload({project}/{file_name}, {len(file_data)} bytes)", "ok")
        self._send_json({"ok": True, "project": project, "filename": file_name, "size": len(file_data)})

    def _handle_flash(self):
        """POST /api/flash — multipart upload + esptool flash on a slot.

        Form fields:
          slot       — slot label (e.g. "SLOT3"), required
          chip       — esp32, esp32s3, esp32c3, ... (default "auto")
          baud       — esptool baud (default "921600")
          flash_mode — dio/qio/... (default "dio", overridden by flash_args)
          flash_freq — 40m/80m/... (default "40m", overridden by flash_args)
          flash_size — 4MB/keep/... (default "keep", overridden by flash_args)
          erase      — "1"/"true" to erase whole flash before write (default off)

        Files (one of):
          • A `flash_args` text part (esptool's @flash_args format) plus the
            referenced .bin files (each as its own multipart part, name=
            <basename>, filename=<basename>). This is exactly what
            ESP-IDF emits in build/flash_args.
          • One or more parts named `bin@<offset>` (e.g. "bin@0x10000"),
            content = the binary, written at that offset.
        """
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"ok": False, "error": "expected multipart/form-data"}, 400)
            return
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
        if not boundary:
            self._send_json({"ok": False, "error": "missing boundary"}, 400)
            return
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return
        body = self.rfile.read(length)
        boundary_bytes = boundary.encode()
        parts_raw = body.split(b"--" + boundary_bytes)

        fields: dict[str, str] = {}
        files: dict[str, bytes] = {}
        offset_files: list[tuple[str, str]] = []
        flash_args_text: str | None = None

        for part in parts_raw:
            part = part.strip()
            if not part or part == b"--":
                continue
            if b"\r\n\r\n" in part:
                header_section, content = part.split(b"\r\n\r\n", 1)
            elif b"\n\n" in part:
                header_section, content = part.split(b"\n\n", 1)
            else:
                continue
            headers_text = header_section.decode("utf-8", errors="replace")
            if content.endswith(b"\r\n"):
                content = content[:-2]
            name = None
            filename = None
            for h in headers_text.split("\n"):
                h = h.strip()
                if h.lower().startswith("content-disposition"):
                    for tok in h.split(";"):
                        tok = tok.strip()
                        if tok.startswith("name="):
                            name = tok[5:].strip().strip('"').strip("'")
                        elif tok.startswith("filename="):
                            filename = tok[9:].strip().strip('"').strip("'")
            if name is None:
                continue
            if filename is not None:
                if "/" in filename or ".." in filename:
                    self._send_json({"ok": False, "error": f"path traversal: {filename}"}, 400)
                    return
                if name == "flash_args":
                    flash_args_text = content.decode("utf-8", errors="replace")
                elif name.startswith("bin@"):
                    offset = name[4:]
                    try:
                        int(offset, 0)  # validates 0x... or decimal
                    except ValueError:
                        self._send_json({"ok": False, "error": f"invalid offset in '{name}'"}, 400)
                        return
                    offset_files.append((offset, filename))
                    files[filename] = content
                else:
                    files[filename] = content
            else:
                fields[name] = content.decode("utf-8", errors="replace").strip()

        slot_label = fields.get("slot")
        if not slot_label:
            self._send_json({"ok": False, "error": "missing 'slot' field"}, 400)
            return
        slot = _find_slot_by_label(slot_label)
        if not slot:
            self._send_json({"ok": False, "error": f"slot '{slot_label}' not found"}, 404)
            return
        if not slot.get("present"):
            self._send_json({"ok": False, "error": f"slot '{slot_label}' has no device"}, 400)
            return

        chip = fields.get("chip", "auto")
        baud = fields.get("baud", "921600")
        flash_mode = fields.get("flash_mode", "dio")
        flash_freq = fields.get("flash_freq", "40m")
        flash_size = fields.get("flash_size", "keep")
        erase = fields.get("erase", "").lower() in ("1", "true", "yes", "on")

        # Resolve offset/file pairs from flash_args (if any) and bin@... parts.
        flash_pairs: list[tuple[str, str]] = []
        if flash_args_text:
            for line in flash_args_text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                tokens = line.split()
                if tokens[0].startswith("--"):
                    if tokens[0] in ("--flash_mode", "--flash-mode") and len(tokens) > 1:
                        flash_mode = tokens[1]
                    elif tokens[0] in ("--flash_freq", "--flash-freq") and len(tokens) > 1:
                        flash_freq = tokens[1]
                    elif tokens[0] in ("--flash_size", "--flash-size") and len(tokens) > 1:
                        flash_size = tokens[1]
                    continue
                if len(tokens) >= 2:
                    try:
                        int(tokens[0], 0)
                    except ValueError:
                        continue
                    base = os.path.basename(tokens[1])
                    if base not in files:
                        self._send_json(
                            {"ok": False,
                             "error": f"flash_args references '{base}' but it was not uploaded"},
                            400,
                        )
                        return
                    flash_pairs.append((tokens[0], base))
        flash_pairs.extend(offset_files)
        if not flash_pairs:
            self._send_json({"ok": False, "error": "no binaries to flash"}, 400)
            return

        base_args = [
            "--chip", chip,
            "--baud", baud,
            "--before", "default-reset",
            "--after", "hard-reset",
        ]
        write_args = [
            "write-flash",
            "--flash-mode", flash_mode,
            "--flash-freq", flash_freq,
            "--flash-size", flash_size,
        ]
        if erase:
            write_args.append("--erase-all")
        for offset, base in flash_pairs:
            write_args.extend([offset, base])

        result = flash_device(slot, files, base_args + write_args)
        self._send_json(result, 200 if result.get("ok") else 500)

    def _handle_flash_read(self):
        """POST /api/flash/read — read a flash region back off a slot's device.

        The read counterpart to /api/flash. JSON body (no upload needed):
          slot / slot_key — which slot, required
          offset          — flash offset, int or "0x..." string, required
          length          — bytes to read, int or "0x..." string, required
          chip            — esp32c3/... (default "auto")
          baud            — esptool baud (default "460800")

        Response: {"ok": true, "offset", "length", "sha256",
                   "data_b64": "<base64 of the bytes>"}. The caller decodes
        data_b64 to bytes. Use for pulling a coredump partition, an NVS blob,
        or the live partition table without dropping to OpenOCD.
        """
        body = self._read_json()
        if body is None:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return
        slot = _resolve_slot(body)
        if slot is None:
            self._send_json(
                {"ok": False, "error": "missing or unknown 'slot' / 'slot_key'"}, 400)
            return

        def _as_int(v):
            return int(v, 0) if isinstance(v, str) else int(v)

        try:
            offset = _as_int(body["offset"])
            length = _as_int(body["length"])
        except (KeyError, ValueError, TypeError):
            self._send_json(
                {"ok": False, "error": "offset and length required (int or 0x-string)"}, 400)
            return
        if offset < 0 or length <= 0:
            self._send_json({"ok": False, "error": "offset >= 0 and length > 0"}, 400)
            return

        chip = body.get("chip", "auto")
        baud = body.get("baud", "460800")
        result = read_flash_device(slot, offset, length, chip=chip, baud=baud)
        if not result.get("ok"):
            self._send_json(result, 500)
            return
        self._send_json({
            "ok": True,
            "offset": offset,
            "length": len(result["data"]),
            "sha256": result["sha256"],
            "data_b64": base64.b64encode(result["data"]).decode("ascii"),
        })

    def _handle_chip_info(self):
        """POST /api/chip/info — read chip and flash identity from the device.

        JSON body: {"slot": "SLOT3", "chip": "auto"?}

        Runs `esptool flash-id`, which reports the *physical* flash size. That
        is not knowable any other way: a build writes its configured size into
        the image header and the bootloader prints that value back, so a boot
        log only ever repeats the config. Configuring more than the part holds
        puts the partition table past the end of flash, and the damage shows up
        later as corruption at whatever offset first exceeds the device.

        Like /api/flash this stops the proxy, drives the device, and restarts
        the proxy — so it **reboots the DUT**. It is a deliberate call, never a
        field on /api/devices, which is polled.

        Returns {"ok", "chip", "revision", "features", "flash_size",
                 "flash_manufacturer", "flash_device", "mac", "output"}.
        """
        body = self._read_json()
        if body is None:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return

        slot_label = body.get("slot")
        if not slot_label:
            self._send_json({"ok": False, "error": "missing 'slot' field"}, 400)
            return
        slot = _find_slot_by_label(slot_label)
        if not slot:
            self._send_json({"ok": False, "error": f"slot '{slot_label}' not found"}, 404)
            return
        # There used to be a 409 here: "this slot has a debug session — stop
        # it first". It never fired. `debugging` is computed for the
        # /api/devices view and was never a field on the slot itself, so the
        # guard read None every time and esptool went ahead against a chip
        # OpenOCD was holding halted. It exited 1, and the handler called
        # that a 500 — an internal server error, for a device that was
        # simply busy. A guard that cannot fire is worse than none: it reads
        # as protection.
        #
        # flash_device now stops any debug session and restores it with the
        # CPU running, so there is nothing to guard against and nothing for
        # the caller to do first.
        chip = body.get("chip", "auto")
        result = flash_device(slot, {}, ["--chip", chip, "flash-id"], timeout_s=60.0)
        if not result.get("ok"):
            # The device did not answer. That is not this server failing.
            self._send_json(result, 502)
            return

        result.update(_parse_chip_info(result.get("output", "")))
        self._send_json(result, 200)

    def _handle_ota(self):
        """POST /api/ota — over-the-air firmware update of a networked device.

        The network sibling of /api/flash (USB esptool): the Pi relays an
        ArduinoOTA (espota) push to a board that is no longer on a USB slot
        (deployed on the LAN). This is what lets a client that cannot reach the
        board directly — e.g. a NAT'd container that can't accept ArduinoOTA's
        reverse TCP connection — still update it, since the Pi is on the LAN.

        Form fields (multipart/form-data):
          target — board IP or hostname (e.g. 192.168.4.42, device.local), required
          port   — ArduinoOTA port (default 3232)
          auth   — OTA password (optional)
        File:
          firmware — the .bin image (part name "firmware"), required

        Returns {"ok", "returncode", "output"}.
        """
        if not os.path.isfile(ESPOTA_PATH):
            self._send_json({"ok": False, "error": f"espota.py not installed at {ESPOTA_PATH}"}, 501)
            return
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"ok": False, "error": "expected multipart/form-data"}, 400)
            return
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
        if not boundary:
            self._send_json({"ok": False, "error": "missing boundary"}, 400)
            return
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return
        body = self.rfile.read(length)
        parts_raw = body.split(b"--" + boundary.encode())

        fields: dict[str, str] = {}
        firmware: bytes | None = None
        for part in parts_raw:
            part = part.strip()
            if not part or part == b"--":
                continue
            if b"\r\n\r\n" in part:
                header_section, content = part.split(b"\r\n\r\n", 1)
            elif b"\n\n" in part:
                header_section, content = part.split(b"\n\n", 1)
            else:
                continue
            if content.endswith(b"\r\n"):
                content = content[:-2]
            headers_text = header_section.decode("utf-8", errors="replace")
            name = None
            is_file = False
            for h in headers_text.split("\n"):
                h = h.strip()
                if h.lower().startswith("content-disposition"):
                    for tok in h.split(";"):
                        tok = tok.strip()
                        if tok.startswith("name="):
                            name = tok[5:].strip().strip('"').strip("'")
                        elif tok.startswith("filename="):
                            is_file = True
            if name is None:
                continue
            if name == "firmware" and is_file:
                firmware = content
            elif not is_file:
                fields[name] = content.decode("utf-8", errors="replace").strip()

        target = fields.get("target", "").strip()
        port = fields.get("port", "3232").strip() or "3232"
        auth = fields.get("auth", "")
        if not target:
            self._send_json({"ok": False, "error": "missing 'target' (board IP/hostname)"}, 400)
            return
        if not firmware:
            self._send_json({"ok": False, "error": "missing 'firmware' file part"}, 400)
            return

        fw_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="ota-", suffix=".bin", delete=False) as f:
                f.write(firmware)
                fw_path = f.name
            cmd = ["python3", ESPOTA_PATH, "-i", target, "-p", str(port), "-f", fw_path, "-r"]
            if auth:
                cmd += ["-a", auth]
            log_activity(f"ota → {target}:{port} ({len(firmware)} bytes)", "step")
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            # espota emits \r progress bars; strip them for a readable response.
            raw = (proc.stdout or "") + (proc.stderr or "")
            output = "\n".join(
                ln for ln in raw.replace("\r", "\n").splitlines()
                if not ln.startswith("Uploading: [")
            )
            ok = proc.returncode == 0
            log_activity(
                f"ota → {target} " + ("ok" if ok else f"FAILED ({proc.returncode})"),
                "ok" if ok else "error",
            )
            self._send_json(
                {"ok": ok, "returncode": proc.returncode, "output": output},
                200 if ok else 500,
            )
        except subprocess.TimeoutExpired:
            self._send_json({"ok": False, "error": "espota timed out (board unreachable?)"}, 504)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)
        finally:
            if fw_path and os.path.exists(fw_path):
                os.remove(fw_path)

    def _handle_firmware_delete(self):
        body = self._read_json()
        if not body:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return
        project = body.get("project", "")
        filename = body.get("filename", "")
        if not project or not filename:
            self._send_json({"ok": False, "error": "missing project or filename"}, 400)
            return
        if ".." in project or ".." in filename:
            self._send_json({"ok": False, "error": "path traversal not allowed"}, 400)
            return
        fpath = os.path.join(FIRMWARE_DIR, project, filename)
        if not os.path.isfile(fpath):
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        os.remove(fpath)
        log_activity(f"firmware.delete({project}/{filename})", "ok")
        self._send_json({"ok": True})

    # -- BLE handlers --

    def _handle_ble_scan(self):
        if not ble_controller or not ble_controller.available():
            self._send_json({"ok": False, "error": "BLE not available (bleak not installed)"}, 501)
            return
        body = self._read_json() or {}
        timeout = body.get("timeout", 0)
        name_filter = body.get("name_filter", "")
        log_activity(f"ble.scan(timeout={timeout}, filter={name_filter!r})", "step")
        result = ble_controller.scan(timeout=timeout, name_filter=name_filter)
        if result.get("ok"):
            log_activity(f"ble.scan — found {len(result.get('devices', []))} devices", "ok")
        else:
            log_activity(f"ble.scan — {result.get('error')}", "error")
        self._send_json(result)

    def _handle_ble_connect(self):
        if not ble_controller or not ble_controller.available():
            self._send_json({"ok": False, "error": "BLE not available (bleak not installed)"}, 501)
            return
        body = self._read_json() or {}
        address = body.get("address", "")
        if not address:
            self._send_json({"ok": False, "error": "missing address"}, 400)
            return
        log_activity(f"ble.connect({address})", "step")
        result = ble_controller.connect(address)
        if result.get("ok"):
            log_activity(f"ble.connect({address}) — connected", "ok")
        else:
            log_activity(f"ble.connect({address}) — {result.get('error')}", "error")
        self._send_json(result, 200 if result.get("ok") else 409)

    def _handle_ble_disconnect(self):
        if not ble_controller or not ble_controller.available():
            self._send_json({"ok": False, "error": "BLE not available (bleak not installed)"}, 501)
            return
        log_activity("ble.disconnect", "step")
        result = ble_controller.disconnect()
        log_activity("ble.disconnect — done", "ok")
        self._send_json(result)

    def _handle_ble_status(self):
        if not ble_controller or not ble_controller.available():
            self._send_json({"ok": True, "state": "unavailable", "error": "bleak not installed"})
            return
        self._send_json(ble_controller.status())

    def _handle_ble_write(self):
        if not ble_controller or not ble_controller.available():
            self._send_json({"ok": False, "error": "BLE not available (bleak not installed)"}, 501)
            return
        body = self._read_json() or {}
        characteristic = body.get("characteristic", "")
        data_hex = body.get("data", "")
        response = body.get("response", True)
        if not characteristic:
            self._send_json({"ok": False, "error": "missing characteristic"}, 400)
            return
        if not data_hex:
            self._send_json({"ok": False, "error": "missing data"}, 400)
            return
        try:
            data = bytes.fromhex(data_hex.replace(" ", ""))
        except ValueError:
            self._send_json({"ok": False, "error": "invalid hex data"}, 400)
            return
        result = ble_controller.write(characteristic, data, response=response)
        self._send_json(result, 200 if result.get("ok") else 500)

    def _handle_ble_read(self):
        if not ble_controller or not ble_controller.available():
            self._send_json({"ok": False, "error": "BLE not available (bleak not installed)"}, 501)
            return
        body = self._read_json() or {}
        characteristic = body.get("characteristic", "")
        if not characteristic:
            self._send_json({"ok": False, "error": "missing characteristic"}, 400)
            return

        log_activity(f"ble.read({characteristic})", "step")
        result = ble_controller.read(characteristic)
        if result.get("ok"):
            log_activity(f"ble.read({characteristic}) — success", "ok")
        else:
            log_activity(f"ble.read({characteristic}) — {result.get('error')}", "error")
        self._send_json(result, 200 if result.get("ok") else 500)

    # -- GDB debug handlers --

    def _handle_debug_start(self):
        body = self._read_json() or {}
        slot_label = body.get("slot")
        chip = body.get("chip")
        probe = body.get("probe")

        # Auto-find slot: pick the first present device (configured or dynamic)
        slot = None
        if not slot_label:
            for s in slots.values():
                if s.get("present"):
                    slot = s
                    slot_label = s.get("label") or s.get("slot_key", "")[-20:]
                    break
            if not slot:
                self._send_json({"ok": False, "error": "no device found"}, 404)
                return
        else:
            slot = _find_slot_by_label(slot_label)
            if not slot:
                self._send_json({"ok": False, "error": f"slot '{slot_label}' not found"}, 404)
                return
        gdb_port = slot.get("gdb_port")
        telnet_port = slot.get("openocd_telnet_port")
        if not gdb_port:
            idx = list(slots.values()).index(slot) if slot in slots.values() else 0
            gdb_port = 3333 + idx
            telnet_port = 4444 + idx

        # Try USB JTAG first, then fall back to probe if available
        result = debug_controller.start(
            slot_label, slot, gdb_port, telnet_port, chip, probe)

        # If USB JTAG auto-detect failed and no probe was specified,
        # try again with the first available probe
        if not result.get("ok") and not probe:
            available_probes = debug_controller.get_probes()
            for p in available_probes:
                if not p["in_use"]:
                    log_activity(
                        f"USB JTAG failed, trying probe {p['label']}",
                        "step")
                    result = debug_controller.start(
                        slot_label, slot, gdb_port, telnet_port,
                        chip, p["label"])
                    if result.get("ok"):
                        break

        if result.get("ok"):
            detected_chip = result.get("chip", chip)
            used_probe = result.get("probe")
            # Dual-USB (role=debug) or probe: serial stays running
            if slot.get("role") != "debug" and not used_probe:
                slot["state"] = STATE_DEBUGGING
            log_activity(
                f"Debug started: {slot_label} ({detected_chip}) "
                f"GDB:{gdb_port}"
                + (f" via {used_probe}" if used_probe else ""),
                "ok")
        self._send_json(result)

    def _handle_debug_stop(self):
        body = self._read_json() or {}
        slot_label = body.get("slot")

        # Auto-find: stop the first active debug session
        if not slot_label:
            sessions = debug_controller.status()
            for label in sessions:
                slot_label = label
                break
            if not slot_label:
                self._send_json({"ok": True})  # nothing to stop
                return
        result = debug_controller.stop(slot_label)
        slot = _find_slot_by_label(slot_label)
        if slot:
            if slot["state"] == STATE_DEBUGGING:
                slot["state"] = STATE_IDLE if slot["present"] else STATE_ABSENT
            log_activity(f"Debug stopped: {slot_label}", "info")
        self._send_json(result)

    def _handle_debug_status(self):
        sessions = debug_controller.status()
        all_slots = {}
        # Include all configured and dynamic slots
        for s in slots.values():
            label = s.get("label") or s.get("slot_key", "")[-20:]
            if not label:
                continue
            if label in sessions:
                all_slots[label] = sessions[label]
            else:
                all_slots[label] = {"debugging": False}
        # Include any sessions on labels not yet in all_slots
        for label, info in sessions.items():
            if label not in all_slots:
                all_slots[label] = info
        self._send_json({"ok": True, "slots": all_slots})

    def _handle_debug_probes(self):
        probes = debug_controller.get_probes()
        self._send_json({"ok": True, "probes": probes})

    def _handle_debug_group(self):
        groups: dict[str, dict] = {}
        for s in slots.values():
            grp = s.get("group")
            if not grp:
                continue
            role = s.get("role", "unknown")
            if grp not in groups:
                groups[grp] = {}
            groups[grp][role] = {
                "label": s.get("label"),
                "tcp_port": s.get("tcp_port"),
                "gdb_port": s.get("gdb_port"),
                "present": s.get("present", False),
                "running": s.get("running", False),
                "state": s.get("state"),
            }
        self._send_json({"ok": True, "groups": groups})

    # -- signal generator handlers (Si5351 + PE4302 with GPCLK fallback) --

    def _siggen_unavailable(self):
        self._send_json(
            {"ok": False, "error": "signal generator not available"}, 503)

    def _handle_siggen_start(self):
        if _siggen is None:
            return self._siggen_unavailable()
        body = self._read_json()
        if not body:
            return self._send_json({"ok": False, "error": "empty body"}, 400)
        freq = body.get("freq_hz") or body.get("freq")
        if freq is None:
            return self._send_json({"ok": False, "error": "missing freq_hz"}, 400)
        try:
            state = _siggen.start(
                freq_hz=float(freq),
                backend=body.get("backend", "auto"),
                channel=body.get("channel"),
                pin=body.get("pin"),
                atten_db=body.get("atten_db"),
                morse=body.get("morse"))
        except Exception as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        log_activity(
            f"siggen started: {state['backend']} @ {state['freq_hz']:.0f} Hz", "ok")
        self._send_json({"ok": True, **state})

    def _handle_siggen_stop(self):
        if _siggen is None:
            return self._siggen_unavailable()
        state = _siggen.stop()
        log_activity("siggen stopped", "info")
        self._send_json({"ok": True, **state})

    def _handle_siggen_status(self):
        if _siggen is None:
            return self._siggen_unavailable()
        self._send_json({"ok": True, **_siggen.status()})

    def _handle_siggen_freq(self):
        if _siggen is None:
            return self._siggen_unavailable()
        body = self._read_json()
        if not body:
            return self._send_json({"ok": False, "error": "empty body"}, 400)
        freq = body.get("freq_hz") or body.get("freq")
        if freq is None:
            return self._send_json({"ok": False, "error": "missing freq_hz"}, 400)
        try:
            state = _siggen.set_frequency(float(freq), channel=body.get("channel"))
        except Exception as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        self._send_json({"ok": True, **state})

    def _handle_siggen_atten(self):
        if _siggen is None:
            return self._siggen_unavailable()
        body = self._read_json()
        if not body:
            return self._send_json({"ok": False, "error": "empty body"}, 400)
        db = body.get("db")
        if db is None:
            return self._send_json({"ok": False, "error": "missing db"}, 400)
        try:
            state = _siggen.set_attenuation(float(db))
        except Exception as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        self._send_json({"ok": True, **state})

    def _handle_siggen_frequencies(self, qs):
        if _siggen is None:
            return self._siggen_unavailable()
        low = float(qs.get("low", [3_500_000])[0])
        high = float(qs.get("high", [4_000_000])[0])
        backend = qs.get("backend", ["auto"])[0]
        try:
            freqs = _siggen.list_frequencies(low, high, backend=backend)
        except Exception as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        self._send_json({"ok": True, "frequencies": freqs})

    # ── SDR receiver (RTL-SDR + rtl_433) ──────────────────────────────

    def _sdr_unavailable(self):
        self._send_json(
            {"ok": False, "error": "SDR receiver not available"}, 503)

    def _handle_sdr_status(self):
        if _sdr is None:
            return self._sdr_unavailable()
        self._send_json({"ok": True, **_sdr.status()})

    def _handle_sdr_live_start(self):
        if _sdr is None:
            return self._sdr_unavailable()
        body = self._read_json() or {}
        try:
            result = _sdr.start_live(
                freqs=body.get("freqs"),
                gain=body.get("gain"),
                sample_rate=body.get("sample_rate"),
                mode=body.get("mode", "decode"),
                flex=body.get("flex"),
                isolate=bool(body.get("isolate", False)),
                squelch=bool(body.get("squelch", False)),
                hop_interval=int(body.get("hop_interval", 5)),
                ppm=body.get("ppm"),
                y_opts=body.get("y_opts"),
                sdr_settings=body.get("sdr_settings"))
        except Exception as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        log_activity(f"sdr live ▶ {result.get('config', {}).get('cmd', '')}", "step")
        self._send_json({"ok": True, **result})

    def _handle_sdr_live_stop(self):
        if _sdr is None:
            return self._sdr_unavailable()
        log_activity("sdr live ■ stopped", "ok")
        self._send_json({"ok": True, **_sdr.stop_live()})

    def _handle_sdr_live_status(self):
        if _sdr is None:
            return self._sdr_unavailable()
        self._send_json({"ok": True, **_sdr.live_status()})

    def _handle_sdr_reset(self):
        if _sdr is None:
            return self._sdr_unavailable()
        try:
            result = _sdr.reset_device()
        except Exception as exc:
            log_activity(f"sdr reset failed: {exc}", "error")
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        log_activity(f"sdr dongle USB-reset ({result.get('path')})", "ok")
        self._send_json({"ok": True, **result})

    def _handle_sdr_live_events(self, qs):
        if _sdr is None:
            return self._sdr_unavailable()
        since = int((qs.get("since", ["0"])[0]) or 0)
        self._send_json({"ok": True, **_sdr.live_events(since)})

    def _handle_sdr_capture(self):
        if _sdr is None:
            return self._sdr_unavailable()
        body = self._read_json() or {}
        try:
            result = _sdr.capture(
                freq_hz=body.get("freq_hz"),
                duration_s=body.get("duration_s"),
                protocols=body.get("protocols"),
                sample_rate=body.get("sample_rate"),
                flex=body.get("flex"),
                gain=body.get("gain"))
        except Exception as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        log_activity(
            f"sdr capture: {result['count']} event(s) @ "
            f"{result['freq_hz']} Hz", "ok")
        self._send_json({"ok": True, **result})

    def _handle_sdr_analyze(self):
        if _sdr is None:
            return self._sdr_unavailable()
        body = self._read_json() or {}
        try:
            result = _sdr.analyze(
                freq_hz=body.get("freq_hz"),
                duration_s=body.get("duration_s"),
                gain=body.get("gain"))
        except Exception as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        self._send_json({"ok": True, **result})

    def _handle_sdr_power(self):
        if _sdr is None:
            return self._sdr_unavailable()
        body = self._read_json() or {}
        try:
            result = _sdr.power(
                freq_hz=body.get("freq_hz"),
                duration_s=body.get("duration_s"),
                span_hz=int(body.get("span_hz", 40000)),
                bin_hz=int(body.get("bin_hz", 2000)),
                notch_hz=int(body.get("notch_hz", 0)),
                gain=body.get("gain"))
        except Exception as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        self._send_json({"ok": True, **result})

    def _handle_sdr_acquire(self):
        if _sdr is None:
            return self._sdr_unavailable()
        body = self._read_json() or {}
        try:
            result = _sdr.acquire(
                freq_hz=body.get("freq_hz"),
                span_hz=int(body.get("span_hz", 500_000)),
                bin_hz=int(body.get("bin_hz", 10_000)),
                gains=body.get("gains"),
                dwell_s=int(body.get("dwell_s", 3)),
                decode_s=int(body.get("decode_s", 12)),
                flex=body.get("flex"),
                wait_s=int(body.get("wait_s", 30)),
                progress=log_activity)
        except Exception as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        log_activity(
            f"sdr acquire: {result.get('ok_phase')} — {result.get('summary')}",
            "ok" if result.get("ok_phase") == "complete" else "warn")
        self._send_json({"ok": True, **result})

    def _handle_sdr_stop(self):
        if _sdr is None:
            return self._sdr_unavailable()
        self._send_json({"ok": True, **_sdr.stop()})

    def _serve_ui(self):
        # Substituted here rather than left to the fetch: the name is then
        # right in the tab title before any JavaScript runs, and stays right
        # on a bench whose /api/devices is wedged — which is exactly when
        # somebody has two tabs open and needs to know which board they are
        # looking at.
        html = _UI_HTML.replace("__BENCH_NAME__", hostname)
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


_UI_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>__BENCH_NAME__</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        html { height: 100%; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            min-height: 100vh;
            padding: 20px;
            display: flex; flex-direction: column;
        }
        h1 { text-align: center; margin-bottom: 5px; color: #00d4ff; }
        .subtitle { text-align: center; color: #aaa; font-size: 1.2em; margin-bottom: 20px; font-family: monospace; }
        h2 { color: #00d4ff; margin: 30px 0 15px; text-align: center; }
        .main-content {
            max-width: 1600px; margin: 0 auto; width: 100%;
            display: flex; flex-direction: column; flex: 1; min-height: 0;
        }
        .slots {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
            gap: 20px;
        }
        .slot {
            background: #16213e; border-radius: 12px; padding: 20px;
            border: 2px solid #0f3460; transition: all 0.3s;
        }
        .slot.idle { border-color: #00d4ff; box-shadow: 0 0 20px rgba(0,212,255,0.2); }
        .slot.running { border-color: #00d4ff; box-shadow: 0 0 20px rgba(0,212,255,0.2); }
        .slot.resetting { border-color: #e67e22; box-shadow: 0 0 20px rgba(230,126,34,0.2); }
        .slot.monitoring { border-color: #9b59b6; box-shadow: 0 0 20px rgba(155,89,182,0.2); }
        .slot.flapping { border-color: #e74c3c; background: #1a0000; }
        .slot.recovering {
            border-color: #e67e22; background: #1a1000;
            animation: pulse-recovering 2s ease-in-out infinite;
        }
        @keyframes pulse-recovering {
            0%, 100% { border-color: #e67e22; box-shadow: 0 0 15px rgba(230,126,34,0.3); }
            50% { border-color: #f39c12; box-shadow: 0 0 30px rgba(243,156,18,0.5); }
        }
        .slot.download_mode { border-color: #2ecc71; box-shadow: 0 0 20px rgba(46,204,113,0.3); }
        .slot.absent { border-color: #333; }
        .slot.present { border-color: #555; }
        .slot-header {
            display: flex; justify-content: space-between;
            align-items: center; margin-bottom: 15px;
        }
        .slot-label { font-size: 1.4em; font-weight: bold; }
        .status {
            padding: 4px 12px; border-radius: 20px;
            font-size: 0.85em; font-weight: bold;
        }
        .status.idle { background: #00d4ff; color: #1a1a2e; }
        .status.running { background: #00d4ff; color: #1a1a2e; }
        .status.resetting { background: #e67e22; color: #fff; }
        .status.monitoring { background: #9b59b6; color: #fff; }
        .status.flapping { background: #e74c3c; color: #fff; }
        .status.recovering { background: #e67e22; color: #fff; }
        .status.download_mode { background: #2ecc71; color: #1a1a2e; }
        .status.absent { background: #333; color: #666; }
        .status.present { background: #555; color: #ccc; }
        .status.stopped { background: #333; color: #666; }
        .slot-info { font-size: 0.9em; color: #aaa; margin-bottom: 15px; }
        .slot-info div { margin: 5px 0; }
        .slot-info span { color: #00d4ff; font-family: monospace; }
        .url-box {
            background: #0f3460; padding: 10px; border-radius: 8px;
            font-family: monospace; font-size: 0.9em;
            word-break: break-all; cursor: pointer; transition: background 0.2s;
        }
        .url-box:hover { background: #1a4a7a; }
        .url-box.empty { color: #666; cursor: default; }
        .copied { background: #00d4ff !important; color: #1a1a2e !important; }
        .debug-active { color: #2ecc71; font-weight: bold; }
        .debug-active span { color: #2ecc71 !important; font-family: monospace; }
        .debug-idle { color: #888; }
        .debug-idle span { color: #888 !important; }
        .usb-device { color: #b388ff; }
        .usb-device span { color: #b388ff !important; font-family: monospace; }
        .probe-info { color: #2ecc71; font-weight: bold; padding: 6px 10px; background: rgba(46,204,113,0.1); border-radius: 4px; margin-top: 8px; }
        .usb-warning { color: #ff6b6b; font-weight: bold; padding: 6px 10px; background: rgba(255,107,107,0.1); border-radius: 4px; margin-top: 8px; }
        .error { color: #ff6b6b; font-size: 0.85em; margin-top: 10px; }
        .flap-warning {
            color: #e74c3c; font-weight: bold; padding: 6px 10px;
            background: rgba(231,76,60,0.15); border-radius: 4px; margin-top: 8px;
        }
        .recover-info {
            color: #e67e22; font-weight: bold; padding: 6px 10px;
            background: rgba(230,126,34,0.15); border-radius: 4px; margin-top: 8px;
        }
        .download-info {
            color: #2ecc71; font-weight: bold; padding: 6px 10px;
            background: rgba(46,204,113,0.15); border-radius: 4px; margin-top: 8px;
        }
        .slot-actions { margin-top: 10px; display: flex; gap: 8px; }
        .slot-actions button {
            padding: 6px 14px; border-radius: 6px; cursor: pointer;
            font-size: 0.85em; border: none; font-weight: bold; transition: all 0.2s;
        }
        .btn-release { background: #2ecc71; color: #1a1a2e; }
        .btn-release:hover { background: #27ae60; }
        .btn-recover { background: #e67e22; color: #fff; }
        .btn-recover:hover { background: #d35400; }
        .info { text-align: center; color: #666; margin-top: 30px; font-size: 0.85em; }
        /* Activity log */
        .log-section {
            margin: 20px 0 0;
            background: #16213e; border-radius: 12px; padding: 20px;
            border: 2px solid #0f3460;
            display: flex; flex-direction: column;
            flex: 1; min-height: 0;
        }
        .log-section h2 { margin: 0 0 10px; font-size: 1.1em; color: #eee; flex-shrink: 0; }
        .log-entries {
            background: #0a0a1a; border-radius: 8px; padding: 10px;
            flex: 1; overflow-y: auto; font-family: monospace;
            font-size: 0.82em; line-height: 1.6;
        }
        .log-entries:empty::after { content: 'No activity yet'; color: #555; }
        .log-entry { white-space: pre-wrap; word-break: break-all; }
        .log-entry .ts { color: #555; }
        .log-entry.cat-info { color: #aaa; }
        .log-entry.cat-step { color: #00d4ff; }
        .log-entry.cat-ok { color: #2ecc71; }
        .log-entry.cat-error { color: #ff6b6b; }
        .log-actions { margin-top: 10px; display: flex; gap: 8px; }
        .log-actions button {
            background: #0f3460; color: #aaa; border: 1px solid #333;
            padding: 6px 14px; border-radius: 6px; cursor: pointer;
            font-size: 0.85em; transition: all 0.2s;
        }
        .log-actions button:hover { background: #1a4a7a; color: #eee; }
        .log-actions button.primary { background: #00d4ff; color: #1a1a2e; border-color: #00d4ff; font-weight: bold; }
        .log-actions button.primary:hover { background: #00b8d9; }
        .log-actions button:disabled { background: #333; color: #555; cursor: not-allowed; }
        /* Human interaction request overlay */
        .human-overlay {
            display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.8); z-index: 9999;
            justify-content: center; align-items: center;
        }
        .human-overlay.visible { display: flex; }
        .human-modal {
            background: #1a1a2e; border: 3px solid #ff8c00; border-radius: 16px;
            padding: 40px; max-width: 520px; width: 90%; text-align: center;
            animation: pulse-border 2s ease-in-out infinite;
        }
        @keyframes pulse-border {
            0%, 100% { border-color: #ff8c00; box-shadow: 0 0 20px rgba(255,140,0,0.3); }
            50% { border-color: #ffa500; box-shadow: 0 0 40px rgba(255,165,0,0.6); }
        }
        .human-modal h2 { color: #ff8c00; margin: 0 0 10px; font-size: 1.4em; }
        .human-modal .human-message { color: #eee; font-size: 1.2em; margin: 20px 0 25px; line-height: 1.5; }
        .human-modal .human-status { color: #aaa; font-size: 0.9em; margin: 10px 0; min-height: 1.2em; }
        .human-modal .human-buttons { display: flex; gap: 15px; justify-content: center; }
        .human-modal .btn-done {
            background: #28a745; color: #fff; border: none; padding: 12px 40px;
            border-radius: 8px; font-size: 1.1em; font-weight: bold; cursor: pointer;
        }
        .human-modal .btn-done:hover { background: #218838; }
        .human-modal .btn-cancel {
            background: #555; color: #ccc; border: none; padding: 12px 30px;
            border-radius: 8px; font-size: 1em; cursor: pointer;
        }
        .human-modal .btn-cancel:hover { background: #666; }
        /* Test progress panel */
        .test-section { margin: 20px 0 0; }
        .test-progress { background: #16213e; border-radius: 12px; padding: 20px; border: 2px solid #0f3460; }
        .test-header { font-size: 1.1em; color: #e0e0e0; margin-bottom: 10px; }
        .test-bar-container { background: #333; border-radius: 6px; height: 24px; margin-bottom: 12px; position: relative; overflow: hidden; }
        .test-bar { background: linear-gradient(90deg, #1a6b2a, #28a745); height: 100%; border-radius: 6px; transition: width 0.5s ease; min-width: 0; }
        .test-bar-label { position: absolute; top: 0; left: 0; right: 0; bottom: 0;
            display: flex; align-items: center; justify-content: center;
            font-size: 0.85em; font-weight: bold; color: #fff; text-shadow: 0 1px 2px rgba(0,0,0,0.5); }
        .test-counter { color: #ccc; font-size: 1.0em; margin-bottom: 12px; font-weight: bold; }
        .test-current { padding: 12px; border-radius: 6px; margin-bottom: 12px;
            background: #1a3a1a; border-left: 4px solid #28a745; }
        .test-current.manual { background: #3a2a00; border-left: 4px solid #f0a030;
            animation: manual-pulse 2s ease-in-out infinite; }
        @keyframes manual-pulse {
            0%, 100% { border-left-color: #f0a030; }
            50% { border-left-color: #ff6600; }
        }
        .test-current .test-id { font-weight: bold; color: #fff; }
        .test-current .test-step { color: #ccc; margin-top: 4px; }
        .test-results { max-height: 200px; overflow-y: auto; }
        .test-result { display: flex; gap: 10px; padding: 4px 0; font-size: 0.9em; color: #aaa; }
        .test-result .badge { font-weight: bold; min-width: 40px; }
        .test-result .badge.pass { color: #28a745; }
        .test-result .badge.fail { color: #dc3545; }
        .test-result .badge.skip { color: #ffc107; }
        /* Signal generator panel */
        .siggen-section { margin: 20px 0 0; }
        .siggen-box { background: #16213e; border-radius: 12px; padding: 20px; border: 2px solid #0f3460; }
        .siggen-row { display: flex; gap: 12px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
        .siggen-row label { color: #ccc; min-width: 70px; font-size: 0.9em; }
        .siggen-row input, .siggen-row select { background: #0f1a30; color: #e0e0e0;
            border: 1px solid #2a4070; border-radius: 4px; padding: 4px 8px; font-size: 0.9em; }
        .siggen-row input[type="number"] { width: 130px; }
        .siggen-row input[type="range"] { flex: 1; min-width: 200px; }
        .siggen-row button { background: #28a745; color: #fff; border: none;
            padding: 6px 14px; border-radius: 4px; cursor: pointer; font-weight: bold; }
        .siggen-row button.stop { background: #dc3545; }
        .siggen-row button:hover { opacity: 0.85; }
        .siggen-hw { font-size: 0.85em; color: #888; margin-bottom: 12px; }
        .siggen-hw .ok { color: #28a745; }
        .siggen-hw .off { color: #dc3545; }
        .siggen-state { color: #f0a030; font-weight: bold; }
        .sdr-section { margin: 20px 0 0; }
        .sdr-section .siggen-row label { min-width: auto; }
        .sdr-section .fld { display: inline-flex; align-items: center; gap: 6px; }
        .sdr-section .chk { display: flex; align-items: center; gap: 4px; color: #ccc; min-width: auto; font-size: 0.9em; }
        .rssi-meter { position: relative; flex: 1; min-width: 220px; height: 18px; border-radius: 9px;
            background: linear-gradient(90deg, #6c3483 0%, #2980b9 18%, #2ecc71 42%, #2ecc71 66%, #f1c40f 84%, #e74c3c 100%); }
        .rssi-marker { position: absolute; top: -3px; width: 3px; height: 24px; background: #fff;
            box-shadow: 0 0 4px #000; left: 0; transition: left 0.25s; }
        .sdr-tabs button { background: #0f1a30; color: #cfe; border: 1px solid #0f3460; }
        .sdr-tabs button.active { background: #0f3460; color: #fff; }
        .sdr-section button.preset { background: #2980b9; }
        .sdr-section button.sherlock { background: #8e44ad; }
        .sdr-section button.sherlock.recording { background: #dc3545; }
        .sdr-view { margin-top: 10px; }
        .sdr-table { width: 100%; border-collapse: collapse; font-size: 0.82em; }
        .sdr-table th, .sdr-table td { text-align: left; padding: 3px 6px; border-bottom: 1px solid #0f3460; color: #cfe; white-space: nowrap; }
        .sdr-table th { color: #8ab; position: sticky; top: 0; background: #16213e; }
        .sdr-table td.data { font-family: monospace; color: #2ecc71; max-width: 300px; overflow: hidden; text-overflow: ellipsis; }
        .sdr-table-wrap { max-height: 300px; overflow: auto; }
        .sdr-mono { background: #0a1020; border-radius: 8px; padding: 10px; font-family: monospace;
            font-size: 0.78em; color: #9fb; max-height: 300px; overflow: auto; white-space: pre-wrap; }
        .sdr-strong { color: #e74c3c; font-weight: bold; }
        .sdr-weak { color: #f1c40f; }
        .codechip { background: #0f3460; color: #2ecc71; font-family: monospace;
            padding: 3px 9px; border-radius: 6px; cursor: pointer; font-size: 0.9em; user-select: all; }
        .codechip:hover { background: #1b4b7a; }
        .codechip.copied { background: #2ecc71; color: #03260f; }
    </style>
</head>
<body>
    <h1 id="title">__BENCH_NAME__</h1>
    <div class="subtitle" id="subtitle"></div>
    <div class="main-content">
    <div class="slots" id="slots"></div>
    <div class="test-section" id="test-section">
        <h2>Test Progress</h2>
        <div class="test-progress">
            <div class="test-header" id="test-header"></div>
            <div class="test-bar-container">
                <div class="test-bar" id="test-bar"></div>
                <div class="test-bar-label" id="test-bar-label"></div>
            </div>
            <div class="test-counter" id="test-counter"></div>
            <div class="test-current" id="test-current"></div>
            <div class="test-results" id="test-results"></div>
        </div>
    </div>
    <div class="siggen-section" id="siggen-section">
        <h2>Signal Generator</h2>
        <div class="siggen-box">
            <div class="siggen-hw" id="siggen-hw"></div>
            <div class="siggen-row">
                <label>Freq (Hz)</label>
                <input type="number" id="siggen-freq" value="3500000" min="8000" max="200000000" step="1000">
                <label>Backend</label>
                <select id="siggen-backend">
                    <option value="auto">auto</option>
                    <option value="si5351">si5351</option>
                    <option value="gpclk">gpclk</option>
                </select>
                <button onclick="siggenStart()">Start</button>
                <button class="stop" onclick="siggenStop()">Stop</button>
            </div>
            <div class="siggen-row">
                <label>Atten (dB)</label>
                <input type="range" id="siggen-atten-slider" min="0" max="31.5" step="0.5" value="0"
                    oninput="siggenAttenPreview(this.value)" onchange="siggenAtten(this.value)">
                <span id="siggen-atten-val" style="color:#e0e0e0; min-width: 50px;">0.0 dB</span>
            </div>
            <div class="siggen-row">
                <label>Morse</label>
                <input type="text" id="siggen-morse-msg" placeholder="VVV DE TEST (leave empty for continuous)" style="flex:1; min-width: 200px;">
                <label>WPM</label>
                <input type="number" id="siggen-morse-wpm" value="15" min="1" max="60" style="width: 60px;">
            </div>
            <div id="siggen-state" class="siggen-state">idle</div>
        </div>
    </div>
    <div class="sdr-section" id="sdr-section">
        <h2>SDR Console (rtl_433)</h2>
        <div class="siggen-box">
            <div class="siggen-row">
                <label>Presets</label>
                <button type="button" class="preset" onclick="sdrPreset('analyze433')">Analyze 433 (AGC)</button>
                <button type="button" class="preset" onclick="sdrPreset('decode433')">Decode 433 (AGC)</button>
                <button type="button" class="preset" onclick="sdrPreset('scanall')">Scan all bands</button>
                <button type="button" class="sherlock" id="sdr-sherlock" onclick="sdrSherlock(this)">&#128269; AI Sherlock</button>
                <span id="sdr-log-state" style="color:#e0e0e0"></span>
            </div>
            <div class="siggen-row">
                <span class="fld"><label>Bands</label>
                    <label class="chk"><input type="checkbox" class="sdr-band" value="433920000" checked> 433.92</label>
                    <label class="chk"><input type="checkbox" class="sdr-band" value="315000000"> 315</label>
                    <label class="chk"><input type="checkbox" class="sdr-band" value="868300000"> 868</label></span>
                <span class="fld"><label>Hop&nbsp;s</label>
                    <input type="number" id="sdr-hop" value="5" min="1" max="60" style="width:60px"></span>
                <span class="fld"><label>Mode</label>
                    <select id="sdr-mode" onchange="sdrModeChange()">
                        <option value="decode">decode (all)</option>
                        <option value="flex">flex (-X)</option>
                        <option value="analyze">analyze (-A)</option>
                    </select></span>
                <button onclick="sdrLiveStart()">Start</button>
                <button class="stop" onclick="sdrLiveStop()">Stop</button>
                <button type="button" onclick="sdrReset()" title="USB-reset a wedged dongle">Reset dongle</button>
            </div>
            <div class="siggen-row">
                <span class="fld"><label>Gain</label>
                    <label class="chk"><input type="checkbox" id="sdr-agc" checked onchange="sdrToggleAgc()"> AGC</label></span>
                <input type="range" id="sdr-gain" min="0" max="49.6" step="0.1" value="25" disabled
                    oninput="document.getElementById('sdr-gain-val').textContent=this.value+' dB'"
                    onchange="sdrLiveApply()">
                <span id="sdr-gain-val" style="min-width:56px;color:#e0e0e0">auto</span>
                <label class="chk"><input type="checkbox" id="sdr-squelch" onchange="sdrLiveApply()"> squelch</label>
            </div>
            <div class="siggen-row" id="sdr-flex-row" style="display:none">
                <label>Flex&nbsp;-X</label>
                <input type="text" id="sdr-flex" placeholder="n=rmt,m=OOK_PWM,s=416,l=2150,r=16000" style="flex:1;min-width:260px">
                <label class="chk"><input type="checkbox" id="sdr-isolate"> only -X (-R&nbsp;0)</label>
            </div>
            <div class="siggen-row">
                <label>RSSI</label>
                <div class="rssi-meter"><div class="rssi-marker" id="sdr-rssi-marker"></div></div>
                <span id="sdr-rssi-val" style="min-width:150px;color:#e0e0e0">&mdash;</span>
            </div>
            <div class="siggen-row">
                <span class="fld"><label>Codes seen</label>
                    <span style="color:#8ab;font-size:0.8em">(click to copy)</span></span>
                <div id="sdr-codes" style="display:flex;gap:6px;flex-wrap:wrap"></div>
                <button type="button" onclick="sdrCodeCounts.clear();sdrRenderCodes()">Clear</button>
            </div>
            <div id="sdr-state" class="siggen-state">idle</div>
            <div class="siggen-row sdr-tabs" style="gap:6px">
                <button type="button" id="sdr-tab-table" class="active" onclick="sdrView('table')">Events</button>
                <button type="button" id="sdr-tab-analyzer" onclick="sdrView('analyzer')">Analyzer</button>
                <button type="button" id="sdr-tab-raw" onclick="sdrView('raw')">Raw</button>
            </div>
            <div id="sdr-view-table" class="sdr-view">
                <div class="sdr-table-wrap"><table class="sdr-table">
                    <thead><tr><th>time</th><th>freq</th><th>rssi</th><th>snr</th><th>mod</th><th>model</th><th>data</th></tr></thead>
                    <tbody id="sdr-tbody"></tbody>
                </table></div>
            </div>
            <div id="sdr-view-analyzer" class="sdr-view sdr-mono" style="display:none"></div>
            <div id="sdr-view-raw" class="sdr-view sdr-mono" style="display:none"></div>
        </div>
    </div>
    <div class="log-section">
        <h2>Activity Log</h2>
        <div class="log-entries" id="log-entries"></div>
        <div class="log-actions">
            <button onclick="clearLog()">Clear</button>
        </div>
    </div>
    <div class="info" id="info">Auto-refresh every 5 seconds</div>
    </div><!-- /main-content -->
    <div class="human-overlay" id="human-overlay">
        <div class="human-modal">
            <h2>Action Required</h2>
            <div class="human-message" id="human-message"></div>
            <div class="human-status" id="human-status"></div>
            <div class="human-buttons">
                <button class="btn-done" id="btn-human-done">Done</button>
                <button class="btn-cancel" id="btn-human-cancel">Cancel</button>
            </div>
        </div>
    </div>
<script>
let hostName = '';
let hostIp = '';
async function fetchDevices() {
    try {
        const resp = await fetch('/api/devices');
        const data = await resp.json();
        hostName = data.hostname || '';
        hostIp = data.host_ip || '';
        // The heading names *this* bench. The guard below was already asking
        // "do we know the hostname?" and then ignoring the answer, so every
        // bench called itself the same thing — and with more than one on a
        // desk, two identical tabs is exactly when you flash the wrong board.
        if (hostName) {
            document.getElementById('title').textContent = hostName;
            document.title = hostName + ' — ' + (hostIp || 'testbench');
        }
        // Show GPIO config in subtitle (Pi-level, not per-slot)
        const gpioSlot = data.slots.find(s => s.has_gpio);
        if (gpioSlot) {
            let gpio = 'GPIO: BOOT=' + (gpioSlot.gpio_boot ?? '?');
            if (gpioSlot.gpio_en != null) gpio += ', EN=' + gpioSlot.gpio_en;
            document.getElementById('subtitle').textContent = gpio;
        }
        renderSlots(data.slots);
        document.getElementById('info').textContent =
            'Hostname: ' + hostName + '  |  IP: ' + hostIp + '  |  Auto-refresh every 5s';
    } catch (e) {
        console.error('Error fetching devices:', e);
    }
}


function slotStatus(s) {
    if (s.state) return s.state;
    // Fallback for older portal without state field
    if (s.flapping) return 'flapping';
    if (s.running) return 'idle';
    if (s.present) return 'idle';
    return 'absent';
}
function statusLabel(s) {
    const st = slotStatus(s);
    const labels = {
        'recovering': 'RECOVERING',
        'download_mode': 'DOWNLOAD MODE',
    };
    return labels[st] || st.toUpperCase();
}
// Identify common USB-serial chips from their VID:PID or product string
// so the Chip line shows something more useful than "unknown device".
function identifyUsbDevice(usb_devices) {
    if (!usb_devices || !usb_devices.length) return 'unknown device';
    const known = {
        '0403:6001': 'FT232R (USB-UART bridge)',
        '0403:6010': 'FT2232 (dual USB-UART)',
        '0403:6011': 'FT4232 (quad USB-UART)',
        '0403:6014': 'FT232H (high-speed USB-UART)',
        '0403:6015': 'FT230X / FT231X (USB-UART)',
        '10c4:ea60': 'CP2102 (USB-UART bridge)',
        '10c4:ea70': 'CP2105 (dual USB-UART)',
        '1a86:7523': 'CH340 (USB-UART bridge)',
        '1a86:55d3': 'CH343 (USB-UART bridge)',
        '1a86:5523': 'CH341 (USB-UART bridge)',
        '303a:1001': 'Espressif USB JTAG/Serial',
        '067b:2303': 'PL2303 (USB-UART bridge)',
    };
    // Prefer the first non-Espressif match (if an ESP32 is present its chip
    // is already set via detected_chip elsewhere).
    for (const d of usb_devices) {
        const name = known[d.vid_pid];
        if (name) return name;
    }
    // Fall back to the raw product string if we can't identify it.
    return usb_devices[0].product || 'unknown device';
}

function renderSlots(slots) {
    const el = document.getElementById('slots');
    el.innerHTML = slots.map(s => {
        const st = slotStatus(s);
        const label = s.label || s.slot_key.slice(-20);
        const ipUrl = s.url || '';
        const copyTarget = ipUrl;
        let statusMsg = '';
        let actionBtns = '';
        if (st === 'recovering') {
            statusMsg = '<div class="recover-info">&#9881; Recovery in progress' +
                (s.recover_retries > 0 ? ' (attempt ' + s.recover_retries + ')' : '') +
                '...</div>';
        } else if (st === 'download_mode') {
            statusMsg = '<div class="download-info">&#10003; Device in download mode — ready to flash</div>';
            actionBtns = '<div class="slot-actions">' +
                '<button class="btn-release" onclick="releaseSlot(\\'' + label + '\\')">Release &amp; Reboot</button>' +
                '</div>';
        } else if (s.has_gpio && s.gpio_wired === true) {
            statusMsg = '<div class="probe-info">&#10003; BOOT/EN wiring verified</div>';
        } else if (s.has_gpio && s.gpio_wired === false) {
            statusMsg = '<div class="flap-warning">&#9888; BOOT/EN pins configured but NOT wired to this board — ' +
                'GPIO recovery is unavailable for this slot</div>';
        } else if (s.is_probe) {
            statusMsg = '<div class="probe-info">&#10003; ESP-Prog debug probe</div>';
        } else if (s.usb_warning) {
            statusMsg = '<div class="usb-warning">&#9888; ' + s.usb_warning + '</div>';
        } else if (s.flapping && !s.recovering) {
            statusMsg = '<div class="flap-warning">&#9888; Device is boot-looping.' +
                (s.recover_retries >= 2 ? ' Needs manual intervention.' : '') +
                '</div>';
            actionBtns = '<div class="slot-actions">' +
                '<button class="btn-recover" onclick="recoverSlot(\\'' + label + '\\')">Retry Recovery</button>' +
                '</div>';
        }
        if (s.has_gpio && s.present && st !== 'download_mode' && st !== 'recovering') {
            const verb = (s.gpio_wired === null || s.gpio_wired === undefined)
                ? 'Test BOOT/EN wiring' : 'Re-test BOOT/EN wiring';
            actionBtns += '<div class="slot-actions">' +
                '<button class="btn-recover" onclick="testGpio(\\'' + label + '\\')">' +
                verb + '</button></div>';
        }
        return `
        <div class="slot ${st}">
            <div class="slot-header">
                <div class="slot-label">${label}</div>
                <div class="status ${st}">${statusLabel(s)}</div>
            </div>
            <div class="slot-info">
                <div>Port: <span>${s.tcp_port || '-'}</span></div>
                <div>Device: <span>${s.devnode || 'None'}</span></div>
                ${s.pid ? '<div>PID: <span>' + s.pid + '</span></div>' : ''}
                ${s.detected_chip ? '<div>Chip: <span>' + s.detected_chip + '</span></div>' :
                  (s.present ? '<div>Chip: <span style="color:#aaa">' + identifyUsbDevice(s.usb_devices) + '</span></div>' : '')}
                ${s.detected_chip && s.jtag_slot ? '<div>JTAG: <span>' + s.jtag_slot + (s.jtag_slot === label ? ' (built-in)' : ' (probe)') + '</span></div>' : (s.detected_chip ? '<div>JTAG: <span>none</span></div>' : '')}
                ${s.debugging ? '<div class="debug-active">Debug: <span>GDB :' + s.debug_gdb_port + '</span></div>' : (s.detected_chip ? '<div class="debug-idle">Debug: <span>idle</span></div>' : '')}
                ${(s.usb_devices || []).map(d => '<div class="usb-device">USB: <span>' + d.product + '</span></div>').join('')}
            </div>
            <div class="url-box ${s.running || st === 'idle' || st === 'download_mode' ? '' : 'empty'}"
                 onclick="${s.running || st === 'idle' ? "copyUrl('" + copyTarget + "',this)" : ''}">
                ${s.running || st === 'idle' ? ipUrl || 'Proxy running' : (st === 'download_mode' ? 'In download mode — flash via RFC2217' : (s.present || st === 'resetting' || st === 'monitoring' ? 'Device present, proxy not running' : (st === 'recovering' ? 'USB unbound — recovering...' : 'No device connected')))}
            </div>
            ${s.last_error ? '<div class="error">Error: ' + s.last_error + '</div>' : ''}
            ${statusMsg}
            ${actionBtns}
        </div>`;
    }).join('');
}


function copyUrl(url, el) {
    navigator.clipboard.writeText(url);
    el.classList.add('copied');
    el.textContent = 'Copied!';
    setTimeout(() => { el.classList.remove('copied'); el.textContent = url; }, 1000);
}

let lastLogTs = '';

async function fetchLog() {
    try {
        const url = lastLogTs ? '/api/log?since=' + encodeURIComponent(lastLogTs) : '/api/log';
        const resp = await fetch(url);
        const data = await resp.json();
        if (data.entries && data.entries.length > 0) {
            const el = document.getElementById('log-entries');
            for (const e of data.entries) {
                const div = document.createElement('div');
                div.className = 'log-entry cat-' + (e.cat || 'info');
                const t = new Date(e.ts);
                const ts = t.toLocaleTimeString();
                div.innerHTML = '<span class="ts">' + ts + '</span> ' + e.msg;
                el.appendChild(div);
                lastLogTs = e.ts;
            }
            el.scrollTop = el.scrollHeight;
        }
    } catch (e) { /* ignore */ }
}

async function enterPortal() {
    const btn = document.getElementById('btn-enter-portal');
    // Find first running slot
    let slotLabel = 'SLOT2';
    try {
        const resp = await fetch('/api/devices');
        const data = await resp.json();
        const running = data.slots.find(s => s.running);
        if (running) slotLabel = running.label;
    } catch (e) { /* use default */ }
    const slot = prompt('Slot to enter captive portal:', slotLabel);
    if (!slot) return;
    btn.disabled = true;
    btn.textContent = 'Running...';
    try {
        await fetch('/api/enter-portal', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({slot: slot})
        });
    } catch (e) {
        alert('Error: ' + e);
    }
    // Re-enable after 30s (operation runs in background)
    setTimeout(() => { btn.disabled = false; btn.textContent = 'Enter Captive Portal'; }, 30000);
}

function clearLog() {
    document.getElementById('log-entries').innerHTML = '';
    lastLogTs = '';
}

async function releaseSlot(label) {
    if (!confirm('Release GPIO and reboot ' + label + ' into firmware?')) return;
    try {
        const resp = await fetch('/api/serial/release', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({slot: label})
        });
        const data = await resp.json();
        if (!data.ok) alert('Release failed: ' + (data.error || 'unknown'));
    } catch (e) { alert('Error: ' + e); }
    refresh();
}

async function testGpio(label) {
    const btn = event && event.target;
    if (btn) { btn.disabled = true; btn.textContent = 'Testing…'; }
    try {
        const resp = await fetch('/api/serial/gpio-test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({slot: label})
        });
        const d = await resp.json();
        if (!d.ok) {
            alert('GPIO test failed: ' + (d.error || 'unknown'));
        } else {
            alert(label + '\\n\\nEN wired:   ' + (d.en_wired ? 'yes' : 'no') +
                  '\\nBOOT wired: ' + (d.boot_wired ? 'yes' : 'no') +
                  '\\n\\n' + (d.detail || ''));
        }
    } catch (e) { alert('Error: ' + e); }
    refresh();
}

async function recoverSlot(label) {
    try {
        const resp = await fetch('/api/serial/recover', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({slot: label})
        });
        const data = await resp.json();
        if (!data.ok) alert('Recovery failed: ' + (data.error || 'unknown'));
    } catch (e) { alert('Error: ' + e); }
    refresh();
}

let humanPending = false;

async function fetchHuman() {
    try {
        const resp = await fetch('/api/human/status');
        const data = await resp.json();
        const overlay = document.getElementById('human-overlay');
        if (data.pending) {
            if (!humanPending) {
                document.getElementById('human-message').textContent = data.message;
                document.getElementById('human-status').textContent = '';
                overlay.classList.add('visible');
            }
            humanPending = true;
        } else {
            if (humanPending) {
                overlay.classList.remove('visible');
                document.getElementById('human-status').textContent = '';
            }
            humanPending = false;
        }
    } catch (e) { /* ignore */ }
}

document.getElementById('btn-human-done').addEventListener('click', async function() {
    const btn = this;
    const statusEl = document.getElementById('human-status');
    btn.disabled = true;
    btn.textContent = 'Sending...';
    statusEl.textContent = '';
    try {
        const resp = await fetch('/api/human/done', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: '{}'
        });
        const data = await resp.json();
        if (data.ok) {
            document.getElementById('human-overlay').classList.remove('visible');
            humanPending = false;
        } else {
            statusEl.textContent = data.error || 'Failed';
        }
    } catch (e) { statusEl.textContent = 'Error: ' + e; }
    btn.disabled = false;
    btn.textContent = 'Done';
});

document.getElementById('btn-human-cancel').addEventListener('click', async function() {
    const btn = this;
    btn.disabled = true;
    try {
        const resp = await fetch('/api/human/cancel', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: '{}'
        });
        const data = await resp.json();
        if (data && data.ok) {
            document.getElementById('human-overlay').classList.remove('visible');
            humanPending = false;
        }
    } catch (e) { /* ignore */ }
    btn.disabled = false;
});

async function fetchTestProgress() {
    try {
        const resp = await fetch('/api/test/progress');
        const data = await resp.json();

        if (!data.active) {
            document.getElementById('test-header').textContent = 'No test session active';
            document.getElementById('test-bar').style.width = '0%';
            document.getElementById('test-bar-label').textContent = '';
            document.getElementById('test-counter').textContent = '';
            document.getElementById('test-current').style.display = 'none';
            document.getElementById('test-results').innerHTML = '';
            return;
        }

        document.getElementById('test-header').textContent = data.spec + ' — ' + data.phase;
        const done = data.completed.length;
        const pct = data.total > 0 ? Math.round(done / data.total * 100) : 0;
        document.getElementById('test-bar').style.width = pct + '%';
        document.getElementById('test-bar-label').textContent = done + ' / ' + data.total + '  (' + pct + '%)';
        const passed = data.completed.filter(r => r.result === 'PASS').length;
        const failed = data.completed.filter(r => r.result === 'FAIL').length;
        const skipped = data.completed.filter(r => r.result === 'SKIP').length;
        let counterParts = [done + ' / ' + data.total + ' completed'];
        if (passed) counterParts.push(passed + ' passed');
        if (failed) counterParts.push(failed + ' failed');
        if (skipped) counterParts.push(skipped + ' skipped');
        document.getElementById('test-counter').textContent = counterParts.join(' — ');
        const bar = document.getElementById('test-bar');
        if (failed > 0) {
            bar.style.background = 'linear-gradient(90deg, #8b1a1a, #dc3545)';
        } else {
            bar.style.background = 'linear-gradient(90deg, #1a6b2a, #28a745)';
        }

        const cur = document.getElementById('test-current');
        if (data.current) {
            cur.style.display = '';
            cur.className = 'test-current' + (data.current.manual ? ' manual' : '');
            cur.innerHTML = '<div class="test-id">' + data.current.id + ': ' + data.current.name + '</div>'
                + '<div class="test-step">' + data.current.step + '</div>';
        } else {
            cur.style.display = 'none';
        }

        const res = document.getElementById('test-results');
        res.innerHTML = data.completed.slice().reverse().map(function(r) {
            return '<div class="test-result"><span class="badge ' + r.result.toLowerCase() + '">'
                + r.result + '</span><span>' + r.id + ': ' + r.name + '</span>'
                + (r.details ? '<span style="color:#666"> — ' + r.details + '</span>' : '')
                + '</div>';
        }).join('');
    } catch (e) { /* ignore */ }
}

async function fetchSiggen() {
    try {
        const resp = await fetch('/api/siggen/status');
        const d = await resp.json();
        const hw = d.hardware || {};
        const fmt = (ok, name) => '<span class="' + (ok ? 'ok' : 'off') + '">' + name + (ok ? ' ✓' : ' ✗') + '</span>';
        document.getElementById('siggen-hw').innerHTML =
            'Hardware: ' + fmt(hw.si5351, 'Si5351') + ' &nbsp; ' + fmt(hw.gpclk, 'GPCLK') + ' &nbsp; ' + fmt(hw.pe4302, 'PE4302');
        let state = 'idle';
        if (d.active) {
            state = d.backend + ' @ ' + (d.freq_hz/1e6).toFixed(6) + ' MHz';
            if (d.channel !== null && d.channel !== undefined) state += ' (CLK' + d.channel + ')';
            if (d.pin !== null && d.pin !== undefined) state += ' (GPIO' + d.pin + ')';
            if (d.atten_db !== null) state += ' · ' + d.atten_db + ' dB';
            if (d.morse) state += ' · Morse: "' + d.morse.message + '" @ ' + d.morse.wpm + ' WPM';
        }
        document.getElementById('siggen-state').textContent = state;
        if (d.atten_db !== null && !document.activeElement.matches('#siggen-atten-slider')) {
            document.getElementById('siggen-atten-slider').value = d.atten_db;
            document.getElementById('siggen-atten-val').textContent = d.atten_db.toFixed(1) + ' dB';
        }
    } catch (e) { /* ignore */ }
}

async function siggenStart() {
    const freq = parseFloat(document.getElementById('siggen-freq').value);
    const backend = document.getElementById('siggen-backend').value;
    const msg = document.getElementById('siggen-morse-msg').value.trim();
    const wpm = parseInt(document.getElementById('siggen-morse-wpm').value) || 15;
    const body = {freq_hz: freq, backend: backend};
    if (msg) body.morse = {message: msg, wpm: wpm, repeat: true};
    try {
        await fetch('/api/siggen/start', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)});
        fetchSiggen();
    } catch (e) { /* ignore */ }
}

async function siggenStop() {
    try {
        await fetch('/api/siggen/stop', {method: 'POST'});
        fetchSiggen();
    } catch (e) { /* ignore */ }
}

function siggenAttenPreview(val) {
    document.getElementById('siggen-atten-val').textContent = parseFloat(val).toFixed(1) + ' dB';
}

async function siggenAtten(val) {
    try {
        await fetch('/api/siggen/atten', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({db: parseFloat(val)})});
        fetchSiggen();
    } catch (e) { /* ignore */ }
}

async function refresh() {
    await Promise.all([fetchDevices(), fetchLog(), fetchHuman(), fetchTestProgress(), fetchSiggen()]);
}
refresh();
setInterval(refresh, 5000);
setInterval(fetchLog, 1500);   // snappier activity log for live operator prompts

// ── SDR Console ──────────────────────────────────────────────────────
let sdrSince = 0, sdrTimer = null, sdrRawBuf = [], sdrAnBuf = [];
let sdrLines = 0, sdrEvents = 0, sdrCmd = '';
let sdrCodeCounts = new Map();   // code -> times seen; chip shown once >=2
let sdrRssiAge = 99;

function sdrLiveApply() { if (sdrTimer) sdrLiveStart(); }   // relaunch if running
function sdrRssiIdle() {
    const el = document.getElementById('sdr-rssi-val');
    el.textContent = '— (no signal)'; el.className = '';
    document.getElementById('sdr-rssi-marker').style.opacity = '0.2';
}

function sdrCopyText(text, el) {   // clipboard API is blocked on plain-HTTP LAN
    let ok = false;
    try {
        const ta = document.createElement('textarea');
        ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        ok = document.execCommand('copy'); document.body.removeChild(ta);
    } catch (e) { ok = false; }
    if (!ok && navigator.clipboard) { try { navigator.clipboard.writeText(text); ok = true; } catch (e) { /* */ } }
    if (el) { el.classList.add('copied'); setTimeout(() => el.classList.remove('copied'), 800); }
}
function sdrCollectCodes(e) {
    const found = [];
    const ev = e.event;
    if (ev) {
        for (const c of ev.codes || []) found.push('' + c);
        for (const r of ev.rows || []) if (r.data && r.len) found.push('{' + r.len + '}' + r.data);
    }
    const m = e.line.match(/codes\\s*:\\s*(.+)/);   // analyze-mode "codes : {18}7f480, ..."
    if (m) for (const t of (m[1].match(/\\{\\d+\\}[0-9a-fA-F]+/g) || [])) found.push(t);
    let changed = false;
    for (const code of found) {
        const bm = code.match(/^\\{(\\d+)\\}/);
        if (!bm || parseInt(bm[1]) < 8) continue;
        const n = (sdrCodeCounts.get(code) || 0) + 1;
        sdrCodeCounts.set(code, n);
        if (n >= 2) changed = true;   // crossed / updated the display threshold
    }
    return changed;
}
function sdrRenderCodes() {
    const box = document.getElementById('sdr-codes');
    box.innerHTML = '';
    const shown = [...sdrCodeCounts.entries()].filter(e => e[1] >= 2).sort((a, b) => b[1] - a[1]);
    for (const [code, n] of shown) {
        const chip = document.createElement('span');
        chip.className = 'codechip';
        chip.textContent = code + '  \\u00d7' + n;
        chip.title = 'click to copy ' + code;
        chip.onclick = () => sdrCopyText(code, chip);
        box.appendChild(chip);
    }
}

const SDR_PRESETS = {
    analyze433: { bands: ['433920000'], mode: 'analyze', agc: true },
    decode433:  { bands: ['433920000'], mode: 'decode', agc: true },
    scanall:    { bands: ['433920000', '315000000', '868300000'], mode: 'decode', agc: true }
};
function sdrPreset(name) {
    const p = SDR_PRESETS[name];
    if (!p) return;
    document.querySelectorAll('.sdr-band').forEach(b => { b.checked = p.bands.includes(b.value); });
    document.getElementById('sdr-mode').value = p.mode;
    sdrModeChange();
    document.getElementById('sdr-agc').checked = !!p.agc;
    sdrToggleAgc(false);
    if (p.flex) document.getElementById('sdr-flex').value = p.flex;
    if (p.gain !== undefined) document.getElementById('sdr-gain').value = p.gain;
    sdrLiveStart();
}
function sdrToggleAgc(apply) {
    const agc = document.getElementById('sdr-agc').checked;
    document.getElementById('sdr-gain').disabled = agc;
    document.getElementById('sdr-gain-val').textContent =
        agc ? 'auto' : document.getElementById('sdr-gain').value + ' dB';
    if (apply !== false) sdrLiveApply();
}
function sdrModeChange() {
    const m = document.getElementById('sdr-mode').value;
    document.getElementById('sdr-flex-row').style.display = (m === 'flex') ? 'flex' : 'none';
}
function sdrView(v) {
    for (const t of ['table', 'analyzer', 'raw']) {
        document.getElementById('sdr-view-' + t).style.display = (t === v) ? '' : 'none';
        document.getElementById('sdr-tab-' + t).classList.toggle('active', t === v);
    }
}
function sdrUpdateRssi(ev) {
    const r = ev.rssi;
    if (r === undefined || r === null) return;
    const pos = Math.max(0, Math.min(1, (r + 40) / 40)) * 100;
    const marker = document.getElementById('sdr-rssi-marker');
    marker.style.left = pos + '%'; marker.style.opacity = '1';
    sdrRssiAge = 0;
    let txt = r.toFixed(1) + ' dB';
    if (ev.snr !== undefined && ev.snr !== null) txt += '  SNR ' + ev.snr.toFixed(0);
    const el = document.getElementById('sdr-rssi-val');
    el.className = '';
    if (r > -3) { txt += '  TOO STRONG'; el.className = 'sdr-strong'; }
    else if (r < -30) { txt += '  weak'; el.className = 'sdr-weak'; }
    el.textContent = txt;
}
function sdrAddRow(ev) {
    const tb = document.getElementById('sdr-tbody');
    const tr = document.createElement('tr');
    const t = (ev.time || '').replace('T', ' ').substring(11);
    const freq = ev.freq ? (+ev.freq).toFixed(3) : '';
    const data = ev.data || (ev.codes ? ev.codes.join(' ') :
        (ev.rows ? ev.rows.map(r => r.data).join(' ') : ''));
    const cells = [t, freq, ev.rssi != null ? (+ev.rssi).toFixed(1) : '',
        ev.snr != null ? (+ev.snr).toFixed(0) : '', ev.mod || '', ev.model || ''];
    for (const c of cells) { const td = document.createElement('td'); td.textContent = c; tr.appendChild(td); }
    const td = document.createElement('td'); td.className = 'data'; td.textContent = data; tr.appendChild(td);
    tb.insertBefore(tr, tb.firstChild);
    while (tb.children.length > 200) tb.removeChild(tb.lastChild);
}
async function sdrLiveStart() {
    const bands = [...document.querySelectorAll('.sdr-band:checked')].map(b => parseInt(b.value));
    if (!bands.length) { document.getElementById('sdr-state').textContent = 'pick at least one band'; return; }
    const mode = document.getElementById('sdr-mode').value;
    const body = { freqs: bands, mode: mode,
        hop_interval: parseInt(document.getElementById('sdr-hop').value) || 5,
        squelch: document.getElementById('sdr-squelch').checked };
    if (!document.getElementById('sdr-agc').checked)
        body.gain = parseFloat(document.getElementById('sdr-gain').value);
    if (mode === 'flex') {
        body.flex = document.getElementById('sdr-flex').value;
        body.isolate = document.getElementById('sdr-isolate').checked;
    }
    const resp = await fetch('/api/sdr/live/start', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const d = await resp.json();
    const st = document.getElementById('sdr-state');
    if (!d.ok) { st.textContent = 'error: ' + d.error; return; }
    sdrSince = 0; sdrRawBuf = []; sdrAnBuf = [];
    document.getElementById('sdr-tbody').innerHTML = '';
    document.getElementById('sdr-view-analyzer').textContent = '';
    document.getElementById('sdr-view-raw').textContent = '';
    sdrCmd = d.config ? d.config.cmd : '';
    st.textContent = 'LIVE — ' + sdrCmd;
    sdrView(mode === 'analyze' ? 'analyzer' : 'table');   // show the right view
    sdrLines = 0; sdrEvents = 0; sdrCodeCounts.clear(); sdrRenderCodes();
    if (sdrTimer) clearInterval(sdrTimer);
    sdrTimer = setInterval(sdrPoll, 500);
}
async function sdrLiveStop() {
    if (sdrTimer) { clearInterval(sdrTimer); sdrTimer = null; }
    try { await fetch('/api/sdr/live/stop', { method: 'POST' }); } catch (e) { /* */ }
    document.getElementById('sdr-state').textContent = 'idle';
}
let sdrLogging = false;
async function sdrSherlock(btn) {
    const st = document.getElementById('sdr-log-state');
    if (!sdrLogging) {
        // AI Sherlock always records in analyze + AGC (the settings the
        // reverse-engineering step needs), then logs the key presses.
        document.getElementById('sdr-mode').value = 'analyze'; sdrModeChange();
        document.getElementById('sdr-agc').checked = true; sdrToggleAgc(false);
        await sdrLiveStart();
        try { await fetch('/api/sdr/log/start', { method: 'POST' }); } catch (e) { /* */ }
        sdrLogging = true;
        btn.innerHTML = '\\u25cf Stop &amp; Analyze';
        btn.classList.add('recording');
        st.textContent = 'recording — press each key a few times, then Stop';
    } else {
        let d = {};
        try { d = await (await fetch('/api/sdr/log/stop', { method: 'POST' })).json(); } catch (e) { /* */ }
        sdrLogging = false;
        btn.innerHTML = '\\ud83d\\udd0d AI Sherlock';
        btn.classList.remove('recording');
        st.textContent = 'recorded ' + (d.count || 0) + ' lines — ask Claude to analyze the log';
    }
}
async function sdrReset() {
    if (sdrTimer) { clearInterval(sdrTimer); sdrTimer = null; }
    const st = document.getElementById('sdr-state');
    st.textContent = 'resetting dongle…';
    try {
        const d = await (await fetch('/api/sdr/reset', { method: 'POST' })).json();
        st.textContent = d.ok ? 'dongle reset OK — available: ' + d.available
                              : 'reset failed: ' + d.error;
    } catch (e) { st.textContent = 'reset error'; }
}
async function sdrPoll() {
    try {
        const resp = await fetch('/api/sdr/live?since=' + sdrSince);
        const d = await resp.json();
        if (!d.ok) return;
        if (!d.live && sdrTimer) {
            clearInterval(sdrTimer); sdrTimer = null;
            document.getElementById('sdr-state').textContent = 'stopped (rtl_433 exited)';
        }
        let newCode = false;
        for (const e of d.events) {
            sdrSince = e.seq;
            sdrRawBuf.push(e.line);
            sdrLines++;
            if (sdrCollectCodes(e)) newCode = true;
            const ev = e.event;
            if (ev) {
                sdrUpdateRssi(ev);
                if (!ev.analyzer) { sdrAddRow(ev); sdrEvents++; }
            } else {
                sdrAnBuf.push(e.line);
            }
        }
        if (newCode) sdrRenderCodes();
        // RSSI is burst-driven: if no burst reported for ~2.5s, show "no signal"
        // instead of freezing on the last value.
        sdrRssiAge++;
        if (sdrRssiAge >= 5) sdrRssiIdle();
        if (d.live) {
            const st = document.getElementById('sdr-state');
            st.textContent = 'LIVE · rx ' + sdrLines + ' lines · ' + sdrEvents + ' decoded';
            st.title = sdrCmd;
        }
        if (sdrRawBuf.length > 400) sdrRawBuf = sdrRawBuf.slice(-400);
        if (sdrAnBuf.length > 400) sdrAnBuf = sdrAnBuf.slice(-400);
        const av = document.getElementById('sdr-view-analyzer');
        const rv = document.getElementById('sdr-view-raw');
        if (av.style.display !== 'none') { av.textContent = sdrAnBuf.join('\\n'); av.scrollTop = av.scrollHeight; }
        if (rv.style.display !== 'none') { rv.textContent = sdrRawBuf.join('\\n'); rv.scrollTop = rv.scrollHeight; }
    } catch (e) { /* ignore */ }
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global slots, host_ip, hostname

    slots = load_config(CONFIG_FILE)

    # A measurement that does not survive a restart is an assumption again by
    # morning. Reload what was measured, keyed by slot label.
    _wiring = _load_gpio_wiring()
    for _s in slots.values():
        _rec = _wiring.get(_s.get("label") or "")
        if _rec:
            _s["gpio_wired"] = _rec.get("gpio_wired")
            _s["gpio_wired_detail"] = (
                f"{_rec.get('detail', '')} (measured {_rec.get('checked_at')})")

    host_ip = get_host_ip()
    hostname = get_hostname()

    # Pre-compute URLs for pre-configured slots
    for slot in slots.values():
        if slot["tcp_port"]:
            slot["url"] = f"rfc2217://{host_ip}:{slot['tcp_port']}"

    # Scan for devices already plugged in at boot (auto-assigns ports)
    scan_existing_devices()

    # Initial USB device scan for all slots
    _refresh_all_usb_devices()

    # Load debug probe configuration from global config
    debug_controller.load_probes(_global_config.get("debug_probes", []))

    # Start UDP log receiver and discovery beacon
    start_udp_log()
    start_beacon()

    # Ensure firmware directory exists
    os.makedirs(FIRMWARE_DIR, exist_ok=True)

    addr = ("", PORT)
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    httpd = http.server.ThreadingHTTPServer(addr, Handler)
    print(
        f"[portal] v5 listening on http://0.0.0.0:{PORT}  "
        f"host_ip={host_ip}  hostname={hostname}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[portal] shutting down", flush=True)
        debug_controller.shutdown()
        if _siggen is not None:
            _siggen.shutdown()
        _udp_shutdown.set()
        _beacon_shutdown.set()
        wifi_controller.shutdown()
        if ble_controller:
            ble_controller.shutdown()
        # Stop all running proxies
        for slot in slots.values():
            if slot["running"] and slot["pid"]:
                stop_proxy(slot)
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main() or 0)
