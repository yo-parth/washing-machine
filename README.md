# Laundry machine occupancy dashboard

Shows which shared washing machines are **free right now**, and texts (SMS) the
owner when their load finishes — so nobody dumps someone else's clothes on the
floor to free up a machine.

Occupancy is read from **live power draw** via a TP-Link Tapo P110 smart plug.
There is deliberately **no remaining-time estimate**: power draw can't produce an
honest one, and a wrong countdown is exactly the problem this replaces.

```
main.py          FastAPI app: state machine, SQLite, endpoints, serves index.html
index.html       dashboard, polls /api/machines every 2s
tapo_poller.py   reads watts from the plug, POSTs to the backend
make_qr.py       generates one QR sticker PNG per machine
```

---

## Setup

Python 3. One virtualenv, five dependencies, no build step.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate      Linux/macOS:  source .venv/bin/activate
pip install fastapi uvicorn requests tapo "qrcode[pil]"
```

### 1. Run the backend

```bash
# set a real device key so random people can't POST fake readings
export DEVICE_KEY=some-long-secret        # Windows: set DEVICE_KEY=some-long-secret
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>. On first run it creates `machines.db` and seeds two
machines, **Washer 1** (`wm-01`) and **Washer 2** (`wm-02`).

### 2. Run the poller (get watts flowing first)

The poller is the thing to get working before anything else — until a real number
is flowing, nothing downstream can be trusted. It prints the watts it reads every
tick; watch that print.

```bash
export TAPO_USER='you@example.com'        # your TP-Link / Tapo app login
export TAPO_PASS='your-tapo-password'
export TAPO_HOST=172.16.0.98              # the plug's IP
export DEVICE_KEY=some-long-secret        # must match the backend
export MACHINE_ID=wm-01                   # which machine this plug is on
python tapo_poller.py
```

Run one poller per plug (each with its own `MACHINE_ID` and `TAPO_HOST`).

### 3. Generate QR stickers

```bash
python make_qr.py https://the-url-phones-will-actually-reach
```

Writes `qr_wm-01.png`, `qr_wm-02.png`, each pointing at `<base_url>/m/<id>`.
Scanning one opens that machine's claim box immediately — scan, type name, done.

> **The base URL must be one that phones can actually reach.** A QR encoding
> `127.0.0.1` or a `172.16.x.x` LAN IP works on the laptop that made it and
> nowhere else — a phone on cellular can't route to it. Use a shared-LAN IP the
> laundry-room phones can reach, a tunnel, or a deployed hostname.

---

## The Tapo gotcha (read before blaming the code)

The Tapo P110 needs **TP-Link cloud credentials to authenticate even for local
requests** — that's why `TAPO_USER` / `TAPO_PASS` are your Tapo *app* login.

Recent firmware (this plug is on **1.4.3**) also **blocks third-party access
until you enable it on the device**:

> Tapo app → **Me** → **Third-Party Services** → on. (If it already looks on,
> toggle it off and back on — the device only picks up the change on a fresh
> handshake.)

Without that, the connection fails with a `FORBIDDEN` "Third-Party Compatibility"
error, no matter how correct the credentials are. The poller prints a message
pointing at this if the connection fails, and exits with a clear error if the
device has no energy module (a P100 instead of a P110 — only the P110 measures
power).

