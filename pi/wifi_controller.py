"""
WiFi Controller — manages hostapd, dnsmasq, wpa_supplicant, iw scan, HTTP relay.

Used by the portal to provide WiFi test-instrument functionality via the Pi's
own wlan0 radio.  Mirrors the ESP32-C3 WiFi Tester command set.
"""

import base64
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from queue import Empty, Queue

import sniffer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WLAN_IF = os.environ.get("WIFI_WLAN_IF", "wlan0")
# Transmit power for the test AP, in mBm (500 = 5 dBm).
#
# Everything on this bench is centimetres from everything else, and full power
# at that range is not extra margin — it drives the receiver into compression
# and splashes over every neighbouring network for no benefit.
#
# Applied on a best-effort basis: this Pi's brcmfmac refuses nl80211 txpower
# control, so in practice only the DUT's end comes down (to 5 dBm, which the
# bench still hears at -36 dBm). Kept because a bench with a radio that does
# support it should use it.
AP_TXPOWER_MBM = int(os.environ.get("WIFI_AP_TXPOWER_MBM", "500"))

# A scan while another is in flight fails with "Device or resource busy",
# and so does one issued while the radio is still settling into AP mode.
# Both clear in seconds, so the bench absorbs them rather than making every
# caller know about them.
SCAN_ATTEMPTS = 5
SCAN_RETRY_S = 2.0
AP_IP = "192.168.4.1"
AP_NETMASK = "255.255.255.0"
AP_SUBNET = "192.168.4.0/24"
DHCP_RANGE_START = "192.168.4.2"
DHCP_RANGE_END = "192.168.4.20"
DHCP_LEASE_TIME = "1h"

WORK_DIR = "/tmp/wifi-tester"
HOSTAPD_CONF = os.path.join(WORK_DIR, "hostapd.conf")
DNSMASQ_CONF = os.path.join(WORK_DIR, "dnsmasq.conf")
DNSMASQ_LEASES = os.path.join(WORK_DIR, "dnsmasq.leases")
HOSTAPD_LOG = os.path.join(WORK_DIR, "hostapd.log")
DNSMASQ_LOG = os.path.join(WORK_DIR, "dnsmasq.log")
WPA_CONF = os.path.join(WORK_DIR, "wpa_supplicant.conf")
WPA_LOG = os.path.join(WORK_DIR, "wpa_supplicant.log")

VERSION = "1.0.0-pi"

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_ap_active = False
_ap_ssid = ""
_ap_password = ""
_ap_channel = 0
_ap_hostapd_proc = None
_ap_dnsmasq_proc = None

_sta_active = False
_sta_ssid = ""
_sta_wpa_proc = None
_saved_ap = None  # saved AP config to restore after sta_leave

_event_queue: Queue = Queue()
_stations: dict = {}  # mac -> {mac, ip}

_start_time = time.monotonic()

# Mode: "wifi-testing" (default) or "serial-interface"
_mode = "wifi-testing"
_mode_ssid = ""  # SSID when in serial-interface mode

_MODE_DISABLED_ERR = "WiFi testing disabled (Serial Interface mode)"


# ---------------------------------------------------------------------------
# Mode management
# ---------------------------------------------------------------------------

def get_mode():
    """Return current mode and connected SSID (if serial-interface)."""
    with _lock:
        result = {"mode": _mode}
        if _mode == "serial-interface":
            result["ssid"] = _mode_ssid
            # Try to read current IP
            try:
                out = _run(["ip", "-4", "addr", "show", WLAN_IF], check=False)
                m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
                if m:
                    result["ip"] = m.group(1)
            except Exception:
                pass
        return result


def set_mode(mode, ssid="", password=""):
    """Switch mode. Returns mode dict."""
    global _mode, _mode_ssid

    if mode not in ("wifi-testing", "serial-interface"):
        raise ValueError(f"Unknown mode: {mode}")

    with _lock:
        if mode == _mode:
            return {"mode": _mode}

        if mode == "serial-interface":
            if not ssid:
                raise ValueError("ssid required for serial-interface mode")
            # Stop any active AP/STA
            _stop_all_unlocked()
            _mode = "serial-interface"
            _mode_ssid = ssid

        elif mode == "wifi-testing":
            # Disconnect from WiFi, return to testing mode
            _stop_all_unlocked()
            _mode = "wifi-testing"
            _mode_ssid = ""

    # Connect wlan0 to WiFi outside the lock (for serial-interface mode)
    if mode == "serial-interface":
        try:
            sta_join(ssid, password, _internal=True)
        except Exception:
            # Revert on failure
            with _lock:
                _mode = "wifi-testing"
                _mode_ssid = ""
            raise

    return get_mode()


