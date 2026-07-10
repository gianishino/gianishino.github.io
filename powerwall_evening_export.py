#!/usr/bin/env python3
"""
Powerwall CSV-driven evening-export automation (Tesla Fleet API).

Each run, the script:
  1. Reads your locked SCE NBT export-rate CSV (data/export_rates.csv).
  2. Looks up today's date -> month + weekday/weekend -> the 24 hourly export rates.
  3. Finds the single HIGHEST export hour in the afternoon/evening window (the "peak").
  4. Picks a backup reserve based on how high that peak is (dynamic / aggressive when
     rates are huge, e.g. August):
        peak >= $0.80  -> keep only 10%   (sell almost everything)
        peak >= $0.40  -> keep 20%
        peak >= $0.20  -> keep 30%
        peak <  $0.20  -> OFF-SEASON, do nothing
  5. In the hour BEFORE the peak: switch to Time-Based Control + Export Everything +
     that reserve. Triggering early absorbs two real-world lags -- GitHub's cron delay
     (5-30 min at busy times) and the Powerwall's own ramp-up after a mode change (TBC
     re-plans on its own schedule; it doesn't dump the instant settings land). The
     peak-hour run re-sends the same commands as a free retry (idempotent).
  6. ~2 hours after the peak (when the dump is long done; RESTORE_HOUR is the upper
     bound, plus an after-midnight catch-up run): back to Self-Powered + Solar-only +
     small night reserve, so the charge you kept powers the house through the evening
     and overnight instead of importing peak-priced grid power. The catch-up judges the
     season by YESTERDAY, so the season's last export day still gets restored even if
     the evening runs were dropped.

Because a Powerwall 3 (11.5 kW) empties 100%->reserve in well under ~2 hours, the dump
lands in basically one hourly price bucket -- so we target that single peak hour.

DST-proof: it decides everything from LOCAL time (America/Los_Angeles); the workflow
fires it hourly across the evening and every run that isn't the peak/restore hour is a
safe no-op. Self-adjusting through 2034 as your locked rates escalate.

Config comes from environment variables (GitHub Actions secrets/vars). See README.md.
Set DRY_RUN=1 (and optionally TEST_DATE=YYYY-MM-DD) to print the plan without calling Tesla.
"""

import base64
import csv
import datetime
import os
import sys

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    print("Python 3.9+ required (needs zoneinfo).")
    sys.exit(1)

import requests

# ---- config ----
API_BASE = os.environ.get("TESLA_API_BASE", "https://fleet-api.prd.na.vn.cloud.tesla.com")
LOCAL_TZ = os.environ.get("LOCAL_TZ", "America/Los_Angeles")
RATE_CSV = os.environ.get("RATE_CSV", "data/export_rates.csv")

PEAK_WINDOW_START = int(os.environ.get("PEAK_WINDOW_START", "14"))  # earliest hour to treat as the peak
PEAK_WINDOW_END = int(os.environ.get("PEAK_WINDOW_END", "20"))      # latest hour (inclusive)
RESTORE_HOUR = int(os.environ.get("RESTORE_HOUR", "22"))            # switch back for overnight self-use
NIGHT_RESERVE = int(os.environ.get("NIGHT_RESERVE", "5"))
DAY_MODE = os.environ.get("DAY_MODE", "self_consumption")
MIN_EXPORT_RATE = float(os.environ.get("MIN_EXPORT_RATE", "0.20"))  # below this, off-season -> skip
EXPORT_LEAD_HOURS = int(os.environ.get("EXPORT_LEAD_HOURS", "1"))   # also trigger this many hours before the peak
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

# Dynamic reserve tiers, checked high -> low: (min peak $/kWh, reserve % to KEEP)
RESERVE_TIERS = [(0.80, 10), (0.40, 20), (0.20, 30)]

AUTH_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"


# ---- rate lookup ----
def now_local():
    base = datetime.datetime.now(ZoneInfo(LOCAL_TZ))
    td = os.environ.get("TEST_DATE")  # for dry-run testing of other dates
    if td:
        y, m, d = map(int, td.split("-"))
        base = base.replace(year=y, month=m, day=d)
    th = os.environ.get("TEST_HOUR")  # for dry-run testing of other hours
    if th:
        base = base.replace(hour=int(th))
    return base


def todays_curve(date):
    month = date.strftime("%b")  # 'Jan'..'Dec'
    daytype = "Weekday" if date.weekday() < 5 else "Weekend/Holiday"
    curve = {}
    with open(RATE_CSV, newline="") as f:
        for r in csv.DictReader(f):
            if r["Year"] == str(date.year) and r["Month"] == month and r["DayType"] == daytype:
                curve[int(r["HourStart_Pacific"])] = float(r["ExportRate_USD_per_kWh"])
    if not curve:
        raise SystemExit(f"No rates for {date.year} {month} {daytype} in {RATE_CSV} "
                         f"(check the file is present and covers this year).")
    return curve, month, daytype


