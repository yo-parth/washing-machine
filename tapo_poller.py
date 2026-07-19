"""
Tapo poller — reads live power draw from a TP-Link Tapo P110 and POSTs it to the
backend's /api/ingest. Run this next to the plug's network.

Build order note: this is the first thing that must work. Until a real number is
flowing, nothing downstream can be trusted. So on every tick it prints the watts
it read; watch that print before wiring anything else up.

LIBRARY NOTE: the spec called for `python-kasa` (Module.Energy). On this plug's
firmware (1.4.3, which negotiates the newer `TPAP` local-encryption scheme),
python-kasa 0.10.2 rejects the device as "Unsupported" before it can read
anything. The `tapo` library speaks TPAP and works, so this poller uses it. The
backend is untouched — it still just receives a number.

Gotchas baked into the hardware:
  * The plug needs TP-Link CLOUD credentials to authenticate even for LOCAL
    requests. Set TAPO_USER / TAPO_PASS to your Tapo app login.
  * Local third-party access must be enabled ON THE DEVICE. In the Tapo app:
    Me -> Third-Party Services (turn on; if it looks on already, toggle it off
    and back on). Until the device actually has this enabled, the connect below
    fails with a FORBIDDEN "Third-Party Compatibility" error.
  * A P100 has no energy monitoring. If the first power read fails we exit with a
    clear message rather than silently reporting zero.

The value we send is watts. The backend treats it as an opaque number (it lands
in the ingest field named `amps`); it never learns that it's watts. That is the
whole point — swap this poller for a CT clamp and the backend never changes.
"""

import asyncio
import os
import sys

import requests

from tapo import ApiClient

TAPO_HOST = os.environ.get("TAPO_HOST", "172.16.0.98")
TAPO_USER = os.environ.get("TAPO_USER")
TAPO_PASS = os.environ.get("TAPO_PASS")

BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")
MACHINE_ID = os.environ.get("MACHINE_ID", "wm-01")
DEVICE_KEY = os.environ.get("DEVICE_KEY", "change-me")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "2"))


def die(msg):
    print(f"[poller] FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def post_reading(watts):
    """POST one reading. Network hiccups must not kill the poll loop."""
    try:
        r = requests.post(
            f"{BACKEND_URL}/api/ingest",
            json={"machine_id": MACHINE_ID, "amps": watts},
            headers={"X-Device-Key": DEVICE_KEY},
            timeout=5,
        )
        if r.status_code >= 400:
            print(f"[poller] backend rejected reading: {r.status_code} {r.text[:120]}")
    except Exception as exc:  # noqa: BLE001
        print(f"[poller] backend unreachable: {exc}")


async def connect():
    """Open a fresh session to the plug. Raises on failure."""
    client = ApiClient(TAPO_USER, TAPO_PASS)
    return await client.p110(TAPO_HOST)


async def main():
    if not (TAPO_USER and TAPO_PASS):
        die("set TAPO_USER and TAPO_PASS (your TP-Link/Tapo cloud login)")

    print(f"[poller] connecting to Tapo at {TAPO_HOST} ...")
    try:
        dev = await connect()
    except Exception as exc:  # noqa: BLE001
        die(
            f"could not connect/authenticate to {TAPO_HOST}: {exc}\n"
            "  - check TAPO_USER / TAPO_PASS\n"
            "  - enable local access on the device: Tapo app -> Me -> "
            "Third-Party Services (toggle off then on if it already looks enabled)"
        )

    # First read doubles as the energy-support check: a P100 has no metering and
    # this call fails, so we exit clearly instead of reporting a silent zero.
    try:
        first = await dev.get_current_power()
    except Exception as exc:  # noqa: BLE001
        die(
            f"device at {TAPO_HOST} won't report power ({exc}) — is it a P100 "
            "instead of a P110? Only the P110 measures power."
        )

    print(f"[poller] connected. First reading {first.current_power} W. "
          f"Sending -> {BACKEND_URL} as machine {MACHINE_ID}. "
          f"Polling every {POLL_INTERVAL}s.\n")

    while True:
        try:
            p = await dev.get_current_power()
            watts = p.current_power
            if watts is None:
                watts = 0.0
            print(f"[poller] {watts:8.1f} W")
            post_reading(float(watts))
        except Exception as exc:  # noqa: BLE001
            # Read failed this tick. Print it, try to re-establish the session,
            # and keep going — the backend flips the machine to OFFLINE on its
            # own if readings stop for good.
            print(f"[poller] read error (reconnecting): {exc}")
            try:
                dev = await connect()
            except Exception as exc2:  # noqa: BLE001
                print(f"[poller] reconnect failed: {exc2}")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[poller] stopped")
