"""GDB debug manager — OpenOCD lifecycle, chip auto-detection, probe allocation.

Manages OpenOCD processes for remote GDB debugging of ESP32 devices.
Supports three modes:
  - USB JTAG (FR-024): built-in USB-Serial/JTAG on C3/S3/C6/H2
  - Dual-USB (FR-025): S3 with both USB ports connected
  - ESP-Prog (FR-026): external FT2232H probe for all ESP32 variants
"""

import os
import re
import signal
import socket
import subprocess
import threading
import time

# OpenOCD paths and defaults
OPENOCD_EXE = os.environ.get(
    "OPENOCD_EXE", "/usr/local/bin/openocd-esp32")
OPENOCD_SCRIPTS = os.environ.get(
    "OPENOCD_SCRIPTS", "/usr/local/share/openocd-esp32/scripts")
OPENOCD_START_TIMEOUT = 5.0
OPENOCD_LOG = os.environ.get("OPENOCD_LOG", "/tmp/openocd.log")

# Per-chip OpenOCD board configs (USB JTAG built-in)
BUILTIN_CONFIGS = {
    "esp32c3": "board/esp32c3-builtin.cfg",
    "esp32c6": "board/esp32c6-builtin.cfg",
    "esp32h2": "board/esp32h2-builtin.cfg",
    "esp32s3": "board/esp32s3-builtin.cfg",
}

# Per-chip OpenOCD target configs (for use with external probes)
PROBE_TARGET_CONFIGS = {
    "esp32":   "target/esp32.cfg",
    "esp32c3": "target/esp32c3.cfg",
    "esp32c6": "target/esp32c6.cfg",
    "esp32h2": "target/esp32h2.cfg",
    "esp32s2": "target/esp32s2.cfg",
    "esp32s3": "target/esp32s3.cfg",
}

# JTAG TAP ID → chip mapping (for auto-detection)
TAP_ID_MAP = {
    0x00005C25: "esp32c3",
    0x0000DC25: "esp32c6",
    0x00010C25: "esp32h2",
    0x120034E5: "esp32s3",
}

# Auto-detect order: try most common chips first
BUILTIN_DETECT_ORDER = ["esp32c3", "esp32s3", "esp32c6", "esp32h2"]
PROBE_DETECT_ORDER = ["esp32", "esp32s3", "esp32c3", "esp32c6", "esp32h2", "esp32s2"]

# Every ESP32 with built-in JTAG enumerates as 303a:1001, so VID:PID cannot
# tell two of them apart.  OpenOCD, told nothing else, opens whichever one
# libusb hands it first — which is not necessarily the slot that asked, and
# may well be a device another session is already driving (observed: a
# detection meant for one slot seized a second board mid-session and produced
# LIBUSB_ERROR_IO on its bulk transfers).  The slot's USB topology is the
# only stable name for "the board in *this* socket", so pass it every time.
def _usb_location(usb_prefix: str | None) -> str | None:
    """Slot usb_prefix ("0:1.1", udev-style) → OpenOCD location ("1-1.1").

    udev numbers buses from 0, the kernel from 1; the port path is shared.
    """
    if not usb_prefix or ":" not in usb_prefix:
        return None
    bus, port_path = usb_prefix.split(":", 1)
    try:
        return f"{int(bus) + 1}-{port_path}"
    except ValueError:
        return None


def tcl_port_for(gdb_port: int) -> int:
    """The TCL port belonging to a slot's GDB port.

    OpenOCD listens on three ports and the portal only ever assigned two.
    The third defaulted to 6666 in every session, so the *second* concurrent
    session anywhere on the bench died with "couldn't bind tcl to socket on
    port 6666: Address already in use" — a bench-wide limit of one debugger,
    imposed by a number nobody had declared.
    """
    return 6666 + (gdb_port - 3333)


# Scratch ports for detection runs, clear of every slot's assigned block.
# Detection never serves a client, so it only needs ports it can bind.
_DETECT_PORTS = (3390, 4490, 6690)

