#!/usr/bin/env python3
"""
intake_app.py — Page d'intake sécurisée pour collecter une clé API Linear
et lancer le scan, sans que la clé transite jamais par email.

Lancer en local :
    pip install -r requirements.txt
    python3 intake_app.py
    # puis ouvrir http://localhost:5000/scan/demo-token

En production (Render) :
    gunicorn intake_app:app --timeout 300 --workers 1

--- PROPRIÉTÉS DE SÉCURITÉ ---
1. La clé n'arrive JAMAIS par email : le founder la saisit sur cette page,
   en HTTPS (chiffré en transit).
2. La clé n'est JAMAIS écrite sur disque ni dans les logs : elle vit en
   mémoire le temps du scan, et n'est transmise à aucun tiers.
3. Seul le résultat agrégé (les 8 métriques) quitte cette application.
   Aucun contenu de ticket, aucune clé.
4. La clé demandée est en lecture seule (scope Read) : même en cas de fuite,
   elle ne peut rien modifier. Et le founder la révoque après le scan.
"""

import os
import logging
from datetime import datetime, timezone

import requests
from flask import Flask, request, render_template_string, abort

from linear_estimate import run_scan  # on réutilise EXACTEMENT le même scan

app = Flask(__name__)

# On coupe le log par défaut de Flask sur les requêtes, par prudence :
# on ne veut prendre AUCUN risque de journaliser un corps de requête.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# URL du webhook n8n qui écrit dans le Google Sheet.
# Définie comme variable d'environnement sur Render — JAMAIS en dur ici.
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "").strip()

# ---------------------------------------------------------------------------
# JETONS PROSPECTS — une ligne par entreprise à qui tu envoies un lien.
# Le lien à envoyer est :  https://<ton-app>.onrender.com/scan/<jeton>
# Choisis des jetons longs et non devinables.
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
    """Transforme le JSON imbriqué en 17 colonnes plates, dans l'ordre
    exact des en-têtes du Google Sheet."""
    v = result.get("volume", {})
    w = result.get("workflow_active", {})
    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "company": company,
        "token": token,
        "teams": v.get("teams"),
        "projects": v.get("projects"),
        "tickets": v.get("tickets"),
        "comments": v.get("comments"),
        "first_ticket": w.get("first_ticket"),
        "months_active": w.get("months"),
        "pct_with_comments": result.get("pct_tickets_with_comments"),
        "median_comments": result.get("median_comments_per_ticket"),
        "pct_solved": result.get("pct_tickets_solved"),
        "pct_tools": result.get("pct_tickets_mentioning_tools"),
        "characters": result.get("text_characters"),
        "tokens_est": result.get("text_tokens_estimate"),
        "golden_tickets": result.get("golden_tickets"),
        "note": result.get("_note", ""),
    }


def push_to_sheet(row):
    """Envoie la ligne aplatie au webhook n8n, qui l'écrit dans le Sheet.
    Un échec ici n'invalide pas le scan : le founder voit quand même son
    résultat, et la ligne peut être rejouée à la main."""
    if not N8N_WEBHOOK_URL:
        print("[warn] N8N_WEBHOOK_URL non définie — rien envoyé au Sheet.")
        return False
    try:
        r = requests.post(N8N_WEBHOOK_URL, json=row, timeout=20)
        r.raise_for_status()
        print(f"[ok] Ligne envoyée au Sheet pour {row['company']}.")
        return True
    except Exception as e:
        print(f"[erreur] Envoi au Sheet échoué : {type(e).__name__}")
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

    # POST : la clé arrive ici, en mémoire uniquement.
    api_key = request.form.get("api_key", "").strip()
    if not api_key:
        return render_template_string(PAGE, company=company,
                                      result=None, error="Clé manquante.")
    try:
        result = run_scan(api_key)          # utilisation en mémoire
    except Exception as e:                   # on n'expose pas la clé dans l'erreur
        return render_template_string(PAGE, company=company, result=None,
                                      error=str(e)[:200])
    finally:
        api_key = None                       # on jette la clé, explicitement

    # Seuls les agrégats quittent l'application.
    row = flatten(result, company, token)
    push_to_sheet(row)

    return render_template_string(PAGE, company=company,
                                  result=True, error=None)


if __name__ == "__main__":
    # En local seulement. En prod : gunicorn derrière HTTPS.
    app.run(host="127.0.0.1", port=5000, debug=False)
