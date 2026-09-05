"""
BLE Proxy Controller — scan, connect, and write to BLE peripherals.

Uses bleak (async BLE library) with its own asyncio event loop running
in a background daemon thread.  All public functions are synchronous
and thread-safe.
"""

import asyncio
import threading

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    BleakClient = None
    BleakScanner = None

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None

# Connection state
_client: "BleakClient | None" = None
_address: str | None = None
_name: str | None = None
_state: str = "idle"  # idle, scanning, connected

BLE_SCAN_TIMEOUT = float(__import__("os").environ.get("BLE_SCAN_TIMEOUT", "5.0"))


def _ensure_loop():
    """Start the asyncio event loop thread if not already running."""
    global _loop, _loop_thread
    if _loop is not None and _loop.is_running():
        return
    _loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(_loop)
        _loop.run_forever()

    _loop_thread = threading.Thread(target=_run, daemon=True, name="ble-loop")
    _loop_thread.start()


def _run_async(coro):
    """Run an async coroutine on the BLE event loop and return the result."""
    _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=30)


def _on_disconnect(client):
    """Callback when BLE device disconnects unexpectedly."""
    global _client, _address, _name, _state
    with _lock:
        _client = None
        _address = None
        _name = None
        _state = "idle"


def available() -> bool:
    """Check if bleak is installed."""
    return BleakScanner is not None


def scan(timeout: float = 0, name_filter: str = "") -> dict:
    """Scan for BLE peripherals."""
    global _state
    if not available():
        return {"ok": False, "error": "bleak not installed — run: pip3 install bleak"}

    if timeout <= 0:
        timeout = BLE_SCAN_TIMEOUT

    with _lock:
        if _state == "scanning":
            return {"ok": False, "error": "scan already in progress"}
        _state = "scanning"

    try:
        async def _scan():
            devices = await BleakScanner.discover(timeout=timeout)
            return devices

        devices = _run_async(_scan())
        results = []
        for d in devices:
            if name_filter and (not d.name or name_filter not in d.name):
                continue
            results.append({
                "address": d.address,
                "name": d.name or "(unknown)",
                "rssi": d.rssi if hasattr(d, "rssi") else None,
            })
        results.sort(key=lambda x: x.get("rssi") or -999, reverse=True)
        return {"ok": True, "devices": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        with _lock:
            if _state == "scanning":
                _state = "idle"


def connect(address: str) -> dict:
    """Connect to a BLE peripheral by address."""
    global _client, _address, _name, _state
    if not available():
        return {"ok": False, "error": "bleak not installed — run: pip3 install bleak"}

    with _lock:
        if _state == "connected" and _address:
            return {"ok": False, "error": f"already connected to {_address}"}

    try:
        async def _connect():
            client = BleakClient(address, disconnected_callback=_on_disconnect)
            await client.connect()
            services = []
            for svc in client.services:
                chars = []
                for ch in svc.characteristics:
                    props = [p.lower() for p in ch.properties]
                    chars.append({"uuid": str(ch.uuid), "properties": props})
                services.append({"uuid": str(svc.uuid), "characteristics": chars})
            return client, services

        client, services = _run_async(_connect())

        with _lock:
            _client = client
            _address = address
            _name = client.services and str(address)  # bleak doesn't always expose name
            _state = "connected"

        return {
            "ok": True,
            "address": address,
            "name": _name or address,
            "services": services,
        }
    except Exception as e:
        return {"ok": False, "error": f"connect failed: {e}"}


def disconnect() -> dict:
    """Disconnect from the current BLE peripheral."""
    global _client, _address, _name, _state

    with _lock:
        client = _client
        if client is None:
            _state = "idle"
            return {"ok": True}

    try:
        async def _disconnect():
            if client.is_connected:
                await client.disconnect()

        _run_async(_disconnect())
    except Exception:
        pass

    with _lock:
        _client = None
        _address = None
        _name = None
        _state = "idle"

    return {"ok": True}


def status() -> dict:
    """Return current BLE connection state."""
    with _lock:
        result = {"ok": True, "state": _state}
        if _state == "connected":
            result["address"] = _address
            result["name"] = _name or _address
        return result


def write(characteristic: str, data: bytes, response: bool = True) -> dict:
    """Write raw bytes to a GATT characteristic."""
    if not available():
        return {"ok": False, "error": "bleak not installed — run: pip3 install bleak"}

    with _lock:
        client = _client
        if client is None or _state != "connected":
            return {"ok": False, "error": "not connected"}

    try:
        async def _write():
            await client.write_gatt_char(characteristic, data, response=response)

        _run_async(_write())
        return {"ok": True, "bytes_written": len(data)}
    except Exception as e:
        return {"ok": False, "error": f"write failed: {e}"}


def read(characteristic: str) -> dict:
    """Read raw bytes from a GATT characteristic."""
    if not available():
        return {"ok": False, "error": "bleak not installed — run: pip3 install bleak"}

    with _lock:
        client = _client
        if client is None or _state != "connected":
            return {"ok": False, "error": "not connected"}

    try:
        async def _read():
            return await client.read_gatt_char(characteristic)

        data = _run_async(_read())
        return {
            "ok": True,
            "characteristic": characteristic,
            "data": data.hex(),
            "size": len(data)
        }
    except Exception as e:
        return {"ok": False, "error": f"read failed: {e}"}


def shutdown():
    """Stop the event loop and clean up."""
    global _loop
    if _loop and _loop.is_running():
        _loop.call_soon_threadsafe(_loop.stop)
    disconnect()
