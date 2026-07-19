"""
Laundry machine occupancy dashboard — backend.

FastAPI app: state machine, SQLite, endpoints, serves index.html.

DESIGN NOTE (load-bearing): the backend receives a *number* per reading and
compares it against thresholds. It does not know or care what that number
physically represents (watts, amps, anything). That is what lets the sensor be
swapped without touching this file. Do not parse plug-specific fields here.
The ingest field is named `amps` and the thresholds carry an `_A` suffix for
historical reasons; in this deployment the number that flows through is watts.
The point stands: it's just a number.
"""

import logging
import os
import re
import sqlite3
import threading
import time

import requests
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# Config — every threshold comes from an env var with a default, so the rig can
# be retuned without editing code. Defaults are tuned for the microwave demo.
# A real washer wants OFF_HOLD_S around 180.
# --------------------------------------------------------------------------- #
ON_THRESHOLD = float(os.environ.get("ON_THRESHOLD_A", "100"))
OFF_THRESHOLD = float(os.environ.get("OFF_THRESHOLD_A", "50"))
OFF_HOLD_S = float(os.environ.get("OFF_HOLD_S", "8"))
GRACE_S = float(os.environ.get("GRACE_S", "60"))
OFFLINE_AFTER_S = float(os.environ.get("OFFLINE_AFTER_S", "60"))
DEVICE_KEY = os.environ.get("DEVICE_KEY", "change-me")
# When the dashboard is public, the no-auth /api/sim endpoint is an abuse vector
# (anyone could fake power readings). Disable it in prod with SIM_ENABLED=0.
SIM_ENABLED = os.environ.get("SIM_ENABLED", "1").lower() not in ("0", "false", "no", "off")
# Bare national numbers (e.g. "7507303008") make Twilio reject with error 21211.
# Numbers without a country code get this one prepended. Default +91 (India).
DEFAULT_COUNTRY_CODE = os.environ.get("DEFAULT_COUNTRY_CODE", "+91")

TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN")
# Twilio sender number for SMS, bare E.164 (e.g. +14787805487).
TWILIO_FROM = os.environ.get("TWILIO_FROM")

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(HERE, "machines.db"))
INDEX_PATH = os.path.join(HERE, "index.html")

STATE_FREE = "FREE"
STATE_RUNNING = "RUNNING"
STATE_DONE = "DONE_UNCOLLECTED"
STATE_OFFLINE = "OFFLINE"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("laundry")

# --------------------------------------------------------------------------- #
# Storage — one SQLite file, single process. A lock serialises every mutation
# so a reading and an HTTP action can't interleave mid-transition.
# --------------------------------------------------------------------------- #
lock = threading.Lock()
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row


