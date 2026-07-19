"""
make_qr.py <base_url>

Reads the machines from the SQLite DB and writes one QR sticker PNG per machine,
qr_<id>.png, each encoding <base_url>/m/<id>. Scanning it opens that machine's
claim box straight away.

CRITICAL: <base_url> must be the address the phones will ACTUALLY reach. A QR
that encodes http://127.0.0.1:8000 or a http://172.16.x.x LAN IP will work on
the laptop that made it and nowhere else — a phone on cellular, or even a
different Wi-Fi, can't route to it. Use the machine's real reachable URL (a
LAN IP that the laundry-room phones share, a tunnel, or a deployed hostname).
"""

import os
import sqlite3
import sys

import qrcode

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(HERE, "machines.db"))


def main():
    if len(sys.argv) != 2:
        print("usage: python make_qr.py <base_url>")
        print("  e.g. python make_qr.py https://laundry.example.com")
        sys.exit(1)

    base_url = sys.argv[1].rstrip("/")

    if not os.path.exists(DB_PATH):
        print(f"no DB at {DB_PATH} — start main.py once so it seeds the machines.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, label FROM machines ORDER BY id").fetchall()
    conn.close()

    if not rows:
        print("no machines in the DB.")
        sys.exit(1)

    for m in rows:
        url = f"{base_url}/m/{m['id']}"
        img = qrcode.make(url)
        out = os.path.join(HERE, f"qr_{m['id']}.png")
        img.save(out)
        print(f"  {m['label']:<12} {url}  ->  {out}")

    print()
    print("WARNING: check the base URL above is one that PHONES can reach.")
    print("  127.0.0.1 or a LAN IP works on this laptop and nowhere else.")


if __name__ == "__main__":
    main()