def _check_wifi_testing_mode():
    """Raise RuntimeError if not in wifi-testing mode."""
    if _mode != "wifi-testing":
        raise RuntimeError(_MODE_DISABLED_ERR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_work_dir():
    os.makedirs(WORK_DIR, exist_ok=True)


def _tail(path, limit=500):
    """Last `limit` characters of a log file, for an error message."""
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace")[-limit:]
    except OSError as exc:
        return f"(could not read {path}: {exc})"


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


def _run(cmd, timeout=10, check=True):
    """Run a command, return stdout."""
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=check,
    )
    return result.stdout


def _kill_existing(name):
    """Kill any existing process by name (best effort)."""
    try:
        subprocess.run(
            ["pkill", "-f", name],
            capture_output=True, timeout=5, check=False,
        )
        time.sleep(0.3)
    except Exception:
        pass


def _release_wlan():
    """Ensure wlan0 is not managed by NetworkManager or wpa_supplicant."""
    # Debian's own wpa_supplicant is D-Bus activated and runs as
    #   /usr/sbin/wpa_supplicant -u -s -O DIR=/run/wpa_supplicant GROUP=netdev
    # with no interface on its command line, so the pattern below never matched
    # it. It adopts wlan0 anyway and registers nl80211 frame matches, and
    # hostapd then fails with "nl80211: kernel reports: Match already
    # configured" — an error that names neither wpa_supplicant nor the cause.
    #
    # The socket must go too: stopping only the service lets D-Bus start it
    # again on the next request.
    for unit in ("wpa_supplicant.socket", "wpa_supplicant.service"):
        try:
            subprocess.run(["systemctl", "stop", unit],
                           capture_output=True, timeout=10, check=False)
        except Exception:
            pass

    # Any remaining instance started with an explicit interface — including the
    # one this module starts for station mode.
    try:
        subprocess.run(
            ["pkill", "-f", f"wpa_supplicant.*{WLAN_IF}"],
            capture_output=True, timeout=5, check=False,
        )
    except Exception:
        pass
    # Remove stale control interface socket (prevents "ctrl_iface exists" error)
    ctrl_path = f"/var/run/wpa_supplicant/{WLAN_IF}"
    try:
        os.remove(ctrl_path)
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("Could not remove %s", ctrl_path)
    # Bring interface down then up to reset state
    try:
        _run(["ip", "link", "set", WLAN_IF, "down"], check=False)
        time.sleep(0.2)
        _run(["ip", "link", "set", WLAN_IF, "up"], check=False)
    except Exception:
        pass


def _flush_addr(iface: str = None):
    """Remove all IP addresses from an interface (wlan0 unless told otherwise)."""
    try:
        _run(["ip", "addr", "flush", "dev", iface or WLAN_IF], check=False)
    except Exception:
        pass


# (table, chain, rule-args) for the wlan0→eth0 NAT bridge — shared by
# _enable_nat/_disable_nat so setup and teardown can't drift apart.
_NAT_RULES = [
    ("nat", "POSTROUTING", ["-s", AP_SUBNET, "-o", "eth0", "-j", "MASQUERADE"]),
    ("filter", "FORWARD", ["-i", WLAN_IF, "-o", "eth0", "-j", "ACCEPT"]),
    ("filter", "FORWARD", ["-i", "eth0", "-o", WLAN_IF, "-m", "state",
                           "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"]),
]


def _enable_nat():
    """Bridge the wlan0 AP to the LAN/internet via NAT masquerade on eth0.

    Lets AP clients (e.g. a provisioned DUT on 192.168.4.x) reach the Pi's
    own LAN (192.168.0.x) and the internet. Rules are added idempotently.
    """
    subprocess.run(["sysctl", "-w", "net.ipv4.ip_forward=1"],
                   capture_output=True, check=False)
    for table, chain, args in _NAT_RULES:
        base = ["iptables", "-t", table]
        exists = subprocess.run(base + ["-C", chain] + args,
                                capture_output=True, check=False).returncode == 0
        if not exists:
            subprocess.run(base + ["-A", chain] + args,
                           capture_output=True, check=False)


def _disable_nat():
    """Remove the NAT bridge rules (best effort, idempotent).

    Without this, one ap_start(internet=True) leaves the bridge in place
    forever — every later plain ap_start would still give the DUT a path to
    the LAN/internet, silently breaking the isolation an air-gapped test
    assumes it has.
    """
    for table, chain, args in _NAT_RULES:
        base = ["iptables", "-t", table]
        for _ in range(10):  # bounded: -D can fail while -C still matches
            if subprocess.run(base + ["-C", chain] + args,
                              capture_output=True, check=False).returncode != 0:
                break
            subprocess.run(base + ["-D", chain] + args,
                           capture_output=True, check=False)