def init_db():
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS machines (
            id            TEXT PRIMARY KEY,
            label         TEXT NOT NULL,
            state         TEXT NOT NULL DEFAULT 'FREE',
            last_reading  REAL,
            last_seen     REAL,
            below_since   REAL,
            running_since REAL,
            done_at       REAL,
            owner_name    TEXT,
            owner_phone   TEXT,
            notified      INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL NOT NULL,
            machine_id TEXT,
            event      TEXT NOT NULL,
            detail     TEXT
        );
        """
    )
    # Seed two machines. Claim-on-use only: no users, no bookings, no queue.
    conn.execute(
        "INSERT OR IGNORE INTO machines (id, label) VALUES (?, ?)",
        ("wm-01", "Washer 1"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO machines (id, label) VALUES (?, ?)",
        ("wm-02", "Washer 2"),
    )
    conn.commit()


def record_event(machine_id, event, detail=None):
    """Insert into the audit log. Caller holds the lock and commits."""
    conn.execute(
        "INSERT INTO events (ts, machine_id, event, detail) VALUES (?, ?, ?, ?)",
        (time.time(), machine_id, event, detail),
    )


def normalize_phone(raw, default_cc=DEFAULT_COUNTRY_CODE):
    """Best-effort E.164 so Twilio doesn't reject bare national numbers (21211).
    '7507303008' -> '+917507303008', '07507…' -> '+917507…', '+91…' kept as-is.
    Country code is configurable for non-India deployments."""
    s = re.sub(r"[^\d+]", "", raw or "")
    if not s:
        return ""
    if s.startswith("+"):
        return s
    if s.startswith("00"):
        return "+" + s[2:]
    digits = s.lstrip("0")
    cc = default_cc.lstrip("+")
    if digits.startswith(cc) and len(digits) > 10:
        return "+" + digits           # already carries the country code, just add +
    return default_cc + digits        # bare national number


# --------------------------------------------------------------------------- #
# SMS — fires exactly once on the transition into DONE_UNCOLLECTED.
# Must fail soft: no creds, Twilio down, or a bad number → log and carry on.
# A broken alert must never take down the dashboard.
# (Originally WhatsApp in the spec; switched to plain Twilio SMS. To/From are
#  bare E.164 numbers — no "whatsapp:" prefix.) Returns the HTTP status on a
# send attempt (None if skipped or errored) so the path is verifiable.
# --------------------------------------------------------------------------- #
def send_sms(phone, name, label):
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM):
        log.warning("sms skipped: Twilio credentials not configured")
        return None
    try:
        body = (
            f"Hi {name}, your laundry in \"{label}\" is done. "
            "Please collect it so the next person can use the machine."
        )
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            data={"From": TWILIO_FROM, "To": normalize_phone(phone), "Body": body},
            auth=(TWILIO_SID, TWILIO_TOKEN),
            timeout=10,
        )
        if resp.status_code >= 400:
            log.warning("sms failed: HTTP %s %s", resp.status_code, resp.text[:300])
        else:
            sid = ""
            try:
                sid = resp.json().get("sid", "")
            except Exception:  # noqa: BLE001
                pass
            log.info("sms sent to %s (HTTP %s sid=%s)", phone, resp.status_code, sid)
        return resp.status_code
    except Exception as exc:  # noqa: BLE001 — fail soft, on purpose
        log.warning("sms error: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# The state machine. Two SEPARATE mechanisms, never collapsed:
#   1. Hysteresis  — ON and OFF are different thresholds. Cross up to ON to turn
#      on; only turn off below OFF. Between them, hold. Stops tile flicker.
#   2. Hold-off    — a TIME rule. The reading must sit below OFF continuously for
#      OFF_HOLD_S before "done" is declared. Any reading above OFF resets the
#      timer. This is what stops a mid-cycle pause (soak / magnetron pulsing)
#      from firing a false "your clothes are done" alert.
# Errors are asymmetric: too short = false alerts (the bug we exist to kill);
# too long = a few minutes late (nobody notices). Bias long.
# --------------------------------------------------------------------------- #
def apply_reading(machine_id, value):
    now = time.time()
    notify_target = None

    with lock:
        m = conn.execute(
            "SELECT * FROM machines WHERE id=?", (machine_id,)
        ).fetchone()
        if m is None:
            return None

        state = m["state"]
        below_since = m["below_since"]
        running_since = m["running_since"]
        done_at = m["done_at"]
        notified = m["notified"]
        new_state = state

        # Mechanism 2: the hold-off timer, keyed purely on the OFF threshold.
        if value < OFF_THRESHOLD:
            if below_since is None:
                below_since = now
        else:
            # Any reading at/above OFF resets the timer.
            below_since = None

        # Mechanism 1: state transitions with hysteresis.
        if value >= ON_THRESHOLD:
            # Crossing up (or staying) at ON → running.
            if state != STATE_RUNNING:
                new_state = STATE_RUNNING
                running_since = now
                done_at = None
                notified = 0  # a fresh cycle re-arms the alert
                record_event(machine_id, "start", f"reading={value}")
        elif state == STATE_RUNNING:
            # Below ON while running: hold unless the hold-off has fully elapsed.
            if below_since is not None and (now - below_since) >= OFF_HOLD_S:
                below_since = None
                if m["owner_name"]:
                    new_state = STATE_DONE
                    done_at = now
                    record_event(machine_id, "done_uncollected", f"reading={value}")
                    if not notified and m["owner_phone"]:
                        notify_target = (m["owner_phone"], m["owner_name"], m["label"])
                        notified = 1
                        record_event(machine_id, "notify", m["owner_phone"])
                else:
                    new_state = STATE_FREE
                    running_since = None
                    record_event(machine_id, "done_free", f"reading={value}")
        elif state == STATE_OFFLINE:
            # Heartbeat is back and the appliance isn't drawing power → free.
            new_state = STATE_FREE
            running_since = None
            record_event(machine_id, "recovered", f"reading={value}")
        # else: FREE or DONE_UNCOLLECTED with a sub-ON reading → hold.

        conn.execute(
            """UPDATE machines
                 SET state=?, last_reading=?, last_seen=?, below_since=?,
                     running_since=?, done_at=?, notified=?
               WHERE id=?""",
            (new_state, value, now, below_since, running_since, done_at,
             notified, machine_id),
        )
        conn.commit()

    # Network call happens OUTSIDE the lock. The notified guard was already
    # persisted above, so a flapping reading can't queue a second message.
    if notify_target:
        send_sms(*notify_target)
    return new_state


def sweep():
    """Run on every read. Handles the two time-based transitions that no
    incoming reading can trigger: going OFFLINE (poller died) and GRACE expiry.

    OFFLINE is not optional. If the poller dies and a machine keeps reading its
    last value, the dashboard would say "free" and send someone to a running
    machine — worse than no dashboard at all.
    """
    now = time.time()
    with lock:
        for m in conn.execute("SELECT * FROM machines").fetchall():
            if (
                m["state"] != STATE_OFFLINE
                and m["last_seen"] is not None
                and (now - m["last_seen"]) > OFFLINE_AFTER_S
            ):
                conn.execute(
                    "UPDATE machines SET state=? WHERE id=?",
                    (STATE_OFFLINE, m["id"]),
                )
                record_event(
                    m["id"], "offline", f"no reading for {int(now - m['last_seen'])}s"
                )
                continue

            if (
                m["state"] == STATE_DONE
                and m["done_at"] is not None
                and (now - m["done_at"]) >= GRACE_S
            ):
                conn.execute(
                    """UPDATE machines
                         SET state=?, owner_name=NULL, owner_phone=NULL,
                             notified=0, running_since=NULL, done_at=NULL,
                             below_since=NULL
                       WHERE id=?""",
                    (STATE_FREE, m["id"]),
                )
                record_event(m["id"], "grace_released", None)
        conn.commit()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
app = FastAPI(title="Laundry occupancy")


class IngestBody(BaseModel):
    machine_id: str
    amps: float


class ClaimBody(BaseModel):
    machine_id: str
    name: str
    phone: str


class CollectBody(BaseModel):
    machine_id: str


@app.on_event("startup")
def _startup():
    init_db()
    log.info(
        "config: ON=%.1f OFF=%.1f OFF_HOLD_S=%.0f GRACE_S=%.0f OFFLINE_AFTER_S=%.0f",
        ON_THRESHOLD, OFF_THRESHOLD, OFF_HOLD_S, GRACE_S, OFFLINE_AFTER_S,
    )
    if DEVICE_KEY == "change-me":
        log.warning("DEVICE_KEY is the default 'change-me' — set it in prod")


@app.post("/api/ingest")
def ingest(body: IngestBody, x_device_key: str = Header(default="")):
    if x_device_key != DEVICE_KEY:
        raise HTTPException(status_code=401, detail="bad device key")
    state = apply_reading(body.machine_id, body.amps)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown machine")
    return {"ok": True, "state": state}


@app.post("/api/sim")
def sim(body: IngestBody):
    # Same path as ingest, no auth. Manual fallback if the plug dies before
    # judging — drive a machine by hand from the terminal. Gated by SIM_ENABLED
    # so it can be switched off on a public deployment.
    if not SIM_ENABLED:
        raise HTTPException(status_code=403, detail="sim disabled")
    state = apply_reading(body.machine_id, body.amps)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown machine")
    return {"ok": True, "state": state}


@app.get("/api/machines")
def machines():
    sweep()
    now = time.time()
    out = []
    with lock:
        rows = conn.execute("SELECT * FROM machines ORDER BY id").fetchall()
    for m in rows:
        running_for_s = None
        done_ago_s = None
        grace_left_s = None
        if m["state"] == STATE_RUNNING and m["running_since"] is not None:
            running_for_s = int(now - m["running_since"])
        if m["state"] == STATE_DONE and m["done_at"] is not None:
            done_ago_s = int(now - m["done_at"])
            grace_left_s = max(0, int(GRACE_S - (now - m["done_at"])))
        out.append(
            {
                "id": m["id"],
                "label": m["label"],
                "state": m["state"],
                "last_reading": m["last_reading"],
                "last_seen": m["last_seen"],
                # Phone is never exposed — the dashboard is public.
                "owner_name": m["owner_name"],
                "claimed": m["owner_name"] is not None,
                "running_for_s": running_for_s,
                "done_ago_s": done_ago_s,
                "grace_left_s": grace_left_s,
            }
        )
    return {
        "machines": out,
        "on_threshold": ON_THRESHOLD,
        "off_threshold": OFF_THRESHOLD,
        "grace_s": GRACE_S,
    }


@app.post("/api/claim")
def claim(body: ClaimBody):
    with lock:
        m = conn.execute(
            "SELECT * FROM machines WHERE id=?", (body.machine_id,)
        ).fetchone()
        if m is None:
            raise HTTPException(status_code=404, detail="unknown machine")
        if m["owner_name"] is not None:
            raise HTTPException(status_code=409, detail="already claimed")
        conn.execute(
            "UPDATE machines SET owner_name=?, owner_phone=?, notified=0 WHERE id=?",
            (body.name.strip(), normalize_phone(body.phone), body.machine_id),
        )
        record_event(body.machine_id, "claim", body.name.strip())
        conn.commit()
    return {"ok": True}


@app.post("/api/collect")
def collect(body: CollectBody):
    with lock:
        m = conn.execute(
            "SELECT * FROM machines WHERE id=?", (body.machine_id,)
        ).fetchone()
        if m is None:
            raise HTTPException(status_code=404, detail="unknown machine")
        if m["state"] == STATE_DONE:
            conn.execute(
                """UPDATE machines
                     SET owner_name=NULL, owner_phone=NULL, notified=0,
                         state=?, running_since=NULL, done_at=NULL, below_since=NULL
                   WHERE id=?""",
                (STATE_FREE, body.machine_id),
            )
        else:
            conn.execute(
                "UPDATE machines SET owner_name=NULL, owner_phone=NULL, notified=0 WHERE id=?",
                (body.machine_id,),
            )
        record_event(body.machine_id, "collect", None)
        conn.commit()
    return {"ok": True}


@app.get("/api/health")
def health():
    # No secrets — just whether things are wired up, for remote diagnosis.
    return {
        "ok": True,
        "sms_configured": bool(TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM),
        "sim_enabled": SIM_ENABLED,
        "default_country_code": DEFAULT_COUNTRY_CODE,
        "version": "v2-phone-normalize+health",
    }


@app.get("/")
def index():
    return FileResponse(INDEX_PATH)


@app.get("/m/{machine_id}")
def index_for_machine(machine_id: str):
    # Same static page. The client reads the path and opens that claim box.
    return FileResponse(INDEX_PATH)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