**Library note:** the spec called for `python-kasa`, but on this firmware's newer
`TPAP` local-encryption scheme `python-kasa` 0.10.2 rejects the plug as
"Unsupported" before it can read anything. The poller therefore uses the
[`tapo`](https://pypi.org/project/tapo/) library, which speaks TPAP. Verified
reading live watts from the P110 at `172.16.0.98`.

---

## Environment variables

Every threshold has an env var with a default, so the rig can be **retuned
without editing code**. Defaults below are tuned for the microwave demo.

| Var | Default | What it does |
|---|---|---|
| `ON_THRESHOLD_A` | `100` | Reading at/above this ⇒ the machine is **running**. |
| `OFF_THRESHOLD_A` | `50` | Reading must drop **below** this to count as "off". |
| `OFF_HOLD_S` | `8` | Reading must stay below OFF **continuously** this long before "done" fires. |
| `GRACE_S` | `60` | After "done", auto-release the machine this many seconds later. |
| `OFFLINE_AFTER_S` | `60` | No reading for this long ⇒ machine shows **Unknown** (offline). |
| `DEVICE_KEY` | `change-me` | Shared secret; the poller sends it as `X-Device-Key` on `/api/ingest`. |
| `TAPO_USER` / `TAPO_PASS` | — | Poller only: your Tapo cloud login. |
| `TAPO_HOST` | `172.16.0.98` | Poller only: the plug's IP. |
| `MACHINE_ID` | `wm-01` | Poller only: which machine this plug reports for. |
| `BACKEND_URL` | `http://127.0.0.1:8000` | Poller only: where to POST readings. |
| `POLL_INTERVAL` | `2` | Poller only: seconds between reads. |
| `TWILIO_SID` / `TWILIO_TOKEN` / `TWILIO_FROM` | — | SMS alerts (`TWILIO_FROM` is the bare E.164 sender number). Absent ⇒ alerts are skipped, everything else runs. |
| `DB_PATH` | `./machines.db` | SQLite file location. |

### Retuning the thresholds

* **`ON`/`OFF` (hysteresis).** These are two *different* values on purpose.
  Crossing up to `ON` turns the tile on; it only turns off below `OFF`; between
  them it holds. That gap stops the tile flickering when the reading sits near
  the boundary. Set `ON` above idle/standby draw and `OFF` below running draw.
* **`OFF_HOLD_S` (hold-off).** A *time* rule, not a threshold. Appliances pause
  mid-cycle — a washer soaks for minutes; a microwave pulses its magnetron on and
  off every few seconds. Without a hold-off, every pause reads as "cycle
  finished" and fires a false alert. **Errors here are asymmetric:** too short =
  false "your clothes are done" alerts (the exact failure this project exists to
  fix); too long = you're a few minutes late, which nobody notices. **Bias long.**
  The `8`s default suits the pulsing microwave demo. **A real washer wants
  `OFF_HOLD_S` around `180`.**

---

## Measured values (Panasonic NN-SM255WFDG microwave demo)

230 V, 1250 W input / 800 W microwave output. On the Tapo:

* **Idle: under 1 W**
* **Full power: 1295 W** (measured; nameplate says 1250 W input)

So `ON=100`, `OFF=50` sit comfortably between idle and running. The microwave
pulses at reduced power, which is exactly why the hold-off matters.

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/ingest` | `{machine_id, amps}`, header `X-Device-Key`. Applies a reading. |
| `GET`  | `/api/machines` | All machines + derived fields (`running_for_s`, `done_ago_s`, `grace_left_s`) and the thresholds the meter needs. |
| `POST` | `/api/claim` | `{machine_id, name, phone}`. `409` if already claimed. |
| `POST` | `/api/collect` | `{machine_id}`. Clears owner; releases if it was Done. |
| `POST` | `/api/sim` | `{machine_id, amps}`. Same path as ingest, **no auth** — see below. |
| `GET`  | `/` | The dashboard. |
| `GET`  | `/m/{machine_id}` | The dashboard, with that machine's claim box opened. |

The ingest field is named `amps` and the thresholds carry `_A`, but the number
that flows through is watts. **The backend never learns what the number
physically is** — it only compares it to thresholds. That abstraction is
load-bearing: it's what lets you swap the Tapo for a CT clamp without touching
`main.py`. Don't "improve" it by parsing plug-specific fields in the backend.

### The `/api/sim` fallback

`/api/sim` takes the same body as `/api/ingest` but needs **no device key**. It's
the manual override for when the plug dies before it has judged a load — drive a
machine by hand from a terminal:

```bash
# mark wm-01 as running, then finished:
curl -X POST localhost:8000/api/sim -H 'Content-Type: application/json' -d '{"machine_id":"wm-01","amps":1295}'
curl -X POST localhost:8000/api/sim -H 'Content-Type: application/json' -d '{"machine_id":"wm-01","amps":1}'
# ...wait OFF_HOLD_S, send one more low reading to trip "done":
curl -X POST localhost:8000/api/sim -H 'Content-Type: application/json' -d '{"machine_id":"wm-01","amps":1}'
```

---

## States

`Free` · `In use` · `Done — uncollected` · `Unknown` (offline).

```
any               --reading >= ON---------------------------->  RUNNING
RUNNING           --below OFF continuously for OFF_HOLD_S----->  DONE_UNCOLLECTED (if claimed)
                                                                 FREE             (if not)
DONE_UNCOLLECTED  --owner taps "collected", or GRACE_S ------->  FREE
any               --no reading for OFFLINE_AFTER_S ----------->  OFFLINE
```

`OFFLINE` is not cosmetic. If the poller dies and a machine keeps reading its last
value, showing "free" would send someone to a running machine — worse than having
no dashboard. The backend sweeps for stale heartbeats on every read.

---

## Demo script (rehearsable)

1. **Start clean.** `uvicorn main:app` (with a `DEVICE_KEY`), open the dashboard.
   Both machines read **Free**.
2. **Claim it.** On your phone, scan the `wm-01` sticker (or open `/m/wm-01`).
   The claim box is already focused — type a name and a mobile number (E.164,
   e.g. `+91…`; on a Twilio trial it must be a *verified* number), tap
   **This is mine**.
3. **Start the microwave.** The live meter bar jumps **past the on-threshold
   line** and the tile flips to **In use**. *(This is the visual moment.)*
4. **Pause it mid-run** (open the door). The reading drops — and the tile **stays
   In use**. That's the hold-off refusing to fire a false alert. Close and resume.
5. **Let it finish.** A few seconds after it stays off, the tile flips to
   **Done — uncollected** and the claimer gets an **SMS**. It fires **once**.
6. **Collect.** Tap **Mark collected** (or let `GRACE_S` elapse) → back to
   **Free**.
7. **Optional — offline.** Kill the poller. After `OFFLINE_AFTER_S` the tile goes
   **Unknown**, not a stale "free".

No hardware? Run every step with `/api/sim` instead of the microwave — the curl
snippets above drive the same path. For a fast rehearsal, lower `OFF_HOLD_S`,
`GRACE_S`, and `OFFLINE_AFTER_S` (e.g. `2`, `4`, `5`).

---

## SMS alerts

Uses Twilio SMS (`TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM` — the sender number,
bare E.164). Fires **exactly once**, on the transition into `Done — uncollected`,
to the claimer's number, guarded by a `notified` flag so a flapping reading can't
spam. It **fails soft**: no credentials, Twilio down, or a bad number → it logs
and carries on. A broken alert never takes down the dashboard.

(This replaced the WhatsApp sandbox the spec originally called for. On a Twilio
**trial** account, delivery only works to *verified* numbers and the body is
prefixed with "Sent from your Twilio trial account".)

---

## What was verified

Driven end-to-end on Python 3.14, first with `/api/sim` and then **against the
real hardware**:

* State machine: hysteresis, hold-off (including timer reset by a mid reading),
  done-when-claimed vs free-when-not, collect, grace auto-release, offline sweep
  and recovery — all exercised and passing.
* Dashboard: live polling, meter, claim/collect, the `/m/<id>` deep-link opening
  and **keeping** focus on the claim box across the 2 s repaint. Full claim
  through the UI works; phone numbers are never exposed by the API.
* **Real plug:** the `tapo` poller reads live watts from the P110 at
  `172.16.0.98` and streams them to the backend.
* **Real microwave:** drawing ~1325 W flipped wm-01 to RUNNING within one poll,
  and a genuine magnetron pulse-off (the reading dipping below OFF for ~4 s) did
  **not** false-fire — the hold-off held it RUNNING. Exactly the failure this
  project exists to prevent, observed on real hardware.
* **Real SMS:** the `send_sms` path returned Twilio `HTTP 201` with a message SID.
* **Public access:** a cloudflared quick tunnel fronts the backend and the QR
  stickers encode that public URL.

---

## Known gaps

* **Honor-system claiming.** Anyone can claim or collect any machine — there are
  no accounts and no verification. This is a deliberate design choice, not an
  oversight: claiming is a courtesy so you get pinged, not a lock.
* **SQLite, single process.** One `uvicorn` worker, one SQLite file. Fine for a
  building's worth of machines; don't scale it horizontally without swapping the
  store. (Run with `--workers 1`.)
* **Twilio trial only texts verified numbers.** On a trial account, SMS is
  delivered only to numbers verified in the Twilio console, with a "Sent from your
  Twilio trial account" prefix. Upgrade the account to reach arbitrary numbers.
* **The public URL is an ephemeral quick tunnel.** `cloudflared tunnel --url`
  mints a new random `*.trycloudflare.com` address each time it starts and drops
  it when the process stops — so the QR stickers must be regenerated whenever the
  tunnel restarts. For a stable address use a *named* Cloudflare tunnel (needs a
  Cloudflare account + domain) or another persistent host.
* **Backend, poller, and tunnel are foreground processes.** They must stay
  running (their own terminals, or installed as services); if the host or terminal
  closes, they stop.
* **One laptop is the server.** If that machine or its network goes down, the
  dashboard goes with it. The `OFFLINE` state is what stops a *dead poller* from
  lying; it can't help if the whole server is gone.

## Not in scope (on purpose)

No remaining-time / ETA estimate. No accounts, bookings, or queue — claim-on-use
only. No websockets/SSE (polling is fine at this scale). No Docker, ORM,
migrations, or frontend build step.