def ap_start(ssid, password="", channel=6, dns_logging=False, internet=False):
    """Start SoftAP on wlan0. Returns dict with ip.

    If dns_logging=True, dnsmasq is configured with DNS forwarding
    (8.8.8.8/8.8.4.4) and query logging for the sniffer.
    If internet=True, DNS forwarding is enabled and wlan0 is NAT-bridged to
    eth0 so AP clients reach the LAN/internet.
    """
    global _ap_active, _ap_ssid, _ap_password, _ap_channel
    global _ap_hostapd_proc, _ap_dnsmasq_proc

    _check_wifi_testing_mode()
    with _lock:
        # Stop anything running first
        _stop_all_unlocked()

        _ensure_work_dir()

        # Write hostapd config
        hostapd_lines = [
            f"interface={WLAN_IF}",
            "driver=nl80211",
            f"ssid={ssid}",
            "hw_mode=g",
            f"channel={channel}",
            # 802.11n with WMM, and a regulatory domain. With `wmm_enabled=0`
            # and no `ieee80211n`, stations associated and then sat there:
            # hostapd logged "associated" followed by "disassociated" without
            # ever sending 1/4 of the 4-way handshake, and the DUT reported
            # reason 15, 4WAY_HANDSHAKE_TIMEOUT. QoS is not optional for an
            # 11n-capable station negotiating EAPOL on this driver.
            "ieee80211n=1",
            "wmm_enabled=1",
            "country_code=CH",
            "ieee80211d=1",
            # Notice a station that vanished, in seconds rather than in five
            # minutes. A DUT that reboots or is reflashed does not send a
            # deauth frame — it simply stops being there — and hostapd's
            # default `ap_max_inactivity` is 300 s, so the bench went on
            # listing a station that had been gone for minutes and no
            # disconnect event was raised. hostapd still polls the station
            # before giving up on it, so one that is merely idle survives.
            "ap_max_inactivity=20",
            "disassoc_low_ack=1",
            "macaddr_acl=0",
            "auth_algs=1",
            "ignore_broadcast_ssid=0",
        ]
        if password:
            hostapd_lines += [
                "wpa=2",
                "wpa_key_mgmt=WPA-PSK",
                f"wpa_passphrase={password}",
                "rsn_pairwise=CCMP",
            ]

        with open(HOSTAPD_CONF, "w") as f:
            f.write("\n".join(hostapd_lines) + "\n")

        # Write dnsmasq config
        lease_script = "/usr/local/bin/wifi-lease-notify.sh"
        dns_log = os.path.join(WORK_DIR, "dns.log")
        dnsmasq_lines = [
            f"interface={WLAN_IF}",
            "bind-interfaces",
            f"dhcp-range={DHCP_RANGE_START},{DHCP_RANGE_END},{AP_NETMASK},{DHCP_LEASE_TIME}",
            f"dhcp-leasefile={DNSMASQ_LEASES}",
            "no-resolv",
            "no-daemon",
            "log-dhcp",
        ]
        if dns_logging or internet:
            dnsmasq_lines += ["server=8.8.8.8", "server=8.8.4.4"]
        if dns_logging:
            dnsmasq_lines += ["log-queries", f"log-facility={dns_log}"]
        if os.path.exists(lease_script):
            dnsmasq_lines.append(f"dhcp-script={lease_script}")

        with open(DNSMASQ_CONF, "w") as f:
            f.write("\n".join(dnsmasq_lines) + "\n")

        # Release wlan, then raise the AP on its own interface
        _release_wlan()
        _flush_addr()

        # Start hostapd, logging to a file rather than a pipe nobody drains.
        #
        # It used to inherit stdout=PIPE with no reader. A pipe holds about
        # 64 KB and then blocks the writer, and hostapd logs a line per
        # association, deauthentication and handshake — so a bench that has
        # been raising APs and cycling stations all day eventually wedges its
        # AP mid-operation. The kernel keeps beaconing, so the AP still looks
        # up and `ap_status` still says active, while hostapd's EAPOL state
        # machine has stopped: stations associate, get no 1/4 message, and
        # are dropped a second later with reason 4 or 15. Started by hand
        # with a terminal to write to, the identical config worked.
        with open(HOSTAPD_LOG, "wb") as log:
            _ap_hostapd_proc = subprocess.Popen(
                ["hostapd", HOSTAPD_CONF],
                stdout=log, stderr=subprocess.STDOUT,
            )
        # Wait for hostapd to initialise
        time.sleep(1.5)
        if _ap_hostapd_proc.poll() is not None:
            raise RuntimeError(f"hostapd failed to start: {_tail(HOSTAPD_LOG)}")

        # brcmfmac re-enables WiFi power save whenever the interface cycles
        # (logged as "power save enabled"), and a power-saving AP sleeps
        # between beacons: stations associate, lose the AP, and report
        # NO_AP_FOUND for minutes. Force it off every AP start. iw lives in
        # /usr/sbin, which is not on PATH for the service.
        _run(["/usr/sbin/iw", "dev", WLAN_IF, "set", "power_save", "off"],
             check=False)
        # Best effort, and said out loud when it fails. This Pi's brcmfmac
        # refuses nl80211 txpower control outright — "Input/example error
        # (-5)" on every interface — and `iw dev … info` then keeps
        # reporting a fixed 31 dBm that is not a measurement of anything.
        # Swallowing that would leave the bench believing it had turned its
        # radio down when it had not.
        try:
            subprocess.run(["/usr/sbin/iw", "dev", WLAN_IF, "set", "txpower",
                            "fixed", str(AP_TXPOWER_MBM)],
                           capture_output=True, text=True, timeout=10,
                           check=True)
            logger.info("AP tx power set to %d mBm", AP_TXPOWER_MBM)
        except Exception as exc:
            logger.info("AP tx power not settable on this radio (%s); the "
                        "DUT's own power is turned down instead", exc)

        # Address the interface only now. hostapd resets the netdev when it
        # claims it, which silently discards an address configured before —
        # and the first thing to notice is dnsmasq refusing to bind, which
        # reads as a missing interface rather than a present one with no
        # address.
        _flush_addr(WLAN_IF)
        _run(["ip", "addr", "add", f"{AP_IP}/24", "dev", WLAN_IF], check=False)
        _run(["ip", "link", "set", WLAN_IF, "up"], check=False)

        # Start dnsmasq — same reasoning as hostapd above. `log-dhcp` makes it
        # chatty, and a blocked dnsmasq stops handing out leases while the AP
        # still looks healthy.
        with open(DNSMASQ_LOG, "wb") as log:
            _ap_dnsmasq_proc = subprocess.Popen(
                ["dnsmasq", "-C", DNSMASQ_CONF],
                stdout=log, stderr=subprocess.STDOUT,
            )
        time.sleep(0.5)
        if _ap_dnsmasq_proc.poll() is not None:
            _kill_proc(_ap_hostapd_proc)
            raise RuntimeError(f"dnsmasq failed to start: {_tail(DNSMASQ_LOG)}")

        if internet:
            _enable_nat()
        else:
            _disable_nat()  # a previous internet=True AP must not leak isolation

        _ap_active = True
        _start_hostapd_watcher()
        _ap_ssid = ssid
        _ap_password = password
        _ap_channel = channel
        _stations.clear()

        logger.info("AP started: ssid=%s channel=%d ip=%s internet=%s",
                    ssid, channel, AP_IP, internet)
        return {"ip": AP_IP}


