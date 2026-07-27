#!/usr/bin/env python3
"""
intake_app.py — Page d'intake sécurisée pour collecter une clé API Linear
et lancer le scan, sans que la clé transite jamais par email.

Lancer :
    pip install flask requests
    python3 intake_app.py
    # puis ouvrir http://localhost:5000/scan/demo-token

En production : déployer derrière HTTPS (obligatoire) sur un hébergeur
serverless ou un petit conteneur. Le "token" dans l'URL est un identifiant
opaque, un par prospect, qui relie le scan au CRM.

--- PROPRIÉTÉS DE SÉCURITÉ (le cœur du sujet) ---
1. La clé n'arrive JAMAIS par email : le founder la saisit sur cette page,
   en HTTPS (chiffré en transit).
2. La clé n'est JAMAIS écrite sur disque ni dans les logs : elle vit en
   mémoire le temps du scan, puis disparaît (variable locale, garbage collectée).
3. Seul le résultat agrégé (le JSON des 8 métriques) est conservé.
   Aucun contenu des tickets, aucune clé.
4. La clé demandée est en lecture seule (scope Read) : même en cas de fuite,
   elle ne peut rien modifier. Et le founder la révoque après le scan.
"""

import os
import json
import logging

from flask import Flask, request, render_template_string, abort

from linear_estimate import run_scan  # on réutilise EXACTEMENT le même scan

app = Flask(__name__)

# On coupe le log par défaut de Flask sur les requêtes, par prudence :
# on ne veut prendre AUCUN risque de journaliser un corps de requête.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# Où déposer les résultats agrégés (jamais la clé).
RESULTS_DIR = os.environ.get("RESULTS_DIR", "./results")
os.makedirs(RESULTS_DIR, exist_ok=True)

# Jetons prospects valides (en prod : base de données / CRM).
VALID_TOKENS = {"demo-token": "Ooak Data"}

PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Estimation de dataset — {{ company }}</title>
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
<h1>Estimer votre dataset Linear</h1>
<p>Pour chiffrer votre dataset, nous mesurons le volume de votre workspace
(nombre de tickets, commentaires, période). <b>Nous ne lisons pas le contenu
et ne stockons aucune donnée brute.</b> ~2 minutes.</p>

{% if result %}
  <p class="ok"><b>✓ Scan terminé.</b> Merci, vous pouvez révoquer la clé
  maintenant. Voici ce que nous avons mesuré :</p>
  <pre style="background:#f7f7f7;padding:1rem;border-radius:6px;overflow:auto">{{ result }}</pre>
{% elif error %}
  <p class="err"><b>Erreur :</b> {{ error }}</p>
  <p><a href="">Réessayer</a></p>
{% else %}
  <div class="step"><b>1.</b> Ouvrez
  <a href="https://linear.app/settings/account/security" target="_blank">
  vos réglages de sécurité Linear</a>.</div>
  <div class="step"><b>2.</b> « New API key » → nommez-la
  <code>{{ company }} export</code> → permissions : cochez
  <b>uniquement <code>Read</code></b> → « All teams ».</div>
  <div class="step"><b>3.</b> Collez la clé ici :</div>
  <form method="post" autocomplete="off">
    <input type="password" name="api_key" placeholder="lin_api_..."
           required autofocus>
    <button type="submit">Lancer l'estimation</button>
  </form>
  <p class="note">La clé est transmise chiffrée (HTTPS), utilisée le temps du
  scan puis effacée. Elle n'est jamais enregistrée ni envoyée par email.
  En lecture seule, elle ne peut rien modifier — et vous la révoquez au même
  endroit après le scan.</p>
{% endif %}
</body></html>
"""


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

    # On ne persiste QUE le résultat agrégé, indexé par le jeton prospect.
    result["_company"] = company
    path = os.path.join(RESULTS_DIR, f"estimate_{token}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    return render_template_string(
        PAGE, company=company,
        result=json.dumps(result, indent=2, ensure_ascii=False), error=None)


if __name__ == "__main__":
    # En local seulement. En prod : serveur WSGI derrière HTTPS.
    app.run(host="127.0.0.1", port=5000, debug=False)