def pick_peak(curve):
    cand = {h: v for h, v in curve.items() if PEAK_WINDOW_START <= h <= PEAK_WINDOW_END}
    peak_hour = max(cand, key=cand.get)
    return peak_hour, cand[peak_hour]


def reserve_for(rate):
    for threshold, reserve in RESERVE_TIERS:
        if rate >= threshold:
            return reserve
    return None


# ---- auth ----
def get_access_token():
    client_id = os.environ.get("TESLA_CLIENT_ID")
    refresh = os.environ.get("TESLA_REFRESH_TOKEN")
    if not (client_id and refresh):
        raise SystemExit("Missing TESLA_CLIENT_ID / TESLA_REFRESH_TOKEN.")
    resp = requests.post(AUTH_URL, data={
        "grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != refresh:
        maybe_update_refresh_secret(new_refresh)
    return data["access_token"]


def maybe_update_refresh_secret(new_refresh):
    pat = os.environ.get("GH_PAT")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (pat and repo):
        print("NOTE: refresh token rotated; set GH_PAT to auto-save it, else re-run the auth helper if runs fail.")
        return
    try:
        from nacl import encoding, public
    except ImportError:
        print("NOTE: pynacl not installed; cannot auto-update the secret.")
        return
    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
    key = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
                       headers=headers, timeout=30).json()
    pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    sealed = base64.b64encode(public.SealedBox(pk).encrypt(new_refresh.encode())).decode()
    r = requests.put(f"https://api.github.com/repos/{repo}/actions/secrets/TESLA_REFRESH_TOKEN",
                     headers=headers, json={"encrypted_value": sealed, "key_id": key["key_id"]}, timeout=30)
    print(f"Updated TESLA_REFRESH_TOKEN secret -> HTTP {r.status_code}")


# ---- energy-site commands ----
def _post(token, endpoint, payload, quiet=False):
    site_id = os.environ["TESLA_ENERGY_SITE_ID"]
    url = f"{API_BASE}/api/1/energy_sites/{site_id}/{endpoint}"
    r = requests.post(url, headers={"Authorization": f"Bearer {token}",
                                    "Content-Type": "application/json"}, json=payload, timeout=30)
    shown = "(tariff json)" if quiet else payload
    print(f"POST {endpoint} {shown} -> {r.status_code} {r.text[:200]}")
    r.raise_for_status()
    return r.json()


# ---- tariff ("the tariff trick") ----
# TBC decides WHETHER to export from the utility rate plan in the Tesla app, which is a
# flat-ish approximation — so it often sees no hour worth dumping for. We push a real
# tariff (today's actual NBT export credits) right before arming, so the optimizer
# genuinely wants to sell at the peak. Restore pushes a sane static plan back.
def _tariff(name, periods, buy_rates, sell_rates):
    """periods: {NAME: [(fromHour, toHour), ...]}; rates: {NAME: $/kWh}"""
    tou = {pname: {"periods": [
                {"fromDayOfWeek": 0, "toDayOfWeek": 6,
                 "fromHour": fh, "toHour": th, "fromMinute": 0, "toMinute": 0}
                for fh, th in spans]}
           for pname, spans in periods.items()}
    seasons = {"Year": {"fromMonth": 1, "toMonth": 12, "fromDay": 1, "toDay": 31,
                        "tou_periods": tou}}
    def charges(rates):
        return {"ALL": {"rates": {"ALL": 0}}, "Year": {"rates": rates}}
    base = {
        "version": 1, "utility": "Southern California Edison", "code": "SCE-NBT25-DYN",
        "name": name, "currency": "USD",
        "monthly_minimum_bill": 0, "min_applicable_demand": 0, "max_applicable_demand": 0,
        "monthly_charges": 0, "daily_charges": [{"name": "Charge", "amount": 0}],
        "daily_demand_charges": {},
        "demand_charges": {"ALL": {"rates": {"ALL": 0}}, "Year": {"rates": {}}},
        "energy_charges": charges(buy_rates), "seasons": seasons,
    }
    base["sell_tariff"] = {
        "utility": "Southern California Edison", "code": "", "currency": "", "name": name,
        "monthly_minimum_bill": 0, "min_applicable_demand": 0, "max_applicable_demand": 0,
        "monthly_charges": 0, "daily_charges": [{"name": "Charge", "amount": 0}],
        "daily_demand_charges": {},
        "demand_charges": {"ALL": {"rates": {"ALL": 0}}, "Year": {"rates": {}}},
        "energy_charges": charges(sell_rates), "seasons": seasons,
    }
    return base


def export_tariff(curve, peak_hour):
    """Today-specific tariff: real sell prices, peaked at today's best hour."""
    arm = max(0, peak_hour - EXPORT_LEAD_HOURS)
    end = min(23, peak_hour + 2)
    peak_rate = curve[peak_hour]
    shoulder_rate = curve.get(peak_hour - 1, peak_rate * 0.9)
    periods = {"ON_PEAK": [(peak_hour, end)],
               "SHOULDER": [(arm, peak_hour)],
               "OFF_PEAK": [(0, arm), (end, 0)]}
    # Tesla requires buy >= sell in every period.
    buy = {"ON_PEAK": round(max(0.45, peak_rate), 5), "SHOULDER": round(max(0.45, shoulder_rate), 5),
           "OFF_PEAK": 0.24}
    sell = {"ON_PEAK": round(peak_rate, 5), "SHOULDER": round(shoulder_rate, 5), "OFF_PEAK": 0.05}
    return _tariff("SCE NBT25 (export day)", periods, buy, sell)


def standard_tariff():
    """Static everyday plan: SCE TOU 4-9pm peak buy, realistic small sell credit."""
    periods = {"ON_PEAK": [(16, 21)], "OFF_PEAK": [(0, 16), (21, 0)]}
    return _tariff("SCE NBT25 (standard)", periods,
                   {"ON_PEAK": 0.40, "OFF_PEAK": 0.24},
                   {"ON_PEAK": 0.06, "OFF_PEAK": 0.05})


def set_tariff(token, tariff):
    _post(token, "time_of_use_settings", {"tou_settings": {"tariff_content_v2": tariff}},
          quiet=True)
    print(f"TARIFF set: {tariff['name']}.")


def export_phase(token, reserve, curve, peak_hour):
    set_tariff(token, export_tariff(curve, peak_hour))                    # make TBC WANT to sell
    _post(token, "operation", {"default_real_mode": "autonomous"})        # Time-Based Control
    _post(token, "grid_import_export", {"customer_preferred_export_rule": "battery_ok"})  # Export Everything
    _post(token, "backup", {"backup_reserve_percent": reserve})           # sell down to here
    print(f"EXPORT done: tariff + TBC + Export Everything, reserve={reserve}%.")


def restore_phase(token):
    _post(token, "backup", {"backup_reserve_percent": NIGHT_RESERVE})
    _post(token, "grid_import_export", {"customer_preferred_export_rule": "pv_only"})
    _post(token, "operation", {"default_real_mode": DAY_MODE})
    set_tariff(token, standard_tariff())
    print(f"RESTORE done: {DAY_MODE} + Solar-only, reserve={NIGHT_RESERVE}%, standard tariff.")


def main():
    now = now_local()
    curve, month, daytype = todays_curve(now.date())
    peak_hour, peak_rate = pick_peak(curve)
    print(f"{now.isoformat()} | {month} {daytype} | today's peak: {peak_hour}:00 @ ${peak_rate:.3f}/kWh")

    # --- restore window (plus the after-midnight catch-up run) ---
    # Dynamic: the dump is long done ~2h after the peak, so switch back then instead of
    # holding TBC all evening (which made the house import peak-priced grid power while
    # charge sat locked behind the export reserve). RESTORE_HOUR remains the upper bound.
    # Judged by the day the export could have happened: after midnight that's YESTERDAY.
    # In the off-season we never touch the battery (so manual app settings stick).
    restore_from = min(RESTORE_HOUR, peak_hour + 2)
    if now.hour >= restore_from or now.hour < 6:
        ref_date = now.date() - datetime.timedelta(days=1) if now.hour < 6 else now.date()
        if ref_date != now.date():
            ref_curve, _, _ = todays_curve(ref_date)
            _, ref_rate = pick_peak(ref_curve)
        else:
            ref_rate = peak_rate
        if ref_rate < MIN_EXPORT_RATE:
            print(f"Restore window, but ${ref_rate:.3f} peak means no export ran -> nothing to restore.")
            return
        if DRY_RUN:
            print("DRY_RUN: would RESTORE now (Self-Powered + Solar-only).")
            return
        restore_phase(get_access_token())
        return

    if peak_rate < MIN_EXPORT_RATE:
        print(f"Peak ${peak_rate:.3f} < MIN_EXPORT_RATE ${MIN_EXPORT_RATE:.2f} -> off-season, no action.")
        return

    reserve = reserve_for(peak_rate)
    if reserve is None:
        print(f"No reserve tier matches ${peak_rate:.3f} -> no action.")
        return
    print(f"Plan: arm export from {peak_hour - EXPORT_LEAD_HOURS}:00 (peak {peak_hour}:00) "
          f"down to {reserve}% reserve; restore from {min(RESTORE_HOUR, peak_hour + 2)}:00.")

    # Arm in the hour before the peak (absorbs GitHub cron delay + Powerwall/TBC ramp-up);
    # the peak-hour run re-sends the same idempotent commands as a retry.
    if peak_hour - EXPORT_LEAD_HOURS <= now.hour <= peak_hour:
        if DRY_RUN:
            print("DRY_RUN: would EXPORT now (tariff + TBC + Export Everything).")
            return
        export_phase(get_access_token(), reserve, curve, peak_hour)
    else:
        print(f"Hour {now.hour}: standing by (arm at {peak_hour - EXPORT_LEAD_HOURS}, "
              f"restore from {min(RESTORE_HOUR, peak_hour + 2)}).")


if __name__ == "__main__":
    main()