# OpenOCD reports the scan-chain result two ways: as the expected TAP when
# the config matches, and as UNEXPECTED when it does not.  Either names the
# silicon, so read both.
_TAP_ID_RE = re.compile(
    r"(?:tap/device found|UNEXPECTED):\s*(0x[0-9a-fA-F]{8})")


def _tap_id_in(output: str) -> str | None:
    """The chip named by any TAP ID OpenOCD reported, or None."""
    for match in _TAP_ID_RE.finditer(output):
        chip = TAP_ID_MAP.get(int(match.group(1), 16))
        if chip:
            return chip
    return None


def detect_chip(probe: dict | None = None,
                usb_prefix: str | None = None) -> str | None:
    """Auto-detect chip type by trying OpenOCD configs until one succeeds.

    Tries each config in sequence. OpenOCD fails fast (~1-2s) on TAP
    mismatch, so this typically completes within a few seconds.

    Args:
        probe: Probe config dict (for ESP-Prog mode). None for USB JTAG.
        usb_prefix: The asking slot's ``_usb_prefix``. Without it a
            built-in-JTAG detection may examine another slot's board.

    Returns:
        Chip name (e.g. "esp32c3") or None if no chip detected.
    """
    if probe:
        configs = PROBE_DETECT_ORDER
    else:
        configs = BUILTIN_DETECT_ORDER

    location = _usb_location(usb_prefix)
    gdb_p, telnet_p, tcl_p = _DETECT_PORTS

    for chip in configs:
        cmd = [OPENOCD_EXE, "-s", OPENOCD_SCRIPTS]

        if probe:
            # Unbind FTDI for each attempt
            bus_port = probe.get("bus_port")
            if bus_port:
                _unbind_ftdi_interface(bus_port)
            cmd += ["-f", probe["interface_config"]]
            cmd += ["-f", PROBE_TARGET_CONFIGS[chip]]
        else:
            if location:
                cmd += ["-c", f"adapter usb location {location}"]
            cmd += ["-f", BUILTIN_CONFIGS[chip]]

        # Detection must not squat on a live session's ports either.
        cmd += ["-c", f"gdb port {gdb_p}"]
        cmd += ["-c", f"telnet port {telnet_p}"]
        cmd += ["-c", f"tcl port {tcl_p}"]
        cmd += ["-c", "init", "-c", "shutdown"]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            if "Examination succeed" in output or "Examined" in output:
                print(f"[debug] auto-detect: {chip} (matched)", flush=True)
                return chip

            # The scan chain answers before the core is examined, and its
            # TAP ID names the silicon outright (§24.3).  Examination can
            # still fail afterwards — "Unexpected read of fp via program
            # buffer" on a RISC-V core that is busy — and the old code read
            # that as "not this chip", tried the three configs that cannot
            # match, and reported "check JTAG wiring" about a board whose
            # identity it had already read correctly.
            found = _tap_id_in(output)
            if found:
                print(f"[debug] auto-detect: {found} at "
                      f"{location or 'any'} (by TAP ID)", flush=True)
                return found

            # A detection that finds nothing is not self-explanatory: the
            # cause is in OpenOCD's output, and without it every failure
            # reads as "check JTAG wiring" whatever actually happened.
            why = [ln for ln in output.splitlines()
                   if "Error:" in ln or "Warn :" in ln]
            print(f"[debug] auto-detect: {chip} at {location or 'any'} "
                  f"no — {'; '.join(why[:2]) or 'no error reported'}",
                  flush=True)
        except subprocess.TimeoutExpired:
            print(f"[debug] auto-detect: {chip} at {location or 'any'} "
                  f"timed out after 8 s", flush=True)
            continue
        except Exception as e:
            print(f"[debug] auto-detect: {chip} at {location or 'any'} "
                  f"raised {e}", flush=True)
            continue

    print("[debug] auto-detect: no chip matched", flush=True)
    return None


