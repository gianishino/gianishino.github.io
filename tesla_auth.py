#!/usr/bin/env python3
"""
One-time Tesla Fleet API setup helper. Run this LOCALLY (on your laptop) once,
during initial setup. You do NOT need it again unless your refresh token expires.

Usage (set env vars first - see README.md):
    python tesla_auth.py register   # one-time: register your domain with Tesla
    python tesla_auth.py login      # get your refresh token (browser login)
    python tesla_auth.py sites      # list your energy site IDs

Required env vars:
    TESLA_CLIENT_ID       from developer.tesla.com
    TESLA_CLIENT_SECRET   from developer.tesla.com
    TESLA_DOMAIN          the domain hosting your public key (e.g. yourname.github.io)
    TESLA_REDIRECT_URI    a redirect URL registered on your app (e.g. https://yourname.github.io/)
    TESLA_API_BASE        optional, default https://fleet-api.prd.na.vn.cloud.tesla.com (North America)
"""

import os
import sys
import urllib.parse

import requests

CLIENT_ID = os.environ.get("TESLA_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TESLA_CLIENT_SECRET")
DOMAIN = os.environ.get("TESLA_DOMAIN")
REDIRECT_URI = os.environ.get("TESLA_REDIRECT_URI")
API_BASE = os.environ.get("TESLA_API_BASE", "https://fleet-api.prd.na.vn.cloud.tesla.com")

AUTH_BASE = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3"
SCOPES = "openid offline_access energy_device_data energy_cmds"


def _require(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)


def partner_token():
    """client_credentials token, used only for partner-account registration."""
    r = requests.post(
        f"{AUTH_BASE}/token",
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": SCOPES,
            "audience": API_BASE,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def cmd_register():
    """One-time: tell Tesla which domain hosts your public key."""
    _require("TESLA_CLIENT_ID", "TESLA_CLIENT_SECRET", "TESLA_DOMAIN")
    token = partner_token()
    r = requests.post(
        f"{API_BASE}/api/1/partner_accounts",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"domain": DOMAIN},
        timeout=30,
    )
    print(f"register -> HTTP {r.status_code}\n{r.text}")


def cmd_login():
    """Browser login -> exchange the code for a refresh token."""
    _require("TESLA_CLIENT_ID", "TESLA_CLIENT_SECRET", "TESLA_REDIRECT_URI")
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": "powerwall-setup",
    }
    url = f"{AUTH_BASE}/authorize?" + urllib.parse.urlencode(params)
    print("\n1) Open this URL in your browser and log in with your Tesla account:\n")
    print(url)
    print("\n2) After approving, your browser will redirect to your redirect URL.")
    print("   Copy the ENTIRE address bar URL (it contains ...?code=XXXX...) and paste it here.\n")
    redirected = input("Paste the full redirected URL: ").strip()
    qs = urllib.parse.urlparse(redirected).query
    code = urllib.parse.parse_qs(qs).get("code", [None])[0]
    if not code:
        print("Could not find ?code= in that URL.")
        sys.exit(1)
    r = requests.post(
        f"{AUTH_BASE}/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "audience": API_BASE,
        },
        timeout=30,
    )
    r.raise_for_status()
    tok = r.json()
    print("\n=== SUCCESS ===")
    print("Save this as the GitHub secret  TESLA_REFRESH_TOKEN :\n")
    print(tok["refresh_token"])
    print("\n(Keep it secret. Now run:  python tesla_auth.py sites )")


def cmd_sites():
    """List energy sites so you can grab your TESLA_ENERGY_SITE_ID."""
    _require("TESLA_CLIENT_ID", "TESLA_CLIENT_SECRET")
    # uses a refresh token if present, else falls back to partner token (read scope may be limited)
    refresh = os.environ.get("TESLA_REFRESH_TOKEN")
    if refresh:
        r = requests.post(
            f"{AUTH_BASE}/token",
            data={"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh},
            timeout=30,
        )
        r.raise_for_status()
        token = r.json()["access_token"]
    else:
        print("Set TESLA_REFRESH_TOKEN first (run 'login').")
        sys.exit(1)
    r = requests.get(
        f"{API_BASE}/api/1/products",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    print("Your products (look for energy_site_id):\n")
    for p in r.json().get("response", []):
        sid = p.get("energy_site_id")
        name = p.get("site_name") or p.get("resource_type")
        if sid:
            print(f"  energy_site_id = {sid}   ({name})")


if __name__ == "__main__":
    cmds = {"register": cmd_register, "login": cmd_login, "sites": cmd_sites}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        print(__doc__)
        sys.exit(1)
    cmds[sys.argv[1]]()
