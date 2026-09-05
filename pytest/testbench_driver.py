"""HTTP driver for the ESP32 testbench.

Communicates with the testbench HTTP API.  Provides serial, debug, WiFi,
BLE, GPIO, firmware, and test progress control over the network.
"""

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ── Response object (mimics requests.Response) ───────────────────────


@dataclass
class Response:
    """HTTP response returned by the relay, mimicking requests.Response."""

    status_code: int = 0
    headers: dict = field(default_factory=dict)
    _body_bytes: bytes = b""

    @property
    def text(self) -> str:
        return self._body_bytes.decode("utf-8", errors="replace")

    def json(self) -> dict:
        return json.loads(self._body_bytes)

    @property
    def content(self) -> bytes:
        return self._body_bytes


# ── Exceptions ───────────────────────────────────────────────────────


class TestbenchError(Exception):
    """Base exception for testbench errors."""

    # pytest collects anything named Test*, so the rename to `Testbench…`
    # turned the driver and its exceptions into candidate test classes. It
    # warns and skips them, which is noise now and a silent gap the day one
    # of them gains a method called test_something.
    __test__ = False


class CommandError(TestbenchError):
    """Portal returned ok=false."""

    def __init__(self, command: str, payload: dict):
        self.command = command
        self.payload = payload
        msg = payload.get("error", "Unknown error")
        super().__init__(f"{command}: {msg}")


class CommandTimeout(TestbenchError):
    """No response received within timeout."""


# ── Driver ───────────────────────────────────────────────────────────


def _body_reason(err) -> str:
    """The `error` field of a refusal body, or the raw body if it is not JSON.

    An HTTPError carries the response, and throwing it away leaves the caller
    with a bare status code for a refusal the bench took the trouble to
    explain.
    """
    try:
        raw = err.read()
    except Exception:
        return "(no body)"
    try:
        return json.loads(raw).get("error") or raw.decode("utf-8", "replace")
    except Exception:
        return raw.decode("utf-8", "replace")[:300]