# Espressif USB VID — chips with built-in USB JTAG expose this
_ESPRESSIF_VID = "303a"


def _slot_has_builtin_jtag(usb_devices: list[dict]) -> bool:
    """True if any USB device on this slot is an Espressif JTAG interface."""
    for d in usb_devices:
        vid = d.get("vid_pid", "").split(":")[0]
        product = d.get("product", "")
        if vid == _ESPRESSIF_VID and "JTAG" in product:
            return True
    return False


def detect_slot_jtag(
    slot_label: str,
    usb_devices: list[dict],
    probe_slot_map: dict[str, str] | None = None,
    usb_prefix: str | None = None,
) -> dict:
    """Detect chip type and JTAG source for a specific slot.

    Checks in order:
      1. Built-in USB JTAG on this slot (Espressif VID + JTAG product)
      2. Each available ESP-Prog probe (verifies wiring by probing)
      3. No JTAG available

    Args:
        slot_label: The DUT slot label (e.g. "SLOT3").
        usb_devices: The slot's ``_usb_devices`` list from portal.
        probe_slot_map: Maps probe label → slot label where the probe
            lives (e.g. ``{"PROBE1": "SLOT1"}``).  Used to report the
            JTAG source as a slot, not a probe name.
        usb_prefix: The slot's ``_usb_prefix``, naming which physical
            socket to examine.  Omitting it on a bench holding two
            built-in-JTAG boards reports the *other* board's chip.

    Returns:
        ``{"chip": "esp32s3", "jtag_slot": "SLOT3"}``  — built-in JTAG
        ``{"chip": "esp32",   "jtag_slot": "SLOT1"}``  — via probe on SLOT1
        ``{"chip": None,      "jtag_slot": None}``     — no JTAG found
    """
    if probe_slot_map is None:
        probe_slot_map = {}
    no_jtag: dict = {"chip": None, "jtag_slot": None}

    # 1. Built-in USB JTAG on this slot?
    if _slot_has_builtin_jtag(usb_devices):
        chip = detect_chip(probe=None, usb_prefix=usb_prefix)
        if chip:
            print(f"[debug] {slot_label}: built-in JTAG → {chip}",
                  flush=True)
            return {"chip": chip, "jtag_slot": slot_label}
        print(f"[debug] {slot_label}: has JTAG USB but detect failed",
              flush=True)

    # 2. Try each available probe
    with _lock:
        available = [
            (label, dict(info))  # copy so we can release lock
            for label, info in _probes.items()
            if not info.get("in_use")
        ]

    for probe_label, probe_info in available:
        chip = detect_chip(probe=probe_info)
        if chip:
            jtag_slot = probe_slot_map.get(probe_label, probe_label)
            print(f"[debug] {slot_label}: probe {probe_label} "
                  f"on {jtag_slot} → {chip}", flush=True)
            return {"chip": chip, "jtag_slot": jtag_slot,
                    "probe": probe_label}

    # 3. Nothing worked
    print(f"[debug] {slot_label}: no JTAG source found", flush=True)
    return no_jtag


# Module state
_lock = threading.Lock()
_sessions: dict[str, dict] = {}   # slot_label → session info
_probes: dict[str, dict] = {}     # probe_label → probe state


def _is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a TCP port is accepting connections."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect((host, port))
        s.close()
        return True
    except (ConnectionRefusedError, OSError):
        return False


def _wait_for_port(port: int, timeout: float = OPENOCD_START_TIMEOUT) -> bool:
    """Poll until a TCP port is listening or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_port_listening(port):
            return True
        time.sleep(0.2)
    return False


def _kill_process(pid: int, timeout: float = 5.0):
    """Send SIGTERM and wait, then SIGKILL if needed."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)  # check if alive
            time.sleep(0.2)
        except ProcessLookupError:
            return
    # Force kill
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _unbind_ftdi_interface(bus_port: str):
    """Unbind an FTDI interface from ftdi_sio kernel driver."""
    unbind_path = "/sys/bus/usb/drivers/ftdi_sio/unbind"
    try:
        with open(unbind_path, "w") as f:
            f.write(bus_port)
    except OSError as e:
        # May already be unbound
        print(f"[debug] unbind {bus_port}: {e}", flush=True)


