#!/bin/bash
# Install RFC2217 Portal on Raspberry Pi
#
# Usage:
#   sudo bash install.sh              # full install (first time)
#   sudo bash install.sh --update     # update scripts only (no system changes)
#
# See docs/Harness-User-Manual.md section 2 for the full SD card rebuild procedure.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# The radio the portal owns. Must match WIFI_WLAN_IF in wifi_controller.py.
WLAN_IF="${WIFI_WLAN_IF:-wlan0}"
UPDATE_ONLY=false
if [ "$1" = "--update" ]; then
    UPDATE_ONLY=true
fi

echo "=== Installing RFC2217 Portal ==="

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
if [ "$UPDATE_ONLY" = false ]; then
    echo "Installing system packages..."
    apt-get update -qq
    apt-get install -y \
        python3-serial python3-pip python3-libgpiod \
        hostapd dnsmasq-base \
        mosquitto mosquitto-clients \
        curl iptables \
        bluetooth bluez \
        rtl-sdr rtl-433

    # Python packages not available via apt
    # esptool >= 5: the portal uses the hyphenated subcommands and flags.
    pip3 install 'esptool>=5' bleak smbus2 --break-system-packages 2>/dev/null || true

    # Enable I2C for Si5351 signal generator
    if command -v raspi-config >/dev/null 2>&1; then
        raspi-config nonint do_i2c 0 2>/dev/null || true
    fi

    # Debian 13 (trixie) soft-blocks bluetooth via rfkill on a fresh image,
    # which leaves hci0 DOWN and every /api/ble/* call failing. Unblock once —
    # systemd-rfkill persists the state across reboots.
    rfkill unblock bluetooth 2>/dev/null || true
    hciconfig hci0 up 2>/dev/null || true

    # OpenOCD for ESP32 (GDB debug support)
    if ! command -v openocd-esp32 >/dev/null 2>&1; then
        echo "Installing openocd-esp32..."
        ARCH=$(uname -m)
        case "$ARCH" in
            aarch64) OCD_ARCH="arm64" ;;
            armv7l|armv6l) OCD_ARCH="armhf" ;;
            x86_64) OCD_ARCH="amd64" ;;
            *) echo "WARNING: unsupported arch $ARCH for openocd-esp32, skipping"; OCD_ARCH="" ;;
        esac
        if [ -n "$OCD_ARCH" ]; then
            OCD_VER="v0.12.0-esp32-20260304"
            OCD_URL="https://github.com/espressif/openocd-esp32/releases/download/${OCD_VER}/openocd-esp32-linux-${OCD_ARCH}-0.12.0-esp32-20260304.tar.gz"
            wget -q "$OCD_URL" -O /tmp/openocd-esp32.tar.gz
            tar xzf /tmp/openocd-esp32.tar.gz -C /tmp/
            cp /tmp/openocd-esp32/bin/openocd /usr/local/bin/openocd-esp32
            mkdir -p /usr/local/share/openocd-esp32
            cp -r /tmp/openocd-esp32/share/openocd/scripts /usr/local/share/openocd-esp32/scripts
            rm -rf /tmp/openocd-esp32 /tmp/openocd-esp32.tar.gz
            echo "openocd-esp32 installed: $(openocd-esp32 --version 2>&1 | head -1)"
        fi
    else
        echo "openocd-esp32 already installed, skipping..."
    fi
fi