def ap_stop():
    """Stop the SoftAP."""
    with _lock:
        _ap_stop_unlocked()


def _ap_stop_unlocked():
    global _ap_active, _ap_ssid, _ap_password, _ap_channel
    global _ap_hostapd_proc, _ap_dnsmasq_proc

    _kill_proc(_ap_dnsmasq_proc)
    _ap_dnsmasq_proc = None
    _kill_proc(_ap_hostapd_proc)
    _ap_hostapd_proc = None
    _disable_nat()

    _ap_active = False
    _ap_ssid = ""
    _ap_password = ""
    _ap_channel = 0
    _stations.clear()

    _flush_addr()
    logger.info("AP stopped")


def ap_status():
    """Return AP status dict."""
    with _lock:
        return {
            "active": _ap_active,
            "ssid": _ap_ssid if _ap_active else "",
            "channel": _ap_channel if _ap_active else 0,
            "stations": list(_stations.values()) if _ap_active else [],
        }


# ---------------------------------------------------------------------------
# Station tracking (called by lease notify script via portal)
# ---------------------------------------------------------------------------

_AP_STA_RE = re.compile(r"AP-STA-(CONNECTED|DISCONNECTED)\s+"
                        r"([0-9a-fA-F:]{17})")


def _start_hostapd_watcher():
    """Raise station events from hostapd, not from DHCP lease expiry.

    Association and disassociation are 802.11 events and hostapd knows them
    the moment they happen. The only source of station events used to be
    dnsmasq, which reports `del` when a *lease* ends — and the lease is an
    hour long. So a DUT that left the air produced no STA_DISCONNECT for an
    hour, and a test that asked for one within a minute timed out against a
    bench that had simply not noticed yet.

    Watching the log is possible only because hostapd now writes to a file;
    it used to write into a pipe nobody read.
    """
    def run():
        pos = 0
        while _ap_active:
            try:
                with open(HOSTAPD_LOG) as f:
                    f.seek(pos)
                    for line in f:
                        m = _AP_STA_RE.search(line)
                        if not m:
                            continue
                        kind, mac = m.group(1), m.group(2).lower()
                        if kind == "DISCONNECTED":
                            _stations.pop(mac, None)
                            _event_queue.put({"type": "STA_DISCONNECT",
                                              "mac": mac})
                            logger.info("Station left the AP: mac=%s", mac)
                    pos = f.tell()
            except OSError:
                pass
            time.sleep(0.5)

    threading.Thread(target=run, name="hostapd-watch", daemon=True).start()