def _rebind_ftdi_interface(bus_port: str):
    """Rebind an FTDI interface to ftdi_sio kernel driver."""
    bind_path = "/sys/bus/usb/drivers/ftdi_sio/bind"
    try:
        with open(bind_path, "w") as f:
            f.write(bus_port)
    except OSError as e:
        print(f"[debug] rebind {bus_port}: {e}", flush=True)


# ── Public API ───────────────────────────────────────────────────────

def start(slot_label: str, slot: dict, gdb_port: int, telnet_port: int,
          chip: str | None = None, probe: str | None = None) -> dict:
    """Start OpenOCD debug session for a slot.

    Args:
        slot_label: Slot label (e.g. "SLOT1")
        slot: Slot dict from portal (must have 'present', 'state', etc.)
        gdb_port: TCP port for GDB RSP
        telnet_port: TCP port for OpenOCD telnet
        chip: Chip type (e.g. "esp32c3"). If None, must be auto-detected.
        probe: Probe label for ESP-Prog mode. If None, uses USB JTAG.

    Returns:
        Result dict with ok, gdb_port, telnet_port, chip, etc.
    """
    with _lock:
        if slot_label in _sessions:
            return {"ok": False,
                    "error": f"{slot_label} already in debug mode"}

        if not slot.get("present"):
            return {"ok": False,
                    "error": f"no device in {slot_label}"}

        # Auto-detect chip if not specified
        if not chip:
            probe_info = _probes.get(probe) if probe else None
            # Same slot, same board: detection must look where the session
            # will run, or it examines a neighbour — and finds nothing when
            # that neighbour is already held by its own session.
            chip = detect_chip(probe_info,
                               usb_prefix=slot.get("_usb_prefix"))
            if not chip:
                return {"ok": False,
                        "error": "could not auto-detect chip type — "
                                 "specify chip manually or check JTAG wiring"}

        # Build OpenOCD command
        cmd = [OPENOCD_EXE, "-s", OPENOCD_SCRIPTS]

        if probe:
            # ESP-Prog mode (FR-026)
            probe_info = _probes.get(probe)
            if not probe_info:
                return {"ok": False, "error": f"probe '{probe}' not found"}
            if probe_info.get("in_use"):
                return {"ok": False,
                        "error": f"probe '{probe}' already in use by "
                                 f"{probe_info.get('slot')}"}
            if chip not in PROBE_TARGET_CONFIGS:
                return {"ok": False,
                        "error": f"unsupported chip '{chip}' for probe"}

            # Unbind FTDI channel A
            bus_port = probe_info.get("bus_port")
            if bus_port:
                _unbind_ftdi_interface(bus_port)

            cmd += ["-f", probe_info["interface_config"]]
            cmd += ["-f", PROBE_TARGET_CONFIGS[chip]]

            # Mark probe as in use
            probe_info["in_use"] = True
            probe_info["slot"] = slot_label
        else:
            # USB JTAG mode (FR-024/025)
            if chip not in BUILTIN_CONFIGS:
                supported = ", ".join(sorted(BUILTIN_CONFIGS.keys()))
                return {"ok": False,
                        "error": f"chip '{chip}' has no USB JTAG — "
                                 f"supported: {supported}"}
            location = _usb_location(slot.get("_usb_prefix"))
            if location:
                cmd += ["-c", f"adapter usb location {location}"]
            cmd += ["-f", BUILTIN_CONFIGS[chip]]

        cmd += ["-c", f"gdb port {gdb_port}"]
        cmd += ["-c", f"telnet port {telnet_port}"]
        cmd += ["-c", f"tcl port {tcl_port_for(gdb_port)}"]
        cmd += ["-c", "bindto 0.0.0.0"]

        # Launch OpenOCD. stdout goes to a file, never a PIPE: a pipe
        # nobody drains fills up (~64KB) and then blocks the daemon in
        # write(), silently wedging it — the failure mode that froze
        # dnsmasq/hostapd in wifi_controller on a live bench 2026-07-18,
        # and OpenOCD is just as chatty across long GDB sessions.
        try:
            logf = open(OPENOCD_LOG, "ab")
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                )
            finally:
                logf.close()  # child keeps its own duplicated fd
        except FileNotFoundError:
            return {"ok": False,
                    "error": f"openocd not found at {OPENOCD_EXE}"}
        except Exception as e:
            return {"ok": False, "error": f"failed to start openocd: {e}"}

        # Wait for GDB port to open
        if not _wait_for_port(gdb_port):
            # Read the log tail for error diagnosis
            output = ""
            try:
                proc.kill()
                with open(OPENOCD_LOG, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    f.seek(max(0, f.tell() - 500))
                    output = f.read().decode(errors="replace")
            except Exception:
                pass
            if probe:
                _probes[probe]["in_use"] = False
                _probes[probe]["slot"] = None
            return {"ok": False,
                    "error": f"openocd failed to start: {output.strip()}"}

        _sessions[slot_label] = {
            "pid": proc.pid,
            "process": proc,
            "chip": chip,
            "gdb_port": gdb_port,
            "telnet_port": telnet_port,
            "probe": probe,
        }

        return {
            "ok": True,
            "slot": slot_label,
            "chip": chip,
            "gdb_port": gdb_port,
            "telnet_port": telnet_port,
            "probe": probe,
            "gdb_target": f"target extended-remote "
                          f"{_bench_ip()}:{gdb_port}",
        }


def _bench_ip() -> str:
    """The address a client can actually reach this bench on.

    This used to hand back an mDNS name, which is unreachable from the
    place most callers run: a container. mDNS does not cross the Docker
    bridge, so the portal was answering a debug-start request with a GDB
    command line that could not connect, and the failure surfaced as GDB
    timing out rather than as a name that never resolved.
    """
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
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("8.8.8.8", 80))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except Exception:
        return "127.0.0.1"


