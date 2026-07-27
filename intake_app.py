#!/usr/bin/env python3
"""
intake_app.py — Secure intake page that collects a Linear API key and runs
the scan, so the key never travels by email.

Run locally:
    pip install -r requirements.txt
    python3 intake_app.py
    # then open http://localhost:5000/scan/demo-token

In production (Render):
    gunicorn intake_app:app --timeout 300 --workers 1

--- SECURITY PROPERTIES ---
1. The key NEVER arrives by email: the founder types it into this page,
   over HTTPS, so it is encrypted in transit.
2. The key is NEVER written to disk or to any log: it lives in memory for
   the duration of the scan and is passed to no third party.
3. Only the aggregated result leaves this application. No ticket content,
   no key.
4. The key is read-only (Read scope): even if leaked it cannot modify
   anything, and the founder revokes it right after the scan.
"""

import os
import logging
from datetime import datetime, timezone

import requests
from flask import Flask, request, render_template_string, abort

from linear_estimate import run_scan  # on réutilise EXACTEMENT le même scan

app = Flask(__name__)

# Silence Flask's default request logging, out of caution: we want zero
# chance of a request body ending up in the logs.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# URL of the n8n webhook that appends to the Google Sheet.
# Set as an environment variable on Render — NEVER hard-coded here.
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "").strip()

# ---------------------------------------------------------------------------
# PROSPECT TOKENS — one line per company you send a link to.
# The link to send is:  https://<your-app>.onrender.com/scan/<token>
# Use long, unguessable tokens.
# ---------------------------------------------------------------------------
VALID_TOKENS = {
    "demo-token": "Démo",
    "ooak-a7f3k9": "Ooak Data",
}

PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workspace measurement — {{ company }}</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:560px;margin:3rem auto;
      padding:0 1rem;color:#1a1a1a;line-height:1.55}
 h1{font-size:1.4rem} .step{margin:1.2rem 0}
 code{background:#f2f2f2;padding:.1rem .3rem;border-radius:3px}
 input{width:100%;padding:.7rem;font-size:1rem;border:1px solid #ccc;
       border-radius:6px;box-sizing:border-box}
 button{margin-top:1rem;padding:.7rem 1.4rem;font-size:1rem;border:0;
        border-radius:6px;background:#5e6ad2;color:#fff;cursor:pointer}
 .note{font-size:.85rem;color:#666;margin-top:1.5rem;border-top:1px solid #eee;
       padding-top:1rem} .ok{color:#137333} .err{color:#c00}
 a{color:#5e6ad2}
</style></head><body>
<h1>Connect your Linear workspace</h1>
<p>Before we can price your operational data, we need to <b>measure the
volume</b>: how many tickets and comments, over what period, and how much
discussion they carry.</p>
<p><b>We do not read the content of your tickets, and we keep no raw data.</b></p>

{% if result %}
  <p class="ok"><b>✓ Measurement complete.</b> Thank you.</p>
  <p><b>You can revoke the key right now</b>:
  <a href="https://linear.app/settings/account/security" target="_blank">Linear
  security settings</a> → delete the key named <code>dataset-audit</code>.
  We no longer need it.</p>
  <p>We will review the measurements on our side and come back to you within
  48 hours with a price, along with the full detail of what we measured.</p>
{% elif error %}
  <p class="err"><b>Error:</b> {{ error }}</p>
  <p><a href="">Try again</a></p>
{% else %}
  <div class="step"><b>1.</b> Open your
  <a href="https://linear.app/settings/account/security" target="_blank">Linear
  security settings</a>.</div>
  <div class="step"><b>2.</b> Click "New API key" → name it
  <code>dataset-audit</code> → under permissions, tick
  <b><code>Read</code> only</b> → "All teams".</div>
  <div class="step"><b>3.</b> Paste the key here:</div>
  <form method="post" autocomplete="off">
    <input type="password" name="api_key" placeholder="lin_api_..."
           required autofocus>
    <button type="submit">Run the measurement</button>
  </form>
  <p class="note">The key is sent encrypted (HTTPS) and used only for the
  duration of the scan. It is never written to disk, never logged, never sent
  by email. Being read-only, it cannot modify anything &mdash; and you revoke it
  in the same place once the scan is done.
  Please keep this tab open while the scan runs.</p>
{% endif %}
</body></html>
"""


def flatten(result, company, token):
    """Flattens the nested result into 15 columns, in the exact order of the
    Google Sheet headers.
    The real Linear organisation name wins; the label attached to the token
    is only a fallback when the API returns nothing."""
    v = result.get("volume", {})
    w = result.get("workflow_active", {})
    org_name = (result.get("organization") or {}).get("name")
    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "company": org_name or company,
        "token": token,
        "teams": v.get("teams"),
        "projects": v.get("projects"),
        "tickets": v.get("tickets"),
        "comments": v.get("comments"),
        "first_ticket": w.get("first_ticket"),
        "months_active": w.get("months"),
        "pct_with_comments": result.get("pct_tickets_with_comments"),
        "pct_solved": result.get("pct_tickets_solved"),
        "pct_tools": result.get("pct_tickets_mentioning_tools"),
        "characters": result.get("text_characters"),
        "golden_tickets": result.get("golden_tickets"),
        "note": result.get("_note", ""),
    }


def push_to_sheet(row):
    """Sends the flattened row to the n8n webhook, which writes it to the
    Sheet. A failure here does not invalidate the scan: the founder still
    gets their confirmation, and the row can be replayed by hand."""
    if not N8N_WEBHOOK_URL:
        print("[warn] N8N_WEBHOOK_URL not set - nothing sent to the Sheet.")
        return False
    try:
        r = requests.post(N8N_WEBHOOK_URL, json=row, timeout=20)
        r.raise_for_status()
        print(f"[ok] Row sent to the Sheet for {row['company']}.")
        return True
    except Exception as e:
        print(f"[error] Sending to the Sheet failed: {type(e).__name__}")
        return False


@app.route("/")
def home():
    return "OK", 200


@app.route("/scan/<token>", methods=["GET", "POST"])
def scan(token):
    company = VALID_TOKENS.get(token)
    if not company:
        abort(404)

    if request.method == "GET":
        return render_template_string(PAGE, company=company,
                                      result=None, error=None)

    # POST: the key arrives here, in memory only.
    api_key = request.form.get("api_key", "").strip()
    if not api_key:
        return render_template_string(PAGE, company=company,
                                      result=None, error="Missing key.")
    try:
        result = run_scan(api_key)          # used in memory
    except Exception as e:                   # never surface the key in an error
        return render_template_string(PAGE, company=company, result=None,
                                      error=str(e)[:200])
    finally:
        api_key = None                       # drop the key, explicitly

    # Only the aggregates leave the application.
    row = flatten(result, company, token)
    push_to_sheet(row)

    return render_template_string(PAGE, company=company,
                                  result=True, error=None)


if __name__ == "__main__":
    # Local only. In production: gunicorn behind HTTPS.
    app.run(host="127.0.0.1", port=5000, debug=False)
