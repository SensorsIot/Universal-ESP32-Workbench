"""
MQTT Controller — manages a mosquitto broker for ESP32 MQTT client testing.

Used by the portal to start/stop a local MQTT broker accessible to devices
on the testbench WiFi AP.
"""

import logging
import os
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_PORT = 1883
WORK_DIR = "/tmp/mqtt-tester"
MOSQUITTO_CONF = os.path.join(WORK_DIR, "mosquitto.conf")
MOSQUITTO_LOG = os.path.join(WORK_DIR, "mosquitto.log")

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_active = False
_proc = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_work_dir():
    os.makedirs(WORK_DIR, exist_ok=True)


def _kill_proc(proc, timeout=5.0):
    """Terminate a subprocess, SIGKILL if it won't die."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


def _kill_existing():
    """Kill a previously-started *test* broker, and only that (best effort).

    The pattern is matched against our own config path, not the bare word
    "mosquitto". A broad `pkill -f mosquitto` also kills a system
    `mosquitto.service` or any other broker on the host — a test fixture must
    never take down infrastructure it does not own.

    This reclaims the port after a portal restart, where the old broker is still
    running but `_proc` no longer refers to it.
    """
    try:
        subprocess.run(
            ["pkill", "-f", f"mosquitto -c {MOSQUITTO_CONF}"],
            capture_output=True, timeout=5, check=False,
        )
        time.sleep(0.3)
    except Exception:
        pass


def _port_owner() -> str:
    """Describe what is listening on MQTT_PORT, or '' if nothing is."""
    try:
        out = subprocess.run(
            ["ss", "-tlnp", f"sport = :{MQTT_PORT}"],
            capture_output=True, timeout=5, check=False,
        ).stdout.decode(errors="replace")
    except Exception:
        return ""
    lines = [ln for ln in out.splitlines()[1:] if ln.strip()]
    return lines[0].strip() if lines else ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start():
    """Start the mosquitto MQTT broker. Returns dict with port."""
    global _active, _proc

    with _lock:
        if _active and _proc is not None and _proc.poll() is None:
            return {"port": MQTT_PORT}

        _ensure_work_dir()
        _kill_existing()

        # If something still holds the port it is not ours, so refuse rather than
        # kill it. Silently killing a broker we did not start is how a test run
        # takes down a service somebody else depends on.
        owner = _port_owner()
        if owner:
            raise RuntimeError(
                f"port {MQTT_PORT} is already in use by a broker this service did "
                f"not start; stop it first. Listener: {owner}")

        # Write mosquitto config — open broker, no auth, listen on all interfaces
        conf_lines = [
            f"listener {MQTT_PORT}",
            "allow_anonymous true",
            f"log_dest file {MOSQUITTO_LOG}",
            "log_type all",
        ]
        with open(MOSQUITTO_CONF, "w") as f:
            f.write("\n".join(conf_lines) + "\n")

        # Start mosquitto. stdout goes to a file, never a PIPE: a pipe
        # nobody drains fills up (~64KB) and then blocks the daemon in
        # write(), silently wedging it — the failure mode that froze
        # dnsmasq/hostapd in wifi_controller on a live bench 2026-07-18.
        stdout_log = os.path.join(WORK_DIR, "mosquitto.stdout.log")
        logf = open(stdout_log, "ab")
        try:
            _proc = subprocess.Popen(
                ["mosquitto", "-c", MOSQUITTO_CONF],
                stdout=logf, stderr=subprocess.STDOUT,
            )
        finally:
            logf.close()  # child keeps its own duplicated fd

        # Wait for it to initialise
        time.sleep(1.0)
        if _proc.poll() is not None:
            _active = False
            try:
                with open(stdout_log, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    f.seek(max(0, f.tell() - 500))
                    out = f.read().decode(errors="replace")
            except OSError:
                out = ""
            raise RuntimeError(f"mosquitto failed to start: {out}")

        _active = True
        logger.info("MQTT broker started on port %d", MQTT_PORT)
        return {"port": MQTT_PORT}


def stop():
    """Stop the mosquitto broker."""
    global _active, _proc

    with _lock:
        _kill_proc(_proc)
        _proc = None
        _active = False
        logger.info("MQTT broker stopped")


def status():
    """Return broker status dict."""
    global _active

    with _lock:
        running = _active and _proc is not None and _proc.poll() is None
        # If process died unexpectedly, update state
        if _active and not running:
            _active = False
        return {
            "running": running,
            "port": MQTT_PORT if running else None,
        }