def stop(slot_label: str) -> dict:
    """Stop OpenOCD debug session for a slot, leaving the chip running.

    Killing the debugger is not the same as detaching from it. This used to
    kill OpenOCD and nothing else, so whatever state the session left behind
    — a halted core, a software breakpoint, the chip's debug module still
    flagged — outlived the session that caused it.

    The consequence lands on a *later* test and accuses the wrong thing.
    ESP-IDF's panic handler is OCD-aware: when it believes a debugger is
    attached it prints "Setting breakpoint at 0x… and returning" and returns
    instead of rebooting, so the debugger can catch the fault. With no
    debugger left to catch it the board panics again immediately and spins,
    watchdog-resetting, until something reflashes it. WT-1800 then flashes
    firmware, waits 20 s for a marker the board prints every 500 ms, sees
    only Guru Meditation errors, and reports a flash failure — three test
    classes after the session that actually did it.

    So resume first, then kill: `reset run` over the session's own telnet
    port. Best-effort with a short timeout, because a wedged OpenOCD must
    still be killable — a stop that can hang is worse than one that leaves
    a board halted.
    """
    with _lock:
        session = _sessions.pop(slot_label, None)
        if not session:
            return {"ok": True}  # idempotent

        try:
            _openocd_command(session["telnet_port"], "reset run", timeout=5)
        except Exception as e:
            print(f"[debug] {slot_label}: could not resume before stop: {e}",
                  flush=True)

        _kill_process(session["pid"])

        # Release probe if used
        probe_label = session.get("probe")
        if probe_label and probe_label in _probes:
            _probes[probe_label]["in_use"] = False
            _probes[probe_label]["slot"] = None
            # Rebind FTDI channel A
            bus_port = _probes[probe_label].get("bus_port")
            if bus_port:
                _rebind_ftdi_interface(bus_port)

        return {"ok": True, "slot": slot_label}