# ---------------------------------------------------------------------------
# 2. Disable services we manage dynamically
# ---------------------------------------------------------------------------
if [ "$UPDATE_ONLY" = false ]; then
    echo "Configuring managed services..."
    systemctl disable --now hostapd 2>/dev/null || true
    systemctl mask hostapd 2>/dev/null || true
    systemctl disable --now dnsmasq 2>/dev/null || true
    systemctl disable --now mosquitto 2>/dev/null || true
    # wpa_supplicant is D-Bus activated: the socket restarts it, so both units
    # go. It claims wlan0 and blocks hostapd; the portal starts its own instance
    # when it needs station mode.
    systemctl disable --now wpa_supplicant.socket 2>/dev/null || true
    systemctl disable --now wpa_supplicant.service 2>/dev/null || true

    # ...and disabling the units is not enough on its own. NetworkManager uses
    # wpa_supplicant as its WiFi backend and starts it again over D-Bus within
    # seconds, so the portal's own attempts to clear wlan0 lost that race every
    # time. The symptom is vicious: hostapd reaches AP-ENABLED, installs a
    # beacon the driver accepts — the SSID is legible in the beacon hexdump —
    # and radiates nothing at all. Nothing reports an error. `iw` says the
    # interface is an AP on the right channel, the portal says the AP is
    # active, and the only sign reaches anyone as the DUT's own NO_AP_FOUND,
    # which accuses the DUT.
    #
    # Marking the interface unmanaged is what actually settles it: NM then has
    # no reason to want a supplicant for it.
    echo "Telling NetworkManager to leave $WLAN_IF to the portal..."
    mkdir -p /etc/NetworkManager/conf.d
    cat > /etc/NetworkManager/conf.d/99-testbench.conf <<NMCONF
# Installed by the testbench. The portal owns this radio: it runs
# hostapd on it for the test AP and its own wpa_supplicant for station mode.
[keyfile]
unmanaged-devices=interface-name:$WLAN_IF
NMCONF
    nmcli general reload 2>/dev/null || systemctl reload NetworkManager 2>/dev/null || true
    pkill -f "wpa_supplicant" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 3. Create directories
# ---------------------------------------------------------------------------
echo "Creating directories..."
mkdir -p /etc/rfc2217
mkdir -p /var/lib/rfc2217/firmware
mkdir -p /tmp/wifi-tester

# ---------------------------------------------------------------------------
# 4. Install Python scripts
# ---------------------------------------------------------------------------
echo "Installing scripts..."
cp "$SCRIPT_DIR/portal.py"                  /usr/local/bin/rfc2217-portal
cp "$SCRIPT_DIR/plain_rfc2217_server.py"    /usr/local/bin/plain_rfc2217_server.py
cp "$SCRIPT_DIR/wifi_controller.py"         /usr/local/bin/wifi_controller.py
cp "$SCRIPT_DIR/ble_controller.py"          /usr/local/bin/ble_controller.py
cp "$SCRIPT_DIR/bcm_gpio.py"                /usr/local/bin/bcm_gpio.py
cp "$SCRIPT_DIR/gpclk.py"                   /usr/local/bin/gpclk.py
cp "$SCRIPT_DIR/morse.py"                   /usr/local/bin/morse.py
cp "$SCRIPT_DIR/si5351.py"                  /usr/local/bin/si5351.py
cp "$SCRIPT_DIR/pe4302.py"                  /usr/local/bin/pe4302.py
cp "$SCRIPT_DIR/signal_generator.py"        /usr/local/bin/signal_generator.py
cp "$SCRIPT_DIR/sdr_controller.py"          /usr/local/bin/sdr_controller.py
cp "$SCRIPT_DIR/debug_controller.py"       /usr/local/bin/debug_controller.py
cp "$SCRIPT_DIR/mqtt_controller.py"         /usr/local/bin/mqtt_controller.py
cp "$SCRIPT_DIR/sniffer.py"                 /usr/local/bin/sniffer.py
cp "$SCRIPT_DIR/rfc2217-learn-slots"        /usr/local/bin/rfc2217-learn-slots

chmod +x /usr/local/bin/rfc2217-portal
chmod +x /usr/local/bin/plain_rfc2217_server.py
chmod +x /usr/local/bin/rfc2217-learn-slots

# ---------------------------------------------------------------------------
# 5. Install helper scripts
# ---------------------------------------------------------------------------
echo "Installing helper scripts..."
cp "$SCRIPT_DIR/scripts/rfc2217-udev-notify.sh" /usr/local/bin/rfc2217-udev-notify.sh
chmod +x /usr/local/bin/rfc2217-udev-notify.sh

cp "$SCRIPT_DIR/scripts/wifi-lease-notify.sh" /usr/local/bin/wifi-lease-notify.sh
chmod +x /usr/local/bin/wifi-lease-notify.sh

