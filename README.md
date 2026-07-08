# Powerwall Auto Evening-Export

Automatically sells your Powerwall to the grid **at the single highest-paid hour each day**
and keeps a reserve for your own overnight use — for free, with no computer left running.

Each hour it reads your locked SCE NBT export-rate CSV, finds today's best export hour, and
acts only then, through Tesla's official **Fleet API**, run by a free **GitHub Actions** job:

| What it reads | What it decides | What it does |
|---|---|---|
| `data/export_rates.csv` for today's date | The single peak export hour + how aggressive to be | At that hour: Time-Based Control + Export Everything + a **dynamic reserve** |

**Dynamic reserve** (how much it keeps) scales with the day's peak rate, so you sell hardest when it pays most:

| Today's peak export rate | Reserve kept | Typical month |
|---|---|---|
| ≥ $0.80/kWh | **10%** (sell almost everything) | August |
| ≥ $0.40/kWh | 20% | September |
| ≥ $0.20/kWh | 30% | July (weekdays) |
| < $0.20/kWh | — **does nothing** (off-season) | Oct–June, July weekends |

> Rates in `data/export_rates.csv` are the **total** export credit — Delivery EEC +
> Generation EEC — from SCE's official EEC Factors file (PTO Group 3, 2025). Both
> components are paid to bundled customers; never use a single component alone.

**Timing:** it arms the export in the hour *before* the peak (`EXPORT_LEAD_HOURS`, default 1).
This absorbs GitHub's cron delay (5–30 min at busy times) and the Powerwall's own ramp-up
after a mode change — TBC re-plans on its own schedule and doesn't dump the instant the
settings land. The peak-hour run re-sends the same commands as a free retry.

At `RESTORE_HOUR` (default 10pm) it switches back to Self-Powered so the charge you kept
powers your house overnight — with an after-midnight catch-up run in case the evening
runs get dropped. Every other moment it does nothing, so it's safe.

---

## Honest heads-up before you start

This is the most involved option you considered. The **script is the easy part** —
the work is Tesla's one-time developer setup (a free app + hosting one public file).
Budget **30–60 minutes** for first-time setup. After that it's fully automatic.

If you get stuck, the **NetZero 30-day free trial does exactly this with no setup** —
a perfectly good fallback. Nothing here costs money: GitHub and the Tesla developer
app are free, and a few API calls a day sit inside Tesla's free usage tier.

> One important note: the script removes the *blockers* to exporting (mode, reserve,
> export permission). The actual export still happens through Tesla's Time-Based
> Control, which exports during the peak window **if your utility rate plan in the
> Tesla app marks the evening as high value** (NBT25 normally does). If after testing
> it still won't push to the grid, see **Troubleshooting → "It runs but doesn't
> export."**

---

## What you'll need (all free)

- Your Tesla account login
- A GitHub account — sign up at https://github.com
- Your `SCE NBT25 Export Rates` CSV (your locked export schedule)
- 30–60 minutes, once

---

## Setup, step by step

### 1. Create your GitHub repo (and host the public key)

1. Sign in to GitHub and create a **new public repository named exactly
   `YOURUSERNAME.github.io`** (replace YOURUSERNAME with your GitHub username).
   Public is required for free GitHub Pages; **no secrets ever go in the code.**
2. Upload the contents of this `powerwall-auto-export` folder into that repo
   (the two `.py` files, `requirements.txt`, the `.github` folder, and the `data` folder).
   You can drag-and-drop with **"Add file → Upload files."**
3. Your rate schedule is **already included** at `data/export_rates.csv` (a copy of your
   locked SCE CSV) — just make sure it uploads with the folder. It's what the script
   reads each day to find the peak hour.
4. Add an **empty file named `.nojekyll`** at the top level of the repo (this lets
   GitHub Pages serve the hidden `.well-known` folder that will hold your public key).
5. In the repo, go to **Settings → Pages**, set **Source = Deploy from a branch**,
   **Branch = main / root**, and Save. After a minute your site is live at
   `https://YOURUSERNAME.github.io/`.

### 2. Generate your key pair

Tesla requires you to host a public key. Open a terminal (on Windows, use **Git Bash**;
or use **GitHub Codespaces** — see step 6) and run:

```bash
openssl ecparam -name prime256v1 -genkey -noout -out private-key.pem
openssl ec -in private-key.pem -pubout -out com.tesla.3p.public-key.pem
```