class TestbenchDriver:
    """HTTP driver for the testbench."""

    __test__ = False        # not a test class — see TestbenchError above

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    # ── Lifecycle ────────────────────────────────────────────────────

    def open(self) -> None:
        """No-op for HTTP driver (no persistent connection)."""

    def close(self) -> None:
        """No-op for HTTP driver."""

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()

    # ── HTTP helpers ─────────────────────────────────────────────────

    def _api_get(self, path: str, timeout: float = 10) -> dict:
        """GET an API endpoint, return parsed JSON."""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # The bench refuses with a status *and a reason*, and the reason
            # is the whole point: "cannot scan while the AP is running"
            # tells a caller what to do, where "HTTP Error 503" tells it
            # only that something went wrong somewhere. Reading the body is
            # what turns the refusal back into information.
            raise CommandTimeout(f"GET {path}: {e} — {_body_reason(e)}")
        except urllib.error.URLError as e:
            raise CommandTimeout(f"GET {path}: {e}")
        except Exception as e:
            raise CommandTimeout(f"GET {path}: {e}")

        if not data.get("ok", False):
            cmd = path.split("/")[-1]
            raise CommandError(cmd, data)
        return data

    def _api_post_raw(self, path: str, body: Optional[dict] = None,
                      timeout: float = 10) -> dict:
        """POST JSON and return the parsed body whatever the status.

        `_api_post` treats `ok: false` as an exception, which is right for a
        command that either works or fails. It is wrong for the access
        manager: a refusal is the answer, and it carries who holds the slot,
        in what mode, since when. Raising would throw that away and leave the
        caller with the silence the manager exists to remove.
        """
        url = f"{self.base_url}{path}"
        data_bytes = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:          # 400/404/409 carry a body
            try:
                return json.loads(e.read())
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}"}
        except urllib.error.URLError as e:
            raise CommandTimeout(f"POST {path}: {e}")

    def _api_post(self, path: str, body: Optional[dict] = None,
                  timeout: float = 10) -> dict:
        """POST JSON to an API endpoint, return parsed JSON."""
        url = f"{self.base_url}{path}"
        data_bytes = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.URLError as e:
            raise CommandTimeout(f"POST {path}: {e}")
        except Exception as e:
            raise CommandTimeout(f"POST {path}: {e}")

        if not data.get("ok", False):
            cmd = path.split("/")[-1]
            raise CommandError(cmd, data)
        return data

    # ── Mode management ──────────────────────────────────────────────

    def get_mode(self) -> dict:
        result = self._api_get("/api/wifi/mode", timeout=5)
        return {k: v for k, v in result.items() if k != "ok"}

    def set_mode(self, mode: str, ssid: str = "",
                 password: str = "") -> dict:
        args: dict = {"mode": mode}
        if ssid:
            args["ssid"] = ssid
        if password:
            args["pass"] = password
        result = self._api_post("/api/wifi/mode", args, timeout=30)
        return {k: v for k, v in result.items() if k != "ok"}

    # ── AP management ────────────────────────────────────────────────

    def ap_start(self, ssid: str, password: str = "",
                 channel: int = 6, internet: bool = False) -> dict:
        args = {"ssid": ssid, "channel": channel, "internet": internet}
        if password:
            args["pass"] = password
        result = self._api_post("/api/wifi/ap_start", args, timeout=15)
        return {k: v for k, v in result.items() if k != "ok"}

    def ap_stop(self) -> None:
        self._api_post("/api/wifi/ap_stop", timeout=10)

    def ap_status(self) -> dict:
        # Longer than the AP takes to reconfigure. ap_start holds the radio
        # lock while it rearranges interfaces and starts two daemons, and a
        # status poll arriving in that window waited behind it.
        result = self._api_get("/api/wifi/ap_status", timeout=30)
        return {k: v for k, v in result.items() if k != "ok"}

    def wifi_http(self, url: str, method: str = "GET",
                  headers: Optional[dict] = None, body: str = None,
                  timeout: int = 8) -> dict:
        """Relay an HTTP request through the Pi's radio to a device on its net."""
        args = {"method": method, "url": url, "timeout": timeout}
        if headers:
            args["headers"] = headers
        if body is not None:
            args["body"] = body
        return self._api_post("/api/wifi/http", args, timeout=timeout + 5)

    # ── MQTT broker (FR-029) ──────────────────────────────────────────

    def mqtt_start(self) -> dict:
        """Start the testbench mosquitto test broker (open, all interfaces)."""
        return self._api_post("/api/mqtt/start", {}, timeout=15)

    def mqtt_stop(self) -> None:
        self._api_post("/api/mqtt/stop", {}, timeout=10)

    def mqtt_status(self) -> dict:
        return self._api_get("/api/mqtt/status")

    # ── Captive-portal provisioning (FR-012 composite) ────────────────

    def provision_wifimanager(self, portal_ssid: str, ssid: str, password: str,
                              extra: Optional[dict] = None,
                              internet: bool = True,
                              save_path: str = "/wifisave",
                              field_ssid: str = "s",
                              field_password: str = "p") -> dict:
        """Provision a DUT onto the testbench AP through its captive portal.

        The defaults are Arduino WiFiManager's (`POST /wifisave`, fields
        `s`/`p`), which is what most project DUTs run. They are defaults and
        not constants because the bench's own test partner is not a WiFiManager
        device — it takes `POST /connect` with `ssid`/`password`, and a
        method that could only speak to one convention would have left the
        bench unable to provision its own board.

        Runs asynchronously on the portal; poll ap_status for the DUT to join.
        """
        return self._api_post("/api/enter-portal", {
            "portal_ssid": portal_ssid, "ssid": ssid, "password": password,
            "save_path": save_path, "field_ssid": field_ssid,
            "field_password": field_password,
            "method": "POST", "internet": internet, "extra": extra or {},
        }, timeout=15)

    # ── STA management ───────────────────────────────────────────────

    def sta_join(self, ssid: str, password: str = "",
                 timeout: int = 15) -> dict:
        args = {"ssid": ssid, "timeout": timeout}
        if password:
            args["pass"] = password
        result = self._api_post("/api/wifi/sta_join", args, timeout=timeout + 10)
        return {k: v for k, v in result.items() if k != "ok"}

    def sta_leave(self) -> None:
        self._api_post("/api/wifi/sta_leave", timeout=10)

    # ── HTTP relay ───────────────────────────────────────────────────

    def http_request(self, method: str, url: str,
                     headers: Optional[dict] = None,
                     body: Optional[bytes] = None,
                     timeout: int = 10) -> Response:
        args: dict = {"method": method, "url": url, "timeout": timeout}
        if headers:
            args["headers"] = headers
        if body:
            args["body"] = base64.b64encode(body).decode("ascii")

        result = self._api_post("/api/wifi/http", args, timeout=timeout + 10)

        resp_body = b""
        if result.get("body"):
            resp_body = base64.b64decode(result["body"])

        return Response(
            status_code=result.get("status", 0),
            headers=result.get("headers", {}),
            _body_bytes=resp_body,
        )

    def http_get(self, url: str, **kwargs) -> Response:
        return self.http_request("GET", url, **kwargs)

    def http_post(self, url: str, json_data: Optional[dict] = None,
                  **kwargs) -> Response:
        if json_data is not None:
            body = json.dumps(json_data).encode("utf-8")
            headers = kwargs.pop("headers", {})
            headers.setdefault("Content-Type", "application/json")
            return self.http_request("POST", url, headers=headers,
                                     body=body, **kwargs)
        return self.http_request("POST", url, **kwargs)

    # ── WiFi scanning ────────────────────────────────────────────────

    def scan(self) -> dict:
        # Longer than the bench's own retry budget. The bench retries a busy
        # radio, and a scan that comes back empty right after the AP is torn
        # down, which together can exceed 20 s — and the client giving up
        # first turns a slow instrument into a connection error, which is a
        # worse diagnosis than the wait.
        result = self._api_get("/api/wifi/scan", timeout=90)
        return {k: v for k, v in result.items() if k != "ok"}

    # ── Events ───────────────────────────────────────────────────────

    def wait_for_event(self, event_type: str,
                       timeout: float = 30) -> dict:
        """Wait for a specific event type via long-polling."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"No {event_type} event within {timeout}s"
                )
            poll_timeout = min(remaining, 5)
            try:
                result = self._api_get(
                    f"/api/wifi/events?timeout={poll_timeout}",
                    timeout=poll_timeout + 5,
                )
            except CommandTimeout:
                continue

            for evt in result.get("events", []):
                if evt.get("type") == event_type:
                    return evt

    def wait_for_station(self, timeout: float = 30) -> dict:
        """Shortcut for waiting for a STA_CONNECT event."""
        return self.wait_for_event("STA_CONNECT", timeout=timeout)

    def drain_events(self) -> list:
        """Return and clear all queued events."""
        try:
            result = self._api_get("/api/wifi/events", timeout=5)
            return result.get("events", [])
        except (CommandTimeout, CommandError):
            return []

    # ── Utility ──────────────────────────────────────────────────────

    def ping(self) -> dict:
        result = self._api_get("/api/wifi/ping", timeout=5)
        return {k: v for k, v in result.items() if k != "ok"}

    def reset(self) -> None:
        """No-op for Pi backend (no hardware to reset)."""

    # ── Serial service ────────────────────────────────────────────

    def get_devices(self) -> list[dict]:
        """GET /api/devices — returns list of slot dicts."""
        url = f"{self.base_url}/api/devices"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            raise CommandTimeout(f"GET /api/devices: {e}")
        return data.get("slots", [])

    def get_slot(self, label: str) -> dict:
        """Find slot by label in /api/devices response."""
        slots = self.get_devices()
        for s in slots:
            if s.get("label") == label:
                return s
        raise CommandError("get_slot", {"error": f"slot '{label}' not found"})

    def serial_reset(self, slot: str = "SLOT2") -> dict:
        """POST /api/serial/reset — returns {ok, output}."""
        result = self._api_post(
            "/api/serial/reset", {"slot": slot}, timeout=30
        )
        return {k: v for k, v in result.items() if k != "ok"}

    def serial_output(self, slot: str = "SLOT2",
                      lines: int = 50, since: float = 0) -> dict:
        """GET /api/serial/output — passive buffer read."""
        return self._api_get(
            f"/api/serial/output?slot={slot}&lines={lines}&since={since}"
        )

    def serial_monitor(self, slot: str = "SLOT2",
                       pattern: Optional[str] = None,
                       timeout: float = 10) -> dict:
        """POST /api/serial/monitor — returns {ok, matched, line, output}."""
        body: dict = {"slot": slot, "timeout": timeout}
        if pattern is not None:
            body["pattern"] = pattern
        result = self._api_post(
            "/api/serial/monitor", body, timeout=timeout + 5
        )
        return {k: v for k, v in result.items() if k != "ok"}

    def enter_portal(self, slot: str = "SLOT2",
                     resets: int = 3) -> dict:
        """POST /api/enter-portal — starts background portal trigger."""
        result = self._api_post(
            "/api/enter-portal", {"slot": slot, "resets": resets}, timeout=10
        )
        return {k: v for k, v in result.items() if k != "ok"}

    def wait_for_state(self, slot_label: str, state: str,
                       timeout: float = 30,
                       poll_interval: float = 1) -> dict:
        """Poll /api/devices until slot reaches target state or timeout."""
        deadline = time.monotonic() + timeout
        last_slot = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                current = last_slot.get("state", "?") if last_slot else "?"
                raise TimeoutError(
                    f"Slot '{slot_label}' did not reach state "
                    f"'{state}' within {timeout}s (current: {current})"
                )
            try:
                slots = self.get_devices()
                for s in slots:
                    if s.get("label") == slot_label:
                        last_slot = s
                        if s.get("state") == state:
                            return s
                        break
            except (CommandTimeout, CommandError):
                pass
            time.sleep(min(poll_interval, max(remaining, 0)))

    def get_log(self, since: Optional[str] = None) -> list[dict]:
        """GET /api/log — returns activity log entries."""
        path = "/api/log"
        if since:
            path += f"?since={since}"
        result = self._api_get(path, timeout=10)
        return result.get("entries", [])

    # ── Human interaction ───────────────────────────────────────────

    def human_interaction(self, message: str, timeout: float = 120) -> bool:
        """Ask a human operator to perform a physical action.

        Displays *message* as a popup on the Pi's web UI.  The call blocks
        (server-side, event-driven — no polling) until the operator clicks
        Done or Cancel, or *timeout* expires.

        Returns True if confirmed, False if cancelled or timed out.
        """
        logger.info("Human interaction: %s", message)
        result = self._api_post(
            "/api/human-interaction",
            {"message": message, "timeout": timeout},
            timeout=timeout + 10,
        )
        confirmed = result.get("confirmed", False)
        logger.info("Human interaction %s", "confirmed" if confirmed else "not confirmed")
        return confirmed

    # ── Test progress ──────────────────────────────────────────────

    def test_start(self, spec: str, phase: str, total: int) -> dict:
        """Start a test session on the Pi UI."""
        return self._api_post("/api/test/update",
                              {"spec": spec, "phase": phase, "total": total})

    def test_step(self, test_id: str, name: str, step: str,
                  manual: bool = False) -> dict:
        """Update the current test step shown on the Pi UI."""
        return self._api_post("/api/test/update",
                              {"current": {"id": test_id, "name": name,
                                           "step": step, "manual": manual}})

    def test_result(self, test_id: str, name: str, result: str,
                    details: str = "") -> dict:
        """Record a test result (PASS/FAIL/SKIP)."""
        return self._api_post("/api/test/update",
                              {"result": {"id": test_id, "name": name,
                                          "result": result, "details": details}})

    def test_end(self) -> dict:
        """End the test session."""
        return self._api_post("/api/test/update", {"end": True})

    # ── GPIO control ──────────────────────────────────────────────────

    def gpio_set(self, pin: int, value) -> dict:
        """Set a GPIO pin on the Pi (0=low, 1=high, 'z'=release/high-Z)."""
        return self._api_post("/api/gpio/set", {"pin": pin, "value": value})

    def gpio_get(self) -> dict:
        """Get current GPIO pin states."""
        return self._api_get("/api/gpio/status")

    # ── Signal generator (Si5351 + PE4302, with GPCLK fallback) ──────

    def siggen_start(self, freq_hz: float, backend: str = "auto",
                     channel: Optional[int] = None, pin: Optional[int] = None,
                     atten_db: Optional[float] = None,
                     morse: Optional[dict] = None) -> dict:
        """Start an RF carrier.

        Args:
            freq_hz: Carrier frequency in Hz.
            backend: "auto" (prefer Si5351), "si5351", or "gpclk".
            channel: Si5351 output channel (0, 1, 2). Default from config.
            pin: GPCLK pin (5 or 6). Default from config.
            atten_db: Initial PE4302 attenuation (0..31.5 dB).
            morse: Optional {"message": str, "wpm": int, "repeat": bool}
                   to key the carrier with Morse instead of continuous tone.

        Returns:
            State dict: backend, freq_hz, channel, pin, atten_db, morse.
        """
        body: dict = {"freq_hz": freq_hz, "backend": backend}
        if channel is not None:
            body["channel"] = channel
        if pin is not None:
            body["pin"] = pin
        if atten_db is not None:
            body["atten_db"] = atten_db
        if morse is not None:
            body["morse"] = morse
        return self._api_post("/api/siggen/start", body, timeout=15)

    def siggen_stop(self) -> dict:
        """Stop the signal generator."""
        return self._api_post("/api/siggen/stop", {}, timeout=10)

    def siggen_freq(self, freq_hz: float,
                    channel: Optional[int] = None) -> dict:
        """Retune the active carrier without restarting the keyer."""
        body: dict = {"freq_hz": freq_hz}
        if channel is not None:
            body["channel"] = channel
        return self._api_post("/api/siggen/freq", body, timeout=10)

    def siggen_atten(self, db: float) -> dict:
        """Set PE4302 attenuation in dB (0..31.5, 0.5 dB steps).

        Raises CommandError if PE4302 is not available.
        """
        return self._api_post("/api/siggen/atten", {"db": db}, timeout=10)

    def siggen_status(self) -> dict:
        """Current generator state + hardware detection."""
        return self._api_get("/api/siggen/status")

    def siggen_frequencies(self, low: float, high: float,
                           backend: str = "auto") -> list:
        """Achievable frequencies in a range (gpclk returns discrete
        dividers; si5351 reports the range as continuously tunable)."""
        result = self._api_get(
            f"/api/siggen/frequencies?low={low}&high={high}&backend={backend}")
        return result.get("frequencies", [])

    # ── SDR receiver (RTL-SDR + rtl_433) ──────────────────────────────

    def sdr_status(self) -> dict:
        """Dongle/tool presence and active-capture state."""
        return self._api_get("/api/sdr/status")

    def sdr_capture(self, freq_hz: Optional[int] = None,
                    duration_s: Optional[int] = None,
                    protocols: Optional[list] = None,
                    sample_rate: Optional[int] = None,
                    flex: Optional[str] = None) -> dict:
        """Decode RF for a bounded window; returns decoded rtl_433 records.

        Args:
            freq_hz: Centre frequency in Hz. Default 433.92 MHz.
            duration_s: Capture window in seconds (clamped to max_duration_s).
            protocols: Optional list of rtl_433 protocol numbers to restrict to.
            sample_rate: Optional sample rate in Hz.
            flex: Optional rtl_433 -X flex-decoder spec for a custom protocol,
                  e.g. "n=awn,m=OOK_PWM,s=416,l=2150,r=16000".

        Returns:
            {freq_hz, duration_s, count, events: [ {rtl_433 record}, ... ]}.
        """
        body: dict = {}
        if freq_hz is not None:
            body["freq_hz"] = freq_hz
        if duration_s is not None:
            body["duration_s"] = duration_s
        if protocols is not None:
            body["protocols"] = protocols
        if sample_rate is not None:
            body["sample_rate"] = sample_rate
        if flex is not None:
            body["flex"] = flex
        return self._api_post("/api/sdr/capture", body, timeout=150)

    def sdr_analyze(self, freq_hz: Optional[int] = None,
                    duration_s: Optional[int] = None) -> dict:
        """Pulse-analyzer capture (rtl_433 -A) for recapturing a remote.

        Returns {freq_hz, duration_s, analyzer} where ``analyzer`` is the raw
        pulse/gap text plus any guessed codeword.
        """
        body: dict = {}
        if freq_hz is not None:
            body["freq_hz"] = freq_hz
        if duration_s is not None:
            body["duration_s"] = duration_s
        return self._api_post("/api/sdr/analyze", body, timeout=150)

    def sdr_power(self, freq_hz: Optional[int] = None,
                  duration_s: Optional[int] = None,
                  span_hz: int = 40000, bin_hz: int = 2000,
                  notch_hz: int = 0,
                  gain: Optional[float] = None) -> dict:
        """Narrowband RF power (rtl_power) around freq_hz → {peak_db, mean_db}.

        A carrier at the frequency lifts peak_db above the noise floor, which
        detects a transmitter that decode-based methods miss in band noise.

        Pass a fixed ``gain`` (dB) whenever the number will be compared against a
        threshold or another reading. Left on AGC the tuner rescales itself from
        whatever it saw recently, so the same quiet band can read 15 dB apart
        minutes apart and a strong carrier is compressed instead of standing out.

        ``notch_hz`` excludes bins within that distance of the tuner centre,
        where the dongle's DC spike always sits.
        """
        body: dict = {"span_hz": span_hz, "bin_hz": bin_hz}
        if freq_hz is not None:
            body["freq_hz"] = freq_hz
        if duration_s is not None:
            body["duration_s"] = duration_s
        if notch_hz:
            body["notch_hz"] = notch_hz
        if gain is not None:
            body["gain"] = gain
        return self._api_post("/api/sdr/power", body, timeout=150)

    def sdr_acquire(self, freq_hz: Optional[int] = None,
                    span_hz: int = 500_000, bin_hz: int = 10_000,
                    gains: Optional[list] = None, dwell_s: int = 3,
                    decode_s: int = 12, flex: Optional[str] = None,
                    wait_s: int = 30) -> dict:
        """Phased receive: locate → level → decode → classify.

        Guided acquisition that finds the true carrier, picks a non-saturating
        tuner gain (or reports the signal is too strong), decodes the codeword
        with a custom flex decoder, then checks the built-in decoders. The
        transmitter must stay keyed for the whole run. `ok_phase` says where it
        stopped; `summary` is the human-readable verdict.
        """
        body: dict = {"span_hz": span_hz, "bin_hz": bin_hz,
                      "dwell_s": dwell_s, "decode_s": decode_s,
                      "wait_s": wait_s}
        if freq_hz is not None:
            body["freq_hz"] = freq_hz
        if gains is not None:
            body["gains"] = gains
        if flex is not None:
            body["flex"] = flex
        return self._api_post("/api/sdr/acquire", body, timeout=180)

    def sdr_stop(self) -> dict:
        """Terminate an in-progress SDR capture."""
        return self._api_post("/api/sdr/stop", {}, timeout=10)

    # ── GDB debug ─────────────────────────────────────────────────────

    def debug_start(self, slot: str = None, chip: str = None,
                    probe: str = None) -> dict:
        """Start OpenOCD debug session.

        All parameters are optional — the testbench auto-detects the
        slot (first present device) and chip (via JTAG TAP ID probing).

        Args:
            slot: Slot label. Auto-detected if omitted.
            chip: Chip type. Auto-detected if omitted.
            probe: Probe label for ESP-Prog mode. Omit for USB JTAG.

        Returns:
            dict with slot, chip, gdb_port, telnet_port, gdb_target.
        """
        body = {}
        if slot:
            body["slot"] = slot
        if chip:
            body["chip"] = chip
        if probe:
            body["probe"] = probe
        return self._api_post("/api/debug/start", body, timeout=45)

    def debug_stop(self, slot: str = None) -> dict:
        """Stop OpenOCD debug session. Auto-finds active session if slot omitted."""
        body = {}
        if slot:
            body["slot"] = slot
        return self._api_post("/api/debug/stop", body)

    def debug_status(self) -> dict:
        """Get debug state for all slots."""
        return self._api_get("/api/debug/status")

    def chip_info(self, slot: str) -> dict:
        """Read the silicon in a slot: chip, revision, flash size, MAC.

        The MAC is per-board, which makes it the one field that settles
        *which* physical device answered — every native-USB ESP32 shares
        VID:PID 303a:1001 (FR-037).
        """
        return self._api_post("/api/chip/info", {"slot": slot}, timeout=60)

    def debug_probes(self) -> list:
        """List available debug probes (ESP-Prog)."""
        result = self._api_get("/api/debug/probes")
        return result.get("probes", [])

    def debug_groups(self) -> dict:
        """Get slot groups for dual-USB configurations."""
        return self._api_get("/api/debug/group").get("groups", {})

    # ── UDP log ──────────────────────────────────────────────────────

    def udplog(self, source: str = None, since: str = None,
               limit: int = None) -> list[dict]:
        """GET /api/udplog — buffered UDP debug log lines from ESP32 devices."""
        params = []
        if source:
            params.append(f"source={source}")
        if since:
            params.append(f"since={since}")
        if limit:
            params.append(f"limit={limit}")
        qs = "?" + "&".join(params) if params else ""
        url = f"{self.base_url}/api/udplog{qs}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            raise CommandTimeout(f"GET /api/udplog: {e}")
        return data.get("lines", [])

    def udplog_clear(self) -> None:
        """DELETE /api/udplog — clear the log buffer."""
        url = f"{self.base_url}/api/udplog"
        req = urllib.request.Request(url, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                json.loads(resp.read())
        except Exception as e:
            raise CommandTimeout(f"DELETE /api/udplog: {e}")

    # ── Firmware ─────────────────────────────────────────────────────

    def firmware_list(self) -> list[dict]:
        """GET /api/firmware/list — list available firmware files."""
        return self._api_get("/api/firmware/list").get("files", [])

    def firmware_upload(self, project: str, filepath: str) -> dict:
        """POST /api/firmware/upload — upload a binary file."""
        boundary = "----TestbenchUpload"
        filename = os.path.basename(filepath)
        with open(filepath, "rb") as f:
            file_data = f.read()

        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="project"\r\n\r\n'
            f"{project}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

        url = f"{self.base_url}/api/firmware/upload"
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            raise CommandTimeout(f"POST /api/firmware/upload: {e}")
        return data

    def firmware_delete(self, project: str, filename: str) -> dict:
        """DELETE /api/firmware/delete — delete a firmware file."""
        url = f"{self.base_url}/api/firmware/delete"
        data_bytes = json.dumps({"project": project, "filename": filename}).encode()
        req = urllib.request.Request(
            url, data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as e:
            raise CommandTimeout(f"DELETE /api/firmware/delete: {e}")

    # ── BLE ──────────────────────────────────────────────────────────

    def ble_scan(self, timeout: int = 5,
                 name_filter: str = None) -> list[dict]:
        """Scan for BLE peripherals."""
        body: dict = {"timeout": timeout}
        if name_filter:
            body["name_filter"] = name_filter
        result = self._api_post("/api/ble/scan", body, timeout=timeout + 10)
        return result.get("devices", [])

    def ble_connect(self, address: str) -> dict:
        """Connect to a BLE peripheral by address."""
        return self._api_post("/api/ble/connect", {"address": address}, timeout=15)

    def ble_disconnect(self) -> dict:
        """Disconnect current BLE connection."""
        return self._api_post("/api/ble/disconnect")

    def ble_write(self, characteristic: str, data: str,
                  response: bool = False) -> dict:
        """Write hex bytes to a GATT characteristic."""
        return self._api_post("/api/ble/write", {
            "characteristic": characteristic,
            "data": data,
            "response": response,
        })

    def ble_read(self, characteristic: str) -> dict:
        """Read hex bytes from a GATT characteristic."""
        return self._api_post("/api/ble/read", {
            "characteristic": characteristic,
        })

    def ble_status(self) -> dict:
        """Get BLE connection state."""
        return self._api_get("/api/ble/status")

    # ── Serial recovery ──────────────────────────────────────────────

    def serial_recover(self, slot: str) -> dict:
        """POST /api/serial/recover — trigger manual flap recovery."""
        return self._api_post("/api/serial/recover", {"slot": slot}, timeout=30)

    def serial_release(self, slot: str) -> dict:
        """POST /api/serial/release — release GPIO after flashing, reboot."""
        return self._api_post("/api/serial/release", {"slot": slot}, timeout=10)

    # ── System info ──────────────────────────────────────────────────

    def info(self) -> dict:
        """GET /api/info — host IP, hostname, slot counts."""
        return self._api_get("/api/info")

    # ---- Slot access manager (FR-031 – FR-035) -----------------------------

    def slot_acquire(self, slot: str, mode: str, owner: str,
                     ttl: float = 60) -> dict:
        """POST /api/slot/acquire — {ok, token, mode, expires_in}, or a 409
        body {ok: false, error, mode, owner, since, expires_in} when held.

        A refusal is a normal outcome, not an exception: the caller is meant to
        read who holds the slot and why.
        """
        return self._api_post_raw("/api/slot/acquire",
                              {"slot": slot, "mode": mode,
                               "owner": owner, "ttl": ttl})

    def slot_renew(self, token: str) -> dict:
        """POST /api/slot/renew — extends by the ttl the grant was made with."""
        return self._api_post_raw("/api/slot/renew", {"token": token})

    def slot_release(self, token: str) -> dict:
        """POST /api/slot/release — returns the slot to idle."""
        return self._api_post_raw("/api/slot/release", {"token": token})

    def slot_mode(self, slot: str) -> dict:
        """GET /api/slot/mode — {ok, mode, owner, since, expires_in}."""
        url = f"{self.base_url}/api/slot/mode?slot={slot}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}"}

    # ---- Serial write and bench reset --------------------------------------

    def serial_write(self, slot: str, text: str | None = None,
                     hex_bytes: str | None = None, newline: bool = True) -> dict:
        """POST /api/serial/write (FR-030) — send bytes to a device."""
        body: dict = {"slot": slot, "newline": newline}
        if hex_bytes is not None:
            body["hex"] = hex_bytes
        elif text is not None:
            body["text"] = text
        return self._api_post_raw("/api/serial/write", body)

    def bench_reset(self) -> dict:
        """POST /api/bench/reset (FR-036) — the first call of every run."""
        return self._api_post_raw("/api/bench/reset", {}, timeout=90)

    def monitor_or_buffer(self, slot: str, pattern: str,
                          seconds: float = 12) -> tuple:
        """Look for *pattern* in live output, then in the recorded buffer.

        A device may answer faster than a monitor can attach, which is why the
        portal records continuously. Checking both is what makes a
        write-then-read sequence reliable rather than usually-reliable.
        """
        res = self._api_post("/api/serial/monitor",
                             {"slot": slot, "pattern": pattern,
                              "timeout": seconds}, timeout=seconds + 20)
        if res.get("matched"):
            return True, res.get("output") or []
        buf = self._api_get(f"/api/serial/output?slot={slot}&lines=40")
        lines = [e.get("text", "") for e in (buf.get("lines") or buf.get("output") or [])]
        return any(pattern in ln for ln in lines), lines

    def flash(self, slot: str, images: dict, chip: str = "auto",
              erase: bool = False, timeout: float = 300) -> dict:
        """POST /api/flash — multipart flash through the portal.

        `images` maps a hex offset to a file path, e.g.
        {"0x0": ".../bootloader.bin", "0x10000": ".../app.bin"}.

        The portal stops the proxy, runs esptool locally against the devnode
        and restarts the proxy. Part names are `bin@<offset>`: the live
        handler rejects anything else, and a dead duplicate in portal.py once
        documented a different form, so this is the shape that works.
        """
        import uuid as _uuid

        boundary = "----wb" + _uuid.uuid4().hex
        parts = []
        for name, value in (("slot", slot), ("chip", chip),
                            ("erase", "true" if erase else "false")):
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
        for offset, path in images.items():
            with open(path, "rb") as fh:
                blob = fh.read()
            base = os.path.basename(path)
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="bin@{offset}"; filename="{base}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n".encode()
                + blob + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)

        req = urllib.request.Request(
            f"{self.base_url}/api/flash", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}"}
        except urllib.error.URLError as e:
            raise CommandTimeout(f"POST /api/flash: {e}")