# espota.py: ArduinoOTA push tool, used by POST /api/ota to update deployed
# (off-USB, on-LAN) boards over the network.
cp "$SCRIPT_DIR/scripts/espota.py" /usr/local/bin/espota.py
chmod +x /usr/local/bin/espota.py

# ---------------------------------------------------------------------------
# 6. Install config files (don't overwrite existing)
# ---------------------------------------------------------------------------
# No default testbench.json — the portal auto-detects Pi model and USB
# hub topology on startup. Users who want custom labels/pins can create
# /etc/rfc2217/testbench.json manually (see pi/config/examples/).
echo "Slot config: auto-detected at runtime from USB topology"

# The config used to be workbench.json. It is operator-written — slot labels,
# GPIO pins, an ESP-Prog declaration — so an upgrade that just stopped reading
# it would come up healthy with somebody's wiring silently forgotten.
if [ -f /etc/rfc2217/workbench.json ] && [ ! -f /etc/rfc2217/testbench.json ]; then
    echo "Renaming /etc/rfc2217/workbench.json -> testbench.json"
    mv /etc/rfc2217/workbench.json /etc/rfc2217/testbench.json
fi

# The bench's DNS belongs to eth0, its management link. wlan0 joins test
# networks whose DHCP offers name a microcontroller as nameserver, and dhcpcd
# runs as a daemon here — so a `--nohook` flag on a `dhcpcd -1 wlan0` control
# command is ignored, and only this per-interface stanza is obeyed. Without
# it, one station test replaces the bench's resolver with an ESP32 that
# answers nothing and then disappears.
if ! grep -q "^interface ${WLAN_IF}$" /etc/dhcpcd.conf 2>/dev/null; then
    echo "Telling dhcpcd not to take DNS from $WLAN_IF (test networks)..."
    cat >> /etc/dhcpcd.conf <<DHCPCDCONF

# Added by the testbench installer: $WLAN_IF joins test networks whose DHCP
# offers advertise the DUT as nameserver. The bench resolves via eth0.
interface ${WLAN_IF}
    nohook resolv.conf
DHCPCDCONF
    systemctl restart dhcpcd 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 6b. Hostname
# ---------------------------------------------------------------------------
# One bench, one name, derived from the hardware: testbench-XXXX, where XXXX
# is the last four hex digits of the MAC. A bench-and-a-number scheme collides
# the moment somebody reimages one; the MAC cannot.
#
# wlan0 first: it is on the SoC on every supported Pi, where eth0 on a Zero 2 W
# is a USB adapter that can be unplugged and replaced — which would rename the
# machine.
#
# The separator is `-`, not `_`, and this is not a preference. `_` is not a
# legal hostname character (RFC 1123: letters, digits, `-`), and systemd does
# not refuse it — it *silently strips it*, which is the worse failure. Asking
# for testbench_7e71 gets you a kernel hostname of testbench7e71 while
# /etc/hosts still says testbench_7e71, the two disagree, and every sudo then
# stalls on "unable to resolve host". Observed on this bench.
MAC_IF=wlan0
[ -r "/sys/class/net/$MAC_IF/address" ] || MAC_IF=eth0
MAC_SUFFIX="$(sed 's/://g' "/sys/class/net/$MAC_IF/address" 2>/dev/null | tail -c 5)"
CURRENT_HOST="$(hostname)"

# A bench can be named on purpose -- for its site, its owner, its rack slot --
# and the rename above runs on *every* install, not once. Without an opt-out a
# chosen name silently reverts the next time someone updates, which reads as
# the machine renaming itself. The manual already says the hostname is for
# humans and the address is what tools use, so let the human keep theirs.
#
# A marker file rather than a config key: this runs before the portal's config
# is read, and the decision has to be legible from a shell prompt on a bench
# that may not be answering HTTP yet.
if [ -f /etc/rfc2217/keep-hostname ]; then
    echo "Keeping host '$CURRENT_HOST' (/etc/rfc2217/keep-hostname present)"