def status() -> dict:
    """Return debug state for all slots."""
    with _lock:
        result = {}
        for label, session in _sessions.items():
            result[label] = {
                "debugging": True,
                "chip": session["chip"],
                "gdb_port": session["gdb_port"],
                "telnet_port": session["telnet_port"],
                "pid": session["pid"],
                "probe": session.get("probe"),
            }
        return result


def is_debugging(slot_label: str) -> bool:
    """Check if a slot is in debug mode."""
    with _lock:
        return slot_label in _sessions


def _openocd_command(telnet_port: int, command: str,
                     timeout: float = 3.0) -> str:
    """Send a command to OpenOCD via its telnet interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", telnet_port))
        time.sleep(0.3)
        s.recv(4096)  # consume banner/negotiation
        s.sendall(f"{command}\n".encode())
        time.sleep(1.0)
        data = s.recv(4096).decode("latin-1", errors="replace")
        return data.strip()
    finally:
        s.close()


def jtag_reset(slot_label: str, halt: bool = False) -> dict:
    """Reset the device via JTAG (no USB re-enumeration).

    Args:
        slot_label: Slot label or truncated key.
        halt: If True, halt after reset. If False, run normally.

    Returns:
        Result dict with ok, output.
    """
    with _lock:
        session = _sessions.get(slot_label)
        if not session:
            return {"ok": False, "error": "no debug session active"}
        telnet_port = session["telnet_port"]

    cmd = "reset halt" if halt else "reset run"
    try:
        output = _openocd_command(telnet_port, cmd)
        return {"ok": True, "method": "jtag", "command": cmd,
                "output": output}
    except Exception as e:
        return {"ok": False, "error": f"JTAG reset failed: {e}"}


def jtag_halt(slot_label: str) -> dict:
    """Halt the CPU via JTAG (stops execution immediately).

    Returns:
        Result dict with ok, output.
    """
    with _lock:
        session = _sessions.get(slot_label)
        if not session:
            return {"ok": False, "error": "no debug session active"}
        telnet_port = session["telnet_port"]

    try:
        output = _openocd_command(telnet_port, "halt")
        return {"ok": True, "method": "jtag", "output": output}
    except Exception as e:
        return {"ok": False, "error": f"JTAG halt failed: {e}"}


def get_probes() -> list[dict]:
    """Return list of configured debug probes."""
    with _lock:
        return [
            {
                "label": label,
                "type": info.get("type", "unknown"),
                "in_use": info.get("in_use", False),
                "slot": info.get("slot"),
            }
            for label, info in _probes.items()
        ]


def load_probes(probe_configs: list[dict]):
    """Load probe configuration from slots.json."""
    with _lock:
        for cfg in probe_configs:
            label = cfg["label"]
            _probes[label] = {
                "type": cfg.get("type", "esp-prog"),
                "interface_config": cfg.get(
                    "interface_config",
                    "interface/ftdi/esp_ftdi.cfg"),
                "usb_serial": cfg.get("usb_serial"),
                "bus_port": cfg.get("bus_port"),
                "in_use": False,
                "slot": None,
            }
        if _probes:
            labels = ", ".join(_probes.keys())
            print(f"[debug] loaded {len(_probes)} probe(s): {labels}",
                  flush=True)


def shutdown():
    """Stop all debug sessions — called on portal shutdown."""
    with _lock:
        for label in list(_sessions.keys()):
            session = _sessions.pop(label)
            _kill_process(session["pid"])
            probe_label = session.get("probe")
            if probe_label and probe_label in _probes:
                _probes[probe_label]["in_use"] = False
                _probes[probe_label]["slot"] = None