def handle_lease_event(action, mac, ip, hostname=""):
    """Called when dnsmasq sends a lease event."""
    mac = mac.lower()
    if action in ("add", "old"):
        _stations[mac] = {"mac": mac, "ip": ip}
        evt = {"type": "STA_CONNECT", "mac": mac, "ip": ip}
        if hostname:
            evt["hostname"] = hostname
        _event_queue.put(evt)
        logger.info("Station connected: mac=%s ip=%s", mac, ip)
    elif action == "del":
        _stations.pop(mac, None)
        _event_queue.put({"type": "STA_DISCONNECT", "mac": mac})
        logger.info("Station disconnected: mac=%s", mac)


# ---------------------------------------------------------------------------
# STA Mode
# ---------------------------------------------------------------------------

def sta_join(ssid, password="", timeout=15, _internal=False):
    """Join a WiFi network as a station. Returns dict with ip, gateway."""
    global _sta_active, _sta_ssid, _sta_wpa_proc, _saved_ap

    if not _internal:
        _check_wifi_testing_mode()
    with _lock:
        # Save AP config so sta_leave can restore it
        if _ap_active:
            _saved_ap = {"ssid": _ap_ssid, "password": _ap_password, "channel": _ap_channel}
            logger.info("Saved AP config for restore: ssid=%s channel=%d", _ap_ssid, _ap_channel)
        else:
            _saved_ap = None
        _stop_all_unlocked()
        _ensure_work_dir()

        _release_wlan()
        _flush_addr()
        _run(["ip", "link", "set", WLAN_IF, "up"], check=False)

        # Write wpa_supplicant config
        if password:
            # Use wpa_passphrase for proper encoding
            try:
                out = _run(["wpa_passphrase", ssid, password])
                # wpa_passphrase output lacks ctrl_interface — prepend it
                wpa_conf_content = 'ctrl_interface=/var/run/wpa_supplicant\n' + out
            except Exception:
                # Fallback to plain text config
                wpa_conf_content = (
                    'ctrl_interface=/var/run/wpa_supplicant\n'
                    'network={\n'
                    f'    ssid="{ssid}"\n'
                    f'    psk="{password}"\n'
                    '}\n'
                )
        else:
            wpa_conf_content = (
                'ctrl_interface=/var/run/wpa_supplicant\n'
                'network={\n'
                f'    ssid="{ssid}"\n'
                '    key_mgmt=NONE\n'
                '}\n'
            )

        with open(WPA_CONF, "w") as f:
            f.write(wpa_conf_content)

        # Start wpa_supplicant
        _sta_wpa_proc = subprocess.Popen(
            [
                "wpa_supplicant",
                "-i", WLAN_IF,
                "-c", WPA_CONF,
                "-B",  # background
                "-f", WPA_LOG,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        _sta_wpa_proc.wait(timeout=5)

        # Wait for connection with polling
        deadline = time.monotonic() + timeout
        connected = False
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(
                    ["wpa_cli", "-i", WLAN_IF, "status"],
                    capture_output=True, text=True, timeout=3, check=False,
                )
                if "wpa_state=COMPLETED" in result.stdout:
                    connected = True
                    break
            except Exception:
                pass
            time.sleep(0.5)

        if not connected:
            _sta_stop_unlocked()
            raise RuntimeError(f"Failed to connect to '{ssid}' within {timeout}s")

        # Get IP via DHCP — try dhcpcd (Debian/Bookworm), dhclient, udhcpc
        # dhcpcd on Bookworm runs as a system daemon; -1 sends a control
        # command that returns immediately while the daemon does DHCP in the
        # background (including ARP probing which takes ~3s).
        #
        # The networks this joins are test networks — a DUT's captive portal,
        # the test partner's own AP — and their DHCP offers name the DUT
        # itself as nameserver. Left alone, dhcpcd believes them and
        # overwrites /etc/resolv.conf with `nameserver 192.168.4.1`: an ESP32
        # that answers no DNS and vanishes when the test ends. The bench then
        # fails every name lookup — apt, git, gh — on a network that is
        # perfectly healthy, which reads as an outage rather than as leftover
        # state from a test twenty minutes ago.
        #
        # The switch below does NOT prevent that on its own, and believing it
        # did cost a run: dhcpcd here runs as a system daemon, so `-1` is a
        # *control command* to that daemon, which does the work with its own
        # configured hooks and ignores this process's flags. What actually
        # holds is the per-interface `nohook resolv.conf` that install.sh
        # writes into /etc/dhcpcd.conf. The flag stays for the case where no
        # daemon is running and this invocation does the DHCP itself.
        try:
            _run(["/usr/sbin/dhcpcd", "-1", "-4", "--nohook", "resolv.conf", WLAN_IF],
                 timeout=timeout, check=False)
        except Exception:
            try:
                # dhclient has no per-hook switch; -e suppresses the script's
                # resolv.conf handling on Debian's dhclient-script.
                _run(["dhclient", "-1", "-v", "-e", "PEERDNS=no", WLAN_IF],
                     timeout=timeout, check=False)
            except Exception:
                try:
                    # busybox udhcpc: -n exit if lease fails, no default script
                    # side effects when we hand it no -s handler.
                    _run(["udhcpc", "-i", WLAN_IF, "-n", "-q"], timeout=timeout, check=False)
                except Exception:
                    pass

        # Poll for IPv4 address (dhcpcd ARP probing takes ~3s)
        ip_addr = ""
        gateway = ""
        deadline = time.monotonic() + min(timeout, 15)
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                out = _run(["ip", "-4", "addr", "show", WLAN_IF])
                m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
                if m:
                    ip_addr = m.group(1)
                    break
            except Exception:
                pass

        if ip_addr:
            try:
                out = _run(["ip", "route", "show", "dev", WLAN_IF])
                m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
                if m:
                    gateway = m.group(1)
            except Exception:
                pass

        if not ip_addr:
            _sta_stop_unlocked()
            raise RuntimeError(f"Connected to '{ssid}' but no IP obtained")

        _sta_active = True
        _sta_ssid = ssid
        logger.info("STA joined: ssid=%s ip=%s gw=%s", ssid, ip_addr, gateway)
        return {"ip": ip_addr, "gateway": gateway}


def sta_leave():
    """Disconnect from a WiFi network. Restores AP if one was active before sta_join."""
    global _saved_ap
    with _lock:
        _sta_stop_unlocked()
        saved = _saved_ap
        _saved_ap = None
    # Restore AP outside lock (ap_start acquires lock)
    if saved:
        logger.info("Restoring AP after sta_leave: ssid=%s channel=%d", saved["ssid"], saved["channel"])
        ap_start(saved["ssid"], password=saved["password"], channel=saved["channel"])


def _sta_stop_unlocked():
    global _sta_active, _sta_ssid, _sta_wpa_proc

    if _sta_wpa_proc is not None:
        _kill_proc(_sta_wpa_proc)
        _sta_wpa_proc = None

    # Kill any wpa_supplicant on our interface
    try:
        subprocess.run(
            ["pkill", "-f", f"wpa_supplicant.*{WLAN_IF}"],
            capture_output=True, timeout=5, check=False,
        )
    except Exception:
        pass

    # Remove stale control interface socket
    ctrl_path = f"/var/run/wpa_supplicant/{WLAN_IF}"
    try:
        os.remove(ctrl_path)
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # Release DHCP — try dhcpcd (Debian/Bookworm), dhclient
    try:
        subprocess.run(
            ["/usr/sbin/dhcpcd", "--release", WLAN_IF],
            capture_output=True, timeout=5, check=False,
        )
    except Exception:
        try:
            subprocess.run(
                ["dhclient", "-r", WLAN_IF],
                capture_output=True, timeout=5, check=False,
            )
        except Exception:
            pass

    _flush_addr()
    _sta_active = False
    _sta_ssid = ""
    logger.info("STA disconnected")


# ---------------------------------------------------------------------------
# Combined stop
# ---------------------------------------------------------------------------

def _stop_all_unlocked():
    """Stop both AP and STA (caller holds _lock)."""
    _ap_stop_unlocked()
    _sta_stop_unlocked()


def shutdown():
    """Clean shutdown — stop everything."""
    global _mode, _mode_ssid
    with _lock:
        _stop_all_unlocked()
        _mode = "wifi-testing"
        _mode_ssid = ""
    logger.info("WiFi controller shut down")


# ---------------------------------------------------------------------------
# WiFi Scan
# ---------------------------------------------------------------------------

def scan():
    """Scan for WiFi networks using iw.

    Returns ``{"networks": [...]}`` on success, or ``{"error": ...}`` when
    the radio could not be asked.

    **An empty list means the air was empty; it never means the scan
    failed.** The two used to be indistinguishable: `iw` was run with
    `check=False`, so "Device or resource busy" — which is what a second
    scan gets while the first is still running — produced no stdout, parsed
    to zero networks, and was returned as a successful observation. A test
    then read that as a shielded room and skipped itself. A caller cannot
    recover from a fault it is told is a measurement.
    """
    _check_wifi_testing_mode()

    # One radio cannot beacon and survey at the same time. With the AP up,
    # `iw … scan` on a beaconing interface returns "Device or resource
    # busy" — so this is refused with its reason
    # rather than answered with an empty list. The test that covers this
    # used to pass, on a bench whose AP was silently not radiating: the
    # radio was idle, so the scan worked and the capability looked real.
    if _ap_active:
        return {"error": "cannot scan while the AP is running: this radio "
                         "cannot beacon and survey at once. Stop the AP "
                         "first (POST /api/wifi/ap_stop)."}

    iface = WLAN_IF
    try:
        _run(["ip", "link", "set", iface, "up"], check=False)
    except Exception:
        pass
    # After ap_stop the primary interface has only just come back; give it a
    # moment rather than reporting the first scan's failure as an empty air.
    for _ in range(20):
        try:
            with open(f"/sys/class/net/{iface}/operstate") as f:
                if f.read().strip() != "down":
                    break
        except OSError:
            pass
        time.sleep(0.1)

    # A busy radio is transient — the kernel is finishing someone else's
    # scan — so retry before declaring the instrument unavailable.
    #
    # A scan that succeeds and returns nothing is retried too. Immediately
    # after the AP comes down the interface is up but the radio has not
    # finished changing roles, and `iw` answers with an empty list rather
    # than an error: a successful measurement of an empty sky, which on a
    # bench with a dozen access points in range it is not. Retrying costs
    # ten seconds in a genuinely quiet room and reports the empty list
    # honestly if every attempt agrees.
    last = ""
    out = ""
    for attempt in range(SCAN_ATTEMPTS):
        try:
            result = subprocess.run(
                ["/usr/sbin/iw", "dev", iface, "scan", "-u"],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            last = "iw scan timed out after 15 s"
        else:
            if result.returncode == 0:
                out = result.stdout
                if "BSS " in out:
                    break
                last = "scan returned no BSS entries"
            else:
                last = (result.stderr or result.stdout or "").strip() \
                    or f"iw scan exited {result.returncode}"
        if attempt < SCAN_ATTEMPTS - 1:
            time.sleep(SCAN_RETRY_S)
    else:
        if not out:
            return {"error": f"scan failed on {iface}: {last}"}

    networks = []
    current = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("BSS "):
            if current.get("ssid"):
                networks.append(current)
            current = {"ssid": "", "rssi": 0, "auth": "OPEN", "channel": 0}
        elif line.startswith("SSID:"):
            ssid = line[5:].strip()
            current["ssid"] = ssid
        elif line.startswith("signal:"):
            # signal: -45.00 dBm
            m = re.search(r"(-?\d+\.?\d*)", line)
            if m:
                current["rssi"] = int(float(m.group(1)))
        elif line.startswith("DS Parameter set: channel"):
            m = re.search(r"channel\s+(\d+)", line)
            if m:
                current["channel"] = int(m.group(1))
        elif line.startswith("freq:") and not current.get("channel"):
            # Not every AP emits a DS Parameter set element; the frequency
            # is always there, and 2.4 GHz channels are a fixed grid.
            m = re.search(r"(\d{4})", line)
            if m:
                mhz = int(m.group(1))
                if 2412 <= mhz <= 2484:
                    current["channel"] = (mhz - 2407) // 5
        elif "WPA" in line or "RSN" in line:
            current["auth"] = "WPA2" if "RSN" in line else "WPA"
        elif "WEP" in line:
            current["auth"] = "WEP"

    # Don't forget last entry
    if current.get("ssid"):
        networks.append(current)

    # Sort by signal strength (strongest first)
    networks.sort(key=lambda n: n.get("rssi", -100), reverse=True)
    return {"networks": networks}


# ---------------------------------------------------------------------------
# HTTP Relay
# ---------------------------------------------------------------------------

def http_relay(method, url, headers=None, body=None, timeout=10):
    """Perform an HTTP request from the Pi. Returns dict with status, headers, body."""
    _check_wifi_testing_mode()
    req_headers = headers or {}
    body_bytes = None
    if body:
        body_bytes = base64.b64decode(body)

    req = urllib.request.Request(
        url,
        data=body_bytes,
        headers=req_headers,
        method=method.upper(),
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read()
            resp_headers = dict(resp.getheaders())
            return {
                "status": resp.status,
                "headers": resp_headers,
                "body": base64.b64encode(resp_body).decode("ascii"),
            }
    except urllib.error.HTTPError as e:
        resp_body = e.read() if e.fp else b""
        resp_headers = dict(e.headers.items()) if e.headers else {}
        return {
            "status": e.code,
            "headers": resp_headers,
            "body": base64.b64encode(resp_body).decode("ascii"),
        }
    except urllib.error.URLError as e:
        raise RuntimeError(f"HTTP request failed: {e.reason}")
    except Exception as e:
        raise RuntimeError(f"HTTP request failed: {e}")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def get_events(timeout=0):
    """Drain the event queue. If timeout > 0, long-poll for first event."""
    events = []

    if timeout > 0 and _event_queue.empty():
        try:
            evt = _event_queue.get(timeout=timeout)
            events.append(evt)
        except Empty:
            return events

    # Drain remaining
    while True:
        try:
            events.append(_event_queue.get_nowait())
        except Empty:
            break

    return events


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------

def ping():
    """Return version and uptime."""
    return {
        "fw_version": VERSION,
        "uptime": int(time.monotonic() - _start_time),
    }


# ---------------------------------------------------------------------------
# WiFi Sniffer
# ---------------------------------------------------------------------------

_sniffer_active = False
_sniffer_ssid = ""

def sniffer_start(ssid, password="", channel=6):
    """Start AP with NAT + internet forwarding + sniffer capture.

    Returns dict with ip and ssid.
    """
    global _sniffer_active, _sniffer_ssid

    _check_wifi_testing_mode()

    # Start AP with DNS logging enabled
    ap_start(ssid, password, channel, dns_logging=True)

    # Enable IP forwarding
    _run(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=False)

    # Add NAT masquerade on eth0
    _run(["iptables", "-t", "nat", "-A", "POSTROUTING", "-o", "eth0",
          "-j", "MASQUERADE"], check=False)
    _run(["iptables", "-A", "FORWARD", "-i", WLAN_IF, "-o", "eth0",
          "-j", "ACCEPT"], check=False)
    _run(["iptables", "-A", "FORWARD", "-i", "eth0", "-o", WLAN_IF,
          "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
         check=False)

    # Start sniffer capture threads
    dns_log = os.path.join(WORK_DIR, "dns.log")
    sniffer.start(interface=WLAN_IF, log_path=dns_log)

    _sniffer_active = True
    _sniffer_ssid = ssid
    logger.info("Sniffer started: ssid=%s", ssid)
    return {"ip": AP_IP, "ssid": ssid}


def sniffer_stop():
    """Stop sniffer capture + NAT + AP."""
    global _sniffer_active, _sniffer_ssid

    # Stop sniffer threads
    sniffer.stop()

    # Remove NAT rules
    _run(["iptables", "-t", "nat", "-D", "POSTROUTING", "-o", "eth0",
          "-j", "MASQUERADE"], check=False)
    _run(["iptables", "-D", "FORWARD", "-i", WLAN_IF, "-o", "eth0",
          "-j", "ACCEPT"], check=False)
    _run(["iptables", "-D", "FORWARD", "-i", "eth0", "-o", WLAN_IF,
          "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
         check=False)

    # Disable IP forwarding
    _run(["sysctl", "-w", "net.ipv4.ip_forward=0"], check=False)

    # Stop AP
    ap_stop()

    _sniffer_active = False
    _sniffer_ssid = ""
    logger.info("Sniffer stopped")


def sniffer_status() -> dict:
    """Return sniffer state + traffic summary."""
    return {
        "active": _sniffer_active,
        "ssid": _sniffer_ssid if _sniffer_active else "",
        "summary": sniffer.get_summary() if _sniffer_active else {},
        "stations": list(_stations.values()) if _sniffer_active else [],
    }