elif [ -n "$MAC_SUFFIX" ]; then
    WANT_HOST="testbench-${MAC_SUFFIX}"
    if [ "$CURRENT_HOST" != "$WANT_HOST" ]; then
        echo "Renaming host '$CURRENT_HOST' -> '$WANT_HOST' (from $MAC_IF MAC)"
        hostnamectl set-hostname "$WANT_HOST" 2>/dev/null || \
            echo "$WANT_HOST" > /etc/hostname

        # Read back what the system actually accepted rather than assuming it
        # took what we asked for — see the note above about silent stripping.
        NEW_HOST="$(hostname)"
        [ -n "$NEW_HOST" ] || NEW_HOST="$WANT_HOST"
        if [ "$NEW_HOST" != "$WANT_HOST" ]; then
            echo "  NOTE: system stored '$NEW_HOST', not '$WANT_HOST'"
        fi

        # /etc/hosts must match the *stored* name, or sudo stalls on every call
        if grep -q "127.0.1.1" /etc/hosts; then
            sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NEW_HOST/" /etc/hosts
        else
            printf '127.0.1.1\t%s\n' "$NEW_HOST" >> /etc/hosts
        fi
        systemctl restart avahi-daemon 2>/dev/null || true
        echo "  Host is now '$NEW_HOST'."
        echo "  Reach the bench by IP — mDNS does not resolve from containers."
    fi
else
    echo "WARNING: no MAC readable on wlan0 or eth0; leaving hostname '$CURRENT_HOST'"
fi

if [ ! -f /etc/rfc2217/signalgen.json ]; then
    echo "Installing default signal generator config..."
    cp "$SCRIPT_DIR/config/signalgen.json" /etc/rfc2217/signalgen.json
else
    echo "Signal generator config already exists, skipping..."
fi

if [ ! -f /etc/rfc2217/sdr.json ]; then
    echo "Installing default SDR receiver config..."
    cp "$SCRIPT_DIR/config/sdr.json" /etc/rfc2217/sdr.json
else
    echo "SDR receiver config already exists, skipping..."
fi

# rtl_433 flex-decoder database (auto-loaded from /etc/rtl_433/rtl_433.conf):
# devices reverse-engineered on the testbench. Ships one worked example;
# add your own as you decode them.
echo "Installing rtl_433 device database..."
mkdir -p /etc/rtl_433
cp "$SCRIPT_DIR/config/rtl_433.conf" /etc/rtl_433/rtl_433.conf

# Mosquitto test broker config
if [ "$UPDATE_ONLY" = false ]; then
    echo "Installing MQTT broker config..."
    cp "$SCRIPT_DIR/config/mosquitto-test-broker.conf" /etc/mosquitto/conf.d/test-broker.conf
    # Create empty password file if it doesn't exist
    touch /etc/mosquitto/passwd
    chown mosquitto:mosquitto /etc/mosquitto/passwd
fi

# ---------------------------------------------------------------------------
# 7. Install systemd service and udev rules
# ---------------------------------------------------------------------------
echo "Installing systemd service and udev rules..."
cp "$SCRIPT_DIR/systemd/rfc2217-portal.service" /etc/systemd/system/
cp "$SCRIPT_DIR/udev/99-rfc2217-hotplug.rules" /etc/udev/rules.d/

# OpenOCD udev rules (Espressif USB JTAG + FTDI debug probes)
cat > /etc/udev/rules.d/60-openocd.rules << 'RULES'
# Espressif USB-Serial/JTAG (ESP32-C3, S3, C6, H2, etc.)
ATTRS{idVendor}=="303a", MODE="0666", GROUP="plugdev"
# FTDI devices (ESP-Prog, FT2232H, FT232H)
ATTRS{idVendor}=="0403", MODE="0666", GROUP="plugdev"
RULES

systemctl daemon-reload
udevadm control --reload-rules

# ---------------------------------------------------------------------------
# 8. Enable and start
# ---------------------------------------------------------------------------
echo "Enabling portal service..."
systemctl enable rfc2217-portal
systemctl restart rfc2217-portal

echo ""
echo "=== Installation complete ==="
echo ""
echo "Portal running at: http://$(hostname -I | awk '{print $1}'):8080"
echo ""
echo "Next steps:"
echo "  Slots auto-detect on boot. Plug in ESP32s and browse the portal."
echo "  Custom config (optional): sudo nano /etc/rfc2217/testbench.json"