- **Keep `private-key.pem` private** — do NOT upload it anywhere. (Energy commands
  don't even use it; it just has to exist as the pair to the public key.)
- Upload **`com.tesla.3p.public-key.pem`** into your repo at this exact path:
  `.well-known/appspecific/com.tesla.3p.public-key.pem`
- Verify it's live by opening this URL in a browser — it should show the key text:
  `https://YOURUSERNAME.github.io/.well-known/appspecific/com.tesla.3p.public-key.pem`

### 3. Create your Tesla developer app

1. Go to https://developer.tesla.com and create an application.
2. Fill in:
   - **Allowed origin / domain:** `YOURUSERNAME.github.io`
   - **Redirect URI:** `https://YOURUSERNAME.github.io/`
   - **Scopes:** check **Energy Product Information** (`energy_device_data`) and
     **Energy Product Commands** (`energy_cmds`).
   - **Grant types:** Authorization Code **and** Client Credentials.
   - **Region:** North America.
3. When done you'll get a **Client ID** and **Client Secret** — copy both somewhere safe.

### 4. Set environment variables for the helper

In your terminal (same place as step 2), set these (Git Bash / macOS / Linux syntax):

```bash
export TESLA_CLIENT_ID="your-client-id"
export TESLA_CLIENT_SECRET="your-client-secret"
export TESLA_DOMAIN="YOURUSERNAME.github.io"
export TESLA_REDIRECT_URI="https://YOURUSERNAME.github.io/"
pip install -r requirements.txt
```

### 5. Register your domain with Tesla (one time)

```bash
python tesla_auth.py register
```

A `200` / success response means Tesla can see your public key. (If it fails, double-check
the public-key URL from step 2 loads in a browser.)

### 6. Get your refresh token

```bash
python tesla_auth.py login
```

Follow the prompts: open the printed URL, log in with your Tesla account, approve, then
paste the full redirected URL back. It prints your **refresh token** — copy it.

> No Python locally? Open your repo on GitHub → **Code ▸ Codespaces ▸ Create codespace**.
> That gives you a free browser terminal where steps 2, 4, 5, 6, 7 all work.

### 7. Find your energy site ID

```bash
export TESLA_REFRESH_TOKEN="the-token-from-step-6"
python tesla_auth.py sites
```

Copy the `energy_site_id` it prints.

### 8. Add your secrets to GitHub

In your repo: **Settings → Secrets and variables → Actions → New repository secret.**
Add these three:

| Secret name | Value |
|---|---|
| `TESLA_CLIENT_ID` | your Client ID |
| `TESLA_REFRESH_TOKEN` | the refresh token from step 6 |
| `TESLA_ENERGY_SITE_ID` | the site ID from step 7 |

**Optional but recommended** — auto-renew the token so it never expires:
create a **fine-grained Personal Access Token** (GitHub → Settings → Developer settings →
Fine-grained tokens) scoped to this one repo with **Secrets: Read and write**, and add it
as a secret named `GH_PAT`. Without it, you may need to repeat step 6 every ~3 months.

### 9. Turn it on and test

1. In your repo go to the **Actions** tab and enable workflows if prompted.
2. Open **"Powerwall evening export" → Run workflow** (the `workflow_dispatch` button)
   to test. It prints today's peak hour and the reserve it would use. Outside the peak/restore
   hour it logs *"standing by"* — that's expected. To preview any date without touching the
   battery, run locally: `DRY_RUN=1 TEST_DATE=2026-08-21 python powerwall_evening_export.py`.

That's it — it now runs every day on its own.

---

## Changing the behavior

Edit the values (or add them as env overrides in `.github/workflows/export.yml`):

| Setting | Default | Meaning |
|---|---|---|
| `RATE_CSV` | `data/export_rates.csv` | path to your locked export-rate schedule |
| `MIN_EXPORT_RATE` | `0.20` | if today's peak is below this, do nothing (off-season) |
| `NIGHT_RESERVE` | `5` | overnight floor — how far the house can draw the saved charge |
| `RESTORE_HOUR` | `22` | hour (local) to switch to overnight self-use |
| `PEAK_WINDOW_START` / `_END` | `14` / `20` | hours the peak is allowed to fall in |
| `EXPORT_LEAD_HOURS` | `1` | arm the export this many hours before the peak (lag buffer) |
| `DAY_MODE` | `self_consumption` | mode to return to (Self-Powered) |
| `LOCAL_TZ` | `America/Los_Angeles` | your timezone |

The dynamic reserve tiers live near the top of `powerwall_evening_export.py` (`RESERVE_TIERS`) — edit those to change how aggressive it is.

---

## Troubleshooting

**It runs but doesn't actually export to the grid.** The script set the right toggles,
but Time-Based Control only sells when it sees the evening as high-value. Open the Tesla
app → check your **utility rate plan / tariff** is the correct NBT25 schedule with the
evening marked as peak. If TBC still under-exports, the next step is the *tariff trick*
(programmatically setting the import price low in the 6–9pm window) — ask and I'll add a
`set_tariff` module to the script.

**`register` fails.** Your public key URL (step 2) isn't loading. Confirm the `.nojekyll`
file exists and the key is at exactly `.well-known/appspecific/com.tesla.3p.public-key.pem`.

**Runs fail after a few months with an auth error.** Your refresh token expired. Re-do
step 6 and update the `TESLA_REFRESH_TOKEN` secret — or set up `GH_PAT` (step 8) to avoid this.

**Wrong hour fires.** GitHub's scheduled runs can be delayed 5–30 min at peak load; the
crons run at :40 (an off-peak minute) and the export arms an hour early, so delays don't
eat into the paid window.

**Runs stop entirely after ~2 months.** GitHub disables scheduled workflows in public
repos after 60 days without repo activity. The workflow's keepalive step resets that
timer on every run, so this shouldn't happen — but if Actions shows the workflow
disabled, just re-enable it from the Actions tab.

---

## Safety notes

- No secrets are stored in code — only as encrypted GitHub Actions secrets.
- The script never moves money or changes anything except mode, reserve, and export rule.
- Worst case if it breaks: your Powerwall stays on whatever setting it last received.
  Re-run the RESTORE phase manually (Actions → Run workflow during your 9pm hour) or just
  fix it in the Tesla app.
