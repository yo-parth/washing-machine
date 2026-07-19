# Deploying the dashboard to Render (permanent URL)

This puts the **dashboard + backend** on Render's free tier at a permanent URL
(e.g. `https://laundry-dashboard.onrender.com`) that survives laptop reboots.

**The poller stays on your laptop.** The plug (`172.16.0.98`) is a private LAN
address the cloud can't reach, so the poller must run at home and *push* readings
up to the Render service. Live data therefore still needs your laptop on and the
poller running — but the URL and the dashboard are now permanent.

```
   [Tapo P110] --LAN--> [poller on your laptop] --HTTPS /api/ingest--> [Render backend] <--- phones
```

---

## 1. Push this folder to GitHub

The repo is already initialised and committed locally. Create an **empty** repo
on GitHub (github.com/new — no README/.gitignore), then:

```bash
git remote add origin https://github.com/yo-parth/laundry-dashboard.git
git branch -M main
git push -u origin main
```

(The first push will prompt you to sign in to GitHub — that auth is yours to do.)

## 2. Create the Render service

1. Go to <https://dashboard.render.com> → **New** → **Blueprint**.
2. Connect your GitHub and pick the repo. Render reads `render.yaml` and proposes
   one free web service.
3. It will ask for the four **secret** env vars (marked `sync: false`). Set:
   - `DEVICE_KEY` — a long random string (the poller must send the same one).
   - `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM` — your Twilio SMS credentials.
4. Click **Apply** / **Deploy**. First build takes a few minutes.

When it's live you'll get a URL like `https://laundry-dashboard.onrender.com`.
Open it — both machines should show, reading **Unknown** until the poller starts
(next step).

## 3. Point the poller at Render (on your laptop)

```bash
# same DEVICE_KEY you set on Render:
export DEVICE_KEY='the-long-secret-you-chose'
export TAPO_USER='you@example.com'            # your TP-Link / Tapo app login
export TAPO_PASS='your-tapo-password'
export TAPO_HOST=172.16.0.98
export MACHINE_ID=wm-01
export BACKEND_URL='https://laundry-dashboard.onrender.com'   # <- your Render URL
python tapo_poller.py
```

Within a couple of seconds the Render dashboard should flip wm-01 to **Free** and
show live watts. (The poller's steady traffic also keeps the free service awake.)

## 4. Regenerate the QR stickers for the permanent URL

```bash
python make_qr.py https://laundry-dashboard.onrender.com
```

These now encode a URL that never changes — print them once and they keep working.

---

## Notes & known gaps for the hosted version

- **`/api/sim` is disabled in prod** (`SIM_ENABLED=0` in `render.yaml`) so nobody
  on the public internet can fake power readings. Keep it that way. Locally it
  stays on by default for manual testing.
- **Claiming is still honor-system** (`/api/claim` / `/api/collect` need no auth) —
  by design. `/api/ingest` is protected by `DEVICE_KEY`.
- **SQLite is ephemeral on Render free.** The filesystem resets on redeploy/restart,
  so in-flight claims are lost then; occupancy rebuilds from the next readings.
  Fine for this app; move to Render's paid disk or Postgres if you need durable
  claims.
- **Free tier cold-starts** after ~15 min with no traffic. While the poller is
  running it posts every 2 s and keeps the service warm; if the poller stops, the
  next visitor triggers a ~30–60 s wake-up.
- **Retune `OFF_HOLD_S`** in the Render dashboard for a real washer (~180) — the
  `8` here suits the pulsing microwave demo.
