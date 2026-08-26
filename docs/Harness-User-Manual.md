# The Harness — User Manual

How to build, wire, and operate the Harness testbench: a Raspberry Pi that turns
into a complete remote test instrument for ESP32 devices — serial, debug, WiFi,
BLE, GPIO, MQTT, RF signal generation, and SDR receive — all over one HTTP API.

**This manual is the operator's guide — the OPERATE plane: how to run the bench.**
It is one of three documents, each answering a different question:

| Plane | Question | Document |
|-------|----------|----------|
| WHAT | What must be true of the bench? | [`Harness-FSD.md`](Harness-FSD.md) — **[Appendix D](Harness-FSD.md#appendix-d-http-api--mcp-reference)** is the complete HTTP API and MCP tool reference |
| HOW | How is it built and changed? | [`Method/`](Method/00-Overview.md) |
| **OPERATE** | **How do I run it?** | **this manual** |

When this manual shows a call, the FSD defines its exact contract. If the two
disagree, this manual is stale — it describes the system as built.

Throughout, `$BENCH` stands for **the bench's IP address**, and the API lives
on port **8080**. Set it once per shell:

```bash
BENCH=$(python3 .claude/skills/esp-idf-handling/discover-testbench.py | jq -r .ip)
curl http://$BENCH:8080/api/info
```

**An IP, never an mDNS name.** `.local` names do not resolve from inside a
container — multicast DNS does not cross the Docker bridge — and a container
is where most of this runs, so a hostname in a command is a command that works
on your laptop and fails in CI for reasons that look like the bench being
down. The bench answers a UDP probe on port 5888 and the discovery script
sweeps the subnet for it, which works from anywhere that can route to the
bench at all. The hostname is for humans reading `hostnamectl`; the address is
what tools use.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Building the Pi](#2-building-the-pi)
3. [Network and ports](#3-network-and-ports)
4. [Connecting to serial devices](#4-connecting-to-serial-devices)
5. [Flashing firmware](#5-flashing-firmware)
6. [GDB / JTAG debugging](#6-gdb--jtag-debugging)
7. [WiFi test instrument](#7-wifi-test-instrument)
8. [GPIO control](#8-gpio-control)
9. [BLE proxy](#9-ble-proxy)
10. [MQTT test broker](#10-mqtt-test-broker)
11. [RF signal generator](#11-rf-signal-generator)
12. [SDR receiver](#12-sdr-receiver)
13. [UDP logging and the firmware repository](#13-udp-logging-and-the-firmware-repository)
14. [Test automation](#14-test-automation)
15. [Driving the bench from Claude](#15-driving-the-bench-from-claude)
16. [Validating the bench](#16-validating-the-bench)
17. [Troubleshooting](#17-troubleshooting)
18. [Security](#18-security)
19. [Quick reference](#19-quick-reference)

---

## 1. What you need

| Component | Purpose |
|-----------|---------|
| **Raspberry Pi** (any model) | Runs the portal. Needs onboard WiFi + Bluetooth. Auto-detects model and USB topology. |
| **USB Ethernet adapter** (Pi Zero 2 W only) | Wired LAN on eth0 — wlan0 is reserved for WiFi testing. Pi 3/4/5 have built-in Ethernet. |
| **USB hub** (Pi Zero 2 W only) | Connect multiple ESP32 boards. Pi 3/4/5 already have 4 USB ports. |
| **RTL-SDR dongle** (optional) | 433/315/868 MHz receive gateway via `rtl_433`. |
| **Si5351 + PE4302** (optional) | RF signal source + step attenuator. |
| **Jumper wires** (optional) | Pi GPIO to DUT boot/reset pins for automated download-mode control. |

Slots are **auto-detected**: on startup the portal walks `/sys/bus/usb/devices/`,
finds every downstream hub, and creates one slot per usable port. No config file
is needed.

| Pi model | Expected slots | Notes |
|----------|---------------|-------|
| Pi Zero 2 W + external hub | 3–4 (hub ports minus ethernet) | Tested |
| Pi 3 B+ | 4 | Phantom port `0:1.4` filtered via model table (tested on Rev 1.3) |
| Pi 4 B | 2 USB2 + 2 USB3 slots | Same kernel API, expected to work |
| Pi 5 | Up to 4 slots on XHCI | Same kernel API, expected to work |

Some Pi boards advertise more hub ports than are physically wired to USB-A jacks.
From sysfs alone these "phantom" ports look identical to empty wired jacks, so the
portal keeps a per-model table keyed on `/proc/device-tree/model`
(`_PHANTOM_PORTS_BY_MODEL` in `pi/portal.py`). Add an entry there if you find a
new phantom on a model not yet listed.

---

## 2. Building the Pi

### 2.1 Flash the OS

Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In the imager
settings:

- **Hostname:** `testbench` — still the old spelling, and deliberately: it is
  what makes `$BENCH` resolve, and every project pointing at this
  bench uses that name. The machine is a *testbench* everywhere it is
  described; renaming the host is a separate change that has to happen on the
  Pi and in those projects at the same moment.
- **Enable SSH:** yes (password or key auth)
- **Username:** `pi`
- **WiFi:** configure your network (country code as needed)
- **Locale:** set timezone as needed

Note that `install.sh` renames the host to `testbench-XXXX`, where `XXXX` is
the last four hex digits of the wlan0 MAC, so whatever you set here is
replaced on the first install — and again on every later one, since the rename
is not a one-time step. To keep a name you chose deliberately:

```bash
sudo mkdir -p /etc/rfc2217 && sudo touch /etc/rfc2217/keep-hostname
```

The installer then reports `Keeping host '<name>'` and leaves it alone. If you
change the hostname by hand, change the `127.0.1.1` line in `/etc/hosts` to
match in the same breath, or every `sudo` stalls on "unable to resolve host".

### 2.2 First boot — system hardening

These changes prevent the OOM crash cycle that kills Pi Zero 2 W boards (512 MB).
**Do this before installing the testbench.** On a Pi 3/4/5 with 1 GB or more you
can skip to [2.3](#23-install-the-testbench).

```bash
ssh pi@$BENCH

# --- Reduce GPU memory (saves 48 MB on a headless Pi) ---
echo "gpu_mem=16" | sudo tee -a /boot/firmware/config.txt

# --- Add real disk swap (zram alone is not enough for 512 MB) ---
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# --- Disable unnecessary services ---
sudo systemctl disable --now ModemManager 2>/dev/null || true
sudo systemctl disable cloud-init cloud-init-local cloud-init-main \
     cloud-init-network cloud-final cloud-config 2>/dev/null || true

# --- Fix dual wpa_supplicant conflict ---
# Keep wpa_supplicant.service (used by NetworkManager), disable the
# interface-specific instance that fights over wlan0
sudo systemctl disable --now wpa_supplicant@wlan0 2>/dev/null || true

# --- Limit journal size ---
sudo mkdir -p /etc/systemd/journald.conf.d
cat <<'EOF' | sudo tee /etc/systemd/journald.conf.d/size.conf
[Journal]
SystemMaxUse=16M
EOF
sudo systemctl restart systemd-journald

# --- Reboot to apply gpu_mem ---
sudo reboot
```

After reboot, verify:

```bash
vcgencmd get_mem gpu          # should show gpu=16M
free -h                       # should show ~480 MB total + swap
```

### 2.3 Install the testbench

```bash
git clone https://github.com/SensorsIot/Embedded-AI-Harness.git
cd Embedded-AI-Harness/pi
sudo bash install.sh
```

The installer pulls in all dependencies (pyserial, hostapd, dnsmasq, bleak,
esptool, OpenOCD, rtl-sdr/rtl_433, mosquitto), copies the scripts to
`/usr/local/bin/`, installs the udev and dnsmasq callbacks, creates the data
directories, and starts the portal as a systemd service.

To update the scripts later without touching system packages or config:

```bash
sudo bash install.sh --update
```

### 2.4 Verify

```bash
curl http://localhost:8080/api/devices   # should list all slots
curl http://localhost:8080/api/info      # portal version and host info
```

Then open `http://$BENCH:8080` in a browser for the web portal — a live
dashboard of every slot (state, detected chip, debug status, USB devices), WiFi
state, activity log, test progress, and the operator-interaction modal.

### 2.5 Optional: pin USB slots

Slots are auto-detected, so skip this unless you need custom labels, fixed
TCP/GDB port numbers, GPIO pin definitions, an ESP-Prog probe, or you want to
exclude ports the hub chip reports but your board doesn't wire.

Plug your boards in and snapshot the topology:

```bash
sudo rfc2217-learn-slots
```

Then edit `/etc/rfc2217/testbench.json`:

```json
{
  "gpio_boot": 18,
  "gpio_en": 17,
  "slots": [
    {"label": "SLOT1", "usb_prefix": "0:1.1.2", "tcp_port": 4001, "gdb_port": 3333, "openocd_telnet_port": 4444},
    {"label": "SLOT2", "usb_prefix": "0:1.1.3", "tcp_port": 4002, "gdb_port": 3334, "openocd_telnet_port": 4445},
    {"label": "SLOT3", "usb_prefix": "0:1.2",   "tcp_port": 4003, "gdb_port": 3335, "openocd_telnet_port": 4446},
    {"label": "SLOT4", "usb_prefix": "0:1.3",   "tcp_port": 4004, "gdb_port": 3336, "openocd_telnet_port": 4447}
  ],
  "debug_probes": [
    {"label": "PROBE1", "type": "esp-prog", "interface_config": "interface/ftdi/esp_ftdi.cfg", "bus_port": "1-1.4:1.0"}
  ]
}
```

| Field | Description |
|-------|-------------|
| `gpio_boot` / `gpio_en` | Pi BCM GPIO wired to DUT BOOT (GPIO0/GPIO9) and EN/RST. Omit if not wired. |
| `slots[].label` | Slot name shown in the UI |
| `slots[].usb_prefix` | USB path prefix, e.g. `"0:1.1"`. Auto-detected if omitted. |
| `slots[].tcp_port` / `gdb_port` / `openocd_telnet_port` | Default to `4000+i` / `3332+i` / `4443+i` |
| `debug_probes[]` | ESP-Prog / FT2232H probe definitions. Omit if using USB JTAG only. |

Matching is prefix-based — see FR-002 in the FSD. Restart to apply:

```bash
sudo systemctl restart rfc2217-portal
```

The signal generator reads a separate `/etc/rfc2217/signalgen.json` (I²C bus,
PE4302 pins, Si5351 address) and the SDR reads `/etc/rfc2217/sdr.json`. Defaults
match the wiring documented in sections [11](#11-rf-signal-generator) and
[12](#12-sdr-receiver) — edit only if you wired things differently.

### 2.6 What gets installed where

| File | Installs to | Purpose |
|------|-------------|---------|
| `portal.py` | `/usr/local/bin/rfc2217-portal` | HTTP portal + proxy supervisor |
| `plain_rfc2217_server.py` | `/usr/local/bin/plain_rfc2217_server.py` | RFC2217 server (direct DTR/RTS) |
| `wifi_controller.py` | `/usr/local/bin/wifi_controller.py` | WiFi test instrument (AP/STA/scan/relay) |
| `ble_controller.py` | `/usr/local/bin/ble_controller.py` | BLE scan/connect/write proxy |
| `mqtt_controller.py` | `/usr/local/bin/mqtt_controller.py` | MQTT broker management |
| `debug_controller.py` | `/usr/local/bin/debug_controller.py` | OpenOCD lifecycle, probe allocation |
| `sniffer.py` | `/usr/local/bin/sniffer.py` | DNS + TLS SNI traffic capture |
| `rfc2217-learn-slots` | `/usr/local/bin/rfc2217-learn-slots` | USB hub slot discovery |

> **Deploying a code change to a running bench.** SSH is only for deploying code,
> never for operating the bench — everything operational goes through the API.
> ```bash
> scp pi/portal.py pi@$BENCH:/tmp/portal.py
> ssh pi@$BENCH 'sudo cp /tmp/portal.py /usr/local/bin/rfc2217-portal && sudo systemctl restart rfc2217-portal'
> ```

---

## 3. Network and ports

```
 LAN (192.168.0.x)
       |
       | eth0 (wired)
       v
  Raspberry Pi ---- wlan0 (WiFi test AP: 192.168.4.x)
  $BENCH      hci0  (Bluetooth LE)
       |             UDP :5555 (log receiver)
       | USB hub (internal on Pi 3/4/5, external on Zero)
       |
  +----+----+----+----+
  |    |    |    |
 :4001 :4002 :4003 :4004  <- auto-assigned (4001 + slot index)
 SLOT1 SLOT2 SLOT3 SLOT4  <- one per detected hub port
```

eth0 carries all management traffic (HTTP API, RFC2217 serial). wlan0 is
dedicated to WiFi testing. They never overlap.

| Port | Protocol | Direction | Purpose |
|------|----------|-----------|---------|
| 8080 | TCP/HTTP | Clients → Pi | Web portal, REST API, firmware downloads |
| 4001+ | TCP/RFC2217 | Clients → Pi | Serial connections (`4000 + slot index`) |
| 3333+ | TCP/GDB | Clients → Pi | GDB connections (`3332 + slot index`) |
| 4444+ | TCP/telnet | Clients → Pi | OpenOCD telnet (`4443 + slot index`) |
| 5555 | UDP | ESP32 → Pi | Debug log receiver |
| 1883 | TCP/MQTT | DUTs → Pi | Test broker (when started) |
| 192.168.4.x | — | WiFi devices → Pi | WiFi AP subnet (when the AP is active) |

### GPIO wiring (optional)

| Pi GPIO (BCM) | Function | DUT pin |
|---------------|----------|---------|
| 17 | Hardware reset (active LOW) | EN / RST |
| 18 | Boot mode select (active LOW) | GPIO0 (ESP32) / GPIO9 (C3) |

Without GPIO the testbench still provides serial and debug for every plugged-in
device; GPIO only adds automated download-mode entry and flap recovery.

---

## 4. Connecting to serial devices

### 4.1 How it works

Each physical USB hub connector is a **slot**, and each slot owns a fixed TCP
port — the same connector always gives you the same port, whatever device is in
it and whatever devnode Linux assigns. The Pi runs an RFC2217 server per slot.
On plug/unplug, udev notifies the portal and the proxy starts or stops
automatically.

RFC2217 is a Telnet extension that carries serial-port control over TCP.

**Why this rather than USB/IP:** no kernel modules, no VM configuration, works
through firewalls (it's just TCP), and it's natively supported by esptool,
pyserial, PlatformIO, and ESP-IDF.

**Limits:** serial only (no USB HID or JTAG over the same channel), **one client
at a time per device**, and slightly higher latency than local serial.

### 4.2 See what's connected

```bash
curl http://$BENCH:8080/api/devices | jq
```

```json
{
  "slots": [
    {
      "label": "SLOT1", "state": "idle", "running": true,
      "url": "rfc2217://$BENCH:4001",
      "detected_chip": "esp32s3", "debugging": true, "debug_gdb_port": 3333,
      "devnodes": ["/dev/ttyACM0", "/dev/ttyACM1"]
    },
    { "label": "SLOT2", "state": "absent", "running": false }
  ]
}
```

### 4.3 Client prerequisites

```bash
pip3 install pyserial      # required
pip3 install esptool       # for flashing
```

Your machine needs to reach the Pi on port 8080 and on the slot ports (4001+).

### 4.4 Python / pyserial

```python
import serial

ser = serial.serial_for_url("rfc2217://$BENCH:4001",
                            baudrate=115200, timeout=1)
while True:
    line = ser.readline()
    if line:
        print(line.decode("utf-8", errors="replace").strip())
```

### 4.5 PlatformIO

```ini
; platformio.ini
[env:esp32]
platform = espressif32
board = esp32dev
framework = arduino

upload_port  = rfc2217://$BENCH:4001?ign_set_control
monitor_port = rfc2217://$BENCH:4001?ign_set_control
monitor_speed = 115200
```

### 4.6 ESP-IDF

```bash
export ESPPORT='rfc2217://$BENCH:4001?ign_set_control'
idf.py flash monitor
```

### 4.7 A local `/dev/tty` with socat

If a tool insists on a local device path:

```bash
apt install -y socat
socat pty,link=/dev/ttyESP32,raw,echo=0 tcp:$BENCH:4001 &
cat /dev/ttyESP32
```

### 4.8 Reading serial through the API

You don't have to hold an RFC2217 connection open. The portal can read for you,
optionally returning as soon as a pattern appears:

```bash
# Reset the board and capture its boot output
curl -X POST http://$BENCH:8080/api/serial/reset \
  -H 'Content-Type: application/json' -d '{"slot":"SLOT1"}'

# Read up to 30 s, returning early on a match
curl -X POST http://$BENCH:8080/api/serial/monitor \
  -H 'Content-Type: application/json' \
  -d '{"slot":"SLOT1","pattern":"WiFi connected","timeout":30}'

# Passive read of the slot's buffer (doesn't disturb anything)
curl 'http://$BENCH:8080/api/serial/output?slot=SLOT1&lines=40'
```

### 4.9 `ign_set_control` — when you need it

Some clients toggle DTR/RTS on connect, which drops boards with a native USB
serial/JTAG interface (ESP32-C3/S3, `/dev/ttyACM*`) into download mode at the
worst moment. Appending `?ign_set_control` to the URL tells pyserial not to send
those control commands.

| Chip | USB interface | Device node | Reset | Caveat |
|------|---------------|-------------|-------|--------|
| ESP32, ESP32-S2 | UART bridge (CP2102, CH340) | `/dev/ttyUSB*` | DTR/RTS toggle | Reliable |
| ESP32-C3, ESP32-S3 | Native USB-Serial/JTAG | `/dev/ttyACM*` | DTR/RTS toggle | Linux asserts DTR+RTS on open → download mode during early boot; the Pi delays 2 s before opening to avoid this |

---

## 5. Flashing firmware

There are three ways to flash, and which one you need depends on the board.

### 5.1 Over RFC2217 (default)

Binaries stay on your machine — no SCP needed.

```bash
esptool --port rfc2217://$BENCH:4001 --chip esp32c3 \
  --before default-reset --after no-reset \
  write-flash 0x0 bootloader.bin 0x8000 partition-table.bin 0x10000 firmware.bin

# Reboot into the new firmware
curl -X POST http://$BENCH:8080/api/serial/reset \
  -H 'Content-Type: application/json' -d '{"slot":"SLOT1"}'
```

If you hit timeouts, add `--no-stub`.

### 5.2 Local-Pi esptool — for USB-serial bridge boards

A classic ESP32 behind a CP2102/CH340/CH9102 bridge has an auto-reset circuit
that can't be driven reliably through RFC2217 — you get `Wrong boot mode (0x13)`.
`POST /api/flash` runs esptool **on the Pi** instead, and manages the proxy
lifecycle for you. Pass each binary as a `bin@<offset>` file part:

```bash
curl -X POST http://$BENCH:8080/api/flash \
  -F slot=SLOT3 -F chip=esp32 -F baud=921600 \
  -F 'bin@0x1000=@bootloader.bin' \
  -F 'bin@0x8000=@partitions.bin' \
  -F 'bin@0xe000=@boot_app0.bin' \
  -F 'bin@0x10000=@firmware.bin'
```

Add `-F erase=1` to erase the whole flash first.

### 5.3 Over the air

For a board already **deployed on the LAN**, off the USB slots, that your client
can't reach directly — for example a NAT'd container that can't accept
ArduinoOTA's reverse connection — the testbench relays the push. (A host already
on the LAN can OTA the board directly and doesn't need this.)

```bash
curl -X POST http://$BENCH:8080/api/ota \
  -F target=192.168.0.176 -F firmware=@.pio/build/<env>/firmware.bin
```

### 5.4 After flashing

The testbench detects the USB re-enumeration and brings serial and debug back up
automatically. If a slot is left in `download_mode`, release it:

```bash
curl -X POST http://$BENCH:8080/api/serial/release \
  -H 'Content-Type: application/json' -d '{"slot":"SLOT1"}'
```

---

## 6. GDB / JTAG debugging

OpenOCD starts **automatically** on plug-in. The testbench detects the chip and
publishes the GDB port in `/api/devices`. Serial and JTAG coexist on the same USB
connection.

```bash
riscv32-esp-elf-gdb build/project.elf \
  -ex "target extended-remote $BENCH:3333" \
  -ex "monitor reset halt"
```

| Approach | Chips | Extra hardware |
|----------|-------|----------------|
| USB JTAG (auto) | C3, C6, H2, S3 (native USB) | None |
| Dual-USB | S3 (two USB ports) | None |
| ESP-Prog | All variants | ESP-Prog + cable |

Verified USB-JTAG TAP IDs: C3 `0x00005c25`, C6 `0x0000dc25`, H2 `0x00010c25`,
S3 `0x120034e5`. A classic ESP32 has no USB JTAG and needs an ESP-Prog probe
declared in `testbench.json` ([2.5](#25-optional-pin-usb-slots)).

```bash
curl http://$BENCH:8080/api/debug/status    # per-slot debug state
curl http://$BENCH:8080/api/debug/probes    # attached ESP-Prog probes
curl http://$BENCH:8080/api/debug/group     # dual-USB slot groups by role

curl -X POST http://$BENCH:8080/api/debug/start \
  -H 'Content-Type: application/json' -d '{"slot":"SLOT1"}'
```

---

## 7. WiFi test instrument

The Pi's onboard **wlan0** radio doubles as a programmable AP or station,
isolated from eth0 and driven entirely over HTTP.

### 7.1 Operating modes

| Mode | wlan0 | WiFi instrument |
|------|-------|-----------------|
| WiFi-Testing (default) | Test instrument | Active |
| Serial Interface | Joins your WiFi for LAN access | Disabled |

Switch via the web UI toggle or the API:

```bash
# wlan0 joins your network instead (bench loses the WiFi instrument)
curl -X POST http://$BENCH:8080/api/wifi/mode \
  -H 'Content-Type: application/json' \
  -d '{"mode":"serial-interface","ssid":"MyWiFi","pass":"password"}'

# Back to instrument mode
curl -X POST http://$BENCH:8080/api/wifi/mode \
  -H 'Content-Type: application/json' -d '{"mode":"wifi-testing"}'
```

### 7.2 SoftAP

```bash
curl -X POST http://$BENCH:8080/api/wifi/ap_start \
  -H 'Content-Type: application/json' \
  -d '{"ssid":"TestNetwork","password":"password123","channel":6}'

curl http://$BENCH:8080/api/wifi/ap_status
curl -X POST http://$BENCH:8080/api/wifi/ap_stop
```

The AP runs at `192.168.4.1/24` with DHCP range `.2`–`.20`, DNS included. Pass
`{"internet": true}` to NAT-bridge it to eth0 so DUTs reach the LAN and the
internet.

### 7.3 Station mode and captive-portal provisioning

```bash
curl http://$BENCH:8080/api/wifi/scan

curl -X POST http://$BENCH:8080/api/wifi/sta_join \
  -H 'Content-Type: application/json' -d '{"ssid":"HomeWiFi","pass":"secret"}'
curl -X POST http://$BENCH:8080/api/wifi/sta_leave
```

`POST /api/enter-portal` is the composite operation for provisioning a
WiFiManager DUT: the bench joins the DUT's setup AP, submits the credentials
form, then hosts the target network itself so the DUT has something to join.

```bash
# Trigger a DUT into portal mode (double-reset on its slot)
curl -X POST http://$BENCH:8080/api/enter-portal \
  -H 'Content-Type: application/json' -d '{"slot":"SLOT1","resets":2}'

# Provision it
curl -X POST http://$BENCH:8080/api/enter-portal \
  -H 'Content-Type: application/json' \
  -d '{"portal_ssid":"ESP32-Setup","ssid":"TestNetwork","password":"password123",
       "save_path":"/wifisave","field_ssid":"s","field_password":"p",
       "method":"POST","internet":true}'
```

### 7.4 HTTP relay

Reach a device on the test network from your machine — the request goes out
through the Pi's wlan0:

```bash
curl -X POST http://$BENCH:8080/api/wifi/http \
  -H 'Content-Type: application/json' \
  -d '{"method":"GET","url":"http://192.168.4.15/status"}'
```

### 7.5 Station events

```bash
# Long-poll for the next join/leave (seconds); 0 returns immediately
curl 'http://$BENCH:8080/api/wifi/events?timeout=5'
```

### 7.6 Networking notes

- The AP is always `192.168.4.1/24`, DHCP `.2`–`.20` (matches the ESP32 default).
- AP and STA are **mutually exclusive** — starting one stops the other.
- Station connect/disconnect events arrive via dnsmasq lease callbacks.
- The `body` field in HTTP relay requests and responses is **base64-encoded**.

---

## 8. GPIO control

Drive Pi GPIO pins to simulate button presses or force boot mode (hold a pin LOW
across a reset).

```bash
curl -X POST http://$BENCH:8080/api/gpio/set \
  -H 'Content-Type: application/json' -d '{"pin":18,"value":0}'   # hold LOW

curl -X POST http://$BENCH:8080/api/gpio/set \
  -H 'Content-Type: application/json' -d '{"pin":18,"value":"z"}' # release

curl http://$BENCH:8080/api/gpio/status
```

**Allowed pins (BCM):** 5, 6, 12, 13, 16–27.

> **Always release with `"z"` when done.** A pin left LOW prevents the DUT from
> booting, and the next person to use the bench will not know why.

Pins 5 and 6 are shared with the GPCLK signal-generator backend, and 6/12/13 with
the PE4302 attenuator — don't drive them by hand while the signal generator is
running ([11](#11-rf-signal-generator)).

---

## 9. BLE proxy

The Pi's onboard Bluetooth scans for, connects to, and writes raw bytes to BLE
peripherals — a BLE-to-HTTP bridge, one connection at a time.

```bash
curl -X POST http://$BENCH:8080/api/ble/scan \
  -H 'Content-Type: application/json' -d '{"timeout":5,"name_filter":"WB-Test"}'

curl -X POST http://$BENCH:8080/api/ble/connect \
  -H 'Content-Type: application/json' -d '{"address":"AA:BB:CC:DD:EE:FF"}'

curl -X POST http://$BENCH:8080/api/ble/write \
  -H 'Content-Type: application/json' \
  -d '{"characteristic":"6e400002-b5a3-f393-e0a9-e50e24dcca9e","data":"48656c6c6f"}'

curl -X POST http://$BENCH:8080/api/ble/disconnect
curl http://$BENCH:8080/api/ble/status
```

`data` is hex. If scans find nothing, Bluetooth is probably powered off:

```bash
sudo rfkill unblock bluetooth && sudo hciconfig hci0 up && sudo bluetoothctl power on
```

---

## 10. MQTT test broker

An on-demand mosquitto broker (open, port 1883) reachable at both `192.168.4.1`
and the Pi's LAN address, so DUTs on the WiFi AP can run pub/sub integration
tests without internet.

```bash
curl -X POST http://$BENCH:8080/api/mqtt/start
curl http://$BENCH:8080/api/mqtt/status
curl -X POST http://$BENCH:8080/api/mqtt/stop
```

---

## 11. RF signal generator

A unified RF source with programmable frequency, attenuation, and optional Morse
keying. It auto-selects between two backends:

- **Si5351** (I²C on GPIO 2/3) — 333 kHz–112.5 MHz, three channels (CLK0–CLK2),
  fractional synthesis. Preferred when detected.
- **GPCLK** (BCM hardware clock on GPIO 5/6) — 122 kHz–250 MHz, integer dividers.
  Always available, no extra hardware.

An optional **PE4302** step attenuator (0–31.5 dB) sits in the RF path. Both
backends share a Morse keyer, so any carrier can be CW-keyed for DF beacons or
sensitivity tests. Without a `morse` argument the carrier runs continuous.

**Wiring** — Si5351: SDA=GPIO2, SCL=GPIO3 · PE4302: LE=GPIO6, CLK=GPIO12,
DATA=GPIO13 · GPCLK: GPIO5 or GPIO6.

```bash
# Check which backend and attenuator are actually present first
curl http://$BENCH:8080/api/siggen/status

curl -X POST http://$BENCH:8080/api/siggen/start \
  -H 'Content-Type: application/json' -d '{"freq_hz":3500000,"backend":"si5351"}'

curl -X POST http://$BENCH:8080/api/siggen/freq \
  -H 'Content-Type: application/json' -d '{"freq_hz":3573000}'

curl -X POST http://$BENCH:8080/api/siggen/atten \
  -H 'Content-Type: application/json' -d '{"db":20}'

curl -X POST http://$BENCH:8080/api/siggen/stop
```

`GET /api/siggen/frequencies` lists the frequencies a backend can actually
synthesise in a range — useful with GPCLK, whose integer dividers can't hit
arbitrary targets.

---

## 12. SDR receiver

An RTL-SDR dongle behind `rtl_433` receives and decodes 433/315/868 MHz OOK/FSK
devices (remotes, weather sensors, TPMS) — the receive-side counterpart to the
signal generator.

Always check the dongle is detected first:

```bash
curl http://$BENCH:8080/api/sdr/status
```

A dongle plugged in after the Pi booted is picked up automatically — no restart
needed. Status re-probes for it while the SDR is idle, so `device` flips to
`true` within a few seconds of plugging in.

**Is the receiver actually working?** One test answers that end to end, with
nothing plugged in but the dongle:

```bash
pytest pytest/ -k wt1909 --wt-url http://$BENCH:8080
```

It transmits on 86.784 MHz (whose 5th harmonic lands on 433.92 MHz) and requires
the dongle to see a ≥15 dB lift, then a drop when the carrier stops. If that
fails, the dongle, antenna or receive path is broken — fix it before debugging
anything else about the SDR.

**One-shot captures**

```bash
# Decode window -> records + signal levels
curl -X POST http://$BENCH:8080/api/sdr/capture \
  -H 'Content-Type: application/json' -d '{"freq_hz":433920000,"duration_s":10}'

# Raw pulse timing + RSSI, independent of any decoder
curl -X POST http://$BENCH:8080/api/sdr/analyze \
  -H 'Content-Type: application/json' -d '{"freq_hz":433920000,"duration_s":12}'

# Narrowband power sweep -> peak_db, peak_freq_hz, mean_db.
# Pin `gain` whenever you will compare the number to anything; `notch_hz`
# drops the dongle's DC spike at the tuner centre.
curl -X POST http://$BENCH:8080/api/sdr/power \
  -H 'Content-Type: application/json' \
  -d '{"freq_hz":433920000,"span_hz":500000,"gain":20,"notch_hz":40000}'

# Phased guided receive: locate -> level -> decode -> classify
curl -X POST http://$BENCH:8080/api/sdr/acquire \
  -H 'Content-Type: application/json' -d '{"freq_hz":433920000}'
```

**Live console** — a persistent `rtl_433` streaming into a sequence-numbered ring
buffer you fast-poll:

```bash
curl -X POST http://$BENCH:8080/api/sdr/live/start \
  -H 'Content-Type: application/json' -d '{"freqs":[433920000],"mode":"decode"}'

curl 'http://$BENCH:8080/api/sdr/live?since=0'
curl http://$BENCH:8080/api/sdr/live/status
curl -X POST http://$BENCH:8080/api/sdr/live/stop
```

**AI Sherlock** — record a session of button presses, then reverse-engineer the
timing, preamble, and per-key field from the log:

```bash
curl -X POST http://$BENCH:8080/api/sdr/log/start
# ... press the remote's buttons ...
curl -X POST http://$BENCH:8080/api/sdr/log/stop
curl http://$BENCH:8080/api/sdr/log
```

Reverse-engineered remotes are named by flex decoders in
`/etc/rtl_433/rtl_433.conf`.

> **One dongle, one user.** One-shot captures and the live console are mutually
> exclusive — the API returns "SDR busy" rather than fighting over the device.
> `POST /api/sdr/stop` cancels a one-shot capture; stopping the live console is
> `POST /api/sdr/live/stop`.

**Levels are only comparable at a fixed gain.** On AGC the tuner rescales itself
from whatever it saw recently, so the same quiet band can read tens of dB apart
between calls and a strong carrier gets compressed rather than standing out. Pass
`gain` on `capture`, `analyze` and `power` for any measurement you intend to
compare or threshold.

If the dongle wedges, `POST /api/sdr/reset` USB-resets it.

**Signal too strong is a real failure mode.** A remote held close to the dongle
with AGC on overloads the front end and decodes as FSK or all-zeros. Fix it with
distance plus a fixed `gain` value rather than AGC.

---

## 13. UDP logging and the firmware repository

### 13.1 UDP log receiver

The portal listens on **UDP 5555** for ESP32 debug output. This is how you get
logs when the USB port is occupied — an S3 running as a USB HID keyboard, say.
The buffer holds the last 2000 lines and is filterable by source IP and time.

```bash
curl 'http://$BENCH:8080/api/udplog?source=192.168.4.15&limit=50'
curl 'http://$BENCH:8080/api/udplog?since=1730000000'   # poll incrementally
curl -X DELETE http://$BENCH:8080/api/udplog            # clear the buffer
```

### 13.2 OTA firmware repository

Uploaded images are served at
`http://$BENCH:8080/firmware/<project>/<file>.bin`, which works directly
as an ESP-IDF `esp_https_ota` URL.

```bash
# project and file are multipart fields; the stored name comes from the filename
curl -X POST http://$BENCH:8080/api/firmware/upload \
  -F project=demo -F file=@build/demo.bin

curl http://$BENCH:8080/api/firmware/list
curl -X DELETE http://$BENCH:8080/api/firmware/delete \
  -H 'Content-Type: application/json' -d '{"project":"demo","filename":"demo.bin"}'
```

A full round trip — upload, trigger, watch:

```bash
curl -X POST http://$BENCH:8080/api/firmware/upload -F project=demo -F file=@build/demo.bin
curl -X POST http://$BENCH:8080/api/wifi/http -H 'Content-Type: application/json' \
  -d '{"method":"POST","url":"http://192.168.4.15/ota"}'
curl 'http://$BENCH:8080/api/udplog?source=192.168.4.15'
```

---

## 14. Test automation

### 14.1 The pytest driver

`TestbenchDriver` wraps every API call. Install it and import:

```bash
pip install -e Embedded-AI-Harness/pytest
```

```python
from testbench_driver import TestbenchDriver

wt = TestbenchDriver("http://$BENCH:8080")
wt.open()

# Serial
wt.serial_reset("SLOT1")
wt.serial_monitor("SLOT1", pattern="WiFi connected", timeout=30)

# WiFi
wt.ap_start("TestAP", "password123", channel=6)
station = wt.wait_for_station(timeout=30)
print(f"Station joined: {station['mac']} at {station['ip']}")
resp = wt.http_get(f"http://{station['ip']}/status")
print(resp.status_code, resp.text)

for net in wt.scan()["networks"]:
    print(f"  {net['ssid']}  {net['rssi']} dBm  {net['auth']}")

# RF
wt.siggen_start(freq_hz=3_571_000, morse={"message": "VVV DE TEST", "wpm": 15, "repeat": True})
wt.siggen_stop()
wt.sdr_capture(freq_hz=433_920_000, duration_s=10)

# BLE, MQTT, test tracking
wt.ble_scan(name_filter="WB-Test")
wt.mqtt_start()
wt.test_start(spec="Firmware v2.1", phase="Integration", total=10)

wt.ap_stop()
wt.close()
```

`TestbenchDriver` is also a context manager (`with TestbenchDriver(url) as wt:`).

### 14.2 Running the bundled suite

```bash
cd pytest
pip install pytest

# Host tier — pure logic, no Pi, no hardware, under a second
pytest host/

# Tests that need no DUT
pytest testbench_test.py --wt-url http://$BENCH:8080

# Everything, including tests that need a device connected
pytest testbench_test.py --wt-url http://$BENCH:8080 --run-dut
```

`--wt-url` defaults to `$TESTBENCH_URL`, then `http://localhost:8080`. Tests
marked `requires_dut` are skipped unless you pass `--run-dut`.

The suite covers basic protocol, SoftAP management, station events, STA mode,
HTTP relay, WiFi scan, signal generator, SDR, RF path, MQTT broker, captive
portal, USB-JTAG debug, auto-debug, serial architecture, and end-to-end flows.

### 14.3 Test progress and operator interaction

Push session state to the web portal so an operator can watch a run:

```bash
curl -X POST http://$BENCH:8080/api/test/update \
  -H 'Content-Type: application/json' \
  -d '{"spec":"Firmware v2.1","phase":"Integration","total":10}'

curl -X POST http://$BENCH:8080/api/test/update \
  -H 'Content-Type: application/json' -d '{"current":"TC-003 flash and boot"}'

curl -X POST http://$BENCH:8080/api/test/update \
  -H 'Content-Type: application/json' -d '{"result":{"name":"TC-003","status":"pass"}}'

curl -X POST http://$BENCH:8080/api/test/update \
  -H 'Content-Type: application/json' -d '{"end":true}'
```

When a step needs a human — press a button, swap a cable, power-cycle something —
block on it. The call **stays open** until the operator clicks Done or Cancel on
the Pi's display, or the timeout expires, and returns `{"confirmed": true|false}`:

```bash
curl -X POST http://$BENCH:8080/api/human-interaction \
  -H 'Content-Type: application/json' \
  -d '{"message":"Press and hold the pairing button for 3 s","timeout":120}'
```

---

## 15. Driving the bench from Claude

### 15.1 Claude Code skills

The repo ships project skills under `.claude/skills/` that teach Claude Code how
to drive the bench. Install them on each dev machine:

```bash
git clone https://github.com/SensorsIot/Embedded-AI-Harness.git /tmp/uew
mkdir -p .claude/skills
cp -r /tmp/uew/.claude/skills/. .claude/skills/
rm -rf /tmp/uew
```

`.claude/skills/` is project-scoped; use `~/.claude/skills/` to install globally.
Claude Code loads skills at session start, so restart your session after copying.

| Skill | Covers |
|-------|--------|
| `esp-idf-handling` · `esp-pio-handling` | Build / flash / monitor lifecycle, local USB or testbench |
| `testbench-logging` | Serial monitor with pattern matching, UDP logs, crash analysis |
| `testbench-wifi` | AP/STA, scan, HTTP relay, captive-portal provisioning |
| `testbench-ble` · `testbench-mqtt` | BLE scan/connect/write · broker lifecycle and pub/sub |
| `testbench-debug` | GDB/JTAG: USB JTAG, dual-USB, ESP-Prog |
| `signal-generator` · `sdr-receiver` | RF transmit · RF receive, decode, reverse-engineering |
| `testbench-test-handling` | The test execution protocol, live progress, operator prompts, `TestbenchDriver` |
| `testbench-integration` | Add testbench support to an existing ESP32 project |
| `define` | Phase 0 — the FSD: requirements, verification contracts, state models, the three planes |
| `harness` | Phase 1 — one-time project setup: planes, plan, capabilities, CI, runner |
| `commission` | Phase 2 — prove the testbench; burn down the debugging agenda |
| `build` | Phase 3 — the loop driver: test design, the plan, audit, reconcile, change requests |

Most `testbench-*` skills assume the bench is at `$BENCH`, or the IP in
`SERIAL_PI` — override that in your shell or devcontainer if your network differs.

### 15.2 MCP server

`mcp/testbench_mcp.py` exposes the whole HTTP API as **70 MCP tools**, so an MCP
client can drive the bench conversationally. It's a thin stdio proxy: each tool
maps 1:1 to an endpoint from a single `SPECS` table. It is **pure Python standard
library — no `pip install`** — so it runs anywhere Python 3 does.

The server runs on the machine running the MCP client (your laptop), **not** on
the Pi. It just needs network reach to the bench, pointed by `TESTBENCH_URL`.

**Claude Desktop, one click.** Download
[`mcp/embedded-ai-harness-testbench.mcpb`](../mcp/embedded-ai-harness-testbench.mcpb),
then in Claude Desktop go to **Settings → Extensions** and drag the file onto the
window. When prompted, enter your testbench URL — e.g.
`http://$BENCH:8080`, or `http://$BENCH:8080` if mDNS resolves.
You need Python 3 on the machine (macOS has it; on Windows install from
python.org and tick **Add Python to PATH**).

To point at a different bench later, open the extension's settings and change the
URL — no reinstall. To update, install a newer `.mcpb` over the old one.

> The bundle is unsigned, so Desktop shows a "not verified" note on install.
> That's expected for a self-hosted extension; continue.

**Claude Code.**

```bash
claude mcp add testbench \
  --env TESTBENCH_URL=http://$BENCH:8080 \
  -- python3 /abs/path/to/Embedded-AI-Harness/mcp/testbench_mcp.py

claude mcp list      # look for: testbench … ✔ Connected
```

Use an **absolute** path. Tools surface as `mcp__testbench__<name>`.

**Claude Desktop, manual config.** **Settings → Developer → Edit Config** opens
`claude_desktop_config.json` (macOS `~/Library/Application Support/Claude/`,
Windows `%APPDATA%\Claude\`, Linux `~/.config/Claude/`). Merge in:

```json
{
  "mcpServers": {
    "testbench": {
      "command": "python3",
      "args": ["/abs/path/to/Embedded-AI-Harness/mcp/testbench_mcp.py"],
      "env": { "TESTBENCH_URL": "http://$BENCH:8080" }
    }
  }
}
```

Then **fully restart** Claude Desktop — quit, not just close the window.

**Rebuilding the bundle** after changing `testbench_mcp.py`:

```bash
cd mcp
npx @anthropic-ai/mcpb pack . embedded-ai-harness-testbench.mcpb
npx @anthropic-ai/mcpb validate manifest.json
```

**Tools by group** — discovery (`testbench_devices/info/log`), flashing (`flash`,
`ota`, `firmware_list/upload/delete`), serial (`serial_reset/monitor/output/
recover/release`), logs (`udplog_get/clear`), sdr (`sdr_status/capture/analyze/
power/acquire`, `sdr_live_*`, `sdr_log_*`, `sdr_reset/stop`), siggen
(`siggen_status/start/stop/freq/atten/frequencies`), wifi (`wifi_mode(_set)/scan/
ap_*/sta_*/http/ping/events`, `enter_portal`), mqtt, ble, gpio, debug
(`debug_status/probes/start/stop/group`), and test/operator tracking
(`test_progress/update`, `human_status/interaction/done/cancel`), plus
`proxy_start/stop`.

`flash`, `ota`, and `firmware_upload` read local files by the paths you pass, so
the client machine must be able to see them. The two udev callbacks
(`/api/hotplug`, `/api/wifi/lease_event`) are deliberately not exposed — they
fire on the Pi itself and aren't client-callable.

Full tool reference:
**[FSD Appendix D](Harness-FSD.md#appendix-d-http-api--mcp-reference)**.

---

## 16. Validating the bench

`test-firmware/` holds a generic ESP-IDF firmware that exercises the testbench
infrastructure without any project-specific logic. Use it to confirm the bench
still works after changing the portal or the skills.

### 16.1 Build and flash it

Requires ESP-IDF **6.x** (verified against v6.0.2 for `esp32`, `esp32s3`, and
`esp32c3`). Two things changed with ESP-IDF 6 and are already handled in the
project files: CMake 3.22 is the minimum, and cJSON is no longer bundled — it is
pulled from the component registry via `main/idf_component.yml`, so the first
build needs network access.

```bash
cd test-firmware
idf.py set-target esp32s3    # or esp32, esp32c3
idf.py build
```

The binary lands at `build/wb-test-firmware.bin`. The default build uses 4 MB
flash with `partitions-4mb.csv`; for larger flash see the `esp-idf-handling`
skill. Upload it for the OTA step and flash it:

```bash
curl -X POST http://$BENCH:8080/api/firmware/upload \
  -F project=test-firmware -F file=@build/wb-test-firmware.bin

esptool --port 'rfc2217://$BENCH:4001?ign_set_control' \
        --chip esp32s3 --baud 460800 write_flash @flash_args
```

Or just use the `esp-idf-handling` skill, which picks the testbench or local USB
automatically.

### 16.2 What the firmware exercises

| Module | What it exercises |
|--------|-------------------|
| `udp_log.c` | UDP log forwarding to `$BENCH:5555` |
| `wifi_prov.c` | SoftAP captive portal (`WB-Test-Setup`), STA mode with stored creds |
| `ble_nus.c` | BLE advertisement as `WB-Test`, NUS service |
| `ota_update.c` | HTTP OTA from the testbench firmware server |
| `http_server.c` | `/status`, `/ota`, `/wifi-reset` endpoints |
| `nvs_store.c` | WiFi credential persistence in NVS (`wb_test` namespace) |
| `mqtt_pub.c` | Publishes to the broker the portal was given, or to its own gateway |
| `serial_console.c` | A line-oriented console on the USB serial port — see below |
| Heartbeat task | Periodic log line confirming the firmware is alive |

**The serial console.** One line in, one line out, no echo and no prompt:
anything reading this port is a program, not a person.

| Command | Answers | Why it exists |
|---|---|---|
| `ping` | `OK pong` | the only proof that a byte written to a slot *reached* something |
| `status` | wifi / ap_mode / ap_ssid / ip / mac / mqtt / topic | the DUT's own account of itself |
| `scan` | one AP per line, then `OK scan end` | two radios reporting is a measurement; one is an assertion |
| `info` | project, version, IDF version, tx power | which image is actually running |
| `mark <text>` | `OK mark <text>` | an observable at a moment of the caller's choosing |
| `wifi <ssid> <pass>` | stores and reboots | provisioning that does not need the radio to work |
| `testap <ssid> <pass>` | raises a WPA2 AP of that name and reboots | the bench has one radio and cannot be the AP its own station tests join |
| `testap off` | reverts and reboots | — |
| `forget` | erases credentials, reboots into the portal | puts the DUT back in front of its captive portal |
| `reboot` | `OK reboot` | — |

`testap` refuses a passphrase between one and seven characters rather than
quietly raising an open AP under a name the caller believes is protected. It
is cleared by the next `wifi` command, so provisioning over serial returns
the board to being a station without a second step.

| Skill | Test steps | What confirms it works |
|-------|-----------|------------------------|
| `esp-idf-handling` | Erase flash, trigger flapping, recover, re-flash | Flapping detected, recovery runs, firmware boots after re-flash |
| `esp-idf-handling` (GPIO) | Toggle EN to reset the device (GPIO slots only) | Serial shows fresh boot output |
| `esp-idf-handling` (OTA) | Upload binary, trigger OTA via HTTP `/ota` | Serial shows `"OTA succeeded"`, device reboots with new firmware |
| `esp-pio-handling` | Build and upload the same firmware from PlatformIO | Upload completes over RFC2217; firmware boots |
| `testbench-logging` | Serial monitor; check UDP logs | Serial shows boot output; `GET /api/udplog` returns heartbeat lines |
| `testbench-wifi` | Run `enter-portal` with the device in AP mode | Serial shows `"STA got IP"`; device joins the testbench network |
| `testbench-ble` | Scan for `WB-Test`, connect, discover services | Scan finds it; NUS UUID appears in characteristics |
| `testbench-mqtt` | Start broker, verify DUT can reach `192.168.4.1:1883` | (Firmware doesn't use MQTT; test broker start/stop independently) |
| `testbench-debug` | Start a debug session on a JTAG-capable slot | `/api/debug/status` reports running; GDB attaches on 3333 |
| `testbench-test-handling` | Run the walkthrough below as a tracked session | Steps appear on the Pi display; all pass |

`signal-generator` and `sdr-receiver` aren't exercised by this firmware — they
drive the bench's own RF hardware, not the DUT. They do verify each other,
though: **WT-1909** points the generator at the dongle and needs no firmware at
all (see [§12](#12-sdr-receiver)).

### 16.3 Walkthrough

Run these in order on each slot under test; each step builds on the previous.
Steps marked **(GPIO only)** need a slot with `gpio_boot` and `gpio_en`
configured. Replace `<SLOT>`, `<PORT>`, and `<DEVNODE>` with values from
`GET /api/devices` (e.g. `SLOT1` / `4001` / `/dev/ttyACM3`).

**1. Flapping detection and recovery.** Erase the flash to trigger a boot loop
and verify the portal notices and recovers.

*GPIO slot* — enter download mode, erase, release:

```bash
curl -s -X POST http://$BENCH:8080/api/serial/recover \
     -H 'Content-Type: application/json' -d '{"slot":"<SLOT>"}'
# poll GET /api/devices until state == "download_mode"
esptool --port 'rfc2217://$BENCH:<PORT>?ign_set_control' --chip esp32s3 erase_flash
curl -s -X POST http://$BENCH:8080/api/serial/release \
     -H 'Content-Type: application/json' -d '{"slot":"<SLOT>"}'
```

*No-GPIO slot* — erase on the Pi (the portal must release the port first, or use
`--before=usb_reset`):

```bash
ssh pi@$BENCH "python3 -m esptool --port <DEVNODE> \
    --before=usb_reset --chip esp32s3 erase_flash"
```

The device now boot-loops. Within ~30 s the portal should report, for that slot:
`"flapping": true`, `"recovering": true`, and an activity-log line
`"flapping detected (N events in 30s)"`.

*GPIO recovery:* the portal unbinds USB, waits `FLAP_COOLDOWN_S` (10 s), holds
BOOT LOW, pulses EN, rebinds. After ~15 s expect `"state": "download_mode"`,
`"flapping": false`, `"recovering": false`.

*No-GPIO recovery:* the portal unbinds, waits, rebinds — but with erased flash the
flapping resumes. After `FLAP_MAX_RETRIES` (2): `"flapping": true`,
`"recovering": false`, `"recover_retries": 2`, and `"needs manual intervention"`
in the log. Once the device stabilises the flag clears itself — aged-out events
are pruned within `FLAP_WINDOW_S` (30 s), so `"flapping": false`,
`"state": "idle"` after waiting.

**2. Serial flashing and boot.** Flash the test firmware (this is also the
recovery step after flapping), then reset and check the boot banner:

```bash
curl -s -X POST http://$BENCH:8080/api/serial/reset \
     -H 'Content-Type: application/json' -d '{"slot":"<SLOT>","lines":80}'
```

Expect: `"=== Testbench Test Firmware v0.1.0 ==="`, `"NVS initialized"`,
`"UDP logging -> $BENCH:5555"`,
`"No WiFi credentials, starting AP provisioning"`,
`"AP mode: SSID='WB-Test-Setup'"`, `"BLE NUS initialized"`,
`"Init complete, running event-driven"`.

**3. WiFi provisioning.** With the device in AP mode, run `enter-portal` with
`portal_ssid: WB-Test-Setup` and the testbench AP's SSID/password. Serial should
show `"Credentials saved, rebooting"`, `"STA mode, connecting to '<ssid>'"`,
`"STA got IP"`.

**4. UDP logging.** `curl -s http://$BENCH:8080/api/udplog` should show
heartbeat lines: `"heartbeat N | wifi=1 ble=0"`.

**5. HTTP endpoints.** Through the relay, `GET http://<device-ip>/status` should
return JSON with `project`, `version`, and `wifi_connected: true`.

**6. BLE.** `POST /api/ble/scan` with `{"timeout":5}` should find `WB-Test`;
connecting should expose NUS UUID `6e400001-b5a3-f393-e0a9-e50e24dcca9e`.

**7. OTA.** With the binary uploaded, relay `POST http://<device-ip>/ota`, then
watch serial for `"OTA succeeded, rebooting..."` and the boot banner again.

**8. WiFi reset.** Relay `POST http://<device-ip>/wifi-reset`; serial should show
`"WiFi credentials erased"` then a reboot into AP mode.

**9. GPIO reset (GPIO only).** Toggle EN LOW then HIGH; serial shows fresh boot
output.

**9b. Measure the BOOT/EN wiring.** The portal fills in `gpio_boot=18` and
`gpio_en=17` as defaults, and no software can see a wire — so `has_gpio` only
means *pins are configured*. Ask the bench to find out:

```bash
curl -s -X POST http://$BENCH:8080/api/serial/gpio-test \
     -H 'Content-Type: application/json' -d '{"slot":"<SLOT>"}'
# {"ok":true,"en_wired":false,"boot_wired":false,"detail":"EN pulse produced no reset ..."}
```

It pulses EN and listens for the ROM boot banner, then — if EN answered —
holds BOOT low across a reset and probes with esptool. Fifteen seconds, and
the board is handed back running either way. The verdict is stored in
`/var/lib/rfc2217/gpio-wiring.json`, survives a restart, and appears as
`gpio_wired` in `/api/devices`. There is a per-slot button for it in the
portal UI.

Until a slot is measured, GPIO recovery is not used on it: the wire-free
unbind/rebind cycle runs instead, and the log says why.

**10. Manual recovery.** `POST /api/serial/recover` starts a fresh recovery cycle
even when the slot isn't flapping, resetting the retry counter. Expect
`{"ok": true, ...}`.

### 16.4 Extending the walkthrough

When you change a testbench skill, add a row to the matrix in
[16.2](#162-what-the-firmware-exercises) if it isn't covered, add a step here if
it needs a new sequence, then flash the test firmware and re-run the affected
steps.

---

## 17. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Device not detected | Bad USB cable / unpowered hub | Use a data-capable cable; check `lsusb` on the Pi |
| Connection refused on a slot port | Proxy not running or device unplugged | `curl http://$BENCH:8080/api/devices` |
| Connection refused on port 5000 | Wrong port | The portal is on **8080**, not 5000 |
| Timeout during flash | Network latency, or proxy not released | `esptool --no-stub`; or use `POST /api/flash`, which manages the proxy |
| `Wrong boot mode (0x13)` on a bridge board | RFC2217 can't drive the DTR/RTS auto-reset | Use `POST /api/flash` (local-Pi esptool) |
| Port busy | Another client is connected | RFC2217 allows one client — close the other |
| Wrong slot after replug | Not a fault | `slot_key` keeps the port stable; the devnode may change |
| USB flapping (rapid connect/disconnect) | Erased or corrupt flash, boot loop | Portal auto-recovers via GPIO; `POST /api/serial/recover` to force |
| Slot stuck in `download_mode` | Device left in the bootloader | Flash it, then `POST /api/serial/release` |
| ESP32-C3 stuck in download mode | DTR asserted on open | `POST /api/serial/reset` |
| GDB won't connect | OpenOCD not started (classic ESP32 has no USB JTAG) | Check `/api/devices` for `debugging:true`; declare an ESP-Prog in `testbench.json` |
| WiFi ping fails | Portal not running | `curl http://$BENCH:8080/api/wifi/ping` |
| wlan0 unavailable | Wrong WiFi mode | Switch to `wifi-testing` mode |
| AP won't start | hostapd missing | Re-run `install.sh`; check `ssh pi@$BENCH which hostapd` |
| BLE scan finds nothing | Bluetooth powered off | `sudo rfkill unblock bluetooth && sudo hciconfig hci0 up && sudo bluetoothctl power on` |
| SDR reads noise / all zeros | Near-field AGC overload, or a wedged dongle | Add distance and a fixed `gain`; `POST /api/sdr/reset` |
| Pi crashes or reboots randomly | Out of memory | Apply the [2.2](#22-first-boot--system-hardening) hardening; check `free -h` |
| `sudo` segfaults | SD card corruption from hard crashes | Reflash the card |
| Stale slot data | Device unplugged mid-session | Auto-cleans on unplug; else restart the portal |

Diagnostics on the Pi:

```bash
sudo systemctl status rfc2217-portal     # service state
sudo journalctl -u rfc2217-portal -f     # live logs
ls -la /dev/ttyUSB* /dev/ttyACM*         # what the kernel sees
ss -tlnp | grep -E '8080|400'            # listening ports
free -h                                  # memory (critical on Pi Zero 2 W)
iw dev                                   # wlan0 present?
sudo systemctl restart rfc2217-portal
```

From your machine:

```bash
ping $BENCH
curl http://$BENCH:8080/api/devices
curl http://$BENCH:8080/api/info
curl http://$BENCH:8080/api/wifi/ping
curl 'http://$BENCH:8080/api/log'      # portal activity log
```

---

## 18. Security

The API and RFC2217 have **no authentication** — anyone who can reach the ports
can drive the bench, flash firmware, and read serial output. Keep the testbench
on a trusted network.

For remote access, tunnel over SSH rather than exposing the ports:

```bash
ssh -L 4001:localhost:4001 -L 8080:localhost:8080 pi@$BENCH
curl http://localhost:8080/api/devices
```

---

## 19. Quick reference

```bash
# Discovery
curl http://$BENCH:8080/api/devices | jq
curl http://$BENCH:8080/api/info

# Serial
curl -X POST .../api/serial/reset   -d '{"slot":"SLOT1"}'
curl -X POST .../api/serial/monitor -d '{"slot":"SLOT1","pattern":"ready","timeout":30}'
curl -X POST .../api/serial/release -d '{"slot":"SLOT1"}'

# WiFi
curl -X POST .../api/wifi/ap_start -d '{"ssid":"TestAP","password":"secret"}'
curl .../api/wifi/ap_status
curl .../api/wifi/scan
curl -X POST .../api/wifi/ap_stop

# GPIO  (always release with "z")
curl -X POST .../api/gpio/set -d '{"pin":18,"value":0}'
curl -X POST .../api/gpio/set -d '{"pin":18,"value":"z"}'

# RF
curl -X POST .../api/siggen/start  -d '{"freq_hz":3500000,"backend":"si5351"}'
curl -X POST .../api/sdr/capture   -d '{"freq_hz":433920000,"duration_s":10}'

# BLE
curl -X POST .../api/ble/scan -d '{"timeout":5,"name_filter":"WB-Test"}'
```

```python
import serial
ser = serial.serial_for_url("rfc2217://$BENCH:4001", baudrate=115200)
```

```bash
esptool --port 'rfc2217://$BENCH:4001?ign_set_control' write_flash 0x0 fw.bin
```

**Web portal:** `http://$BENCH:8080`

**Full API and MCP tool reference:**
[FSD Appendix D](Harness-FSD.md#appendix-d-http-api--mcp-reference)
