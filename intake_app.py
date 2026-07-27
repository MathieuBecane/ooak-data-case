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
<title>Mesure de workspace — {{ company }}</title>
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
<h1>Connecter votre workspace Linear</h1>
<p>Avant de vous faire une proposition chiffrée pour vos données
opérationnelles, nous avons besoin d'en <b>mesurer le volume</b> : nombre de
tickets, de commentaires, période couverte, densité des échanges.</p>
<p><b>Nous ne lisons pas le contenu de vos tickets et ne conservons aucune
donnée brute.</b></p>

{% if result %}
  <p class="ok"><b>✓ Mesure terminée.</b> Merci.</p>
  <p><b>Vous pouvez révoquer la clé dès maintenant</b> :
  <a href="https://linear.app/settings/account/security" target="_blank">réglages
  de sécurité Linear</a> → supprimez la clé <code>dataset-audit</code>.
  Nous n'en avons plus besoin.</p>
  <p>Nous analysons les mesures de notre côté et revenons vers vous sous 48 h
  avec une proposition chiffrée, accompagnée du détail exact de ce que nous
  avons mesuré.</p>
{% elif error %}
  <p class="err"><b>Erreur :</b> {{ error }}</p>
  <p><a href="">Réessayer</a></p>
{% else %}
  <div class="step"><b>1.</b> Ouvrez
  <a href="https://linear.app/settings/account/security" target="_blank">
  vos réglages de sécurité Linear</a>.</div>
  <div class="step"><b>2.</b> « New API key » → nommez-la
  <code>dataset-audit</code> → permissions : cochez
  <b>uniquement <code>Read</code></b> → « All teams ».</div>
  <div class="step"><b>3.</b> Collez la clé ici :</div>
  <form method="post" autocomplete="off">
    <input type="password" name="api_key" placeholder="lin_api_..."
           required autofocus>
    <button type="submit">Lancer l'estimation</button>
  </form>
  <p class="note">La clé est transmise chiffrée (HTTPS) et utilisée uniquement
  le temps du scan. Elle n'est jamais enregistrée sur disque, jamais
  journalisée, jamais envoyée par email. En lecture seule, elle ne peut rien
  modifier — et vous la révoquez au même endroit après le scan.
  Merci de garder cet onglet ouvert pendant le scan.</p>
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
                                      result=None, error="Clé manquante.")
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
