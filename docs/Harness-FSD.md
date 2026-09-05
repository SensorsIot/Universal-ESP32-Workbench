# The Harness — Functional Specification Document

## Contents

| § | Section | Covers |
|---|---------|--------|
| 1 | [Overview](#1-overview) | Purpose, architecture, hardware, modes, components, state model |
| 2 | [Definitions](#2-definitions) | Terms and the slot-based identity principle |
| 3 | [Serial Interface](#3-serial-interface) | FR-001 – FR-009: hotplug, slots, serial API, RFC2217, reset, monitor, flap recovery · FR-030 serial write · FR-031 – FR-035 access manager · FR-036 bench reset |
| 4 | [WiFi Service](#4-wifi-service) | FR-010 – FR-016: AP, STA, scan, HTTP relay, events, mode switching |
| 5 | [Device Control & Test Support](#5-device-control--test-support) | FR-017 – FR-021: operator prompts, GPIO, test progress, UDP logs, OTA repository |
| 6 | [Peripheral Bridges](#6-peripheral-bridges) | FR-022, FR-029: BLE proxy, MQTT test broker |
| 7 | [Debug Services](#7-debug-services) | FR-024 – FR-026, FR-037: USB JTAG, dual-USB, ESP-Prog, per-slot isolation |
| 8 | [RF Instruments](#8-rf-instruments) | FR-027, FR-028: signal generator, SDR receiver |
| 9 | [Client Interfaces](#9-client-interfaces) | MCP server |
| 10 | [Web Portal](#10-web-portal) | Single-page UI |
| 11 | [Non-Functional Requirements](#11-non-functional-requirements) | Performance, reliability, constraints |
| 12 | [Test Cases](#12-test-cases) | Verification |
| 13 | [Revision History](#13-revision-history) | |
| A | [Technical Details](#appendix-a-technical-details) | Implementation notes |
| B | [Slot Learning Workflow](#appendix-b-slot-learning-workflow) | |
| D | [HTTP API & MCP Reference](#appendix-d-http-api--mcp-reference) | **Complete endpoint and tool reference** |

This document is the **WHAT** plane: what must be true of the bench. The plane
map is [`00-Overview.md`](00-Overview.md); the other planes are
[`Method/`](Method/00-Overview.md) (HOW) and the
[User Manual](Harness-User-Manual.md) (OPERATE). Nothing here
duplicates them.

---

## 1. Overview

### 1.1 Purpose

The testbench is the physical subsystem of the **Harness** — the instrument
through which **AI Closed-Loop Programming** reaches real hardware. In AICLP
the AI develops a product's firmware in a closed loop: code, build, flash,
verify against tests derived from the product's FSD, correct — exiting only
when the tests run clean. The testbench is the loop's hands and eyes: it
flashes the DUT, resets it, watches its serial and UDP output, surrounds it
with WiFi, MQTT, BLE and RF peers, and reports every observation over one
HTTP API.

A harnessed project moves through five phases, each ending at a **gate
derived from project state, never declared**. The phases, their commands
and their gates are in [`00-Overview.md`](00-Overview.md#the-journey) —
stated once there rather than twice.

Two rules bind the loop. **No code without a clause** — work no requirement
covers enters through Definition or not at all. **A change is done when its
requirement is met and the journey still runs** — its own tests green,
including prohibited outcomes, and the standard end-to-end run green.

The instrument itself: a combined serial interface and WiFi test instrument on
a single Raspberry Pi.  The serial interface exposes USB serial devices to
network clients via RFC2217 protocol with event-driven hotplug and slot-based
port assignment.  The WiFi testbench uses the Pi's onboard wlan0 radio as a
test instrument — starting SoftAP, joining networks, scanning, relaying HTTP,
and reporting station events — all controlled over the same HTTP API.

### 1.2 System Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          Network (192.168.0.x)                           │
└──────────────────────────────────────────────────────────────────────────┘
       │  eth0 (USB Ethernet)                          │
       │                                               │
       ▼                                               ▼
┌─────────────────────────┐              ┌─────────────────────────────────┐
│  Testbench              │              │  VM Host (192.168.0.160)        │
│  $BENCH        │              │                                 │
│                         │              │  ┌─────────────────────┐        │
│  ┌───────────┐          │              │  │ Container A         │        │
│  │ SLOT1     │──────────┼─ :4001 ──────┼──│ rfc2217://:4001     │        │
│  └───────────┘          │              │  └─────────────────────┘        │
│  ┌───────────┐          │              │  ┌─────────────────────┐        │
│  │ SLOT2     │──────────┼─ :4002 ──────┼──│ Container B         │        │
│  └───────────┘          │              │  │ rfc2217://:4002     │        │
│  ┌───────────┐          │              │  └─────────────────────┘        │
│  │ SLOT3     │──────────┼─ :4003       │                                 │
│  └───────────┘          │              └─────────────────────────────────┘
│                         │
│  ┌───────────────────┐  │
│  │ WiFi Testbench    │  │
│  │ wlan0 (onboard)   │  │
│  │  AP: 192.168.4.1  │  │
│  │  STA / Scan       │  │
│  └───────────────────┘  │
│                         │                    ┌──────────────────┐
│  ┌───────────────────┐  │                    │  ESP32 DUT       │
│  │ BLE Proxy         │  │◄ ─ BLE (GATT) ─ ─ │  iOS-Keyboard    │
│  │ hci0 (onboard)    │  │                    │  BLE peripheral  │
│  │  Scan / Connect   │  │                    └──────────────────┘
│  │  Write GATT chars │  │
│  └───────────────────┘  │                    ┌──────────────────┐
│                         │                    │  ESP32 DUT       │
│  ┌───────────────────┐  │◄ ─ UDP :5555 ─ ─ ─│  debug logs      │
│  │ UDP Log Receiver  │  │                    └──────────────────┘
│  │  Port 5555        │  │
│  └───────────────────┘  │
│                         │
│  ┌───────────────────┐  │
│  │ Firmware Repo     │  │─── GET /firmware/<project>/<file>.bin
│  │ /var/lib/.../fw   │  │
│  └───────────────────┘  │
│                         │
│  Web Portal ────────────┼─ :8080
└─────────────────────────┘
```

### 1.3 Hardware

| Component | Details |
|-----------|---------|
| Raspberry Pi Zero W | $BENCH, onboard wlan0 radio |
| USB Hub | 4-port hub connected to single USB port |
| USB Ethernet adapter | eth0 — wired LAN for management and serial traffic |
| Devices | ESP32, Arduino, or any USB serial device |
| GPIO wiring | Pi GPIO 17 → DUT EN/RST (reset, active LOW); Pi GPIO 18 → DUT GPIO0/GPIO9 (boot select, active LOW); Pi GPIO 27 → Spare 1; Pi GPIO 22 → Spare 2 |

### 1.4 Operating Modes

The system operates in one of two modes at any time:

| Mode | Default | eth0 | wlan0 | Serial | WiFi Testbench |
|------|---------|------|-------|--------|-------------|
| **WiFi-Testing** | Yes | LAN (management + serial) | Test instrument (AP/STA/scan) | Active | Active |
| **Serial Interface** | No | LAN (management + serial) | Joins WiFi for additional LAN | Active | Disabled |

- **WiFi-Testing** (default): eth0 provides wired LAN connectivity.  wlan0 is
  dedicated to the WiFi test instrument — it can start a SoftAP, join external
  networks, scan, and relay HTTP.  Both serial slots and WiFi testbench are active.

- **Serial Interface**: wlan0 joins a user-specified WiFi network to provide
  wireless LAN connectivity (useful when no wired Ethernet is available).
  Serial slots remain active.  WiFi testbench endpoints return an error.

Mode is switched via `POST /api/wifi/mode` or the web UI toggle.

### 1.5 Components

| Component | Location | Purpose |
|-----------|----------|---------|
| portal.py (rfc2217-portal) | /usr/local/bin/rfc2217-portal | Web UI, HTTP API, proxy supervisor, hotplug handler, WiFi API, BLE API, UDP log, firmware serving |
| wifi_controller.py | /usr/local/bin/wifi_controller.py | WiFi instrument backend (AP, STA, scan, relay, events) |
| ble_controller.py | /usr/local/bin/ble_controller.py | BLE proxy backend (scan, connect, write GATT characteristics via bleak) |
| plain_rfc2217_server.py | /usr/local/bin/plain_rfc2217_server.py | RFC2217 server with direct DTR/RTS passthrough (all devices) |
| rfc2217-udev-notify.sh | /usr/local/bin/rfc2217-udev-notify.sh | Posts udev events to portal API |
| wifi-lease-notify.sh | /usr/local/bin/wifi-lease-notify.sh | Posts dnsmasq DHCP lease events to portal API |
| rfc2217-learn-slots | /usr/local/bin/rfc2217-learn-slots | Slot configuration helper |
| 99-rfc2217-hotplug.rules | /etc/udev/rules.d/ | udev rules for hotplug |
| testbench.json | /etc/rfc2217/testbench.json | Hardware config (GPIO pins, debug probes) — optional |
| testbench_driver.py | pytest/ | HTTP test driver for the WiFi instrument |
| conftest.py | pytest/ | Pytest fixtures and CLI options |
| testbench_test.py | pytest/ | Testbench self-tests |
| signal_generator.py | /usr/local/bin/signal_generator.py | Unified RF source — Si5351 + PE4302 attenuator, GPCLK fallback, Morse keyer |
| si5351.py | /usr/local/bin/si5351.py | Si5351A I²C clock-generator driver |
| pe4302.py | /usr/local/bin/pe4302.py | PE4302 3-wire serial step-attenuator driver |
| gpclk.py | /usr/local/bin/gpclk.py | BCM2835/7 GPCLK hardware clock primitive |
| morse.py | /usr/local/bin/morse.py | Backend-agnostic Morse keyer |
| debug_controller.py | /usr/local/bin/debug_controller.py | GDB debug manager (OpenOCD lifecycle, probe allocation) |

### 1.6 State Model

The system provides two independent services — Serial and WiFi — each with
its own state machine.  Serial operates per slot; WiFi operates on wlan0.

**Serial Service (per slot):**

| State | Description |
|-------|-------------|
| Absent | No USB device in this slot |
| Idle | Device present, proxy running, no active operation |
| Flashing | `POST /api/flash` in progress — proxy stopped, esptool running locally |
| Resetting | DTR/RTS reset in progress — proxy stopped, direct serial in use |
| Monitoring | Reading serial output for pattern matching |
| Writing | `POST /api/serial/write` in progress — bytes going out through the proxy (FR-030) |
| Flapping | USB connect/disconnect cycling detected — recovery failed or pending |
| Recovering | USB unbound, recovery in progress (GPIO or backoff) |
| Download Mode | GPIO holding BOOT LOW, device stable in bootloader — ready to flash |
| Debugging | OpenOCD running for this slot — GDB clients can connect; RFC2217 proxy stopped (FR-024) or running (FR-025/026) |

State transitions:

| From | To | Trigger |
|------|----|---------|
| Absent | Idle | Hotplug add + proxy start |
| Idle | Absent | Hotplug remove |
| Idle | Flashing | `POST /api/flash` — portal stops proxy, runs esptool |
| Flashing | Idle | Flash complete — portal restarts proxy |
| Idle | Resetting | `POST /api/serial/reset` — stops proxy, opens direct serial, sends DTR/RTS |
| Resetting | Idle | Reset complete, proxy restarts via hotplug |
| Idle | Monitoring | `POST /api/serial/monitor` — reads serial via RFC2217 (non-exclusive) |
| Monitoring | Idle | Pattern matched or timeout expired |
| *any mode* | Writing | `POST /api/serial/write` — brief, through the proxy; the slot keeps its lease |
| Writing | *the prior mode* | Write finished, or the proxy could not be reached |
| Idle | Flapping | 6+ hotplug events in 30s |
| Flapping | Recovering | Active recovery started (USB unbind) |
| Recovering | Download Mode | GPIO recovery succeeds (BOOT held LOW) |
| Recovering | Idle | No-GPIO rebind succeeds (device stable) |
| Recovering | Flapping | No-GPIO rebind fails (flapping resumes, up to 4 retries) |
| Download Mode | Idle | `POST /api/serial/release` (BOOT released, EN pulsed) |
| Flapping | Idle | Cooldown expires passively (fallback) |
| Idle | Debugging | `POST /api/debug/start` — starts OpenOCD (FR-024/025/026) |
| Debugging | Idle | `POST /api/debug/stop` — stops OpenOCD, restarts proxy |
| Idle | *any mode* | `POST /api/slot/acquire` granted (FR-031) |
| *any mode* | Idle | `POST /api/slot/release`, or the lease expires unrenewed (FR-032) |
| *any mode* | *unchanged* | A conflicting `acquire` — refused 409, the incumbent keeps the slot (FR-033) |
| *any* | *unchanged* | An `acquire` while an unexpected process holds the devnode — refused 409 naming it (FR-034) |

Every transition into a non-idle mode is a grant by the access manager
(FR-031). A consumer that changes a slot's mode without acquiring is outside
the manager's knowledge, and the manager's guarantees do not hold for that
slot until it returns to `idle`.

**WiFi Service (wlan0):**

| State | Description |
|-------|-------------|
| Idle | wlan0 not in use for testing |
| Captive | wlan0 joined DUT's portal AP as STA (Pi at 192.168.4.x, DUT at 192.168.4.1) |
| AP | wlan0 running test AP (Pi at 192.168.4.1, DUT connects at 192.168.4.x) |

State transitions:

| From | To | Trigger |
|------|----|---------|
| Idle | Captive | `POST /api/wifi/sta_join` to DUT's captive portal AP |
| Captive | Idle | `POST /api/wifi/sta_leave` |
| Idle | AP | `POST /api/wifi/ap_start` |
| Captive | AP | `POST /api/wifi/ap_start` (stops STA, starts AP) |
| AP | Idle | `POST /api/wifi/ap_stop` |
| AP | Captive | `POST /api/wifi/sta_join` (stops AP, joins network) |

**Note:** Serial-interface mode (wlan0 for LAN) is a separate operating mode
that disables the WiFi test service entirely (see §1.4).

---

## 2. Definitions

| Entity | Description |
|--------|-------------|
| **Slot** | A fixed position (`SLOT1`, `SLOT2`, ..., `SLOTn`) pre-created at boot. The slot count `n` is determined at startup by auto-detection of the Pi's USB hub topology (one slot per usable hub port, see FR-002), or by explicit configuration in `testbench.json` if present. Each slot is mapped to a physical USB hub port by prefix match and is always visible in the UI. A slot can track multiple devnodes when a dual-USB board (e.g., ESP32-S3 with sub-hub) is connected. |
| **slot_key** | Stable identifier for physical port topology (derived from udev `ID_PATH`). Multiple slot_keys can map to the same slot via prefix matching (e.g., `0:1.1:1.0` and `0:1.1.4:1.0` both match SLOT1's prefix `0:1.1`). |
| **usb_prefix** | Substring of `ID_PATH` that identifies a physical hub port (configured in `testbench.json`). Longer prefixes match first, so a sub-hub port like `0:1.1.4` can be distinguished from its parent `0:1.1`. |
| **devnode** | Current tty device path (e.g., `/dev/ttyACM0`) — may change on reconnect |
| **proxy** | RFC2217 server process for a serial device: `plain_rfc2217_server.py` for all devices (direct DTR/RTS passthrough) |
| **seq** (sequence) | Global monotonically increasing counter, incremented on every hotplug event |
| **Mode** | Operating mode: `wifi-testing` (wlan0 = instrument) or `serial-interface` (wlan0 = LAN) |

### Key Principle: Slot-Based Identity

The system keys on physical connector position, NOT on `/dev/ttyACMx`
(changes on reconnect), serial number (two identical boards would conflict),
or VID/PID (not unique).

`slot_key` = udev `ID_PATH` ensures:
- Same physical connector → same TCP port (always)
- Device can be swapped → same TCP port
- Two identical boards → different TCP ports (different slots)

---

## 3. Serial Interface

### FR-001 — Event-Driven Hotplug

**Plug flow:**
1. udev emits `add` event for the serial device
2. udev rule invokes `rfc2217-udev-notify.sh` via `systemd-run --no-block`
3. Notify script sends `POST /api/hotplug` with `{action, devnode, id_path, devpath}`
4. Portal determines `slot_key` from `id_path` (or `devpath` fallback)
5. Portal increments global `seq_counter`, records event metadata on the slot
6. Portal spawns a background thread that acquires the slot lock, waits for the device to settle, then starts the proxy bound to `devnode` on the configured TCP port
7. Slot state becomes `running=true`, `present=true`

**Unplug flow:**
1. udev emits `remove` event
2–4. Same notification path as plug
5. Portal increments `seq_counter`, records metadata
6. Portal stops the proxy process in a **background thread** (non-blocking,
   so the single-threaded HTTP server can immediately process the subsequent
   `add` event from USB re-enumeration)
7. Slot state becomes `running=false`, `present=false`

**USB re-enumeration (esptool reset/flash):**
When esptool performs a watchdog reset or flash operation, the ESP32-C3's
USB-Serial/JTAG controller disconnects and reconnects.  This triggers a
`remove` → `add` hotplug sequence.  The portal handles this automatically:
the proxy is stopped on `remove` and restarted on `add` (with the 2s
ttyACM boot delay).  No manual intervention is required.

**Fixed slot pre-creation:** On startup the portal produces a slot list,
either by loading `testbench.json` (if present) or by auto-detecting the
Pi's USB hub topology (see "Auto-detection" below). The result is `n`
slots labelled `SLOT1..SLOTn`, each with a `usb_prefix` that maps to a
physical USB hub port. `n` is hardware-dependent, not hard-coded — a Pi
Zero 2 W with a 4-port hub yields 3–4 slots, a Pi 3B+ yields 4, a Pi 4B
or Pi 5 yields 4. Slots are always visible in `/api/devices` and the web
UI, even when no devices are connected (state = `absent`).

**USB prefix matching:** When a device's `slot_key` (from udev `ID_PATH`)
contains a slot's `usb_prefix`, that device belongs to that slot. Longer
prefixes match first. Multiple devices can map to the same slot (dual-USB
boards with sub-hubs). Each slot tracks all its devnodes and remains
`present` as long as any devnode is active.

**Boot scan:** The portal scans `/dev/ttyACM*` and `/dev/ttyUSB*`, queries
`udevadm info` for each, and maps each device to its fixed slot by prefix.
The first devnode to arrive becomes the primary (used for the RFC2217 proxy).

**Hotplug:** On add, the portal matches the `slot_key` against configured
prefixes and adds the devnode to the matching slot. On remove, the devnode
is removed from the slot's set — the slot only goes absent when all devnodes
are gone. If no prefix matches, a dynamic slot (AUTO-N) is created.

**USB device scanning:** After every hotplug event and at boot, the portal
scans sysfs (`/sys/bus/usb/devices/`) for all USB devices on each slot's
prefix. This includes non-serial devices (HID keyboards, mass storage) which
are reported in the `usb_devices` field of `/api/devices`.

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-001 | Plug a device into a configured slot | Within 10 s the slot reports `present: true, running: true` and its `tcp_port` accepts a connection | The slot staying absent; a proxy bound to the wrong devnode; the port answering before the device settled |
| FR-001 | Unplug it | The slot reports `present: false, running: false` and the proxy process is gone | A stopped slot leaving an orphan proxy holding the devnode |
| FR-001 | Trigger a USB re-enumeration by flashing | `remove` then `add` are handled and the proxy returns without manual action | The portal blocking on `remove` so the following `add` is missed |

### FR-002 — Slot Configuration

**Note (v9):** Slots are configured in `testbench.json` with USB path prefixes
that map physical hub ports to fixed labels. The portal pre-creates all
configured slots at boot. Devices are matched to slots by prefix — no manual
slot assignment needed at runtime. Dual-USB boards (sub-hub) are handled
transparently via prefix matching.

Configuration file: `/etc/rfc2217/testbench.json`

```json
{
  "gpio_boot": 18,
  "gpio_en": 17,
  "slots": [
    {"label": "SLOT1", "usb_prefix": "0:1.1", "tcp_port": 4001, "gdb_port": 3333, "openocd_telnet_port": 4444},
    {"label": "SLOT2", "usb_prefix": "0:1.3", "tcp_port": 4002, "gdb_port": 3334, "openocd_telnet_port": 4445},
    {"label": "SLOT3", "usb_prefix": "0:1.4", "tcp_port": 4003, "gdb_port": 3335, "openocd_telnet_port": 4446}
  ],
  "debug_probes": [
    {"label": "PROBE1", "type": "esp-prog", "interface_config": "interface/ftdi/esp_ftdi.cfg", "bus_port": "1-1.4:1.0"}
  ]
}
```

The `usb_prefix` is a substring of the udev `ID_PATH`. Discover it by
plugging a device into each physical port and running:
`udevadm info -q property -n /dev/ttyACMx | grep ID_PATH`

**Auto-detection (no config):** If `/etc/rfc2217/testbench.json` is absent,
the portal auto-generates the slot list at startup by walking
`/sys/bus/usb/devices/`, enumerating every downstream hub, and emitting one
slot per port (`SLOT1..SLOTn` with default TCP/GDB/OpenOCD ports). Ports
bound to non-serial drivers (USB Ethernet, storage, HID) are filtered out.

**Phantom-port filter:** Some Pi boards advertise more hub ports than the
PCB wires to physical USB-A jacks. An unwired port is indistinguishable
from an empty wired jack via sysfs alone, so `pi/portal.py` keeps a
per-model lookup table (`_PHANTOM_PORTS_BY_MODEL`) keyed on
`/proc/device-tree/model` that names the unwired `usb_prefix` values to
skip. Current entries:

| Pi model | Phantom prefix(es) |
|----------|--------------------|
| Raspberry Pi 3 Model B Plus | `0:1.4` |

Adding a new model: plug devices into every physical jack, compare
`[portal] auto-detected N USB hub port(s): [...]` against the occupied
jack count, and add any unoccupied prefix(es) to the table.

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-002 | Move a device from one physical connector to another | It appears under the slot belonging to the new connector, with that slot's TCP port | The same TCP port following the device rather than the connector |
| FR-002 | Plug two identical boards into different connectors | Two slots, two distinct TCP ports | Both resolving to one slot because their USB ids match |
| FR-002 | Boot with more advertised hub ports than exist | Phantom ports are absent from `/api/devices` | Slots offered for connectors that cannot hold a device |

### FR-003 — Serial API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /api/devices | List all slots with status |
| POST | /api/hotplug | Receive udev hotplug event (add/remove) |
| POST | /api/start | Manually start proxy for a slot `{"slot"}`, optional `devnode` |
| POST | /api/stop | Manually stop proxy for a slot `{"slot"}` |
| GET | /api/info | Pi IP, hostname, slot counts |
| POST | /api/serial/reset | Reset device — DTR/RTS, or JTAG while a debug session is active (FR-008) |
| POST | /api/serial/monitor | Read serial output with pattern match (FR-009) |

**GET /api/devices** returns:

```json
{
  "slots": [
    {
      "label": "SLOT1",
      "slot_key": "_fixed_SLOT1",
      "tcp_port": 4001,
      "present": true,
      "running": true,
      "devnode": "/dev/ttyACM0",
      "devnodes": ["/dev/ttyACM0", "/dev/ttyACM1"],
      "pid": 1234,
      "url": "rfc2217://$BENCH:4001",
      "seq": 5,
      "last_action": "add",
      "last_event_ts": "2026-02-05T12:34:56+00:00",
      "last_error": null,
      "flapping": false,
      "state": "idle",
      "detected_chip": "esp32s3",
      "debugging": false,
      "debug_chip": null,
      "debug_gdb_port": null,
      "usb_devices": [
        {"product": "USB JTAG/serial debug unit", "vid_pid": "303a:1001"},
        {"product": "USB Single Serial", "vid_pid": "1a86:55d3"}
      ]
    }
  ],
  "host_ip": "$BENCH",
  "hostname": "$BENCH"
}
```

**POST /api/hotplug** body: `{action, devnode, id_path, devpath}`.

**POST /api/start** body: `{slot_key, devnode}`.

**POST /api/stop** body: `{slot_key}`.

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-003 | `GET /api/devices` with one device present | JSON listing every configured slot, the present one carrying `detected_chip` and `devnode` | A present device missing from the list; a slot reported present with no devnode |
| FR-003 | `POST /api/stop` then `/api/start` for a slot | The proxy stops and returns on the same TCP port | The port changing across a stop/start cycle |

### FR-004 — Serial Traffic Logging

- Serial traffic is observable via RFC2217 clients (e.g. pyserial).

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-004 | Attach an RFC2217 client while a device is emitting | The client receives the device's output | Output reaching only the portal and not an attached client |

### FR-005 — Web Portal (Serial Section)

- Display all 3 slots (always visible, even if empty)
- Show slot status: RUNNING / IDLE / ABSENT / RECOVERING / DOWNLOAD MODE
- Show current devnode(s) and PID when running
- Show detected chip type (e.g., ESP32-C6) when identified via JTAG
- Show debug status: active GDB port or idle
- Show USB devices on each physical port (including HID, mass storage)
- Show GPIO config (BOOT/EN pins) in header subtitle
- Copy RFC2217 URL to clipboard (hostname and IP variants)

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-005 | Load the portal page with one slot present and one absent | Each slot's state is shown and matches `/api/devices` | The page showing a state the API contradicts |

### FR-006 — ESP32-C3 Native USB-Serial/JTAG Support

ESP32-C3 (and ESP32-S3) chips with native USB use a built-in USB-Serial/JTAG
controller that maps to `/dev/ttyACM*` on Linux (CDC ACM class).  This differs
fundamentally from UART bridge chips (CP2102, CH340 → `/dev/ttyUSB*`) in how
DTR/RTS signals are interpreted.

#### 6.1 USB-Serial/JTAG Signal Mapping

| Signal | GPIO | Function |
|--------|------|----------|
| DTR | GPIO9 | Boot strap: DTR=1 → GPIO9 LOW → **download mode** |
| RTS | CHIP_EN | Reset: RTS=1 → chip held in **reset** |

The Linux `cdc_acm` kernel driver asserts **both DTR=1 and RTS=1** in
`acm_port_activate()` on every port open.  This puts the chip into download
mode during the boot-sensitive phase.

#### 6.2 Proxy Selection

The portal uses `plain_rfc2217_server.py` for **all** device types:

| devnode | Device Type | Server |
|---------|-------------|--------|
| `/dev/ttyACM*` | Native USB (CDC ACM) | `plain_rfc2217_server.py` |
| `/dev/ttyUSB*` | UART bridge (CP2102/CH340) | `plain_rfc2217_server.py` |

`plain_rfc2217_server.py` passes DTR/RTS directly to the serial port — esptool
on the client side implements the correct reset sequences for each chip type.

#### 6.3 Controlled Boot Sequence (plain_rfc2217_server.py)

When `plain_rfc2217_server.py` opens the serial port, it performs a controlled
boot sequence to ensure the chip boots in SPI mode (not download mode):

```python
ser = serial.serial_for_url(port, do_not_open=True, exclusive=True)
ser.timeout = 3
ser.dtr = False   # Pre-set: GPIO9 HIGH (SPI boot)
ser.rts = False   # Pre-set: not in reset
ser.open()
# Linux cdc_acm still asserts DTR+RTS on open, but pyserial immediately
# applies the pre-set values in _reconfigure_port()

# Clear HUPCL to prevent DTR assertion on close
attrs = termios.tcgetattr(ser.fd)
attrs[2] &= ~termios.HUPCL
termios.tcsetattr(ser.fd, termios.TCSANOW, attrs)

ser.dtr = False   # GPIO9 HIGH — select SPI boot
time.sleep(0.1)   # Let USB-JTAG controller latch DTR=0
ser.rts = False   # Release reset — chip boots normally
time.sleep(0.1)
```

#### 6.4 Device Settle Check (ttyACM)

For ttyACM devices, `wait_for_device()` checks only that the device node
exists — it does **not** call `os.open()`, because opening the port would
assert DTR/RTS and put the chip into download mode:

```python
def wait_for_device(devnode, timeout=5.0):
    is_native_usb = devnode and "ttyACM" in devnode
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(devnode):
            if is_native_usb:
                return True  # Don't open — avoids DTR reset
            # ttyUSB: probe with open as before
            try:
                fd = os.open(devnode, os.O_RDWR | os.O_NONBLOCK)
                os.close(fd)
                return True
            except OSError:
                pass
        time.sleep(0.1)
    return False
```

#### 6.5 Hotplug Boot Delay (ttyACM)

When a ttyACM device is hotplugged (USB re-enumeration after reset/flash),
the portal delays proxy startup by `NATIVE_USB_BOOT_DELAY_S` (2 seconds)
to allow the chip to boot past the download-mode-sensitive phase before the
proxy opens the serial port:

```python
NATIVE_USB_BOOT_DELAY_S = 2

def _bg_start(s=slot, lk=lock, dn=devnode):
    if dn and "ttyACM" in dn:
        time.sleep(NATIVE_USB_BOOT_DELAY_S)
    with lk:
        # ... start proxy
```

#### 6.6 Reset Types (Core vs System)

| Reset Type | Mechanism | Re-samples GPIO9? | Result on USB-Serial/JTAG |
|------------|-----------|-------------------|---------------------------|
| Core reset | RTS toggle (DTR/RTS sequence) | **No** | Stays in current boot mode |
| System reset | Watchdog timer (RTC WDT) | **Yes** | Boots based on physical pin state |

**Critical:** After entering download mode, only a **system reset** (watchdog)
can return the chip to SPI boot mode.  Core reset (RTS toggle) keeps the chip
in download mode because GPIO9 is not re-sampled.

#### 6.7 Flashing via RFC2217

Flashing uses esptool from the host over the RFC2217 proxy.  Binaries
stay on the host — no SCP or file upload needed.  After flash, the
client calls `POST /api/serial/reset` to reboot the device.

**Design constraint:** The Pi Zero 2 W's `dwc_otg` USB driver crashes
when two processes hold the same USB serial device open simultaneously.
The portal never opens serial devices directly — only the RFC2217 proxy
holds the serial port.  esptool connects through the proxy as a client.

**Flash flow:**

1. Stop debug if active (`POST /api/debug/stop`) — native USB chips
   share serial and JTAG on the same USB interface
2. Run esptool from the host with `--after no-reset` (avoids USB
   re-enumeration that crashes `dwc_otg`)
3. Reboot device: `POST /api/serial/reset`
4. Restart debug: `POST /api/debug/start`

**Key esptool flags and offsets:**

| Device | Bootloader offset | `--before` | `--after` |
|--------|------------------|-----------|----------|
| ESP32 (ttyUSB) | `0x1000` | `default-reset` | `no-reset` |
| ESP32-C3/S3/C6/H2 (ttyACM) | `0x0000` | `default-reset` | `no-reset` |

**Example:**

```bash
esptool --port rfc2217://$BENCH:4001 --chip esp32c3 \
  --before default-reset --after no-reset \
  write-flash --flash-mode dio --flash-size 4MB \
  0x0000 bootloader.bin 0x8000 partition-table.bin 0x10000 firmware.bin

curl -X POST http://$BENCH:8080/api/serial/reset \
  -H "Content-Type: application/json" -d '{"slot":"SLOT1"}'
```

**Note:** A harmless RFC2217 parameter negotiation error may appear at
the end of flashing — the flash and verify still complete successfully.

#### 6.7.1 Local flashing via `POST /api/flash`

**When to use:** classic ESP32 boards behind a USB-serial bridge (CP2102,
CH340, CH9102) whose external DTR/RTS auto-reset circuit cannot be driven
reliably through the RFC2217 proxy — esptool reaches the chip but it stays
in normal boot (`Wrong boot mode detected (0x13)`). Native-USB C3/S3/C6/H2
chips strap into the bootloader internally and flash fine over RFC2217
(§6.7); `/api/flash` is the method for the bridge-chip case.

**Behavior:** the portal runs esptool **locally on the Pi**, where the
DTR/RTS reset works natively. It sets the slot state to `flashing`, stops
the slot's proxy (freeing the devnode), runs `esptool write-flash`, then
restarts the proxy and returns to `idle`. The request is **refused if a
debug session is active** on the slot (stop debug first).

**Request:** `POST /api/flash`, `multipart/form-data`:

| Part | Kind | Meaning |
|------|------|---------|
| `slot` | field | Slot label, e.g. `SLOT3` (required) |
| `chip` | field | esptool chip, default `auto` |
| `baud` | field | Flash baud, default `921600` |
| `erase` | field | `1`/`true` to erase-all first (optional) |
| `bin@0x1000`, `bin@0x8000`, … | file | One file part per flash image. The part name is **`bin@` followed by the hex offset**; a part named with the bare offset is stored but never flashed |
| `flash_args` | file | ESP-IDF's `build/flash_args`. Offsets then come from it, and each image's part name must equal its basename |

Standard ESP32 (Arduino) layout: `0x1000` bootloader, `0x8000` partitions,
`0xe000` boot_app0, `0x10000` firmware. On C3/S3/C6/H2 the bootloader is at
`0x0`, and an ESP-IDF project has no `boot_app0`.

**Response:** `{"ok": bool, "returncode": int, "output": "<esptool stdout+stderr>",
"error": <string if failed>}`.

**Example:**

```bash
curl -X POST http://$BENCH:8080/api/flash \
  -F slot=SLOT3 -F chip=esp32 -F baud=921600 \
  -F 'bin@0x1000=@.pio/build/<env>/bootloader.bin' \
  -F 'bin@0x8000=@.pio/build/<env>/partitions.bin' \
  -F 'bin@0xe000=@boot_app0.bin' \
  -F 'bin@0x10000=@.pio/build/<env>/firmware.bin'
```

The portal fixes `--before default_reset --after hard_reset`; the request
carries no reset flags. No `POST /api/serial/reset` is needed afterwards.

#### 6.7.1a Reading flash back via `POST /api/flash/read`

The read counterpart to `/api/flash`: pulls a flash region off a slot's device
without OpenOCD — a coredump partition, an NVS blob, the live partition table.

**Request:** `POST /api/flash/read`, JSON body — `slot`/`slot_key` (required),
`offset` and `length` (int or `0x…` string, required), `chip` (default
`auto`), `baud` (default `460800`).

**Response:** `{"ok": true, "offset", "length", "sha256", "data_b64"}` — the
bytes base64-encoded with a SHA-256 to verify.

**Behavior:** same proxy lifecycle as a flash — the portal stops the slot's
proxy, runs `esptool read-flash` locally, restarts the proxy; the running
firmware is disturbed exactly as by a flash. An active debug session must be
stopped first: OpenOCD holds the port and esptool cannot open it.

#### 6.7.2 Over-the-air flashing via `POST /api/ota`

**When to use:** a board that is **no longer on a USB slot** — deployed on the
LAN and running ArduinoOTA. A LAN host can OTA a board directly (`espota`,
`pio run -t upload --upload-port <ip>`); this endpoint exists for clients that
**cannot** make ArduinoOTA's reverse TCP connection back to themselves — e.g. a
**NAT'd container or an off-site agent**. The Pi is on the LAN, so it relays the
push. It is the network sibling of `/api/flash` (USB esptool).

**Behavior:** the portal writes the uploaded image to a temp file and runs
`espota.py` (installed to `/usr/local/bin/espota.py`) against the target. It
touches no slot and no proxy — the board is off-USB. A failed OTA does not brick
the board: the ESP32 only switches partitions on a verified success.

**Request:** `POST /api/ota`, `multipart/form-data`:

| Part | Kind | Meaning |
|------|------|---------|
| `firmware` | file | The `.bin` image (part name `firmware`, required) |
| `target` | field | Board IP or hostname, e.g. `192.168.4.42` / `device.local` (required) |
| `port` | field | ArduinoOTA port, default `3232` |
| `auth` | field | OTA password, if the board sets one (optional) |

**Response:** `{"ok": bool, "returncode": int, "output": "<espota output>",
"error": <string if failed>}`. `504` if espota times out (board unreachable).

**Example:**

```bash
curl -X POST http://$BENCH:8080/api/ota \
  -F target=192.168.0.176 \
  -F firmware=@.pio/build/<env>/firmware.bin
```

#### 6.7.3 Chip identity via `POST /api/chip/info`

**When to use:** before choosing a flash size or a partition table, and whenever
a board's identity is in doubt. It reports the **physical** flash size, which
nothing else on the bench can tell you: a build writes its configured size into
the image header and the bootloader prints that value back, so a boot log only
repeats the configuration. Configuring more than the part holds places the
partition table past the end of flash — the build and the flash both succeed,
and the damage appears later as corruption at whatever offset first exceeds the
device.

**Behavior:** the portal runs `esptool flash_id` locally on the Pi with the same
lifecycle as `/api/flash` — stop the proxy, drive the device, restart the proxy.
**It reboots the DUT**, so it is a deliberate call and never a field on
`/api/devices`, which is polled. Refused with `409` while a debug session holds
the slot.

**Request:** `POST /api/chip/info`, JSON: `{"slot": "SLOT3", "chip": "auto"}`.

**Response:** `{"ok", "chip", "revision", "features", "crystal", "usb_mode",
"mac", "flash_size", "flash_manufacturer", "flash_device", "output",
"returncode"}`. Every identity field is optional — esptool's wording differs
between major versions, and a missing key is preferable to a wrong one; `output`
always carries the raw text.

**Example:**

```bash
curl -X POST http://$BENCH:8080/api/chip/info \
  -H 'Content-Type: application/json' -d '{"slot": "SLOT3"}'
```

#### 6.8 RFC2217 Client Best Practices (ttyACM)

When connecting to an ESP32-C3 via RFC2217, the client must prevent DTR
assertion during connection negotiation:

```python
ser = serial.serial_for_url('rfc2217://$BENCH:4001', do_not_open=True)
ser.baudrate = 115200
ser.timeout = 2
ser.dtr = False   # CRITICAL: prevents download mode
ser.rts = False   # CRITICAL: prevents reset
ser.open()
```

**Never** use `serial.Serial('rfc2217://...')` directly — it opens the port
immediately and the RFC2217 negotiation may toggle DTR/RTS.

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-006 | Attach to a native-USB slot's RFC2217 port with DTR and RTS cleared before opening | The device continues running and its output is received | The device entering download mode or halting because the connection asserted the control lines |
| FR-006 | Flash a native-USB part over the API | The image is written and the part boots it | A flash that reports success while the part is left in download mode |

### FR-008 — Serial Reset

Reset a device via DTR/RTS signals, providing a clean boot cycle without
requiring SSH access to the Pi.

**Endpoint:** `POST /api/serial/reset`

**Request body:**
```json
{"slot": "SLOT2"}
```

**Procedure:**
1. Stop the RFC2217 proxy for the slot
2. Open direct serial (`/dev/ttyACMx`) with `dtr=False, rts=False`
3. Send DTR/RTS reset pulse: DTR=1, RTS=1 for 50ms, then release both
4. Wait for device to boot — read serial until first output line or 5s timeout
5. Close serial connection
6. Wait `NATIVE_USB_BOOT_DELAY_S` (2s), then restart the proxy (DTR/RTS reset
   does not cause USB re-enumeration, so hotplug won't restart it automatically)

**Response:**
```json
{"ok": true, "output": ["ESP-ROM:esp32c3-api1-20210207", "Boot count: 1"]}
```

**Error:** Returns `{"ok": false, "error": "..."}` if slot not found, device
not present, or serial open fails.

**Used by:** flapping recovery (FR-007), integration tests

#### 8.2 JTAG Reset (when debugging is active)

When an OpenOCD debug session is active for the slot, `/api/serial/reset`
automatically uses JTAG reset instead of the DTR/RTS serial sequence.

**Advantages over DTR/RTS reset:**
- No USB re-enumeration — the USB-Serial/JTAG controller stays connected
- No flapping risk — the device node doesn't disappear and reappear
- No boot delay needed — the chip resets internally
- Works even when the serial port is unresponsive

**JTAG reset procedure:**
1. Send `reset run` command to OpenOCD via its telnet interface
2. The chip resets and boots normally
3. Serial proxy remains running — no restart needed
4. OpenOCD session remains active

**Fallback:** If no debug session is active, the existing DTR/RTS serial
reset (§8.1) is used. The caller does not need to know which method was
selected — the API auto-selects.

**Verification contract**

FR-008 carries a full contract because a naive observation cannot distinguish
a device that reset from one that was already silent.

```yaml
id: FR-008
verification:
  preconditions:
    - A device is present on the slot and emitting a periodic marker.
    - Its boot output contains an identifiable first line.
  stimulus:
    - POST /api/serial/reset for the slot.
  expected_observations:
    - The response carries boot output beginning at the device's first line.
    - The proxy is running again afterwards and the slot reports idle.
    - The periodic marker resumes.
  timing: boot output returned within 5 s of the reset
  tolerance: "+2 s"
  prohibited_outcomes:
    - An empty output list reported as success.
    - The device left in download mode.
    - The proxy left stopped.
    - A native-USB part reset by DTR/RTS while a debug session is active,
      rather than by JTAG.
  tier: bench
  evidence:
    - The response body.
    - Slot state before and after.
  cleanup:
    - Confirm the slot is idle with its proxy running.
```

**Both reset paths return the device's boot output.** A JTAG reset does not
re-enumerate USB, so the proxy stays up and the boot lines are read through it.
The listener attaches **before** the reset is issued: a banner printed while
nothing is reading is gone, and a monitor opened afterwards reports a silence
indistinguishable from a device that never started. `output` therefore carries
boot lines in both paths, and OpenOCD's own reply is returned separately as
`openocd`.

### FR-009 — Serial Monitor

Read serial output from a device, optionally waiting for a pattern match.
Uses the RFC2217 proxy (non-exclusive) so the proxy stays running.

**Endpoint:** `POST /api/serial/monitor`

**Request body:**
```json
{"slot": "SLOT2", "pattern": "Boot count", "timeout": 10}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| slot | string | Yes | — | Slot label (e.g. "SLOT2") |
| pattern | string | No | null | Substring to match in serial output |
| timeout | number | No | 10 | Max seconds to wait |

**Procedure:**
1. Connect to the slot's RFC2217 proxy (non-exclusive read)
2. Read serial lines until pattern is matched or timeout expires
3. Return all captured output and match result

**Response (pattern matched):**
```json
{"ok": true, "matched": true, "line": "Boot count: 1", "output": ["ESP-ROM:...", "Boot count: 1"]}
```

**Response (timeout, no pattern):**
```json
{"ok": true, "matched": false, "line": null, "output": ["line1", "line2"]}
```

**Used by:** flapping recovery (FR-007), test verification

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-009 | Monitor with a pattern the device emits | `matched: true`, `line` carrying the matching line | `matched: true` with an empty `line` |
| FR-009 | Monitor with a pattern the device never emits, over a window spanning several markers | `matched: false` and `output` carrying every line seen in the window | An empty `output` where the device was emitting — silence is a claim about the observer until a positive control says otherwise |
| FR-009 | Monitor while another consumer holds the slot | Refused per FR-033, naming the holder | A monitor that returns no lines because the port was taken, indistinguishable from a silent device |

### FR-007 — USB Flap Detection & Recovery

When a device enters a boot loop (crash → reboot → crash every ~2-3s), the
Pi sees rapid USB connect/disconnect cycles.  Without protection, the portal
spawns a new proxy thread for every "add" event, and the udev event flood
(246+ disconnects observed) can make a 416MB Pi unreachable via SSH.

#### 7.1 Detection

```python
FLAP_WINDOW_S = 30       # Look at events within this window
FLAP_THRESHOLD = 10      # 10 events in 30s — allows dual-USB devices (2 events per plug)
FLAP_COOLDOWN_S = 10     # Cooldown before recovery attempt
FLAP_MAX_RETRIES = 2     # Max no-GPIO recovery attempts
```

Each slot tracks `_event_times[]` — timestamps of recent hotplug events.
When the count within the window exceeds the threshold, the slot enters
`flapping=true` state and active recovery begins immediately.

#### 7.2 USB Unbind — Stopping the Storm

On flap detection, the portal **unbinds the USB device at the kernel level**
by writing the sysfs device name (e.g. `1-1.1.2`) to
`/sys/bus/usb/drivers/usb/unbind`.  This immediately stops the event storm —
no more udev events, no more hotplug notifications.

The slot_key (e.g. `platform-3f980000.usb-usb-0:1.1.2:1.0`) is parsed to
extract the sysfs USB device name using `rfind("usb-")` to skip the
controller name.

While `_recovering=true`, all hotplug events for the slot are **ignored**
(early exit in the handler).  This prevents the unbind's own synthetic udev
remove event from interfering with recovery state.

#### 7.3 Recovery — GPIO Path

For slots with `gpio_boot` and optionally `gpio_en` configured in
`testbench.json`, the portal performs automatic GPIO-based recovery:

1. Wait `FLAP_COOLDOWN_S` (10s) for hardware to settle
2. Hold BOOT/GPIO0 LOW via `gpio_boot` pin (forces download mode)
3. Pulse EN/RST via `gpio_en` pin if configured (clean reset)
4. Rebind USB (`/sys/bus/usb/drivers/usb/bind`) — device enumerates
   in download mode (stable, no crash loop)
5. State → `download_mode`; BOOT stays held LOW

The device is now stable in the bootloader.  Flash firmware directly on
the Pi (the RFC2217 proxy is not running in this state):

```bash
ssh pi@$BENCH "python3 -m esptool --chip esp32s3 --port /dev/ttyACM1 \
  write_flash 0x0 bootloader.bin 0x8000 partition-table.bin \
  0xf000 ota_data_initial.bin 0x20000 app.bin"
```

After flashing, release GPIO and reboot:

```
POST /api/serial/release {"slot": "SLOT1"}
```

This sets BOOT to high-Z (input with pull-up), pulses EN for a clean
reboot, and transitions the slot back to `idle`.

**JTAG-based recovery (when debugging is active):**
When an OpenOCD session is active, flapping recovery can use JTAG halt
(`monitor halt`) to stop the CPU immediately, preventing further USB
cycling. This is more reliable than the USB unbind/rebind approach
because it stops the root cause (the boot loop) rather than managing
its symptoms. JTAG halt is attempted first when available; the existing
GPIO/unbind recovery remains as fallback.

#### 7.4 Recovery — No-GPIO Path

For slots without GPIO pins, the portal uses exponential backoff:

1. Wait fixed `FLAP_COOLDOWN_S` (10s) — corrupt flash won't self-heal,
   so increasing the delay is pointless
2. Clear `_recovering`, rebind USB
3. If flapping resumes → hotplug handler detects → another recovery cycle
4. After `FLAP_MAX_RETRIES` (2) failed attempts → state stays `flapping`
   with error "needs manual intervention"
5. Flash directly on the Pi (`esptool --before=usb_reset write_flash ...`)
6. Once booted, flapping flag auto-clears on next `/api/devices` poll
   (stale events age out of `_event_times` within `FLAP_WINDOW_S`)

#### 7.5 Manual Recovery

```
POST /api/serial/recover {"slot": "SLOT1"}
```

Resets the retry counter and starts a fresh recovery cycle.  Works even
when the slot is not currently flapping.

#### 7.6 API Fields

`/api/devices` exposes per-slot recovery state:

| Field | Type | Description |
|-------|------|-------------|
| `recovering` | bool | USB unbound, recovery thread running |
| `recover_retries` | int | No-GPIO attempt counter (0-2) |
| `has_gpio` | bool | Slot has `gpio_boot` configured |
| `gpio_boot` | int/null | Pi BCM pin for BOOT/GPIO0 |
| `gpio_en` | int/null | Pi BCM pin for EN/RST |

#### 7.7 Slot Configuration

```json
{"label": "SLOT1", "slot_key": "...", "tcp_port": 4001, "gpio_boot": 18, "gpio_en": 17}
```

`gpio_boot` and `gpio_en` are optional per slot.  Slots without them use
the no-GPIO backoff path.

#### 7.8 Web UI

| State | Badge | Visual |
|-------|-------|--------|
| `flapping` | Red "FLAPPING" | Warning + "Retry Recovery" button |
| `recovering` | Amber "RECOVERING" (pulsing) | Progress message |
| `download_mode` | Green "DOWNLOAD MODE" | "Release & Reboot" button |

Polling interval reduced from 2s to 5s to lower load on resource-constrained Pi.

---

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-007 | Induce 6 or more hotplug events within 30 s | The slot is reported `flapping` and recovery starts | Flapping reported as ordinary hotplug; recovery never starting |
| FR-007 | Allow recovery to complete on a slot with GPIO | The slot reaches `download mode` or `idle` | Endless recovery attempts beyond the stated retry limit |

### FR-031 — Serial Access Manager

One authority decides which mode a slot is in at any moment. Today that
decision is distributed: eight portal functions call `stop_proxy()` on their
own initiative, `debug_controller` moves the chip through a channel none of
them observe, and no component can refuse a conflicting request because none
is asked. The result is that any operation silently terminates whatever was
using the slot.

**The manager arbitrates mode, never the data path.** Bytes continue to flow
exactly as they do now — esptool opens the devnode, OpenOCD opens JTAG, the
proxy serves RFC2217. The manager answers one question: *who owns this slot,
in what mode, until when.*

**Every consumer acquires before touching a slot.** That includes the portal's
own operations (FR-003, FR-008, FR-009), flap recovery (FR-007), debug
sessions (FR-024–FR-026), and any external client — a project's test suite
reaching for RFC2217 acquires like everything else. A consumer left outside
the manager silently defeats it, which is why the set is enumerated rather
than described.

**Modes** extend the slot states of §1: `absent`, `idle`, `flashing`,
`resetting`, `monitoring`, `flapping`, `recovering`, `download mode`,
`debugging`. The manager grants a mode; it does not perform the work of that
mode.

**Endpoint:** `POST /api/slot/acquire`

```json
{"slot": "SLOT1", "mode": "flashing", "owner": "ci-verify-31223471629", "ttl": 60}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| slot | string | Yes | — | Slot label |
| mode | string | Yes | — | Requested mode |
| owner | string | Yes | — | Identifies the requester in refusals and logs |
| ttl | number | No | 60 | Seconds before the grant expires unless renewed |

**Response (granted):**
```json
{"ok": true, "token": "a1b2c3", "mode": "flashing", "expires_in": 60}
```

**Response (refused):** HTTP 409, see FR-034.

**Endpoints:** `POST /api/slot/release` (`{"token": "..."}`),
`POST /api/slot/renew` (`{"token": "..."}`), `GET /api/slot/mode?slot=SLOT1`.

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-031 | Acquire `flashing` on an idle slot | 200 with a token, and `GET /api/slot/mode` reports `flashing` with that owner | A grant that does not change the reported mode; two tokens live for one slot | bench |
| FR-031 | Acquire, then read the slot's data path | Bytes still flow by the mode's own mechanism | The manager interposing itself in the data path | bench |

### FR-032 — Leases and reclaim

A grant expires. `ttl` defaults to **60 s** and a holder renews while it
works, so a long flash keeps its grant and a dead client does not keep the
slot.

This exists because the failure it prevents is observed: an RFC2217 client
whose reader thread dies holds the proxy's single connection slot with no way
to reclaim it, and the bench then answers every later request with silence.
An expiry turns that into a bounded wait and a named reclaim.

**Procedure:** on expiry the manager records the reclaim with the previous
owner and returns the slot to `idle`. It does not attempt to terminate the
previous holder's process — a holder that is still alive discovers its token
is invalid on its next call.

**Verification contract**

```yaml
id: FR-032
verification:
  preconditions:
    - A slot is idle.
    - A client acquires `monitoring` with ttl 60 and then stops renewing,
      simulating a dead holder.
  stimulus:
    - Wait 60 s without renewing.
    - Acquire the same slot from a second owner.
  expected_observations:
    - The first grant is reported as expired, naming the previous owner.
    - The second acquire is granted.
    - The elapsed time from expiry to grant is under 5 s.
  timing: grant available within 65 s of the last renewal
  tolerance: "+5 s"
  prohibited_outcomes:
    - The slot remains held indefinitely.
    - The manager kills the previous holder's process.
    - The second owner is granted before the lease has expired.
    - A renewing holder loses its grant.
  tier: bench
  evidence:
    - Acquire and renew timestamps.
    - The refusal body before expiry and the grant body after.
  cleanup:
    - Release the second grant.
```

### FR-033 — Refusal, never pre-emption

A conflicting request is refused. The incumbent keeps the slot.

**Response (refused):** HTTP 409

```json
{"ok": false, "error": "held", "mode": "debugging",
 "owner": "gdb-session-4", "since": "2026-08-08T09:14:22Z", "expires_in": 37}
```

The body names the mode, the owner and how long it has been held, because a
refusal that says only "busy" leaves the caller with the same mystery the
manager exists to remove.

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-033 | Hold `debugging`; request `flashing` | 409 naming mode, owner and since-time | 200; the debug session being stopped; a 409 body without the incumbent's identity | bench |
| FR-033 | Hold `flashing`; request `flashing` from a second owner | 409 | Both owners believing they hold it | bench |

### FR-034 — Out-of-band detection

The manager is cooperative: it cannot stop a process opening `/dev/ttyACMx`
directly. It can, however, **notice**.

Before granting, the manager inspects the open file descriptors of running
processes for the slot's device node. If a holder is found that is not the
expected one for the current mode, the request is refused naming the process
and its command line.

This converts the failure that costs the most time — an operation that
mysteriously reads nothing — into a named refusal.

**A squatter on the RFC2217 port is not covered, and should be.** The
detection above looks for openers of the *device node*. A consumer that
reaches a slot over RFC2217 holds a **TCP socket** instead, and the devnode is
held — legitimately — by the bench's own proxy, so nothing unexpected appears
in `/proc`. Observed: an `esptool` killed mid-flash left the connection in
`CLOSE-WAIT`, which wedged the slot against every later client. The lease of
FR-032 does not help either; it reclaims a dead owner's *grant*, not a dead
client's *socket*. Two mitigations exist today — the proxy no longer blocks
indefinitely on a stuck client, and FR-036 restarts each slot's proxy — but
neither is detection, and a wedged slot is still reported to whoever meets it
next as "the port is busy" rather than as a named holder. **Extending this
requirement to the RFC2217 port is an open specification item, not an
implemented behaviour.**

**Kernel enforcement, with a stated fallback.** The proxy opens the devnode
exclusively, so the kernel refuses a second opener and a stray client fails
loudly instead of silently stealing bytes or asserting the control lines.
Nothing on this bench needs a shared open: every portal path that touches the
devnode stops the proxy first. Where a bench does have another holder the
exclusive open would fail, so the proxy logs the refusal and opens
non-exclusively rather than leaving the slot dead — and FR-034's detection is
the guard there.

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-034 | Open the devnode from an unrelated process; acquire any mode | 409 naming that pid and command line | A grant issued while an unknown process holds the device; a refusal that does not identify the holder | bench |
| FR-034 | Acquire with only the expected holder present | Granted | A refusal caused by the manager's own proxy | bench |

### FR-035 — Debugging is a granted mode

OpenOCD claims a **different USB interface of the same physical device**, so a
debug session never appears as an opener of the tty and FR-034 cannot see it.
Debugging is therefore a mode the manager grants: `debug_controller` acquires
`debugging` before starting OpenOCD and releases it after stopping.

The manager does not start or stop OpenOCD. It records that the slot is in
that mode so every other consumer is refused with a reason.

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-035 | Start a debug session; request `flashing` | 409 naming `debugging` | A flash proceeding while OpenOCD holds the USB interface | bench |
| FR-035 | Stop the debug session; request `flashing` | Granted | The slot remaining held after the session ends | bench |

### FR-030 — Serial Write

Send bytes to a device.

Three contributors independently wrote this endpoint before it existed (PRs
#18, #7, and #5 for erase), and this project needed it too. Its absence was
not merely inconvenient: without it a caller's only option is to open the
slot's RFC2217 port directly, and RFC2217 negotiation asserts DTR and RTS —
which on a native-USB ESP32 mean download mode and reset. **The missing
endpoint made careless device-halting the default path.**

**Endpoint:** `POST /api/serial/write`

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| slot | string | Yes | — | Slot label |
| text | string | one of | — | UTF-8 text to send |
| hex | string | one of | — | Raw bytes as hex, for binary protocols |
| newline | bool | No | true | Append CRLF to `text`. A console command without one is never executed, which reads as the device ignoring it |

Writes through the proxy, which owns the device. Control lines are driven low
**before** the port opens — `serial_for_url()` asserts them on construction.

For as long as the write lasts the slot reports state `writing`, so the panel
shows which slot is being talked to. With the retry above that can be several
seconds, which is exactly the case where the operator wants to see it. The
prior state is restored afterwards rather than forced to idle — a write during
a debug session must not end with the slot claiming nothing is attached — and
only if the state is still `writing`: a reset or a flap that took the slot
meanwhile owns it.

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-030 | Write a console command to a peer that answers | `{"ok": true, "written": n}`, and the reply appears in the slot's buffer | The device resetting or entering download mode as a side effect of the write |
| FR-030 | Write to a slot that is in a debug session | The slot reports `debugging` again once the write returns | The slot left reporting `idle`, so the panel shows no live GDB session where one is running |

**Open gap — the bench owns no responder.** The first row is the only one
that proves a byte left the bench, and it needs something on the far end that
answers. The bench has nothing of its own, so the test borrowed a project's
simulator; when that project reflashed it, a testbench test went red for a
reason that had nothing to do with the testbench — the law of §"no project
tests the testbench", inverted. Until the bench can answer for itself — a
loopback slot, or entering ROM download mode on demand and observing the
SYNC reply, which is silicon behaviour no firmware can remove — the responder
is declared by the operator (`WT_ECHO_SLOT` / `WT_ECHO_CMD` /
`WT_ECHO_REPLY`) and its absence is recorded as an unmet precondition rather
than a pass.
| FR-030 | Write with neither `text` nor `hex` | 400 naming what is required | A silent no-op reported as success |
| FR-030 | Write `hex` that is not hex | 400 naming the fault | Partial bytes written |
| FR-030 | Write to an unknown slot | 404 | Bytes sent to some other slot |

### FR-036 — Bench Reset

Return the whole bench to its initial state. **Intended as the first call of
every test run.**

A test that begins from whatever the previous test left behind is measuring
history, and the resulting failures are attributed to the wrong thing — a
stopped broker, an AP left up, a slot still held, a debug session nobody
closed.

**Endpoint:** `POST /api/bench/reset`

Initial state, per subsystem:

| Subsystem | Initial state |
|---|---|
| Every present slot | proxy **restarted**, no access grant (FR-031), no debug session |
| WiFi | idle — no SoftAP, not joined |
| SDR | no live console, no logging, no capture |
| Operator prompt | none pending |
| Test session | ended |
| MQTT broker | **running** — shared infrastructure, so it is ensured, never stopped |

Every step is attempted even if an earlier one fails: a reset that gives up
halfway leaves the bench dirtier than one never run, and the caller cannot
tell which. The response reports what actually changed, so a caller can see
whether the bench was already clean.

**A slot's proxy is restarted, not merely checked.** Restarting only a proxy
that had *died* left the one case this call exists to fix: RFC2217 is
single-client, and a client that goes away without closing cleanly leaves the
connection in `CLOSE-WAIT`. The proxy is then alive, reports `running: true`,
and refuses every later client with "the port is busy" — so the check passed
and the slot stayed wedged for every subsequent run.

**What this requirement does not cover: the DUT's firmware.** A defined bench
is only half a defined starting state. A suite that flashes a slot — which
`FR-020` exists to make possible, and which the bench's own end-to-end tests
do deliberately — changes what the DUT *is* for every run that follows.
Restoring it needs an image, and the bench cannot know which image the caller
considers correct, so this is stated as the consumer's obligation rather than
silently assumed: **a test run that flashes a DUT is responsible for the state
it leaves it in.** The bench's own suite discharges this by reflashing its
known-good bench-DUT image at session start and after its flash tests.

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-036 | Hold a slot grant, raise the AP, then reset | `changed` names both; mode is `idle` and the AP is down afterwards | A reset reporting success while a subsystem is still dirty |
| FR-036 | Reset an already-clean bench | `ok: true` with an empty or near-empty `changed` | Spurious changes reported, which would make "was it dirty?" unanswerable |
| FR-036 | Reset while one subsystem errors | The remaining subsystems are still reset, and the error is reported in `errors` | Aborting on the first failure |
| FR-036 | Reset | The MQTT broker is running afterwards | The broker stopped — other work depends on it |

## 4. WiFi Service

### FR-010 — API Summary

Complete API for both Serial and WiFi services.  WiFi testbench endpoints (all
except `/api/wifi/mode` and `/api/wifi/ping`) return `{"ok": false, "error":
"WiFi testing disabled (Serial Interface mode)"}` when the system is in
serial-interface mode.

| Method | Endpoint | Description |
|--------|----------|-------------|
| **Serial** | | |
| GET | /api/devices | List all slots with status |
| POST | /api/hotplug | Receive udev hotplug event (add/remove) |
| POST | /api/start | Manually start proxy for a slot `{"slot"}`, optional `devnode` |
| POST | /api/stop | Manually stop proxy for a slot `{"slot"}` |
| GET | /api/info | Pi IP, hostname, slot counts |
| POST | /api/serial/reset | Reset device — DTR/RTS, or JTAG while a debug session is active (FR-008) |
| POST | /api/serial/monitor | Read serial output with pattern match (FR-009) |
| **WiFi** | | |
| GET | /api/wifi/ping | Version and uptime |
| GET | /api/wifi/mode | Current operating mode |
| POST | /api/wifi/mode | Switch operating mode |
| POST | /api/wifi/ap_start | Start SoftAP (WiFi state → AP); `{internet: true}` NAT-bridges to LAN (FR-011) |
| POST | /api/wifi/ap_stop | Stop SoftAP (WiFi state → Idle) |
| GET | /api/wifi/ap_status | AP status, SSID, channel, stations |
| POST | /api/wifi/sta_join | Join WiFi network as station (WiFi state → Captive) |
| POST | /api/wifi/sta_leave | Disconnect from WiFi network (WiFi state → Idle) |
| GET | /api/wifi/scan | Scan for WiFi networks |
| POST | /api/wifi/http | HTTP relay through Pi's radio |
| GET | /api/wifi/events | Event queue (long-poll supported) |
| POST | /api/wifi/lease_event | Receive dnsmasq lease callback |
| **Human Interaction** | | |
| POST | /api/human-interaction | Block until operator confirms a physical action (FR-017) |
| GET | /api/human/status | Check if a human interaction request is pending |
| POST | /api/human/done | Operator confirms action complete (wakes blocked request) |
| POST | /api/human/cancel | Operator or test script cancels request |
| **GPIO** | | |
| POST | /api/gpio/set | Drive a Pi GPIO pin low/high or release to input (FR-018) |
| GET | /api/gpio/status | Read state of all actively driven GPIO pins (FR-018) |
| **Test Progress** | | |
| POST | /api/test/update | Push test session start, step, result, or end (FR-019) |
| GET | /api/test/progress | Poll current test session state (FR-019) |
| **GDB Debug** | | |
| POST | /api/debug/start | Start OpenOCD for a slot (FR-024/025/026) |
| POST | /api/debug/stop | Stop OpenOCD, release slot/probe (FR-024/025/026) |
| GET | /api/debug/status | Debug state for all slots (FR-024/025/026) |
| GET | /api/debug/group | Slot groups and roles — dual-USB (FR-025) |
| GET | /api/debug/probes | Available debug probes — ESP-Prog (FR-026) |
| **Signal Generator** | | |
| POST | /api/siggen/start | Start RF carrier; optional Morse keying (FR-027) |
| POST | /api/siggen/stop | Stop carrier (FR-027) |
| POST | /api/siggen/freq | Retune active carrier (FR-027) |
| POST | /api/siggen/atten | Set PE4302 attenuation (FR-027) |
| GET | /api/siggen/status | Current state + hardware detection (FR-027) |
| GET | /api/siggen/frequencies | List achievable frequencies in a range (FR-027) |
| **SDR Receiver** | | |
| GET | /api/sdr/status | Dongle/tool detection + active-capture state (FR-028) |
| POST | /api/sdr/capture | Decode RF for a bounded window (FR-028) |
| POST | /api/sdr/analyze | Pulse-analyzer capture for recapturing a remote (FR-028) |
| POST | /api/sdr/power | Narrowband RF power / carrier location (FR-028) |
| POST | /api/sdr/acquire | Phased guided receive: locate→level→decode→classify (FR-028) |
| GET | /api/sdr/live | Poll the live console ring buffer since a sequence number (FR-028) |
| GET | /api/sdr/live/status | Live console running state + config (FR-028) |
| POST | /api/sdr/live/start | Start the persistent rtl_433 live console (FR-028) |
| POST | /api/sdr/live/stop | Stop the live console, release the dongle (FR-028) |
| POST | /api/sdr/reset | USB-reset a wedged dongle (operator recovery) (FR-028) |
| POST | /api/sdr/log/start | Begin recording the live stream for AI analysis (FR-028) |
| POST | /api/sdr/log/stop | Stop recording; returns the captured line count (FR-028) |
| GET | /api/sdr/log | Retrieve the recorded session lines (FR-028) |
| POST | /api/sdr/stop | Terminate an in-progress capture (FR-028) |
| **MQTT Broker** | | |
| GET | /api/mqtt/status | Broker running state + port (FR-029) |
| POST | /api/mqtt/start | Start the mosquitto test broker (FR-029) |
| POST | /api/mqtt/stop | Stop the broker (FR-029) |
| **Composite** | | |
| GET | /api/log | Activity log (timestamped entries, filterable with `?since=`) |
| POST | /api/enter-portal | Ensure device is connected to testbench AP — provision via captive portal if needed |

#### Enter-Portal Composite Operation

`POST /api/enter-portal` provisions a DUT that is showing a captive portal.
The testbench joins the DUT's portal SoftAP, submits the credentials of the
AP the testbench will then offer, disconnects, and raises that AP so the DUT
reboots and connects to it. The DUT ends up on the testbench's WiFi network.

The portal form contract is parameterized so different DUT firmwares are
supported. The defaults target the WiFi-Tester DUT (`POST /connect` with
`ssid`/`password`); a WiFiManager DUT uses
`save_path="/wifisave"`, `field_ssid="s"`, `field_password="p"`, and passes
its extra portal fields (MQTT host/port/user/pass) via `extra`.

**Request body:**
```json
{"portal_ssid": "Device-Setup", "ssid": "test-net", "password": "testpass123",
 "save_path": "/wifisave", "field_ssid": "s", "field_password": "p",
 "method": "POST", "internet": true,
 "extra": {"host": "$BENCH", "port": "1883"}}
```

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `portal_ssid` | Yes | — | The DUT's captive-portal SoftAP name |
| `ssid` | Yes | — | Testbench AP SSID (submitted to the portal, then started) |
| `password` | Yes | — | Testbench AP password |
| `save_path` | No | `/connect` | Portal form endpoint (WiFiManager: `/wifisave`) |
| `field_ssid` | No | `ssid` | Form field name for the SSID (WiFiManager: `s`) |
| `field_password` | No | `password` | Form field name for the password (WiFiManager: `p`) |
| `method` | No | `POST` | Form submit method (`GET` or `POST`) |
| `extra` | No | — | Additional form fields, e.g. MQTT `host`/`port`/`user`/`pass` |
| `internet` | No | `false` | Start the testbench AP NAT-bridged to `eth0` (FR-011) so the DUT reaches the LAN/internet |

**Procedure:**
1. Join the DUT's captive-portal SoftAP (`portal_ssid`)
2. Submit `{field_ssid: ssid, field_password: password, **extra}` to
   `http://<portal_ip><save_path>` using `method`
3. Disconnect from the DUT's SoftAP
4. Start the testbench AP (`ssid`/`password`); with `internet: true` the AP is
   NAT-bridged to the LAN. The DUT reboots and connects to it.

Each step is logged to the activity log (`GET /api/log?since=<ts>`).

**Response:** `{"ok": true, "message": "enter-portal started in background"}` —
the flow runs asynchronously; observe completion via the activity log and
`GET /api/wifi/ap_status` (the DUT appears as a station).

**Captive-portal provisioning test (WT-2100–2102).** End-to-end provisioning
of a WiFiManager DUT onto a NAT-bridged AP, verifying it reaches the LAN:

1. DUT is unprovisioned and broadcasting its portal SoftAP (`portal_ssid`).
2. `POST /api/mqtt/start` — bring up the broker on the Pi's LAN address.
3. `POST /api/enter-portal` with the WiFiManager form params and
   `internet: true`, submitting the testbench AP creds and an `extra` MQTT
   `host` set to the Pi's LAN IP (`192.168.0.x`).
4. The testbench joins the portal, submits `/wifisave` (HTTP 200), disconnects,
   and raises the NAT AP. The DUT reboots and connects.
5. **WT-2101** — `GET /api/wifi/ap_status` shows the DUT as a station with a
   `192.168.4.x` lease; `POST /api/wifi/http` relay to the DUT's status
   endpoint returns `wifi: true`.
6. **WT-2102** — the DUT's status shows `mqtt: true`, proving it reached the
   LAN broker at `192.168.0.x:1883` through the NAT-bridged AP.

### FR-011 — AP Mode

The Pi's wlan0 runs hostapd + dnsmasq to create a SoftAP:

- **SSID/password/channel** configurable per `POST /api/wifi/ap_start`
- **WiFi power save is forced off on every AP start** — brcmfmac re-enables it
  when the interface cycles, and a power-saving AP sleeps between beacons:
  stations associate, lose the AP, and report `NO_AP_FOUND` for minutes
- **IP addressing:** AP IP is `192.168.4.1/24`
- **DHCP range:** `192.168.4.2` – `192.168.4.20`, 1-hour leases
- **Station tracking:** dnsmasq calls `wifi-lease-notify.sh` on DHCP events
  (add/old/del), which posts to `POST /api/wifi/lease_event`.  The portal
  maintains an in-memory station table `{mac, ip}` and emits STA_CONNECT /
  STA_DISCONNECT events.
- **AP status** (`GET /api/wifi/ap_status`): returns `{active, ssid, channel, stations[]}`
- Starting AP while AP is already running restarts with new configuration
- AP and STA are mutually exclusive — starting one stops the other
- **Internet bridging:** `POST /api/wifi/ap_start` with `{internet: true}`
  NAT-bridges the AP to the LAN — it enables `net.ipv4.ip_forward`, adds an
  `iptables` `MASQUERADE` on `eth0` and the `wlan0↔eth0` FORWARD rules
  (idempotently), and configures dnsmasq to forward DNS. AP clients (e.g. a
  provisioned DUT on `192.168.4.x`) then reach the Pi's LAN (`192.168.0.x`)
  and the internet. Used by `enter-portal` with `internet: true`.

### FR-012 — Captive Mode (STA)

Join an external WiFi network (typically a DUT's captive portal AP) using
wpa_supplicant + DHCP:

- `POST /api/wifi/sta_join` with `{ssid, pass, timeout}`
- Portal writes wpa_supplicant.conf (with `ctrl_interface=` prepended for
  `wpa_cli` compatibility), starts wpa_supplicant, polls `wpa_cli status`
  until `wpa_state=COMPLETED`, then obtains IP via `dhcpcd -1 -4` (or
  `dhclient`/`udhcpc` fallback)
- Stale wpa_supplicant control sockets (`/var/run/wpa_supplicant/wlan0`) are
  cleaned up before each start to prevent "ctrl_iface exists" errors
- Returns `{ip, gateway}` on success; raises error on timeout or no IP
- `POST /api/wifi/sta_leave` disconnects and releases DHCP
- STA and AP are mutually exclusive — starting STA stops the AP

**Testing this requirement needs an access point the bench does not own.**
The last clause is the reason: one radio, so the bench cannot be the network
its own station tests join, and no requirement here can be verified against
the bench alone. The DUT supplies it. This is stated because the obvious
substitute is the one that cannot work — pointing the tests at a network on
the house LAN, which the bench's `eth0` is already on, so the association
never carries the traffic and the test passes with the radio idle. The far
end has to be somewhere only the station link reaches, which on this bench
means the DUT's own `192.168.4.0/24`.

Two of the four cases need that AP to be **protected**: a correct passphrase
accepted (WT-401) and a wrong one refused (WT-402). A DUT's provisioning
portal is open by design, so the bench's own suite asks its DUT firmware for
a WPA2 AP of a known name — the `testap` console command in `test-firmware/`
— and WT-402's refusal is then observed against an AP that is demonstrably
beaconing, rather than being indistinguishable from WT-403's "nothing there".

A project consuming this bench inherits the requirement as verified and owes
nothing here; the obligation falls on whatever DUT firmware the *testbench's*
own suite runs, alongside the FR-036 obligation above.

### FR-013 — WiFi Scan

- `GET /api/wifi/scan` uses `iw dev wlan0 scan -u`
- Returns `{networks: [{ssid, rssi, auth}, ...]}` sorted by signal strength
- `auth` is one of: `OPEN`, `WPA`, `WPA2`, `WEP`
- Scan works while AP is running (the AP's own SSID is excluded from results)
- **An empty list means the air was empty; it never means the scan failed.**
  A radio that could not be asked — busy with another scan, or still
  settling into AP mode — is retried, and if it still cannot answer the
  endpoint returns 503 with the reason. The two were once indistinguishable,
  and a test read a broken scanner as a shielded room.

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-013 | Bench within range of any AP; `GET /api/wifi/scan` | `ok: true` with at least one network, each carrying `ssid`, negative `rssi`, and an `auth` from the four values | An empty list reported as a successful observation |
| FR-013 | Issue overlapping scans | Each call either returns networks or `ok: false` with 503 and a reason | `{"ok": true, "networks": []}` from a scan that never ran |
| FR-013 | Scan while the bench AP is running | The AP stays up and its own SSID is absent from the results | The scan stopping the AP |

### FR-014 — HTTP Relay

Proxy HTTP requests through the Pi's radio so tests can reach devices on the
WiFi side of the network:

- `POST /api/wifi/http` with `{method, url, headers, body, timeout}`
- Request body is base64-encoded; response body is returned base64-encoded
- Returns `{status, headers, body}`
- Works in both AP mode (reaching devices at 192.168.4.x) and STA mode
  (reaching the external network)

### FR-015 — Event System

- Events: `STA_CONNECT` (mac, ip, hostname) and `STA_DISCONNECT` (mac)
- `GET /api/wifi/events` drains the event queue
- Long-poll: `GET /api/wifi/events?timeout=N` blocks up to N seconds if queue
  is empty, returning immediately when an event arrives

### FR-016 — Mode Switching

- `POST /api/wifi/mode` with `{mode, ssid?, pass?}`
- Switching to `serial-interface` requires `ssid` (and optional `pass`);
  stops any active AP/STA, then joins the specified WiFi network via
  wpa_supplicant + DHCP on wlan0
- Switching to `wifi-testing` disconnects wlan0 from WiFi, returns wlan0 to
  instrument duty
- Mode switch failure (e.g., can't join WiFi) reverts to `wifi-testing`
- `GET /api/wifi/mode` returns `{mode}` (and `ssid`, `ip` when in
  serial-interface mode)
- While in serial-interface mode, testbench endpoints (`ap_start`, `ap_stop`,
  `sta_join`, `sta_leave`, `scan`, `http`) return a guard error

## 5. Device Control & Test Support

Services a test script drives to manipulate the DUT and report on its own
progress: GPIO lines, operator prompts, session tracking, log capture, and
the firmware repository.

### FR-017 — Human Interaction Request

Some test steps require physical actions that cannot be automated — pressing a
button, connecting a cable, power-cycling a device, repositioning an antenna.
The human interaction endpoint lets test scripts request operator assistance via
the web UI and block until the action is confirmed.

**Endpoint:** `POST /api/human-interaction`

**Request body:**
```json
{"message": "Connect the USB cable to port 2 and click Done", "timeout": 120}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| message | string | Yes | — | Free-text instruction displayed to operator |
| timeout | number | No | 120 | Max seconds to wait for confirmation |

**Behaviour:**

1. Server stores the message and creates a `threading.Event`
2. Server blocks the HTTP response on `event.wait(timeout)`
3. Web UI polls `GET /api/human/status` (every 2s via existing refresh loop)
   and shows a pulsing orange modal overlay with the message text
4. Operator performs the action, then clicks **Done** (`POST /api/human/done`)
   or **Cancel** (`POST /api/human/cancel`)
5. Done/Cancel sets the event — the blocked handler wakes and returns immediately
6. If timeout expires before confirmation, handler returns with `timeout: true`

**Response (confirmed):**
```json
{"ok": true, "confirmed": true}
```

**Response (cancelled):**
```json
{"ok": true, "confirmed": false}
```

**Response (timeout):**
```json
{"ok": true, "confirmed": false, "timeout": true}
```

**Concurrency:** Only one request can be pending at a time. A second request
while one is active returns `409 Conflict`. The portal uses
`ThreadingHTTPServer` so the blocked handler does not prevent other API
requests from being served.

**Driver method:**
```python
wt.human_interaction("Press the reset button and click Done", timeout=60)
# Returns True if confirmed, False if cancelled or timed out
```

**Activity log:** Each request, confirmation, cancellation, and timeout is
logged to the activity log.

### FR-018 — GPIO Control

Drive Pi GPIO pins from test scripts to control DUT hardware signals — for
example, holding DUT GPIO 2 low during boot to trigger captive portal mode
without requiring the rapid-reset approach or physical button presses.

**Pin allowlist:** Only these Pi GPIO pins may be controlled:

```
{5, 6, 12, 13, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27}
```

Requests for pins outside this set return HTTP 400.

**Pin values:** Only use LOW (`0`) and HIGH (`1`).  Release = drive HIGH.

#### 18.1 Endpoints

**`POST /api/gpio/set`** — Drive a GPIO pin

Request body:
```json
{"pin": 17, "value": 0}
```

| Field | Type | Required | Values | Description |
|-------|------|----------|--------|-------------|
| pin | int | Yes | See allowlist | Pi BCM GPIO pin number |
| value | int | Yes | `0`, `1` | 0 = drive low, 1 = drive high |

Response:
```json
{"ok": true, "pin": 17, "value": 0}
```

**`GET /api/gpio/status`** — Read state of all actively driven pins

Response:
```json
{"ok": true, "pins": {"17": {"direction": "output", "value": 0}}}
```

All driven pins appear in the response.

#### 18.2 Implementation

- **Lazy init:** `gpiod.Chip("/dev/gpiochip0")` is opened on first use
- **Thread-safe:** All GPIO operations are serialized via `_gpio_lock`
- **gpiod v2 API:** Uses `gpiod.line.Direction.OUTPUT`,
  `gpiod.line.Value.ACTIVE`/`INACTIVE`, `request_lines()`, `set_value()`,
  `get_value()`, `release()`
- **Resource management:** Pins remain driven until explicitly changed

#### 18.3 Captive Portal via GPIO

GPIO control provides an alternative approach to triggering captive portal
mode on the DUT (complementary to `POST /api/enter-portal` which handles
the WiFi provisioning flow after the device is already in portal mode):

1. `POST /api/gpio/set` `{"pin": 18, "value": 0}` — hold DUT boot pin (GPIO0) LOW
2. `POST /api/gpio/set` `{"pin": 17, "value": 0}` — pull DUT EN/RST LOW (reset)
3. Wait 100ms, then `POST /api/gpio/set` `{"pin": 17, "value": 1}` — release reset HIGH; DUT boots into portal mode
4. Verify captive portal from serial output (look for `CAPTIVE PORTAL MODE TRIGGERED` or `AP Started:`)
5. `POST /api/gpio/set` `{"pin": 18, "value": 1}` — release boot pin HIGH

The `ok: true` response from `/api/gpio/set` confirms the pin is driven —
there is no need to poll `/api/gpio/status` to verify.

**Driver methods:**
```python
wt.gpio_set(18, 0)           # Hold DUT boot pin (GPIO0) LOW
wt.gpio_set(17, 0)           # Pull EN/RST LOW (reset)
time.sleep(0.1)
wt.gpio_set(17, 1)           # Release reset HIGH — DUT boots into portal mode
# Check serial output for portal confirmation
wt.gpio_set(18, 1)           # Release boot pin HIGH
```

### FR-019 — Test Progress Tracking

Test scripts can push live progress updates to the portal web UI so
operators can monitor test execution without a terminal.

**Endpoints:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /api/test/update | Push session start, step updates, results, or end |
| GET | /api/test/progress | Poll current test session state |

**Session lifecycle:**

1. `POST /api/test/update` with `{spec, phase, total}` — start session
2. `POST /api/test/update` with `{current: {id, name, step, manual}}` — update current test
3. `POST /api/test/update` with `{result: {id, name, result, details}}` — record result (PASS/FAIL/SKIP)
4. `POST /api/test/update` with `{end: true}` — end session

**Driver methods:**
```python
wt.test_start("Modbus Proxy v1.4", "Integration", total=58)
wt.test_step("TC-001", "WiFi Connect", "Joining AP...", manual=False)
wt.test_result("TC-001", "WiFi Connect", "PASS")
wt.test_end()
```

### FR-020 — UDP Log Receiver

ESP32 devices send debug logs over UDP (since their USB port is often
occupied by HID or other functions).  The Pi listens for these UDP log
packets and makes them available through the HTTP API and web UI.

**Configuration:**

| Constant | Value |
|----------|-------|
| UDP_LOG_PORT | `5555` (env: `UDP_LOG_PORT`) |
| UDP_LOG_MAX_LINES | `2000` |

**Behaviour:**

1. Portal spawns a background thread with a UDP socket bound to `0.0.0.0:5555`
2. Each received datagram is decoded as UTF-8, split by newlines
3. Lines are stored in a `collections.deque(maxlen=2000)` with timestamps
   and source IP
4. Lines are also forwarded to the activity log via `log_activity()`
5. The UDP socket thread is daemon — it exits when the portal exits

**Endpoints:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /api/udplog | Retrieve buffered UDP log lines |
| DELETE | /api/udplog | Clear the UDP log buffer |

**GET /api/udplog** query parameters:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| since | float | 0 | Only return lines with timestamp > since |
| source | string | (all) | Filter by source IP address |
| limit | int | 200 | Max lines to return |

**Response:**
```json
{
  "ok": true,
  "lines": [
    {"ts": 1740000000.123, "source": "192.168.0.121", "line": "I (12345) wifi_mgr: Connected"},
    {"ts": 1740000000.456, "source": "192.168.0.121", "line": "I (12346) ble_nus: Client connected"}
  ]
}
```

**Driver methods:**
```python
logs = wt.udplog(since=0, source="192.168.0.121", limit=100)
wt.udplog_clear()
```

**Implementation notes:**
- Thread-safe: deque operations are atomic; timestamp+source stored per entry
- Non-blocking: UDP recv in a loop with 1s timeout for clean shutdown
- ESP32 remote_log.c sends to the configured host:port (default $BENCH:5555)

### FR-021 — OTA Firmware Repository

The Pi serves firmware binaries over HTTP so ESP32 devices can perform
OTA updates from the local network.  This eliminates the need for
internet access or external hosting during development and testing.

**Configuration:**

| Constant | Value |
|----------|-------|
| FIRMWARE_DIR | `/var/lib/rfc2217/firmware` (env: `FIRMWARE_DIR`) |

**Directory layout:**
```
/var/lib/rfc2217/firmware/
├── ios-keyboard/
│   └── ios-keyboard.bin
├── modbus-proxy/
│   └── modbus-proxy.bin
└── ...
```

**Endpoints:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /firmware/`<project>`/`<filename>` | Download firmware binary (used by ESP32 OTA) |
| GET | /api/firmware/list | List all available firmware files |
| POST | /api/firmware/upload | Upload a firmware binary |
| DELETE | /api/firmware/delete | Delete a firmware file |

**GET /firmware/`<project>`/`<filename>`**

Serves the raw binary file with `Content-Type: application/octet-stream`.
This is the URL the ESP32 OTA client points to, e.g.:
```
http://$BENCH:8080/firmware/ios-keyboard/ios-keyboard.bin
```

Path traversal is rejected (no `..` allowed in project or filename).

**GET /api/firmware/list** response:
```json
{
  "ok": true,
  "files": [
    {"project": "ios-keyboard", "filename": "ios-keyboard.bin", "size": 1048576, "modified": "2026-02-25T10:00:00+00:00"}
  ]
}
```

**POST /api/firmware/upload** body (multipart/form-data):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| project | string | Yes | Project subdirectory name |
| file | file | Yes | The firmware binary |

**Response:**
```json
{"ok": true, "project": "ios-keyboard", "filename": "ios-keyboard.bin", "size": 1048576}
```

**DELETE /api/firmware/delete** body:
```json
{"project": "ios-keyboard", "filename": "ios-keyboard.bin"}
```

**Driver methods:**
```python
files = wt.firmware_list()
wt.firmware_upload("ios-keyboard", "/path/to/ios-keyboard.bin")
wt.firmware_delete("ios-keyboard", "ios-keyboard.bin")
# ESP32 OTA URL: http://$BENCH:8080/firmware/ios-keyboard/ios-keyboard.bin
```

**End-to-end OTA workflow:**

The testbench supports a complete remote OTA workflow for ESP32 devices
connected to its WiFi AP.  The HTTP relay (`POST /api/wifi/http`) bridges
the LAN and WiFi AP networks, allowing OTA to be triggered from any
client on the LAN.

1. **Upload firmware** to the testbench's OTA repository:
   ```
   POST /api/firmware/upload  (multipart: project=ios-keyboard, file=ios-keyboard.bin)
   ```
2. **Verify** the firmware is downloadable at the serving URL:
   ```
   GET /firmware/ios-keyboard/ios-keyboard.bin
   ```
3. **Trigger OTA** on the ESP32 via the HTTP relay:
   ```
   POST /api/wifi/http  {"method":"POST", "url":"http://192.168.4.15/ota"}
   ```
   The ESP32 must expose a `POST /ota` endpoint that calls `esp_ota_ops`
   to download from `http://$BENCH:8080/firmware/<project>/<file>.bin`.
4. **Monitor progress** via UDP logs:
   ```
   GET /api/udplog?source=192.168.4.15
   ```
   The ESP32 logs OTA progress (download bytes, partition writes, reboot)
   which the testbench captures on UDP port 5555.

**Prerequisites for the ESP32 device:**
- Connected to the testbench's WiFi AP (via `POST /api/enter-portal` or manual provisioning)
- HTTP server running with a `POST /ota` trigger endpoint
- OTA URL configured to point at the testbench's firmware repository

**Implementation notes:**
- Path traversal protection: reject `..` in both project and filename
- Directory auto-creation: project subdirectory created on first upload
- install.sh creates `/var/lib/rfc2217/firmware` with appropriate permissions
- Binary serving uses chunked reads (8 KB blocks) to avoid loading large
  files into memory

## 6. Peripheral Bridges

Radios and services the Pi lends to a DUT that cannot reach them itself.

### FR-022 — BLE Proxy

The Pi's onboard Bluetooth radio acts as a BLE Central (client) that can
scan for, connect to, and send commands to BLE peripherals.  This enables
remote control of BLE devices (e.g., sending keystrokes to an ESP32
running the iOS-Keyboard firmware) from test scripts or AI agents via the
HTTP API.

The Pi is a **dumb BLE-to-HTTP bridge** — it handles only scan, connect,
disconnect, status, and raw byte writes.  All higher-level protocol logic
(command encoding, text diffing, chunking) is the responsibility of the
caller.

**Dependencies:**
- `bleak>=0.20.0` (Python async BLE library, uses BlueZ on Linux)
- BlueZ 5.43+ (standard on Raspberry Pi OS)

**Configuration:**

| Constant | Value |
|----------|-------|
| BLE_SCAN_TIMEOUT | `5.0` seconds (env: `BLE_SCAN_TIMEOUT`) |

**State model:**

| State | Description |
|-------|-------------|
| Idle | No BLE activity |
| Scanning | Actively scanning for BLE peripherals |
| Connected | Connected to a BLE peripheral |

State transitions:

| From | To | Trigger |
|------|----|---------|
| Idle | Scanning | `POST /api/ble/scan` |
| Scanning | Idle | Scan completes (timeout) |
| Idle | Connected | `POST /api/ble/connect` |
| Connected | Idle | `POST /api/ble/disconnect` or remote disconnect |

**Endpoints:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /api/ble/scan | Scan for BLE peripherals, return list |
| POST | /api/ble/connect | Connect to a BLE peripheral by address |
| POST | /api/ble/disconnect | Disconnect from current peripheral |
| GET | /api/ble/status | Connection state and device info |
| POST | /api/ble/write | Write raw bytes to a GATT characteristic |

**POST /api/ble/scan** body (optional):
```json
{"timeout": 5.0, "name_filter": "iOS-Keyboard"}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| timeout | float | 5.0 | Scan duration in seconds |
| name_filter | string | (none) | Only return devices whose name contains this string |

**Response:**
```json
{
  "ok": true,
  "devices": [
    {"address": "1C:DB:D4:84:58:CC", "name": "iOS-Keyboard", "rssi": -45}
  ]
}
```

**POST /api/ble/connect** body:
```json
{"address": "1C:DB:D4:84:58:CC"}
```

**Response:**
```json
{
  "ok": true,
  "address": "1C:DB:D4:84:58:CC",
  "name": "iOS-Keyboard",
  "services": [
    {
      "uuid": "6e400001-b5a3-f393-e0a9-e50e24dcca9e",
      "characteristics": [
        {"uuid": "6e400002-b5a3-f393-e0a9-e50e24dcca9e", "properties": ["write", "write-without-response"]},
        {"uuid": "6e400003-b5a3-f393-e0a9-e50e24dcca9e", "properties": ["notify"]}
      ]
    }
  ]
}
```

**POST /api/ble/disconnect** — no body required.

**Response:**
```json
{"ok": true}
```

**GET /api/ble/status** response:
```json
{
  "ok": true,
  "state": "connected",
  "address": "1C:DB:D4:84:58:CC",
  "name": "iOS-Keyboard"
}
```

States: `"idle"`, `"scanning"`, `"connected"`.

**POST /api/ble/write** body:
```json
{"characteristic": "6e400002-b5a3-f393-e0a9-e50e24dcca9e", "data": "024865 6c6c6f", "response": true}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| characteristic | string | Yes | Target GATT characteristic UUID |
| data | string | Yes | Hex-encoded bytes to write |
| response | bool | No (default true) | Use write-with-response (true) or write-without-response (false) |

**Response:**
```json
{"ok": true, "bytes_written": 6}
```

**Error responses:**

| Condition | HTTP | Response |
|-----------|------|----------|
| Not connected | 409 | `{"ok": false, "error": "not connected"}` |
| Already connected | 409 | `{"ok": false, "error": "already connected to 1C:DB:D4:84:58:CC"}` |
| Device not found | 404 | `{"ok": false, "error": "device not found"}` |
| Write failed | 500 | `{"ok": false, "error": "write failed: ..."}` |
| Invalid hex data | 400 | `{"ok": false, "error": "invalid hex data"}` |

**Driver methods:**
```python
devices = wt.ble_scan(timeout=5.0, name_filter="iOS-Keyboard")
info = wt.ble_connect("1C:DB:D4:84:58:CC")
status = wt.ble_status()
wt.ble_write("6e400002-b5a3-f393-e0a9-e50e24dcca9e", bytes([0x02]) + b"Hello")
wt.ble_disconnect()
```

**Implementation notes:**
- `ble_controller.py` runs its own `asyncio` event loop in a background
  thread (bleak is async, portal is sync)
- Module-level lock (`_lock`) serializes all BLE operations
- Connection state tracked in module globals: `_client`, `_address`, `_name`
- Disconnect callback updates state automatically on remote disconnect
- Scan results are ephemeral (not cached)
- Only one BLE connection at a time (Raspberry Pi hardware limitation
  with single radio)

### FR-029 — MQTT Broker

An on-demand mosquitto broker for testing DUT MQTT clients, backed by
`mqtt_controller.py`. The broker is open (anonymous, no auth) and listens on
all interfaces at port 1883, so it is reachable both from the testbench AP
(`192.168.4.1:1883`) and from the Pi's LAN address (`192.168.0.x:1883`) — a
DUT on a NAT-bridged AP (FR-011) reaching the LAN address exercises the full
provisioned network path.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /api/mqtt/status | `{running, port}` |
| POST | /api/mqtt/start | Start the broker (idempotent); returns `{port}`. Fails with the listener named if port 1883 is held by a broker this service did not start |
| POST | /api/mqtt/stop | Stop the broker |

The broker is a portal-managed subprocess and stops when the portal restarts.

Start reclaims the port from a **previous test broker** left behind by a portal
restart, matching only processes launched with this service's own config path.
It does not kill brokers it did not start: if port 1883 is held by anything
else — a system `mosquitto.service`, another user's broker — start fails and
names the listener rather than taking down infrastructure it does not own.

---

## 7. Debug Services

Remote GDB via OpenOCD, in three variants selected by what the board
physically exposes.

### FR-024 — GDB Debug: USB JTAG (ESP32-C3/S3 Single-Port)

Remote GDB debugging for ESP32-C3 and ESP32-S3 boards that expose a built-in
USB-Serial/JTAG controller on their native USB port.  The same USB cable
already used for serial also carries JTAG — no additional hardware required.

#### 24.1 Principle

ESP32-C3 and ESP32-S3 chips contain a USB-Serial/JTAG controller that
exposes **two USB interfaces** on a single cable:

| USB Interface | Linux Driver | Function |
|---------------|-------------|----------|
| Interface 0 | `cdc_acm` → `/dev/ttyACM*` | Serial console (current RFC2217 proxy) |
| Interface 1 | libusb (userspace) | JTAG debug (OpenOCD) |

OpenOCD communicates with the JTAG interface via libusb, completely
independent of the serial interface.  The portal starts OpenOCD for a slot
and exposes the GDB Remote Serial Protocol (RSP) on a per-slot TCP port.
Remote containers connect GDB to that port — no USB/JTAG drivers needed on
the client side.

#### 24.2 Supported Chips

| Chip | USB JTAG | Condition |
|------|:--------:|-----------|
| ESP32-C3 | Yes | Board must use native USB (not CP2102/CH340 bridge) |
| ESP32-S3 | Yes | Board must use native USB (not CH340 hub bridge) |
| ESP32 (classic) | No | No USB JTAG — use FR-026 (ESP-Prog) |
| ESP32-S2 | No | USB-OTG only, no built-in JTAG controller |

**Note:** Some S3 boards (e.g. boards with built-in CH340 USB hub) route
USB through a UART bridge chip instead of the S3's native USB-Serial/JTAG
controller.  These boards appear as VID `1a86` (QinHeng) rather than
`303a` (Espressif) and do NOT support USB JTAG.  Only boards where the
S3's native USB D+/D- lines connect directly to the USB connector expose
the JTAG interface.

#### 24.3 Chip Auto-Detection

All chips with native USB-Serial/JTAG share the same USB PID (`303a:1001`),
so the chip type **cannot** be determined from USB enumeration alone.
However, the JTAG TAP ID read during OpenOCD's scan chain interrogation
uniquely identifies the chip architecture:

| JTAG TAP ID | Manufacturer | Architecture | Chip | Verified |
|-------------|-------------|-------------|------|:---:|
| `0x00005c25` | Espressif (`0x612`) | RISC-V single-core | ESP32-C3 | Yes |
| `0x00010c25` | Espressif (`0x612`) | RISC-V single-core | ESP32-H2 | Yes |
| `0x0000dc25` | Espressif (`0x612`) | RISC-V single-core | ESP32-C6 | Yes |
| `0x120034e5` | Tensilica (`0x272`) | Xtensa dual-core | ESP32-S3 | Yes |

**Auto-detection strategy:** The portal can attempt OpenOCD with a candidate
config.  If the TAP ID mismatches, try the other config.  Alternatively,
accept `chip` as an optional parameter — if omitted, probe both configs.

#### 24.4 USB Interface Layout

The native USB-Serial/JTAG controller exposes three USB interfaces:

| Interface | Class | Linux Driver | Purpose |
|-----------|-------|-------------|---------|
| 0 | CDC-ACM | `cdc_acm` → `/dev/ttyACM*` | Serial console (RFC2217 proxy) |
| 1 | CDC Data | `cdc_acm` | Serial data channel |
| 2 | Vendor Specific | **none** (unclaimed) | JTAG (OpenOCD via libusb) |

**Key finding:** Interface 2 (JTAG) is **not claimed** by any kernel driver.
OpenOCD accesses it directly via libusb without needing `unbind` or
`detach_kernel_driver`.  This means serial (RFC2217) and JTAG (OpenOCD) can
coexist on the same physical USB connection without any driver manipulation.

This differs from the ESP-Prog (FR-026) where the `ftdi_sio` kernel driver
claims both FTDI channels and channel A must be explicitly unbound.

#### 24.5 Software Dependencies

**On the Pi:**
- `esp-openocd` v0.12.0+ — Espressif's fork (not upstream OpenOCD).  Required
  for ESP32 flash drivers, reset sequences, and USB JTAG support.
- **Prebuilt binary:** download `openocd-esp32-linux-arm64-*.tar.gz` from
  [espressif/openocd-esp32 releases](https://github.com/espressif/openocd-esp32/releases).
  The `install.sh` script handles this automatically.
- Installation path: `/usr/local/bin/openocd-esp32`
- Scripts path: `/usr/local/share/openocd-esp32/scripts/`
- Target configs: `board/esp32c3-builtin.cfg`, `board/esp32s3-builtin.cfg`
- **Must pass** `-s /usr/local/share/openocd-esp32/scripts` to OpenOCD

**On the remote container (developer side):**
- `riscv32-esp-elf-gdb` (for C3) or `xtensa-esp32s3-elf-gdb` (for S3)
  — included in ESP-IDF toolchain
- No special drivers or USB access needed — pure TCP connection

#### 24.6 Configuration

| Constant | Default | Env Override | Description |
|----------|---------|-------------|-------------|
| GDB_PORT_BASE | 3333 | `GDB_PORT_BASE` | First GDB RSP port (per-slot: +0, +1, +2) |
| OPENOCD_TELNET_BASE | 4444 | `OPENOCD_TELNET_BASE` | First OpenOCD telnet port |
| OPENOCD_EXE | `/usr/local/bin/openocd-esp32` | `OPENOCD_EXE` | Path to esp-openocd binary |

**Slot configuration** (`testbench.json` extension):
```json
{
  "slots": [
    {
      "label": "SLOT1",
      "slot_key": "platform-...",
      "tcp_port": 4001,
      "gdb_port": 3333,
      "openocd_telnet_port": 4444
    }
  ]
}
```

#### 24.7 State Model Extension

New slot state `Debugging` added to the Serial Service state machine:

| State | Description |
|-------|-------------|
| Debugging | OpenOCD running — GDB clients can connect; RFC2217 proxy stopped |

State transitions:

| From | To | Trigger |
|------|----|---------|
| Idle | Debugging | `POST /api/debug/start` — stops proxy, starts OpenOCD |
| Debugging | Idle | `POST /api/debug/stop` — stops OpenOCD, restarts proxy |
| Debugging | Debugging | Hotplug events suppressed (USB re-enumeration during JTAG reset is normal) |

**Mutual exclusion:** A slot in `Debugging` state rejects `serial/reset`,
`serial/monitor`, and `enter-portal` requests.  Flashing via esptool is
blocked — the chip's CPU is under OpenOCD control.

#### 24.8 Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /api/debug/start | Start OpenOCD for a slot, expose GDB port |
| POST | /api/debug/stop | Stop OpenOCD, release slot back to serial |
| GET | /api/debug/status | Debug state for all slots |

**POST /api/debug/start** body:
```json
{"slot": "SLOT1", "chip": "esp32c3"}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| slot | string | Yes | — | Slot label |
| chip | string | Yes | — | Chip type: `esp32c3` or `esp32s3` |

**Response:**
```json
{
  "ok": true,
  "slot": "SLOT1",
  "gdb_port": 3333,
  "telnet_port": 4444,
  "chip": "esp32c3",
  "gdb_target": "target extended-remote $BENCH:3333"
}
```

**POST /api/debug/stop** body:
```json
{"slot": "SLOT1"}
```

**Response:**
```json
{"ok": true, "slot": "SLOT1"}
```

**GET /api/debug/status** response:
```json
{
  "ok": true,
  "slots": {
    "SLOT1": {"debugging": true, "chip": "esp32c3", "gdb_port": 3333, "pid": 5678},
    "SLOT2": {"debugging": false}
  }
}
```

**Error responses:**

| Condition | HTTP | Response |
|-----------|------|----------|
| Slot not found | 404 | `{"ok": false, "error": "slot not found"}` |
| Device not present | 409 | `{"ok": false, "error": "no device in SLOT1"}` |
| Already debugging | 409 | `{"ok": false, "error": "SLOT1 already in debug mode"}` |
| Not debugging (on stop) | 200 | `{"ok": true}` (idempotent) |
| OpenOCD failed to start | 500 | `{"ok": false, "error": "openocd failed: ..."}` |
| Unsupported chip | 400 | `{"ok": false, "error": "chip 'esp32' has no USB JTAG — use ESP-Prog"}` |

#### 24.9 OpenOCD Lifecycle

**Start sequence:**
1. Validate slot is `Idle` and device is present
2. RFC2217 proxy may remain running (serial and JTAG use separate USB
   interfaces — see §24.4).  Stopping the proxy is optional and depends
   on whether exclusive serial access is needed during debug.
3. Launch `openocd-esp32` as subprocess:
   ```
   openocd-esp32 -s /usr/local/share/openocd-esp32/scripts \
     -f board/{chip}-builtin.cfg \
     -c "gdb port {gdb_port}" \
     -c "telnet port {telnet_port}" \
     -c "bindto 0.0.0.0"
   ```
4. Wait up to 5s for OpenOCD to bind (poll TCP port)
5. Set slot state to `Debugging`, record PID

**Stop sequence:**
1. Send SIGTERM to OpenOCD process
2. Wait up to 5s for exit
3. Set slot state to `Idle`
4. Restart RFC2217 proxy via simulated hotplug

**Hotplug during debug:** USB re-enumeration events (from JTAG-initiated
resets) are logged but do NOT trigger proxy restarts while in `Debugging`
state.  OpenOCD manages USB reconnection internally.

#### 24.10 Serial Console During Debug

**Verified:** Serial console and JTAG debugging coexist on the same physical
USB connection.  The native USB-Serial/JTAG controller exposes serial
(Interface 0, `cdc_acm`) and JTAG (Interface 2, unclaimed) as separate
USB interfaces.  The RFC2217 proxy can remain running while OpenOCD uses
the JTAG interface — developers can see `printf` output alongside GDB.

This eliminates the originally anticipated need to stop the serial proxy
during debug sessions for native USB-Serial/JTAG devices.

#### 24.11 Driver Methods

```python
# Start debug session
info = wt.debug_start("SLOT1", chip="esp32c3")
print(f"GDB port: {info['gdb_port']}")
# → Connect GDB: target extended-remote $BENCH:3333

# Check status
status = wt.debug_status()

# Stop debug session (restarts RFC2217 proxy)
wt.debug_stop("SLOT1")
```

#### 24.12 IDE Integration (Client Side)

**VS Code (launch.json):**
```json
{
  "type": "cppdbg",
  "request": "launch",
  "program": "${workspaceFolder}/build/project.elf",
  "miDebuggerPath": "riscv32-esp-elf-gdb",
  "miDebuggerServerAddress": "$BENCH:3333",
  "setupCommands": [
    {"text": "set remote hardware-breakpoint-limit 2"},
    {"text": "monitor reset halt"}
  ]
}
```

**Command-line GDB:**
```bash
riscv32-esp-elf-gdb build/project.elf \
  -ex "target extended-remote $BENCH:3333" \
  -ex "monitor reset halt"
```

**PlatformIO (platformio.ini):**
```ini
debug_tool = esp-builtin
debug_server =
  # empty — use remote server instead
debug_port = $BENCH:3333
```

#### 24.13 Auto-Start on Hotplug

OpenOCD starts automatically when a device is hotplugged or at boot,
requiring zero manual configuration:

- **Slot-aware detection**: Detection is per-DUT-slot, not global.  For each
  DUT slot, the portal determines:
  1. **Chip type** — which MCU is in this slot
  2. **JTAG source** — which slot provides JTAG (own slot for built-in USB
     JTAG, or another slot if an ESP-Prog probe is wired to the DUT)
- **Detection sequence**: For each DUT slot:
  1. Check the slot's own USB devices for built-in JTAG (Espressif VID `303a`
     with "JTAG" in product name) → try `BUILTIN_CONFIGS` in order:
     C3 → S3 → C6 → H2
  2. If no built-in JTAG, try each available ESP-Prog probe →
     `PROBE_TARGET_CONFIGS` in order: ESP32 → S3 → C3 → C6 → H2 → S2
  3. If neither succeeds → no debug for this slot
- **Probe-only slots skipped**: Slots that contain only a debug probe (FTDI
  VID `0403`, no other USB devices) are never auto-debugged themselves.
- **API visibility**: The `/api/devices` response includes per-slot:
  - `detected_chip` — MCU type (e.g. `esp32s3`), persists after debug stop
  - `jtag_slot` — slot label providing JTAG (own slot or probe's slot), or null
  - `debugging` (bool), `debug_chip`, `debug_gdb_port` — active session info
- **Flashing via `/api/flash`**: For native USB chips (C3/S3/C6/H2),
  the portal stops both OpenOCD and the proxy before running esptool,
  then restarts both.  For boards with a dedicated USB-serial chip
  (CP2102, CH343), the serial and JTAG interfaces are independent.
- **Flapping suppression**: Auto-debug is suppressed while a slot is in
  flapping/recovery state — OpenOCD is not started until the device stabilises.
- **Hotplug suppression**: While debugging is active on a slot, hotplug events
  are suppressed to prevent USB re-enumeration from killing the OpenOCD process.
- **Manual override**: A manual `debug_stop` clears the auto-debug flag for the
  slot — the portal will not auto-restart debugging on the next hotplug event.

---

### FR-025 — GDB Debug: Dual-USB (ESP32-S3 Two-Port)

Remote GDB debugging for ESP32-S3 boards that break out **both** USB
connectors — USB-OTG and USB-Serial/JTAG.  This is the optimal debug
configuration: serial console, JTAG debugger, and application USB all run
simultaneously with zero contention.

#### 25.1 Principle

The ESP32-S3 has two independent USB controllers:

| USB Port | Controller | Hub Port | Function |
|----------|-----------|----------|----------|
| USB-Serial/JTAG | Dedicated debug | SLOT*n* | Serial console (RFC2217) + JTAG (OpenOCD) |
| USB-OTG | Full-speed peripheral | SLOT*n*-APP | Application USB (HID, CDC, MSC, etc.) |

Both ports plug into the Pi's USB hub, consuming **two hub ports per DUT**.
The serial/JTAG port runs RFC2217 AND OpenOCD simultaneously because they
use separate USB endpoints.  The OTG port provides the DUT's actual USB
function (e.g., HID keyboard, CDC serial, mass storage).

#### 25.2 Key Advantage: No Contention

Unlike FR-024 (single-port), the RFC2217 proxy does NOT need to stop during
debugging.  All three functions coexist:

| Function | USB Port | Simultaneous |
|----------|----------|:---:|
| Serial console (RFC2217) | Serial/JTAG | Yes |
| GDB debugging (OpenOCD) | Serial/JTAG | Yes |
| Application USB | OTG | Yes |

This means:
- `printf` debugging and GDB breakpoints work at the same time
- Test scripts can interact with the DUT's USB function while debugging
- No state machine changes — the slot stays in `Idle` while OpenOCD runs

#### 25.3 Supported Boards

Only ESP32-S3 boards that break out **both** USB connectors:

| Board | USB-Serial/JTAG | USB-OTG | Dual-USB |
|-------|:---:|:---:|:---:|
| ESP32-S3-DevKitC-1 (v1.1+) | Yes | Yes | Yes |
| ESP32-S3-DevKitM-1 | Yes | No | No |
| Custom boards with both ports | Yes | Yes | Yes |

ESP32-C3 boards do not have USB-OTG — they have only one USB port.

#### 25.4 Slot Pairing

Two hub ports belong to the same DUT.  Configuration uses a `slot_group`:

```json
{
  "slots": [
    {
      "label": "SLOT1",
      "slot_key": "platform-...-usb-0:1.1:1.0",
      "tcp_port": 4001,
      "gdb_port": 3333,
      "openocd_telnet_port": 4444,
      "group": "DUT1",
      "role": "debug"
    },
    {
      "label": "SLOT1-APP",
      "slot_key": "platform-...-usb-0:1.2:1.0",
      "tcp_port": 4002,
      "group": "DUT1",
      "role": "application"
    }
  ]
}
```

The `group` field links the two slots.  The `role` field identifies which
USB port is which:
- `debug` — USB-Serial/JTAG port (serial + JTAG)
- `application` — USB-OTG port (DUT's USB function)

#### 25.5 Endpoints

Same endpoints as FR-024 (`/api/debug/start`, `/api/debug/stop`,
`/api/debug/status`), plus:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /api/debug/group | Show slot groups and their roles |

**POST /api/debug/start** — same as FR-024.  The portal automatically
identifies the `debug`-role slot within the group.

**GET /api/debug/group** response:
```json
{
  "ok": true,
  "groups": {
    "DUT1": {
      "debug": {"label": "SLOT1", "tcp_port": 4001, "gdb_port": 3333, "present": true},
      "application": {"label": "SLOT1-APP", "tcp_port": 4002, "present": true}
    }
  }
}
```

#### 25.6 OpenOCD Lifecycle

Same as FR-024 §24.7, except:
- The RFC2217 proxy is **NOT stopped** when OpenOCD starts (serial and JTAG
  coexist on separate USB endpoints)
- Slot state remains `Idle` — no `Debugging` state needed
- OpenOCD is tracked as a parallel process alongside the RFC2217 proxy

#### 25.7 Application USB Port

The application USB port (SLOT*n*-APP) appears as whatever USB device class
the DUT firmware implements.  Common cases:

| DUT USB Class | Linux Device | Testbench Use |
|---------------|-------------|---------------|
| CDC-ACM (serial) | `/dev/ttyACM*` | Second RFC2217 proxy (data channel) |
| HID (keyboard/mouse) | `/dev/hidraw*` | Capture HID reports |
| MSC (mass storage) | `/dev/sd*` | Mount filesystem |
| Custom vendor | — | Raw USB via libusb |

The RFC2217 proxy on the APP slot proxies CDC-ACM output.  For non-serial
USB classes, the portal does not proxy — the application port is available
for direct use or future extensions.

#### 25.8 Driver Methods

```python
# Discover slot groups
groups = wt.debug_groups()
dut1 = groups["DUT1"]
print(f"Debug serial: rfc2217://...:{dut1['debug']['tcp_port']}")
print(f"App USB: rfc2217://...:{dut1['application']['tcp_port']}")

# Start debug (serial proxy stays running)
info = wt.debug_start("SLOT1", chip="esp32s3")

# Now you have all three simultaneously:
#   - Serial console via RFC2217 on port 4001
#   - GDB via port 3333
#   - App USB via RFC2217 on port 4002

wt.debug_stop("SLOT1")
```

#### 25.9 Hub Port Planning

The Pi Zero 2 W has a single USB 2.0 port driving an external hub.  With
dual-USB boards consuming 2 ports each:

| Hub Ports | DUTs | Remaining |
|-----------|------|-----------|
| 3-port hub | 1 DUT + 1 port for USB Ethernet | None |
| 4-port hub | 1 DUT + USB Ethernet + 1 spare | — |
| 7-port hub | 3 DUTs + USB Ethernet | None |

A larger hub is recommended for dual-USB debugging.

---

### FR-026 — GDB Debug: ESP-Prog External Probe

Remote GDB debugging using an ESP-Prog (FT2232H) external debug probe for
**any ESP32 variant** — including the classic ESP32 which has no USB JTAG.
The probe connects to the DUT's JTAG pins via a ribbon cable and to the Pi's
USB hub for OpenOCD control.

#### 26.1 Principle

The ESP-Prog is Espressif's reference debug probe based on the FTDI FT2232H
dual-channel chip:

| Channel | Function |
|---------|----------|
| Channel A | JTAG (TCK, TDI, TDO, TMS) |
| Channel B | UART (TX, RX) — optional serial console |

The probe plugs into the Pi's USB hub and connects to the DUT via a 10-pin
JTAG header or individual wires.  OpenOCD uses the `ftdi` driver with
ESP-Prog-specific configuration.

**Key advantage:** Serial and JTAG are on completely separate physical
connections — the DUT's USB serial (RFC2217) and the probe's JTAG operate
simultaneously with zero contention.

#### 26.2 Supported Chips

All ESP32 variants with accessible JTAG pins:

| Chip | JTAG Pins (TCK/TDI/TDO/TMS) | Notes |
|------|------------------------------|-------|
| ESP32 (classic) | 13 / 12 / 15 / 14 | Conflicts with SD card interface; GPIO12 is a strapping pin |
| ESP32-C3 | 4 / 5 / 6 / 7 | Cannot use USB JTAG and pin JTAG simultaneously |
| ESP32-S2 | 39 / 40 / 41 / 42 | — |
| ESP32-S3 | 39 / 40 / 41 / 42 | Prefer USB JTAG (FR-024) unless pins are already wired |
| ESP32-C6 | 4 / 5 / 6 / 7 | Same as C3 |
| ESP32-H2 | 4 / 5 / 6 / 7 | Same as C3 |

**Requirement:** The DUT board must expose the JTAG pins on a header or
test points.  Many production modules do not — check the board's schematic.

#### 26.3 Hardware Setup

| Component | Description |
|-----------|-------------|
| ESP-Prog | ~$15, Espressif reference probe (FT2232H-based) |
| JTAG cable | 10-pin ribbon or 4 jumper wires (TCK, TDI, TDO, TMS + GND) |
| USB cable | ESP-Prog → Pi USB hub (consumes 1 hub port) |

**Wiring (ESP-Prog JTAG header to DUT):**

| ESP-Prog Pin | Signal | DUT Pin (varies by chip) |
|-------------|--------|--------------------------|
| 1 | VDD (3.3V) | 3.3V (optional, for probe power sensing) |
| 2 | TMS | Chip-specific (see §26.2) |
| 3 | GND | GND |
| 4 | TCK | Chip-specific |
| 5 | GND | GND |
| 6 | TDO | Chip-specific |
| 7 | GND | GND |
| 8 | TDI | Chip-specific |
| 9 | GND | GND |
| 10 | NC | — |

#### 26.4 Software Dependencies

**On the Pi:**
- `esp-openocd` — same binary as FR-024
- Target configs: `board/esp32-wrover-kit-1.8v.cfg` (classic ESP32),
  `interface/ftdi/esp32_devkitj_v1.cfg` (ESP-Prog interface), etc.
- `libftdi1` and `libudev-dev` for FTDI device access
- udev rule for non-root FTDI access (or run OpenOCD as root)

#### 26.5 Configuration

The ESP-Prog is configured as a **shared resource** — not tied to a specific
slot.  The portal tracks which slot's DUT is connected to the probe.

```json
{
  "debug_probes": [
    {
      "label": "PROBE1",
      "type": "esp-prog",
      "usb_serial": "FT2232H-A",
      "interface_config": "interface/ftdi/esp32_devkitj_v1.cfg"
    }
  ]
}
```

| Constant | Default | Description |
|----------|---------|-------------|
| PROBE_GDB_PORT | 3333 | GDB RSP port for the probe |
| PROBE_TELNET_PORT | 4444 | OpenOCD telnet port for the probe |

#### 26.6 Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /api/debug/start | Start OpenOCD via ESP-Prog for a slot |
| POST | /api/debug/stop | Stop OpenOCD, release probe |
| GET | /api/debug/status | Debug state (probe and slot info) |
| GET | /api/debug/probes | List available debug probes |

**POST /api/debug/start** body:
```json
{"slot": "SLOT1", "chip": "esp32", "probe": "PROBE1"}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| slot | string | Yes | — | Slot label (identifies which DUT) |
| chip | string | Yes | — | Chip type: `esp32`, `esp32c3`, `esp32s3`, etc. |
| probe | string | No | first available | Probe label |

**Response:**
```json
{
  "ok": true,
  "slot": "SLOT1",
  "probe": "PROBE1",
  "chip": "esp32",
  "gdb_port": 3333,
  "telnet_port": 4444,
  "gdb_target": "target extended-remote $BENCH:3333"
}
```

**GET /api/debug/probes** response:
```json
{
  "ok": true,
  "probes": [
    {"label": "PROBE1", "type": "esp-prog", "in_use": false, "slot": null}
  ]
}
```

#### 26.7 OpenOCD Lifecycle

**Start sequence:**
1. Validate probe is available and slot has a device
2. **Unbind channel A from `ftdi_sio`** — the Linux kernel claims both
   FT2232H channels as serial ports.  Channel A (JTAG) must be released
   to libusb before OpenOCD can use it:
   ```bash
   echo '{bus}-{port}:1.0' > /sys/bus/usb/drivers/ftdi_sio/unbind
   ```
   Channel B (UART, `/dev/ttyUSB1`) remains bound for optional serial use.
3. Launch OpenOCD:
   ```
   openocd-esp32 -s /usr/local/share/openocd-esp32/scripts \
     -f interface/ftdi/esp32_devkitj_v1.cfg \
     -f target/esp32.cfg \
     -c "gdb port 3333" \
     -c "telnet port 4444" \
     -c "bindto 0.0.0.0"
   ```
4. Wait up to 5s for OpenOCD to bind
5. Mark probe as in-use, record slot assignment

**Stop sequence:**
1. SIGTERM → OpenOCD
2. Rebind channel A to `ftdi_sio` (restore `/dev/ttyUSB0`)
3. Release probe, clear slot assignment

**Key difference from FR-024:** The RFC2217 proxy is NOT stopped.  The probe
uses the JTAG pins, not the USB serial connection.  Serial console remains
available throughout the debug session.

#### 26.8 Simultaneous Serial + Debug

This is a primary advantage of the ESP-Prog approach:

| Connection | Path | Available During Debug |
|-----------|------|:---:|
| Serial console | USB → RFC2217 → ttyACM/ttyUSB | Yes |
| GDB debugger | ESP-Prog → JTAG pins → OpenOCD → TCP | Yes |
| esptool flash | USB → RFC2217 → ttyACM/ttyUSB | No (CPU halted at breakpoint) |

Developers can see `printf` output in the serial console while
single-stepping through code in GDB.

#### 26.9 Classic ESP32 JTAG Caveats

**GPIO12 strapping pin:** On the classic ESP32, JTAG TDI is GPIO12, which
is also the flash voltage selection strapping pin.  If GPIO12 is HIGH at
boot, the chip configures 1.8V flash — which causes a crash on boards with
3.3V flash.

**Mitigations:**
- Burn the `VDD_SDIO` eFuse to force 3.3V flash (permanent, one-time)
- Use `openocd -c "reset_config none"` to prevent OpenOCD from toggling
  signals during connection
- Ensure the probe's TDI line is LOW or floating at DUT power-up

**JTAG eFuse:** On some ESP32 variants, the JTAG interface can be
permanently disabled by burning the `JTAG_DISABLE` eFuse.  Production-fused
chips cannot be debugged regardless of probe.

#### 26.10 Driver Methods

```python
# List probes
probes = wt.debug_probes()

# Start debug via ESP-Prog (serial stays running)
info = wt.debug_start("SLOT1", chip="esp32", probe="PROBE1")

# Serial + debug coexist:
wt.serial_monitor("SLOT1", pattern="WiFi connected", timeout=10)
# Meanwhile: GDB connected on port 3333

wt.debug_stop("SLOT1")
```

#### 26.11 Compatible Alternatives to ESP-Prog

| Probe | Chip | OpenOCD Driver | JTAG Speed | Notes |
|-------|------|---------------|-----------|-------|
| ESP-Prog | FT2232H | `ftdi` | 20 MHz | Reference probe, recommended |
| Generic FT2232H board | FT2232H | `ftdi` | 20 MHz | Requires custom `ftdi_vid_pid` |
| FT232H (single-channel) | FT232H | `ftdi` | 20 MHz | No UART channel |
| Segger J-Link | — | `jlink` | 15 MHz | Expensive but very reliable |
| Tigard | FT2232H | `ftdi` | 20 MHz | Multi-protocol, open-source |

All alternatives use the same portal API — only the OpenOCD interface config
changes.

### FR-037 — Debug Sessions Are Per-Slot and Bound to Their Own Board

Every debug operation — chip detection as much as a running session — **shall**
name the physical device it acts on by the asking slot's USB topology, and
**shall** listen on ports derived from that slot alone. Sessions on different
slots are independent: starting, running or stopping one **shall not** disturb
another.

Two mechanisms are needed, and neither is optional once a second built-in-JTAG
board is on the bench.

**Device selection.** Every ESP32 with a built-in USB-Serial/JTAG controller
enumerates as `303a:1001` (§24.3). VID:PID therefore identifies the *class* of
device, never *which one*. OpenOCD given only a board config opens whichever
matching device libusb offers first — so a detection asked on behalf of one
slot may examine, and report the chip of, a board in a different socket, and
may seize a board another session is already driving. The slot's USB port path
is the only stable name for "the board in this socket", and is passed as
`adapter usb location`.

**Port allocation.** OpenOCD listens on three ports — GDB, telnet and TCL. A
slot that is assigned only the first two leaves the third at its default, and
every session on the bench then competes for the same TCL port; the second one
to start dies. The TCL port is derived from the slot's GDB port, alongside the
other two.

The failure this prevents is not a crash. It is a bench that answers
`detected_chip` confidently and wrongly, and a second slot that cannot be
debugged at all — read, for two years, as a hardware limitation of shared
VID:PID rather than as two undeclared numbers.

**Verification contract**

| ID | Precondition · stimulus | Expected observation | Must NOT happen | Tier |
|---|---|---|---|---|
| FR-037 | Two slots hold built-in-JTAG boards of *different* chip types; read `/api/devices` | Each slot reports its own `detected_chip` | Both slots reporting the same chip, or either reporting its neighbour's | bench |
| FR-037 | With a session live on slot A, `POST /api/debug/start` on slot B | Both sessions run concurrently, each on its own GDB port | `Address already in use`, or slot A's session disturbed | bench |
| FR-037 | Inspect the OpenOCD command line of any built-in-JTAG session | It carries `adapter usb location` for that slot and a TCL port unique to it | A session started with no location filter | bench |
| FR-037 | Stop the session on slot B | Slot A still reports `debugging: true` and its GDB port still accepts a connection | A stop on one slot ending another's session | bench |

## 8. RF Instruments

The bench's own transmit and receive hardware, independent of any DUT.

### FR-027 — Signal Generator (RF Source + Step Attenuator)

Unified RF-source service that emits a continuous carrier, optionally
Morse-keyed, with programmable step attenuation. Two backends are
auto-selected at runtime, with optional in-line attenuation control.

#### 27.1 Backends

| Backend | Hardware | Frequency range | Notes |
|---------|----------|-----------------|-------|
| `si5351` | Si5351A on I2C1 (GPIO 2 SDA, GPIO 3 SCL), 3 channels (`clk0..clk2`) | 333 kHz – 112.5 MHz | Preferred; precise fractional synthesis. The range is `VCO_min/MS_max`..`VCO_max/MS_min` for the divider path this driver programs; the chip reaches 8 kHz – 160 MHz only via the R divider and divide-by-4 mode, which are not implemented |
| `gpclk` | BCM2835 GPCLK1 on GPIO 5 (alt GPCLK2 on GPIO 6) | Discrete PLLD/N only (~25–30 kHz steps in 80m band) | Fallback when Si5351 is absent |
| `auto` | Prefers `si5351` if the chip ACKs on I2C; falls back to `gpclk` | — | Default |

The Si5351 backend programs each active CLK output for the lowest supported
drive-current setting, 2 mA (`CLKx_IDRV[1:0] = 00`). This reduces the raw
square-wave output level before any external attenuation. Higher drive-current
settings (4 mA, 6 mA, 8 mA) are intentionally not exposed through the API;
precise level control remains the PE4302 attenuator's responsibility.
Because the portal imports `/usr/local/bin/si5351.py`, drive-current changes
require deploying that file to the Pi and restarting `rfc2217-portal` before
starting or retuning the Si5351 carrier.

The GPCLK backend uses `/dev/mem` mmap of the BCM2835 clock manager
(requires root — the portal runs as root via systemd) and switches GPIO
function select between ALT0 (clock out) and INPUT (high-Z) for keying,
so the oscillator runs continuously and on/off transitions are
phase-glitch-free. The peripheral base is auto-detected from
`/proc/device-tree/soc/ranges` (Pi Zero W, Zero 2 W, Pi 3, Pi 4).

**Pin sharing:** GPCLK pins 5/6 are shared with the gpiod-based GPIO
control (FR-018). Do not use both `siggen` and `POST /api/gpio/set` on
the same pin simultaneously.

#### 27.2 Attenuator

PE4302 RF step attenuator, 3-wire serial mode (DATA = GPIO 13, CLK = GPIO
12, LE = GPIO 6). Range 0 – 31.5 dB in 0.5 dB steps. Board jumpers: close
J4, open J5/J6/J7 to enable serial mode.

**Pin conflict:** LE shares GPIO 6 with GPCLK2. When the `gpclk` backend
is active on GPIO 6 the attenuator's LE line is unavailable; only the
`si5351` backend (or `gpclk` on GPIO 5) can be combined with live
attenuation control.

#### 27.3 Morse Keying

When `morse` is supplied to `/api/siggen/start`, the keyer gates the
carrier using PARIS-standard Morse timing. Dit duration = 1.2 / WPM
seconds.

| Element | Duration |
|---------|----------|
| Dit | 1 unit |
| Dah | 3 units |
| Inter-element gap | 1 unit |
| Inter-character gap | 3 units |
| Inter-word gap | 7 units |

WPM is configurable from 1 to 60. With `repeat: true` the message loops
indefinitely until `/api/siggen/stop`.

#### 27.4 API

| Method | Endpoint | Body / Query | Description |
|--------|----------|--------------|-------------|
| POST | /api/siggen/start | `{freq_hz, backend?, channel?, pin?, atten_db?, morse?}` | Start carrier; optional Morse keying |
| POST | /api/siggen/stop | — | Stop carrier |
| POST | /api/siggen/freq | `{freq_hz, channel?}` | Retune active carrier without restarting the keyer |
| POST | /api/siggen/atten | `{db}` | Set PE4302 attenuation (0–31.5 dB) |
| GET | /api/siggen/status | — | Current state + hardware detection |
| GET | /api/siggen/frequencies | `?low=&high=&backend=` | Achievable frequencies in a range |

Parameters for `start`:

- `freq_hz` (number, required) — carrier frequency in Hz. The Si5351 hits
  this exactly; `gpclk` snaps to the nearest integer divider (the response
  reports the actual `freq_hz`).
- `backend` (string, optional) — `auto` (default) | `si5351` | `gpclk`.
- `channel` (int, optional) — Si5351 output, 0 (default) | 1 | 2.
- `pin` (int, optional) — GPCLK pin, 5 (default) | 6.
- `atten_db` (float, optional) — initial PE4302 setting.
- `morse` (object, optional) — `{message, wpm?, repeat?}`; without it the
  carrier runs continuous.

Starting a new carrier replaces any active one (single-instance service).

#### 27.5 Configuration

`/etc/rfc2217/signalgen.json` (installed by `pi/install.sh` from
`pi/config/signalgen.json`):

```json
{
  "si5351": {"bus": 1, "address": 96, "default_channel": 0},
  "gpclk":  {"default_pin": 5},
  "pe4302": {"enabled": true, "data_pin": 13, "clk_pin": 12, "le_pin": 6}
}
```

#### 27.6 Driver Methods

```python
wt.siggen_start(freq_hz=3_500_000)                     # auto backend, continuous
wt.siggen_start(freq_hz=3_571_000,
                morse={"message": "VVV DE TEST", "wpm": 15, "repeat": True})
wt.siggen_freq(freq_hz=7_100_000)                       # retune
wt.siggen_atten(db=12.5)                                # PE4302
wt.siggen_status()
wt.siggen_frequencies(low=3_500_000, high=4_000_000, backend="gpclk")
wt.siggen_stop()
```

---

### FR-028 — SDR Receiver (RTL-SDR + rtl_433)

Receive-side RF service and counterpart to the transmit-only Signal
Generator (FR-027). An RTL2832U dongle and the `rtl_433` toolchain are
exposed over HTTP so callers decode and recapture RF remotes and sensors
without opening a shell on the Pi. One dongle backs the service, so
captures are single-instance and serialized.

#### 28.1 Hardware

| Component | Detail |
|-----------|--------|
| Dongle | RTL2832U + tuner (RTL-SDR) on a Pi USB port |
| Decoder | `rtl_433` (decode + pulse analyzer) |
| Probe | `rtl_test -t` reports dongle presence at start-up |

Tool and dongle presence are detected at start-up and reported in
`GET /api/sdr/status`. When either is missing the service loads but every
capture returns a clean error.

A dongle hot-plugged after boot is picked up without restarting the portal:
both the next capture and `GET /api/sdr/status` re-probe when no device is
known. Status re-probes only while nothing is using the dongle — `rtl_test`
opens the device — and no more than once every 5 s, since the web UI polls
status continuously and the probe costs up to 6 s. Once a device is found the
result is cached until it disappears.

#### 28.2 Capture Modes

| Mode | Backend command | Returns |
|------|-----------------|---------|
| `decode` | `rtl_433 -F json -M time:iso -T <duration>` | List of decoded records (remotes, weather sensors, TPMS, …) as JSON objects |
| `analyze` | `rtl_433 -A -T <duration>` | Raw pulse/gap timing text plus any guessed codeword — the recapture workflow for OOK remotes |

Every capture is bounded by `duration_s` (clamped to `max_duration_s`,
default 120 s); `rtl_433` self-exits at the window end. A capture in
progress rejects a second concurrent request; `POST /api/sdr/stop`
terminates an active capture early.

#### 28.3 API

| Method | Endpoint | Body / Query | Description |
|--------|----------|--------------|-------------|
| GET | /api/sdr/status | — | Tool/dongle detection + active-capture state |
| POST | /api/sdr/capture | `{freq_hz?, duration_s?, protocols?, sample_rate?, flex?, gain?}` | Decode RF for a window; returns decoded records + signal levels |
| POST | /api/sdr/analyze | `{freq_hz?, duration_s?, gain?}` | Pulse-analyzer capture for recapturing a remote |
| POST | /api/sdr/power | `{freq_hz?, duration_s?, span_hz?, bin_hz?, notch_hz?, gain?}` | Narrowband RF power (rtl_power) → `{peak_db, peak_freq_hz, mean_db}`. `notch_hz` excludes bins within that distance of the tuner centre, where the dongle's DC spike sits |
| POST | /api/sdr/acquire | `{freq_hz?, span_hz?, bin_hz?, gains?, dwell_s?, decode_s?, flex?, wait_s?}` | Phased guided receive → per-phase report + `summary` |
| POST | /api/sdr/stop | — | Terminate an in-progress **one-shot** capture. Does not stop the live console (that is `/api/sdr/live/stop`) and never blocks on it |

`POST /api/sdr/power` runs `rtl_power` over a small span centred on `freq_hz`
and returns the peak/mean power (dB). Unlike decode-based detection, a raw
carrier lifts `peak_db` clear of broadband band noise — the robust way to
confirm a transmitter is emitting (WT-1909). `peak_freq_hz` reports the centre
frequency of the strongest bin, locating a carrier of unknown frequency across
a wide sweep. Centre the span off the target frequency so the carrier does not
sit on the dongle's DC spike, or set `notch_hz` to drop bins around the tuner
centre where the DC spike sits.

**Pin `gain` for any reading that will be compared** — against a threshold, or
against another reading. Left on AGC the tuner rescales from whatever it saw
recently, so the same quiet band reads tens of dB apart between calls and a
strong carrier is compressed instead of standing clear. WT-1909 measures a
~20 dB on/off difference at fixed gain; on AGC the same measurement collapsed
to under 4 dB.

`POST /api/sdr/acquire` runs a four-phase guided receive as one call and returns
a report keyed by phase plus a human-readable `summary` and an `ok_phase`
marker: **locate** polls `rtl_power` (up to `wait_s`) until a carrier clears the
noise floor — waiting for the signal rather than sampling one fixed window, so a
momentary keyfob press is caught whenever sent; **level** sweeps `gains` at the
carrier and picks the lowest gain that reads clean OOK (rtl_433 emits a flex
suggestion only on a clean read), or stops with `too_strong` when the front end
saturates into FSK at every gain; **decode** extracts the repeating codeword with
a custom flex decoder; **classify** then checks the built-in decoders. The
`tools/sdr_acquire.py` CLI drives these phases interactively with live operator
prompts — the operator, not a remote caller, times the transmissions. `acquire`
also streams each phase prompt into the portal activity log as it runs (START →
carrier → gain → decoded → DONE), so an operator watching the testbench web UI
sees the live instructions without a terminal.

**Live console.** The **SDR Console** panel on the portal page wraps a
persistent `rtl_433` as an interactive instrument: rtl_433's own flags are the
controls (band(s) with hop, tuner gain / AGC, sample rate, squelch, decode /
flex `-X` / analyze `-A` mode, `-M level`) and its output streams back live.
`POST /api/sdr/live/start` launches the process; a reader thread fans its merged
stdout/stderr into a 600-entry sequence-numbered ring buffer that the browser
fast-polls via `GET /api/sdr/live?since=<seq>` (~500 ms; no events dropped — the
poll cadence only affects latency). The console displays a parsed event table
(time/freq/RSSI/SNR/mod/model/data), a burst-driven RSSI meter (green mid-band,
red too-strong/too-weak rails — meaningful only at fixed gain, since AGC pins
RSSI near full scale), an analyzer view, a raw toggle, and a de-duplicated
"Codes seen" row of click-to-copy chips (collected from both flex `rows`/`codes`
and analyze-mode `codes:` lines, filtered to ≥8-bit codewords; copy uses an
execCommand fallback since the clipboard API is blocked on the plain-HTTP LAN). Any control change is
applied by a fast rtl_433 relaunch. The live session holds the single-dongle
lock for its lifetime, so the one-shot capture/analyze/power/acquire endpoints
report "SDR busy" until it is stopped with `POST /api/sdr/live/stop`.

**Session log for AI analysis.** Because `-A` runs in every mode, the stream
carries each burst's pulse timing + modulation guess + decoded bits regardless
of whether it matches a decoder. The console's **Record** controls
(`/api/sdr/log/start` … `/api/sdr/log/stop`) capture that stream between two
marks while the operator presses several keys; `GET /api/sdr/log` returns the
recorded lines. The intended workflow is *log → press keys → stop → AI reverse-
engineers the timing*: an AI reads the recorded bursts and derives the
modulation/encoding, the constant preamble/device-ID, and the per-key varying
field. In-session an assistant reads the log directly; a testbench-hosted
Claude-API endpoint is the later productization. The recorded row is folded into
the console's **AI Sherlock** toggle (analyze+AGC, record → stop & analyze).

**Device database.** Devices reverse-engineered this way are made permanent as
`rtl_433` flex decoders in `pi/config/rtl_433.conf`, installed to
`/etc/rtl_433/rtl_433.conf` (which `rtl_433` auto-loads). Each distinct code
becomes one `decoder n=<name>,m=OOK_PWM,s=…,l=…,r=…,match={<bits>}<hex>` line, so
`rtl_433` recognises the device **by name** in every decode with no `-X` needed.
Verify a new entry offline with `rtl_433 -y '{<bits>}<hex>'`. The reference
build ships one **worked example** decoder (433.92 MHz OOK-PWM, 18-bit, device id
`0x7F4`; buttons up=`7f454` down=`7f45c` auto=`7f480` manual=`7f484`).

**Dongle recovery.** Heavy use can wedge the RTL-SDR into a state where it still
enumerates but fails at the streaming step (`rtl_433` exits 3 right after
allocating buffers). `POST /api/sdr/reset` issues a `USBDEVFS_RESET` to the
device (located via sysfs vendor/product `0bda:2838`/`2832`) and re-probes —
operator recovery from the web UI with no SSH or physical replug. `start_live`
also self-heals: if rtl_433 exits within ~1.4 s of launch it USB-resets the
dongle and retries once before surfacing a "check USB/power or replug" error.

Parameters for `capture`:

- `freq_hz` (int, optional) — centre frequency in Hz (default 433.92 MHz).
- `duration_s` (int, optional) — capture window (default 10 s, clamped to
  `max_duration_s`).
- `protocols` (int[], optional) — `rtl_433` protocol numbers to restrict
  decoding to; omitted means all enabled decoders.
- `sample_rate` (int, optional) — sample rate in Hz (default 250 kHz).
- `flex` (string, optional) — an `rtl_433` `-X` flex-decoder spec (e.g.
  `"n=awn,m=OOK_PWM,s=416,l=2150,r=16000"`) to decode a custom protocol.
  This is the recapture/verify path — it cuts through band noise the generic
  analyzer can't resolve.
- `gain` (number|`"auto"`, optional) — fixed tuner gain in dB. Omitted leaves
  the driver default (auto-AGC). A fixed gain avoids front-end saturation from
  a near-field transmitter: with AGC a strong close source rails the input to
  full scale, filling an OOK signal's off-gaps so it demodulates as a
  continuous/misdetected-FSK carrier and slices to all-zero codewords. `analyze`
  takes the same `gain` parameter.

Captures run `rtl_433 -M level`, so each decoded record carries `rssi`,
`snr`, and `noise` (dB). The `decode` response is
`{freq_hz, duration_s, count, events, max_snr, max_rssi, strong, snr_gate_db}`:
`events` is the list of `rtl_433` JSON records; `max_snr`/`max_rssi` are the
strongest package's levels; `strong` is the count of packages at or above
`snr_gate_db`. Callers distinguish signal from noise by thresholding on
`strong`/`max_snr` rather than trusting raw decode hits, which also fire on
ambient noise. The `analyze` response is `{freq_hz, duration_s, analyzer}`
where `analyzer` is the raw pulse text (ANSI-stripped).

#### 28.4 Configuration

`/etc/rfc2217/sdr.json` (installed by `pi/install.sh` from
`pi/config/sdr.json`):

```json
{
  "default_freq_hz": 433920000,
  "default_sample_rate": 250000,
  "default_duration_s": 10,
  "max_duration_s": 120,
  "snr_gate_db": 8.0,
  "rtl_433_bin": "rtl_433",
  "rtl_test_bin": "rtl_test"
}
```

#### 28.5 Driver Methods

```python
wt.sdr_status()
wt.sdr_capture(freq_hz=433_920_000, duration_s=15)     # decode → {count, events, max_snr}
wt.sdr_capture(protocols=[12], duration_s=30)          # restrict to one decoder
wt.sdr_capture(flex="n=awn,m=OOK_PWM,s=416,l=2150,r=16000")  # custom-protocol decode
wt.sdr_analyze(freq_hz=433_920_000, duration_s=10)     # recapture a remote
wt.sdr_stop()
```

---

## 9. Client Interfaces

How clients other than raw HTTP reach the API.

### 9.1 MCP Interface

An MCP (Model Context Protocol) server (`mcp/testbench_mcp.py`) exposes the HTTP
API as MCP tools, so an MCP client (Claude Desktop, Claude Code, …) can drive the
bench directly. It is a thin **stdio proxy** — 70 tools, one per endpoint, held
in a single `SPECS` table that mirrors the API: `GET` args become query params,
`POST`/`DELETE` args a JSON body, and `flash`/`ota`/`firmware_upload` upload local
files. Adding an API endpoint is one row in `SPECS`. The two udev callbacks
(`/api/hotplug`, `/api/wifi/lease_event`) are deliberately not exposed — they are
fired on the Pi itself, not client-callable.

The server runs on the **client** machine (not the Pi) and reaches the testbench
via the `TESTBENCH_URL` env var (default `http://<host>:8080`). It uses only the
Python standard library (stdio JSON-RPC + `urllib`), so it needs **no dependency
install** — only Python 3. It ships two ways, both covered in the
[User Manual §15.2](Harness-User-Manual.md#152-mcp-server):

- **`mcp/embedded-ai-harness-testbench.mcpb`** — a Claude Desktop extension (built
  from `mcp/manifest.json` + the server via `npx @anthropic-ai/mcpb pack`).
  Installed by drag-and-drop; the `testbench_url` user-config field prompts for
  `TESTBENCH_URL` at install.
- **manual registration** — `claude mcp add` (Claude Code) or a
  `claude_desktop_config.json` entry.

**Verified with Claude Code:**

```bash
claude mcp add testbench --env TESTBENCH_URL=http://<host>:8080 \
  -- python3 /abs/path/to/mcp/testbench_mcp.py
claude mcp list      # → testbench … ✔ Connected
```

The health check completes the MCP handshake and enumerates all 70 tools; live
tool calls (`sdr_status`, `testbench_devices`, `mqtt_status`, …) return real
bench data. Tools surface to the client as `mcp__testbench__<name>`.

---

## 10. Web Portal

The portal serves a single-page HTML UI at `GET /` (port 8080):

- **Serial slot cards** — one card per configured slot showing label, status
  badge (RUNNING/PRESENT/EMPTY), devnode, PID, and copyable RFC2217 URL
- **WiFi Testbench section** — mode toggle (WiFi-Testing / Serial Interface),
  AP status (SSID, channel, station count), and mode-specific information
- **Mode toggle** — clicking "Serial Interface" prompts for SSID/password;
  clicking "WiFi-Testing" switches back immediately
- **Activity Log** — scrollable log panel showing timestamped entries for
  hotplug events, WiFi testbench operations (sta_join, sta_leave, scan, HTTP
  relay), and enter-portal sequence steps.  Entries are categorised (info,
  ok, error, step) with colour coding.  "Enter Captive Portal" button
  triggers `POST /api/enter-portal` to connect to a DUT's captive portal
  SoftAP and submit WiFi credentials.  "Clear" button resets the display.  Log is polled every
  2 seconds via `GET /api/log?since=<last_ts>`.
- **Human interaction modal** — full-screen dark overlay with pulsing orange
  border, shown when a test script posts a human interaction request.
  Displays the operator instruction text with Done and Cancel buttons.
  Polled via `GET /api/human/status` as part of the auto-refresh cycle.
- **Test progress panel** — shown when a test session is active.  Displays
  spec name, phase, progress bar, current test step, and completed results
  (PASS/FAIL/SKIP with colour badges).  Polled via `GET /api/test/progress`.
- **Auto-refresh** — every 2 seconds via `setInterval`, fetches
  `/api/devices`, `/api/wifi/mode`, `/api/wifi/ap_status`, `/api/log`,
  `/api/human/status`, and `/api/test/progress`
- **Title** — `RFC2217 Embedded Testbench`

---

## 11. Non-Functional Requirements

### 6.1 Must Tolerate

| Scenario | How Handled |
|----------|-------------|
| `/dev/ttyACM0` → `/dev/ttyACM1` renaming | slot_key unchanged (based on physical port) |
| Duplicate udev events | API idempotency, per-slot locking |
| "Remove after add" races (USB reset) | Per-slot locking serializes operations; sequence counter aids diagnostics |
| Two identical boards | Different slot_keys (different physical connectors) |
| Hub/Pi reboot | Static config preserves port assignments; boot scan starts proxies |

### 6.2 Determinism

- Same physical connector → same TCP port (always)
- Configuration survives reboots
- No dynamic port assignment

### 6.3 Reliability

- Portal API must be idempotent
- Actions serialized per slot (threading.Lock)
- Stale events prevented via per-slot locking; sequence counter for observability

### 6.4 WiFi Mutual Exclusivity

- AP and STA are mutually exclusive — starting one stops the other
- Mode guard prevents testbench endpoints from running in serial-interface mode;
  guarded endpoints return HTTP 200 with `{"ok": false, "error": "WiFi testing
  disabled (Serial Interface mode)"}`

### 6.5 Edge Cases

| Case | Behavior |
|------|----------|
| Two identical boards | Works — different slot_keys (different physical connectors) |
| Device re-enumeration (USB reset) | Per-slot locking serializes add/remove; background thread restart is safe |
| Duplicate events | Idempotency prevents flapping |
| Unknown slot_key | Portal tracks the slot (present, seq) but does not start a proxy; logged for diagnostics |
| Hub topology changed | Must re-learn slots and update config |
| Dual-USB hub board | Board exposes onboard hub with JTAG + UART interfaces — occupies two slots (see §6.6) |
| Device not ready | Settle checks with timeout, then fail with `last_error` |
| ttyACM DTR trap | `wait_for_device()` skips `os.open()` for ttyACM; proxy uses controlled boot sequence (FR-006) |
| Boot loop (USB flapping) | Portal auto-recovers: unbinds USB, and enters download mode via GPIO (FR-007) **only where the BOOT/EN wiring has been measured** — `gpio_boot`/`gpio_en` are defaults the portal fills in, and configured pins are not connected pins. Unmeasured or unwired slots take the wire-free unbind/rebind cycle. Measure with `POST /api/serial/gpio-test`; manual trigger: `POST /api/serial/recover` |
| ESP32-C3 stuck in download mode | Run esptool on Pi with `--after=watchdog-reset` to trigger system reset (FR-006.6) |
| udev PrivateNetwork blocking curl | udev runs RUN+ handlers in a network-isolated sandbox (`PrivateNetwork=yes`). Direct `curl` to localhost silently fails. Fix: wrap the notify script with `systemd-run --no-block` in the udev rule so it runs outside the sandbox. |

### 6.6 Dual-USB Hub Boards

Some ESP32-S3 development boards contain an **onboard USB hub** that exposes
two USB interfaces through a single cable:

| Interface | USB ID | Purpose | Slot role |
|-----------|--------|---------|-----------|
| USB-Serial/JTAG | Espressif `303a:1001` | Flashing (esptool), DTR/RTS reset | **JTAG slot** |
| USB-to-UART bridge | e.g. CH340 `1a86:55d3`, CP2102 `10c4:ea60` | UART0 console output | **UART slot** |

These boards occupy **two slots** in the testbench configuration because the hub
presents two independent `ttyACM` (or `ttyUSB`) devices with distinct `ID_PATH`
values.  Both paths share a common hub parent — e.g. `usb-0:1.1.2:1.0` and
`usb-0:1.1.4:1.0` both descend from the hub at `usb-0:1.1`.

**Identifying which slot is which:**

```bash
# On the Pi — check each ttyACM device:
udevadm info -q property /dev/ttyACM0 | grep ID_SERIAL
# "Espressif" → JTAG slot (flash via this slot's RFC2217 URL)
# "1a86", "CH340", "CP210x" → UART slot (serial console output here)
```

**Operational rules for dual-USB hub boards:**

1. **Flashing:** always use the JTAG slot's RFC2217 URL with esptool
2. **Serial console (monitor/reset):** use the UART slot — this is where
   `ESP_LOGI` output appears when `CONFIG_ESP_CONSOLE_UART_DEFAULT=y`
3. **Serial reset via JTAG slot:** sends DTR/RTS signals through the
   USB-Serial/JTAG controller, which triggers the onboard auto-download
   circuit (reset + boot mode select).  This resets the chip but the
   resulting boot output appears on the UART slot, not the JTAG slot
4. **GPIO control:** these boards typically have GPIO0/EN connected to the
   onboard auto-download circuit, so external Pi GPIO wiring for reset/boot
   mode may not be needed — DTR/RTS on the JTAG slot suffices

**Slot configuration example:**

```json
{
  "slots": [
    {"label": "SLOT1", "slot_key": "platform-3f980000.usb-usb-0:1.1.2:1.0", "tcp_port": 4001},
    {"label": "SLOT2", "slot_key": "platform-3f980000.usb-usb-0:1.1.4:1.0", "tcp_port": 4002}
  ]
}
```

Where SLOT1 is the JTAG interface and SLOT2 is the UART bridge.  Label
convention: append `-jtag` and `-uart` to the label when documenting for
clarity.

### 6.7 GPIO Control Probe — Auto-Detecting Board Capabilities

Not all boards have their EN/BOOT pins wired to the Pi's GPIO headers.
Dual-USB hub boards have an onboard auto-download circuit that handles
reset and boot mode via DTR/RTS on the USB-Serial/JTAG interface, making
external GPIO wiring unnecessary.  Single-USB boards **may or may not**
have GPIO wires connected.

The testbench can auto-detect whether a board responds to Pi GPIO control
using a two-step probe:

#### Probe Algorithm

**CRITICAL:** Only use LOW (`0`) and HIGH (`1`) on EN and BOOT pins.  Release = drive HIGH.

```
Step 1: Try GPIO-based download mode entry
  1a. Drive Pi GPIO18 LOW (BOOT pin)
  1b. Wait 1 second (let pin settle)
  1c. Drive Pi GPIO17 LOW (EN/RST — assert reset)
  1d. Wait 200ms
  1e. Drive Pi GPIO17 HIGH (release reset — ESP32 samples BOOT pin now)
  1f. Wait 500ms
  1g. Drive Pi GPIO18 HIGH (release BOOT)
  1h. Monitor slot serial output for 3 seconds
  1i. Check for USB disconnect/reconnect in dmesg or boot mode in serial:
      - USB re-enumeration or "DOWNLOAD" boot mode → GPIO controls this board ✓
      - No USB event and no output → GPIO has no effect, go to Step 2

Step 2: Try USB DTR/RTS reset (fallback)
  2a. POST /api/serial/reset on the slot
  2b. Check boot output:
      - Got output with rst type indicating hardware reset → USB reset works
      - No output → slot may be wrong type or device not responding
```

#### Interpreting Results

| GPIO probe result | USB reset result | Conclusion |
|-------------------|-----------------|------------|
| DOWNLOAD mode | — | **GPIO-controlled board** — Pi GPIOs are wired to EN/BOOT |
| No effect | Hardware reset output | **USB-controlled board** — no GPIO wiring; use DTR/RTS via serial reset |
| No effect | No output | **No control available** — check wiring, or board may be on a different slot |
| DOWNLOAD mode | Also works | GPIO wired AND USB works — prefer USB (less invasive) |

#### Key Indicators in Serial Output

- **Reset reason (`rst:`)**: `0x1` = power-on, `0x3` = software, `0xc` = RTC watchdog/panic,
  `0x15` = USB_UART_CHIP_RESET (DTR/RTS hardware reset)
- **Boot mode (`boot:`)**: `0x23` or `0x03` = DOWNLOAD mode (GPIO probe succeeded),
  `0x28` or `0x29` = SPI_FAST_FLASH_BOOT (normal boot)

#### Caveats

1. **Only use LOW (`0`) and HIGH (`1`) on EN/BOOT pins.**  Release = drive HIGH.
2. **Firmware crash loops** produce continuous `rst:0xc` resets that can mask a
   GPIO-triggered reset.  For reliable probing, first erase flash
   (`esptool.py erase_flash`) so the board sits idle in bootloader, or flash
   known-good firmware that boots cleanly.
3. **Dual-USB hub boards** always respond to USB DTR/RTS on the JTAG slot.
   The GPIO probe will show no effect on these boards (GPIOs not connected
   to the onboard auto-download circuit).
4. The probe only needs to be run once per physical board — the result is
   stable and can be cached in the slot configuration.

---

## 12. Test Cases

### 7.1 Serial Tests

| ID | Name | Pass Criteria |
|----|------|---------------|
| TC-001 | Plug into SLOT3 | SLOT3 shows `running=true`, `devnode` set, `tcp_port=4003` within 5 s |
| TC-002 | Unplug from SLOT3 | SLOT3 shows `running=false`, `devnode=null` within 2 s |
| TC-003 | Replug into SLOT3 | SLOT3 `running=true`, same `tcp_port=4003`, devnode may differ |
| TC-004 | Two identical boards | Both running on different TCP ports (4001, 4002) |
| TC-005 | USB reset race | No "stuck stopped" state; per-slot locking serializes events |
| TC-006 | Devnode renaming | Original device still on SLOT1's port (4001) after renumbering |
| TC-007 | Boot persistence | Same slots get same ports after reboot |
| TC-008 | Unknown slot | Portal logs "unknown slot_key", no crash |

### 7.2 WiFi Testbench Tests

Tests are implemented in `pytest/testbench_test.py` and run via:
```
pytest testbench_test.py --wt-url http://<pi-ip>:8080
```

Add `--run-dut` to include tests that require a WiFi device under test.

| ID | Name | Category | Requires DUT |
|----|------|----------|:------------:|
| WT-100 | Ping response | Basic Protocol | No |
| WT-104 | Rapid commands | Basic Protocol | No |
| WT-200 | Start AP | SoftAP | No |
| WT-201 | Start open AP | SoftAP | No |
| WT-202 | Stop AP | SoftAP | No |
| WT-203 | Stop when not running | SoftAP | No |
| WT-204 | Restart AP new config | SoftAP | No |
| WT-205 | AP status when running | SoftAP | No |
| WT-206 | AP status when stopped | SoftAP | No |
| WT-207 | Max SSID length (32) | SoftAP | No |
| WT-208 | Channel selection | SoftAP | No |
| WT-300 | Station connect event | Station Events | Yes |
| WT-301 | Station disconnect event | Station Events | Yes |
| WT-302 | Station in AP status | Station Events | Yes |
| WT-303 | IP matches event | Station Events | Yes |
| WT-401 | Join a WPA2 network the DUT hosts, right passphrase | STA Mode | Yes |
| WT-402 | Wrong passphrase, against an AP that is on the air | STA Mode | Yes |
| WT-403 | Nonexistent SSID | STA Mode | No |
| WT-404 | Leave STA | STA Mode | Yes |
| WT-405 | AP stops during STA | STA Mode | Yes |
| WT-500 | GET request | HTTP Relay | Yes |
| WT-501 | POST with body | HTTP Relay | Yes |
| WT-502 | Custom headers | HTTP Relay | Yes |
| WT-503 | Connection refused | HTTP Relay | No* |
| WT-504 | Request timeout | HTTP Relay | No* |
| WT-505 | Large response | HTTP Relay | Yes |
| WT-506 | HTTP via STA mode | HTTP Relay | Yes |
| WT-600 | Scan finds networks | WiFi Scan | No |
| WT-601 | Scan returns fields | WiFi Scan | No |
| WT-602 | Scan is measured, not cached — an SSID off the air is gone from the next scan | WiFi Scan | No |
| WT-603 | Scan while AP running | WiFi Scan | No |

| WT-700 | Human interaction confirm | Human Interaction | No |
| WT-701 | Human interaction cancel | Human Interaction | No |
| WT-702 | Human interaction timeout | Human Interaction | No |
| WT-703 | Concurrent request rejected | Human Interaction | No |
| WT-800 | GPIO set low | GPIO Control | No |
| WT-801 | GPIO set high | GPIO Control | No |
| WT-802 | GPIO release to input | GPIO Control | No |
| WT-803 | GPIO status shows active pins | GPIO Control | No |
| WT-804 | GPIO disallowed pin rejected | GPIO Control | No |
| WT-805 | GPIO invalid value rejected | GPIO Control | No |
| WT-806 | GPIO captive portal trigger | GPIO Control | Yes |
| WT-900 | Test progress start session | Test Progress | No |
| WT-901 | Test progress step update | Test Progress | No |
| WT-902 | Test progress result recording | Test Progress | No |
| WT-903 | Test progress end session | Test Progress | No |
| WT-1000 | UDP log receive single line | UDP Log | Yes |
| WT-1001 | UDP log receive from multiple sources | UDP Log | Yes |
| WT-1002 | UDP log filter by source | UDP Log | Yes |
| WT-1003 | UDP log filter by since | UDP Log | Yes |
| WT-1004 | UDP log clear | UDP Log | No |
| WT-1005 | UDP log buffer overflow (>2000 lines) | UDP Log | Yes |
| WT-1100 | Firmware upload | OTA Firmware | No |
| WT-1101 | Firmware list | OTA Firmware | No |
| WT-1102 | Firmware download | OTA Firmware | No |
| WT-1103 | Firmware delete | OTA Firmware | No |
| WT-1104 | Firmware path traversal rejected | OTA Firmware | No |
| WT-1105 | ESP32 OTA from Pi firmware repo | OTA Firmware | Yes |
| WT-1200 | BLE scan finds devices | BLE Proxy | Yes |
| WT-1201 | BLE scan with name filter | BLE Proxy | Yes |
| WT-1202 | BLE connect to device | BLE Proxy | Yes |
| WT-1203 | BLE status shows connected | BLE Proxy | Yes |
| WT-1204 | BLE write to characteristic | BLE Proxy | Yes |
| WT-1205 | BLE disconnect | BLE Proxy | Yes |
| WT-1206 | BLE write when not connected | BLE Proxy | No |
| WT-1207 | BLE double connect rejected | BLE Proxy | Yes |
| WT-1300 | Signal generator start and status | Signal Generator | No |
| WT-1301 | Signal generator stop | Signal Generator | No |
| WT-1302 | Signal generator frequency list (gpclk) | Signal Generator | No |
| WT-1303 | Signal generator Morse keying | Signal Generator | No |
| WT-1304 | Signal generator replaces previous | Signal Generator | No |
| WT-1909 | RF loopback self-test: bench transmitter (86.784 MHz, 5th harmonic) lifts peak_db at 433.92 MHz by >= 15 dB at fixed gain | RF Path | No |
| WT-2400 | GPCLK: every listed frequency is exactly PLLD / integer | RF Synthesis (host) | No |
| WT-2401 | GPCLK: results stay inside the requested range | RF Synthesis (host) | No |
| WT-2402 | GPCLK: dividers stay within hardware limits | RF Synthesis (host) | No |
| WT-2403 | GPCLK: a gap with no achievable point returns nothing | RF Synthesis (host) | No |
| WT-2404 | GPCLK: frequency falls as divider rises | RF Synthesis (host) | No |
| WT-2410 | Si5351: returns the frequency actually programmed | RF Synthesis (host) | No |
| WT-2411 | Si5351: 5th harmonic of 86.784 MHz lands on 433.92 MHz (WT-1909's premise) | RF Synthesis (host) | No |
| WT-2412 | Si5351: VCO stays in 600-900 MHz across the range | RF Synthesis (host) | No |
| WT-2413 | Si5351: rejects frequencies outside the reachable range | RF Synthesis (host) | No |
| WT-2414 | Si5351: rejects an invalid channel | RF Synthesis (host) | No |
| WT-2415 | Si5351: PLL multiplier stays within 15..90 | RF Synthesis (host) | No |
| WT-2416 | Si5351: never programs an MS divider above the hardware limit | RF Synthesis (host) | No |
| WT-2417 | Si5351: the declared range is reachable, not aspirational | RF Synthesis (host) | No |
| WT-2420 | Morse: keys the expected number of elements | RF Synthesis (host) | No |
| WT-2421 | Morse: key_on and key_off always pair (no stuck carrier) | RF Synthesis (host) | No |
| WT-2422 | Morse: unknown characters are skipped, not keyed | RF Synthesis (host) | No |
| WT-2423 | Morse: rejects empty message and out-of-range WPM | RF Synthesis (host) | No |
| WT-2424 | Morse: code table matches ITU | RF Synthesis (host) | No |
| WT-2200 | `/api/devices` returns slots with labels and state | Serial Architecture | No |
| WT-2201 | Present device has `detected_chip` set | Serial Architecture | Yes |
| WT-2202 | Every present DUT slot has a detected chip | Serial Architecture | Yes |
| WT-2203 | `GET /api/serial/output` returns buffered lines | Serial Architecture | Yes |
| WT-2204 | `serial_output` respects the `since` timestamp filter | Serial Architecture | Yes |
| WT-2205 | `serial_monitor` reads from the buffer, not hardware | Serial Architecture | Yes |
| WT-2206 | `serial_monitor` matches a pattern from the buffer | Serial Architecture | Yes |
| WT-2207 | Multiple slots independently detect their chips | Serial Architecture | Yes |
| WT-2000 | MQTT broker start reports running + port 1883 | MQTT Broker | No |
| WT-2001 | MQTT broker status when stopped | MQTT Broker | No |
| WT-2002 | MQTT broker start is idempotent | MQTT Broker | No |
| WT-2100 | Captive-portal provisioning of a WiFiManager DUT | Captive Portal | Yes |
| WT-2101 | Provisioned DUT joins the testbench AP (appears as station) | Captive Portal | Yes |
| WT-2102 | NAT-bridged AP: DUT reaches the LAN broker (192.168.0.x MQTT) | Captive Portal | Yes |
| WT-1400 | Debug start (USB JTAG) | Debug: USB JTAG | Yes |
| WT-1401 | Debug stop restores serial | Debug: USB JTAG | Yes |
| WT-1402 | Debug status | Debug: USB JTAG | Yes |
| WT-1403 | Debug reject absent slot | Debug: USB JTAG | No |
| WT-1404 | Debug reject unsupported chip | Debug: USB JTAG | No |
| WT-1405 | Debug reject duplicate start | Debug: USB JTAG | Yes |
| WT-1406 | Hotplug suppressed during debug | Debug: USB JTAG | Yes |
| WT-1500 | Dual-USB group discovery | Debug: Dual-USB | Yes |
| WT-1501 | Debug start with serial coexist | Debug: Dual-USB | Yes |
| WT-1502 | Application USB port accessible | Debug: Dual-USB | Yes |
| WT-1503 | Serial monitor during debug | Debug: Dual-USB | Yes |
| WT-1600 | Probe list | Debug: ESP-Prog | No |
| WT-1601 | Debug start via probe | Debug: ESP-Prog | Yes |
| WT-1602 | Debug stop releases probe | Debug: ESP-Prog | Yes |
| WT-1603 | Serial available during probe debug | Debug: ESP-Prog | Yes |
| WT-1604 | Probe busy rejected | Debug: ESP-Prog | Yes |
| WT-1605 | Classic ESP32 via probe | Debug: ESP-Prog | Yes |
| WT-1700 | Auto-debug on hotplug (C3) | Debug: Auto | Yes |
| WT-1701 | Auto-debug on hotplug (S3) | Debug: Auto | Yes |
| WT-1702 | Auto-debug on hotplug (C6) | Debug: Auto | Yes |
| WT-1703 | Auto-debug on hotplug (H2) | Debug: Auto | Yes |
| WT-1704 | Auto-debug on boot | Debug: Auto | Yes |
| WT-1705 | Auto-debug reports in /api/devices | Debug: Auto | Yes |
| WT-1706 | Auto-debug fallback to ESP-Prog | Debug: Auto | Yes |
| WT-1707 | Manual debug_stop prevents auto-restart | Debug: Auto | Yes |
| WT-1708 | Hotplug suppressed during auto-debug | Debug: Auto | Yes |
| WT-1709 | Auto-debug skipped during flapping | Debug: Auto | No |
| WT-1800 | End-to-end: flash + serial verify | End-to-End | Yes |
| WT-1801 | End-to-end: halt and resume via JTAG | End-to-End | Yes |
| WT-1802 | End-to-end: single-step via JTAG | End-to-End | Yes |
| WT-1803 | End-to-end: memory read via JTAG | End-to-End | Yes |
| WT-1804 | End-to-end: hardware breakpoint | End-to-End | Yes |
| WT-1805 | End-to-end: debug auto-restarts after flash | End-to-End | Yes |

\* WT-503/504 require a running AP (wifi_network fixture) but not a physical DUT.
\* WT-18xx require debug-test firmware binaries in `debug-test/output/<chip>/`.

---

## 13. Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-05 | Claude | Initial FSD (serial only) |
| 1.1 | 2026-02-05 | Claude | Implemented serial-based port assignment |
| 1.2 | 2026-02-05 | Claude | Testing complete for serial-based approach |
| 2.0 | 2026-02-05 | Claude | Major rewrite: event-driven slot-based architecture |
| 3.0 | 2026-02-05 | Claude | Portal v3: direct hotplug handling, in-memory seq + locking, systemd-run udev |
| 4.0 | 2026-02-07 | Claude | WiFi Testbench integration: combined Serial + WiFi FSD, two operating modes, appendices for technical details |
| 5.0 | 2026-02-07 | Claude | ESP32-C3 native USB support: FR-006 (ttyACM handling, plain RFC2217 server, controlled boot sequence, USB reset types, flashing via SSH), FR-007 (USB flap detection), updated edge cases and device settle checks |
| 5.1 | 2026-02-08 | Claude | plain_rfc2217_server for ALL devices (ttyACM and ttyUSB); esp_rfc2217_server deprecated; flashing via RFC2217 works for both chip types (no SSH needed); updated proxy selection, flashing docs, deliverables |
| 5.3 | 2026-02-08 | Claude | Activity log system (`GET /api/log`, `POST /api/enter-portal` for captive portal trigger via rapid resets); WiFi testbench fixes (stale wpa_supplicant socket cleanup, `ctrl_interface=` in wpa_passphrase output, `dhcpcd` DHCP client support); activity logging for hotplug events and WiFi testbench operations; activity log UI panel with colour-coded entries |
| 5.2 | 2026-02-08 | Claude | Removed esp_rfc2217_server.py and serial_proxy.py (no longer installed); proxy auto-restart after esptool USB re-enumeration (background stop_proxy, BrokenPipeError fix, curl timeout 10s); FR-004 logging removed; updated deliverables |
| 6.0 | 2026-02-08 | Claude | Service separation — Serial and WiFi as independent services with state models (§1.6); serial reset (FR-008) and serial monitor (FR-009) as first-class API operations; flapping recovery via active reset; WiFi section renamed to WiFi Service with states Idle/Captive/AP; enter-portal rewritten as composite serial operation; consolidated API table (FR-010) |
| 6.1 | 2026-02-09 | Claude | Human interaction request (FR-017): blocking endpoint for test steps requiring physical operator actions; pulsing orange UI modal; ThreadingHTTPServer for concurrent requests; driver `human_interaction()` method; WT-700–703 test cases |
| 6.2 | 2026-02-09 | Claude | GPIO control (FR-018): drive Pi GPIO pins from test scripts to control DUT hardware signals (e.g. hold GPIO 2 low during boot for captive portal trigger); pin allowlist, lazy gpiod init, release-to-input lifecycle; WT-800–806 test cases. Test progress tracking (FR-019): live test session updates pushed to web UI; WT-900–903 test cases |
| 7.0 | 2026-02-25 | Claude | Three new services: UDP log receiver (FR-020) for ESP32 remote debug logs on port 5555; OTA firmware repository (FR-021) for serving .bin files to ESP32 OTA clients; BLE proxy (FR-022) for scan/connect/write to BLE peripherals via HTTP API using bleak. New deliverable: `ble_controller.py`. WT-1000–1207 test cases |
| 7.1 | 2026-03-15 | Claude | Hostname renamed Serial1 → workbench; all references updated to workbench.local. UDP discovery beacon added to portal.py (port 5888) — containers can discover the bench automatically. Skills consolidated from 14 → 9: merged flash skills into `esp-idf-handling` (auto-detects local vs bench), PIO skills into `esp-pio-handling`, FSD + WiFi tests into `fsd-writer` with 9 test spec libraries (WiFi, captive portal, MQTT, BLE, OTA, USB HID, NVS, watchdog, logging). Removed `esp32-` prefix from bench service skills. `fsd-writer` renamed from `esp32-fsd-writer` to be project-agnostic |
| 8.1 | 2026-03-28 | Claude | Auto-debug: OpenOCD starts automatically on hotplug/boot with chip auto-detection (C3/S3/C6/H2 via USB JTAG, classic ESP32 via ESP-Prog fallback). Debug status in /api/devices. Hotplug suppression during active debug. Zero-config: just plug in any ESP32. WT-1700–1709 test cases. TASK-160–166 |
| 8.3 | 2026-03-28 | Claude | Auto-discovery: fully plug-and-play slot management. No slots.json needed — devices auto-assigned labels (AUTO-1, AUTO-2), TCP ports (4001+), GDB ports (3333+). Renamed slots.json to testbench.json (hardware config only). Remove hotplug events processed during debugging (unplug detection fix). End-to-end verified: plug→flash→debug with zero configuration |
| 8.2 | 2026-03-28 | Claude | JTAG-based reset and recovery: `/api/serial/reset` auto-selects JTAG reset when debug session is active (no USB re-enumeration, no flapping risk). Flapping recovery via JTAG halt when available. Skills updated with JTAG reset documentation |
| 8.0 | 2026-03-27 | Claude | Remote GDB debugging — three variants: FR-024 USB JTAG (C3/S3 single-port, OpenOCD via built-in USB-Serial/JTAG), FR-025 Dual-USB (S3 two-port, serial+JTAG+app USB simultaneously), FR-026 ESP-Prog (external FT2232H probe for all ESP32 variants including classic). New `Debugging` slot state, `debug_controller.py` module, 5 API endpoints, slot groups for dual-USB, probe allocation for ESP-Prog. WT-1400–1605 test cases (18 tests). TASK-130–155 |
| 7.2 | 2026-03-27 | Claude | CW beacon (FR-023): Morse-keyed RF carrier via BCM2835 GPCLK hardware on GPIO 5/6 for direction finder testing; PLLD 500 MHz integer divider for jitter-free 80m band output; PARIS-standard Morse timing 1–60 WPM; cw_beacon.py module; 4 API endpoints; driver methods cw_start/stop/status/frequencies; WT-1300–1304 test cases |
| 9.0 | 2026-04-27 | Claude | Signal generator cleanup: retired the legacy `/api/cw/*` API and `cw_beacon.py` shim. FR-023 (CW beacon, GPCLK-only) merged into FR-027 (Signal Generator). FR-027 now covers Morse keying, the `freq`, `status`, and `frequencies` endpoints, and the full PE4302 attenuator path. Driver `cw_*` methods removed; tests WT-1300–1304 retargeted at `siggen_*`. Skill `cw-beacon` replaced by `signal-generator`. |
| 9.1 | 2026-04-28 | Codex | Si5351 output level handling documented: backend programs the lowest 2 mA CLK drive-current setting and leaves precise RF level control to the PE4302 attenuator. |
| 9.2 | 2026-07-05 | Claude | SDR receiver (FR-028): RTL-SDR + `rtl_433` receive-side service, counterpart to the transmit-only signal generator. `decode` mode returns decoded records (remotes/sensors/TPMS); `analyze` mode returns raw pulse timing for recapturing OOK remotes. Single-instance, bounded captures. New `sdr_controller.py`; 4 API endpoints `/api/sdr/{status,capture,analyze,stop}`; driver methods `sdr_*`; WT-1900–1905 test cases. |
| 9.3 | 2026-07-05 | Claude | Captive-portal provisioning + LAN bridge, verified against a WiFiManager DUT. `enter-portal` parameterized for arbitrary portal forms (WiFiManager `/wifisave`, `s`/`p` + `extra` MQTT fields) with an `internet` option; AP mode gains NAT bridging to `eth0` (`ap_start internet=true`, FR-011) so a provisioned DUT reaches the LAN/internet; MQTT broker wired to the API (FR-029, `/api/mqtt/*`, `mqtt_controller.py`). SDR (FR-028) gains a `flex` `-X` custom-decoder param and `-M level` rssi/snr signal-vs-noise reporting. Test cases WT-1906–1908 (SDR flex/RSSI, RF path), WT-2000–2002 (broker), WT-2100–2102 (captive-portal provisioning). |
| 9.4 | 2026-07-05 | Claude | SDR (FR-028) gains: fixed-gain (`-g`) + `peak_freq_hz`/`notch_hz` on power; phased `acquire` (locate→level→decode→classify) with `tools/sdr_acquire.py` CLI and live activity-log prompts; the interactive **live console** (persistent `rtl_433`, ring-buffer fast-poll `/api/sdr/live*`, RSSI meter, presets, `-A` in every mode so the signal meter is decode-independent); **AI Sherlock** session log (`/api/sdr/log*`) for AI reverse-engineering of unknown remotes; USB self-heal + `/api/sdr/reset`; and an `rtl_433` device database (`pi/config/rtl_433.conf`) shipping one worked-example decoder. New skill `sdr-receiver`. |
| 9.5 | 2026-08-03 | Claude | MCP surface completed to 70 tools — added `firmware_upload/delete`, `udplog_get/clear`, `debug_group`, `test_update`, `wifi_events`, `human_interaction/done/cancel`; `DELETE` added as a transport method. Only the two udev callbacks (`/api/hotplug`, `/api/wifi/lease_event`) remain unexposed. |
| 10.0 | 2026-08-03 | Claude | Documentation consolidated to two documents: this FSD (WHAT) and the User Manual (HOW). The separate user manual, WiFi HTTP manual, skill-testing guide, and the `pi/` and `mcp/` READMEs merged into `Harness-User-Manual.md`; root `README.md` reduced to a landing page. FSD sections regrouped by subsystem — FR-017–FR-021 out of "WiFi Service" into §5, BLE + MQTT into §6, the three GDB specs into §7, signal generator + SDR into §8, and the MCP interface promoted out of FR-006 into §9. FR numbers and clause text unchanged. |

---

## Appendix A: Technical Details

### A.1 Slot Key Derivation

```python
def get_slot_key(udev_env):
    """Derive slot_key from udev environment variables."""
    # Preferred: ID_PATH (stable across reboots)
    if 'ID_PATH' in udev_env and udev_env['ID_PATH']:
        return udev_env['ID_PATH']

    # Fallback: DEVPATH (less stable but usable)
    if 'DEVPATH' in udev_env:
        return udev_env['DEVPATH']

    raise ValueError("Cannot determine slot_key: no ID_PATH or DEVPATH")
```

### A.2 Sequence Counter

The portal owns a single global monotonic `seq_counter` in memory (no files
on disk).  Every hotplug event increments the counter and stamps the affected
slot:

```python
# Module-level state (in portal.py)
seq_counter: int = 0

# Inside _handle_hotplug:
seq_counter += 1
slot["seq"] = seq_counter
slot["last_action"] = action       # "add" or "remove"
slot["last_event_ts"] = datetime.now(timezone.utc).isoformat()
```

The sequence number provides a total ordering of events for diagnostics.
Because the portal processes hotplug requests serially per slot (via per-slot
locks), stale-event races are prevented by locking rather than by comparing
counters.

### A.3 API Idempotency

**POST /api/start semantics:**
- If slot running with same devnode: return OK (no restart)
- If slot running with different devnode: restart cleanly
- If slot not running: start
- Never fails if already in desired state

**POST /api/stop semantics:**
- If slot not running: return OK
- If running: stop
- Never fails if already in desired state

### A.4 Per-Slot Locking

Portal serializes operations per slot using in-memory `threading.Lock` objects:

```python
# Each slot dict holds its own lock (created at config load time)
slot["_lock"] = threading.Lock()

# Usage (e.g., inside hotplug add handler):
with slot["_lock"]:
    stop_proxy(slot)   # stop old proxy if running
    start_proxy(slot)  # start new proxy
```

No file-based locks or `/run/rfc2217/locks/` directory is used.

### A.5 Device Settle Checks

The portal's `start_proxy` function performs settle checks inline (no separate
handler).  It polls the device node before launching the proxy:

```python
def wait_for_device(devnode, timeout=5.0):
    """Wait for device to be usable (called inside portal)."""
    is_native_usb = devnode and "ttyACM" in devnode
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(devnode):
            if is_native_usb:
                return True  # Don't open — avoids DTR reset (see FR-006)
            try:
                fd = os.open(devnode, os.O_RDWR | os.O_NONBLOCK)
                os.close(fd)
                return True
            except OSError:
                pass
        time.sleep(0.1)
    return False
```

**ttyACM devices:** Only checks file existence — `os.open()` is skipped
because the Linux `cdc_acm` driver asserts DTR+RTS on open, which puts
ESP32-C3 native USB devices into download mode (see FR-006.4).

**ttyUSB devices:** Probes with `os.open()` as before — UART bridge chips
are not affected by DTR on open.

If the device does not settle within the timeout, the slot's `last_error` is
set and the proxy is not started.

### A.6 udev Rules

```
# /etc/udev/rules.d/99-rfc2217-hotplug.rules
# Notify portal of USB serial add/remove events.
# systemd-run escapes udev's PrivateNetwork sandbox so curl can reach localhost.

ACTION=="add", SUBSYSTEM=="tty", KERNEL=="ttyACM*", RUN+="/usr/bin/systemd-run --no-block /usr/local/bin/rfc2217-udev-notify.sh %E{ACTION} %E{DEVNAME} %E{ID_PATH} %E{DEVPATH}"
ACTION=="remove", SUBSYSTEM=="tty", KERNEL=="ttyACM*", RUN+="/usr/bin/systemd-run --no-block /usr/local/bin/rfc2217-udev-notify.sh %E{ACTION} %E{DEVNAME} %E{ID_PATH} %E{DEVPATH}"
ACTION=="add", SUBSYSTEM=="tty", KERNEL=="ttyUSB*", RUN+="/usr/bin/systemd-run --no-block /usr/local/bin/rfc2217-udev-notify.sh %E{ACTION} %E{DEVNAME} %E{ID_PATH} %E{DEVPATH}"
ACTION=="remove", SUBSYSTEM=="tty", KERNEL=="ttyUSB*", RUN+="/usr/bin/systemd-run --no-block /usr/local/bin/rfc2217-udev-notify.sh %E{ACTION} %E{DEVNAME} %E{ID_PATH} %E{DEVPATH}"
```

The udev notify script posts a JSON payload to the portal:

```bash
#!/bin/bash
# /usr/local/bin/rfc2217-udev-notify.sh
# Args: ACTION DEVNAME ID_PATH DEVPATH

curl -m 10 -s -X POST http://127.0.0.1:8080/api/hotplug \
  -H 'Content-Type: application/json' \
  -d "{\"action\":\"$1\",\"devnode\":\"$2\",\"id_path\":\"${3:-}\",\"devpath\":\"$4\"}" \
  || true
```

### A.7 WiFi Lease Notify Script

dnsmasq calls this script on DHCP lease events (add/old/del):

```bash
#!/bin/sh
# /usr/local/bin/wifi-lease-notify.sh
# Args: ACTION MAC IP HOSTNAME

curl -s -X POST -H "Content-Type: application/json" \
     -d "{\"action\":\"${1}\",\"mac\":\"${2}\",\"ip\":\"${3}\",\"hostname\":\"${4:-}\"}" \
     --max-time 2 "http://127.0.0.1:8080/api/wifi/lease_event" >/dev/null 2>&1 || true
```

### A.8 systemd Service

The portal runs as a long-lived systemd service.  udev events are delivered
via `systemd-run` and the notify script (see A.6).

```ini
# /etc/systemd/system/rfc2217-portal.service
[Unit]
Description=RFC2217 Portal
After=network.target

[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/rfc2217-portal
Restart=on-failure
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### A.9 Network Ports

| Port | Protocol | Service |
|------|----------|---------|
| 8080 | TCP/HTTP | Web portal, REST API, firmware downloads |
| 4001 | TCP/RFC2217 | SLOT1 serial proxy |
| 4002 | TCP/RFC2217 | SLOT2 serial proxy |
| 4003 | TCP/RFC2217 | SLOT3 serial proxy |
| 5555 | UDP | ESP32 debug log receiver |
| 5888 | UDP | Discovery beacon responder |

### A.10 WiFi Configuration Constants

| Constant | Value |
|----------|-------|
| WLAN_IF | `wlan0` (env: `WIFI_WLAN_IF`) |
| AP_IP | `192.168.4.1` |
| AP_NETMASK | `255.255.255.0` |
| AP_SUBNET | `192.168.4.0/24` |
| DHCP_RANGE_START | `192.168.4.2` |
| DHCP_RANGE_END | `192.168.4.20` |
| DHCP_LEASE_TIME | `1h` |
| WORK_DIR | `/tmp/wifi-testbench` |
| VERSION | `1.0.0-pi` |

---

## Appendix B: Slot Learning Workflow

**Note (v8.3):** The rfc2217-learn-slots tool is no longer required for basic operation. Devices are auto-detected on plug-in. This tool is only useful for identifying physical hub port topology.

### B.1 Tool: rfc2217-learn-slots

```bash
$ rfc2217-learn-slots
Plug a device into the USB hub connector you want to identify...

Detected device:
  DEVNAME:  /dev/ttyACM0
  ID_PATH:  platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0
  DEVPATH:  /devices/platform/scb/fd500000.pcie/.../ttyACM0
  BY-PATH:  /dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0

Add this to /etc/rfc2217/testbench.json:
  {"label": "SLOT?", "slot_key": "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0", "tcp_port": 400?}
```

### B.2 Initial Setup Procedure

1. Start with empty `testbench.json`
2. Plug device into first hub connector
3. Run `rfc2217-learn-slots`, note the `ID_PATH`
4. Add to config as SLOT1 with `tcp_port: 4001`
5. Repeat for each hub connector
6. Restart portal service

---

## Appendix D: HTTP API & MCP Reference

All endpoints are served from `http://<pi-ip>:8080`. No authentication. Requests
and responses are JSON (except firmware/OTA/flash upload+download, which use
multipart form-data and raw binary). Every response includes `"ok": true|false`;
errors add `"error": "..."`.

### D.1 Device Discovery

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/devices` | List all slots with status, RFC2217 URL, detected chip, debug port, USB device info |
| GET | `/api/info` | Pi IP, hostname, total slot count, portal uptime |

`state` is one of `absent`, `idle`, `monitoring`, `resetting`, `debugging`, `recovering`, `download_mode`.

### D.2 Serial Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/serial/reset` | Reset a device `{"slot"}`. **The method depends on the slot; the response shape does not.** `output` is always the device's boot lines. No debug session: DTR/RTS, stopping and restarting the proxy. Debug session active: JTAG `reset run` — no USB re-enumeration, proxy stays up — and the reply adds `"method": "jtag"`, `"command"` and `"openocd"` carrying OpenOCD's own text. Falls back to DTR/RTS if the JTAG reset fails |
| POST | `/api/serial/monitor` | Wait for a pattern `{"slot", "pattern?", "timeout?"}` → `{"ok", "matched", "line", "output"}` |
| GET | `/api/serial/output` | Passive buffer read `?slot=&lines=&since=` |
| POST | `/api/serial/write` | Send bytes `{"slot", "text"\|"hex", "newline?"}` → `{"ok", "written"}` (FR-030) |
| POST | `/api/serial/recover` | Manual flap-recovery trigger `{"slot"}`. Reports the *attempt*; the outcome appears in `/api/devices` as `download_mode` or `flapping` + `last_error` |
| POST | `/api/serial/gpio-test` | Measure whether BOOT/EN are physically wired to this slot's board `{"slot"}` → `{en_wired, boot_wired, detail}` |
| POST | `/api/serial/release` | Release BOOT GPIO + reboot after a download-mode flash `{"slot"}` |
| POST | `/api/enter-portal` | Provision a captive-portal DUT (WiFiManager: `portal_ssid`, `ssid`, `password`, `save_path=/wifisave`, `field_ssid=s`, `field_password=p`, `method=POST`, `internet`, `extra`); or trigger with `{slot, resets}` |
| POST | `/api/start` · `/api/stop` | Manually start / stop the proxy for a slot `{"slot"}` (or `slot_key`). `start` takes an optional `devnode`, defaulting to the slot's own |
| POST | `/api/hotplug` | udev hotplug event (internal) |

**Access manager (FR-031 – FR-035).** Every consumer acquires a slot before
using it — the portal's own operations, external clients and project test
suites alike. A conflicting request is refused, never pre-empted.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/slot/acquire` | Request a mode `{"slot", "mode", "owner", "ttl?"}` → `{"ok", "token", "mode", "expires_in"}`; or **409** `{"ok": false, "error": "held", "mode", "owner", "since", "expires_in"}` |
| POST | `/api/slot/renew` | Extend a grant `{"token"}` → `{"ok", "expires_in"}`. Hold by renewing; `ttl` defaults to 60 s |
| POST | `/api/slot/release` | Give the slot back `{"token"}` |
| GET | `/api/slot/mode` | Current mode `?slot=` → `{"ok", "mode", "owner", "since", "expires_in"}` |

**Bench reset (FR-036).** The first call of every test run.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/bench/reset` | Return every subsystem to its initial state → `{"ok", "changed": [...], "errors": [...]}` |

### D.3 GDB Debug

Auto-started on plug-in for USB-JTAG chips / configured ESP-Prog probes; these override auto-detection.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/debug/start` | Start OpenOCD `{"slot?", "chip?", "probe?"}` → `{"ok", "slot", "chip", "gdb_port", "telnet_port"}` |
| POST | `/api/debug/stop` | Stop OpenOCD `{"slot?"}` |
| GET | `/api/debug/status` | Debug state per slot |
| GET | `/api/debug/group` | Slot groups and roles (dual-USB ESP32-S3) |
| GET | `/api/debug/probes` | Available ESP-Prog probes |

### D.4 WiFi Instrument

AP and STA modes are mutually exclusive.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET · POST | `/api/wifi/mode` | Get / switch mode `{"mode"}` |
| POST | `/api/wifi/ap_start` | Start SoftAP `{"ssid", "password?", "channel?", "internet?"}` → `{"ok", "ip"}` |
| POST | `/api/wifi/ap_stop` | Stop SoftAP |
| GET | `/api/wifi/ap_status` | `{"active", "ssid", "channel", "stations": [{"mac", "ip"}, ...]}` |
| POST | `/api/wifi/sta_join` · `sta_leave` | Join / leave a network `{"ssid", "pass?"}` |
| GET | `/api/wifi/scan` | Scan nearby WiFi networks |
| POST | `/api/wifi/http` | HTTP relay through wlan0 `{"method", "url", "headers?", "body?"}` |
| GET | `/api/wifi/events` | Long-poll station events `?timeout=` |
| GET | `/api/wifi/ping` | Version and uptime. Answers in either mode, so it is the reachability probe |
| POST | `/api/wifi/lease_event` | dnsmasq lease hook `{"action", "mac", "ip", "hostname"}` — called by the Pi, not by clients |

### D.5 BLE Proxy

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/ble/scan` | Scan `{"timeout?", "name_filter?"}` → list of `{"address", "name", "rssi"}` |
| POST | `/api/ble/connect` · `disconnect` | Connect by MAC `{"address"}` / disconnect |
| GET | `/api/ble/status` | `{"state", "address?"}` |
| POST | `/api/ble/write` | Write a GATT characteristic `{"characteristic", "data" (hex), "response?"}` |

### D.6 GPIO Control

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/gpio/set` | Drive pin `{"pin", "value": 0\|1\|"z"}` |
| GET | `/api/gpio/status` | State of all driven pins |

Allowlist `{16,17,18,19,20,21,22,23,24,25,26,27}` (others reserved for I²C/GPCLK/PE4302). Always release with `"z"`.

### D.7 UDP Log

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/udplog` | `?since=&source=&limit=` — buffered log lines |
| DELETE | `/api/udplog` | Clear the buffer |

### D.8 Firmware Repository

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/firmware/<project>/<file>` | Download binary (ESP32 OTA clients) |
| GET | `/api/firmware/list` | List firmware files |
| POST | `/api/firmware/upload` | Upload (multipart: `project` + `file`) |
| DELETE | `/api/firmware/delete` | Delete `{"project", "filename"}` |

### D.9 Flashing (USB + OTA)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/flash` | Local-Pi esptool flash of a slot (bridge-chip boards). Multipart: `slot`, `chip`, `baud`, `erase?`, one `bin@<offset>` file part per image (§6.7.1) |
| POST | `/api/flash/read` | Read a flash region back off a slot. JSON: `slot`, `offset`, `length`, `chip?`, `baud?` → `{"sha256", "data_b64"}` (§6.7.1a) |
| POST | `/api/ota` | OTA a deployed on-LAN board (espota relayed by the Pi). Multipart: `firmware` file, `target`, `port?`, `auth?` (§6.7.2) |
| POST | `/api/chip/info` | Chip and **physical** flash identity via `esptool flash_id` `{"slot", "chip?"}` → `{"chip", "revision", "features", "crystal", "usb_mode", "mac", "flash_size", "flash_manufacturer", "flash_device", "output"}`. Reboots the DUT (§6.7.3) |

RFC2217 flashing (esptool from the host) needs no endpoint (§6.7).

### D.10 SDR Receiver

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/sdr/status` | Dongle + tool detection, active state |
| POST | `/api/sdr/capture` | Decode window `{freq_hz, duration_s, gain?, sample_rate?, flex?}` |
| POST | `/api/sdr/analyze` | Pulse-analyzer window (raw timing + RSSI) |
| POST | `/api/sdr/power` | `rtl_power` sweep `{freq_hz, span_hz, bin_hz, notch_hz?, gain?}`. Pin `gain` for any reading compared against a threshold — on AGC the tuner rescales from recent history |
| POST | `/api/sdr/acquire` | Phased locate → level → decode → classify |
| POST | `/api/sdr/live/start` · `/stop` | Live rtl_433 console |
| GET | `/api/sdr/live` · `/live/status` | Poll ring buffer `?since=` · console state |
| POST | `/api/sdr/log/start` · `/stop`, GET `/api/sdr/log` | AI-Sherlock session recording |
| POST | `/api/sdr/reset` · `/stop` | USB-reset the dongle · stop a capture |

### D.11 Signal Generator

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/siggen/start` | Start carrier `{"freq_hz", "backend?", "channel?", "pin?", "atten_db?", "morse?"}` |
| POST | `/api/siggen/stop` | Stop carrier |
| POST | `/api/siggen/freq` | Retune `{"freq_hz", "channel?"}` |
| POST | `/api/siggen/atten` | PE4302 attenuation `{"db": 0..31.5}` |
| GET | `/api/siggen/status` | State + hardware detection (`si5351`, `gpclk`, `pe4302`) |
| GET | `/api/siggen/frequencies` | Achievable frequencies `?low=&high=&backend=` |

### D.12 MQTT Test Broker

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/mqtt/start` · `/stop` | Start / stop the mosquitto test broker |
| GET | `/api/mqtt/status` | Running state + port |

### D.13 Test Progress & Human Interaction

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/test/update` | Push start/step/result/end |
| GET | `/api/test/progress` | Current session state |
| POST | `/api/human-interaction` | Show a modal, block until confirmed `{"message", "timeout?"}` |
| GET | `/api/human/status` | Is an interaction pending? |
| POST | `/api/human/done` · `/cancel` | Confirm / cancel the pending interaction |

### D.14 Activity Log

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/log` | Recent activity entries `?since=<iso-timestamp>` |

### D.15 MCP Interface

`mcp/testbench_mcp.py` exposes this entire API as **70 MCP tools** (one per
endpoint, from a single `SPECS` table) for MCP clients such as Claude Desktop and
Claude Code. It is a thin **stdio proxy** that runs on the client machine and
reaches the bench via `TESTBENCH_URL`: `GET` args become query params, `POST`/`DELETE`
args a JSON body, and `flash`/`ota`/`firmware_upload` upload local files. Adding an
endpoint above is one row in `SPECS`. The server is standard-library only (no
`pip install`); it ships as a one-click Claude Desktop `.mcpb` extension
(`mcp/manifest.json`) or via manual `claude mcp add` / `claude_desktop_config.json`.
Install and client setup: [User Manual §15.2](Harness-User-Manual.md#152-mcp-server).
Verified with Claude Code
(`claude mcp add` → `claude mcp list` → ✔ Connected).

---

## Changelog

### 2026-03-28 — Flash architecture, auto-detection, test progress

**Flash via RFC2217 (FR-006 §6.7):**
- Flashing uses esptool from the host over RFC2217 — binaries stay on the host
- Uses `--after no-reset` to avoid USB re-enumeration; device rebooted via `POST /api/serial/reset`
- Stop debug before flash on native USB chips (serial + JTAG share USB)
- Removed `SerialReader` thread — portal never opens serial devices directly
- Root cause: dual process access to USB serial crashes `dwc_otg` on Pi Zero 2 W

**Auto-detection and OpenOCD:**
- Boot scan auto-detects chip type via JTAG TAP ID probing
- Auto-starts OpenOCD debug session on device plug-in
- `detected_chip` and `jtag_slot` exposed in `/api/devices` per slot
- Debug auto-restarts after flash without manual intervention

**Test progress UI:**
- Progress bar with percentage (`2 / 6 (33%)`) in portal web UI
- Pass/fail/skip counters with color-coded bar (green/red)
- Fixed `test_result` reporting (was silently failing due to parameter name mismatch)

**Renames:**
- `test_instrument.py` → `testbench_test.py`
- `WIFI_TESTER_URL` → `TESTBENCH_URL` environment variable

---

### 2026-08-10 — one name for the machine, one name for its counterpart

The bench was a *workbench* in 302 places and a *testbench* in 21, and its own
ESP32 was called a *DUT* — a device under test, which it is not: the bench is
what is under test, and that board is the counterpart it measures itself
against. Both names are now single-valued throughout the repository, the
skills, the Pi and the MCP bundle.

**Renames:**
- everything `workbench` → `testbench`, including the Pi's hostname
  (`$BENCH`), the eight `testbench-*` skills, `testbench_test.py`,
  `testbench_driver.py`, `testbench_mcp.py`, `/etc/rfc2217/testbench.json`,
  `TESTBENCH_URL`, `TestbenchDriver`, `TestbenchError`, and the discovery
  beacon's `service` field
- the bench's own ESP32 `bench DUT` → **test partner**: `docs/test-partner.md`,
  `TEST_PARTNER_PORTAL`, `TEST_PARTNER_HTTP_PORT`, the `test_partner` fixture,
  `WT_TEST_PARTNER_IMAGE`, and the `test-partner-<target>` CI artefact

**Kept deliberately:** `--run-dut` and `requires_dut` select on *hardware
attached*, which is still what they mean, and `provision_dut` provisions any
project's device rather than this one.

**Migrations, because two of these are operator state rather than spelling:**
`install.sh` renames `/etc/rfc2217/workbench.json` if it finds one, and the
portal reads the old path when the new one is absent and says so — the file
holds hand-written slot labels, GPIO pins and probe declarations, so an
upgrade that silently stopped reading it would come up looking healthy with
somebody's wiring forgotten. `install.sh` also sets the hostname and fixes
`/etc/hosts` to match, since a stale `127.0.1.1` stalls every `sudo`.

**Generalisation.** Project-specific content is gone: the reverse-engineered
remote that shipped in `pi/config/rtl_433.conf` is now a commented worked
example, and the portal-form and OTA-target examples name `device.local` and
`Device-Setup` rather than one project's board.

---

### 2026-08-10 — flap recovery, which had never run on a default bench

`/api/serial/recover` reported `ok: true` and did nothing on any bench whose
slots are auto-detected — which the manual calls the normal case. The USB
device to unbind was parsed out of `slot_key`, and that key is a udev
`ID_PATH` only for slots pinned in a config file; auto-detected slots carry a
synthetic `_fixed_SLOT2`, so the parse returned `None` and recovery aborted at
its first guard.

- the USB device is now resolved from the slot's devnode when the key cannot
  name it (`_usb_device_for_slot`)
- the endpoint refuses up front when neither can name it, instead of
  reporting a start it did not perform
- GPIO recovery **probes** the device with esptool (`--before no-reset`, so
  the check cannot create the condition it observes) instead of asserting
  `download_mode`; the devnode is re-resolved from sysfs first, because a
  rebind re-enumerates and the kernel hands out a different `ttyACM` number
- a failed attempt releases BOOT instead of leaving the board unable to boot
- `/api/serial/release` is no longer gated on `download_mode`: lifting a pin
  is cleanup, and refusing it stranded the board that most needed it
