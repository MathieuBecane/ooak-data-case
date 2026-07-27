#!/usr/bin/env python3
"""
linear_estimate.py — Extrait 8 métriques d'un workspace Linear pour estimer
la valeur d'un dataset.

Deux usages :
  1. En ligne de commande :
       LINEAR_API_KEY="lin_api_xxx" python3 linear_estimate.py
  2. Comme module (import) :
       from linear_estimate import run_scan
       result = run_scan("lin_api_xxx")   # renvoie un dict

La clé doit avoir le scope "Read" uniquement. Le script ne fait que lire.
Aucun contenu texte n'est écrit sur disque : on lit, on compte, on jette.

Les 8 métriques :
  1. Volume : équipes, projets, tickets, commentaires
  2. Workflow actif depuis (mois / années)
  3. % de tickets avec au moins un commentaire
  4. Nombre médian de commentaires par ticket
  5. % de tickets résolus
  6. % de tickets mentionnant un autre outil
  7. Nombre de caractères de texte (titres, descriptions, commentaires)
  8. Nombre absolu de golden tickets

Golden ticket = les 4 conditions :
  - description > 200 caractères
  - >= 3 commentaires
  - >= 2 participants distincts (créateur + assigné + auteurs de commentaires)
  - dernier statut = résolu ET au moins 2 transitions de statut
"""

import os
import re
import sys
import json
import time
import statistics
from datetime import datetime, timezone

import requests

API = "https://api.linear.app/graphql"
PAGE = 25  # tickets par requête (limite la complexité GraphQL)

TOOL_DOMAINS = (
    "slack.com", "github.com", "gitlab.com", "notion.so", "figma.com",
    "docs.google.com", "drive.google.com", "sheets.google.com",
    "atlassian.net", "jira", "sentry.io", "zendesk.com", "intercom.com",
    "loom.com", "vercel.com", "stripe.com",
)
URL_RE = re.compile(r"https?://[^\s)]+")

DEFAULT_ONBOARDING = {
    "Get familiar with Linear", "Connect your tools",
    "Import your data", "Set up your teams",
}

Q_CONTEXT = """
query {
  teams(first: 250) { nodes { id } }
  projects(first: 250, includeArchived: true) {
    nodes { id name description }
  }
}
"""

Q_ISSUES = """
query($after: String, $n: Int!) {
  issues(first: $n, after: $after, includeArchived: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      title
      description
      createdAt
      completedAt
      creator { id }
      assignee { id }
      comments(first: 100) { nodes { body user { id } } }
      attachments(first: 50) { nodes { sourceType url } }
      history(first: 100) { nodes { toState { id } } }
    }
  }
}
"""


def _gql(headers, query, variables=None, tries=5):
    for _ in range(tries):
        r = requests.post(API, headers=headers,
                          json={"query": query, "variables": variables or {}},
                          timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 30)))
            continue
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise RuntimeError(str(data["errors"])[:400])
        return data["data"]
    raise RuntimeError("Trop de tentatives (rate limit).")


def _mentions_tool(issue):
    for a in issue.get("attachments", {}).get("nodes", []):
        src = (a.get("sourceType") or "").lower()
        if src and src not in ("file", "link"):
            return True
        for dom in TOOL_DOMAINS:
            if dom in (a.get("url") or ""):
                return True
    blob = issue.get("description") or ""
    for c in issue.get("comments", {}).get("nodes", []):
        blob += " " + (c.get("body") or "")
    return any(dom in url for url in URL_RE.findall(blob) for dom in TOOL_DOMAINS)


def _is_golden(issue):
    desc = issue.get("description") or ""
    comments = issue.get("comments", {}).get("nodes", [])
    if len(desc) <= 200:
        return False
    if len(comments) < 3:
        return False
    participants = set()
    if issue.get("creator"):
        participants.add(issue["creator"]["id"])
    if issue.get("assignee"):
        participants.add(issue["assignee"]["id"])
    for c in comments:
        if c.get("user"):
            participants.add(c["user"]["id"])
    if len(participants) < 2:
        return False
    if not issue.get("completedAt"):
        return False
    transitions = sum(
        1 for h in issue.get("history", {}).get("nodes", []) if h.get("toState"))
    return transitions >= 2


def run_scan(api_key):
    """Cœur réutilisable : prend une clé, renvoie le dict des 8 métriques.
    La clé n'est utilisée qu'ici, en mémoire, et n'est jamais journalisée."""
    headers = {"Authorization": api_key, "Content-Type": "application/json"}

    ctx = _gql(headers, Q_CONTEXT)
    n_teams = len(ctx["teams"]["nodes"])
    projects = ctx["projects"]["nodes"]
    n_projects = len(projects)

    issues, after = [], None
    while True:
        conn = _gql(headers, Q_ISSUES, {"after": after, "n": PAGE})["issues"]
        issues.extend(conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            break
        after = conn["pageInfo"]["endCursor"]

    n = len(issues)
    if n == 0:
        raise RuntimeError("Workspace vide ou clé sans accès.")

    comment_counts, dates = [], []
    n_comments = n_with = n_solved = n_tool = n_golden = n_default = chars = 0

    for p in projects:
        chars += len(p.get("name") or "") + len(p.get("description") or "")

    for it in issues:
        title = it.get("title") or ""
        desc = it.get("description") or ""
        comments = it.get("comments", {}).get("nodes", [])
        nc = len(comments)
        comment_counts.append(nc)
        n_comments += nc
        if nc:
            n_with += 1
        if it.get("completedAt"):
            n_solved += 1
        if _mentions_tool(it):
            n_tool += 1
        if _is_golden(it):
            n_golden += 1
        if title in DEFAULT_ONBOARDING:
            n_default += 1
        chars += len(title) + len(desc)
        for c in comments:
            chars += len(c.get("body") or "")
        if it.get("createdAt"):
            dates.append(it["createdAt"])

    first = min(dates)
    first_dt = datetime.fromisoformat(first.replace("Z", "+00:00"))
    months = (datetime.now(timezone.utc) - first_dt).days / 30.44
    pct = lambda x: round(100 * x / n, 1)

    result = {
        "volume": {"teams": n_teams, "projects": n_projects,
                   "tickets": n, "comments": n_comments},
        "workflow_active": {"first_ticket": first[:10],
                            "months": round(months, 1),
                            "years": round(months / 12, 1)},
        "pct_tickets_with_comments": pct(n_with),
        "median_comments_per_ticket": statistics.median(comment_counts),
        "pct_tickets_solved": pct(n_solved),
        "pct_tickets_mentioning_tools": pct(n_tool),
        "text_characters": chars,
        "text_tokens_estimate": round(chars / 4),
        "golden_tickets": n_golden,
    }
    if n_default:
        result["_note"] = (
            f"{n_default} ticket(s) d'onboarding Linear par défaut : gonflent "
            "pct_tickets_mentioning_tools. À exclure sur un vrai workspace.")
    return result


def main():
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        sys.exit("Définis LINEAR_API_KEY dans l'environnement.")
    print("Extraction en cours...")
    r = run_scan(key)
    with open("linear_estimate.json", "w", encoding="utf-8") as f:
        json.dump(r, f, indent=2, ensure_ascii=False)

    v = r["volume"]
    w = r["workflow_active"]
    print("=" * 55)
    print(f"1. Volume ............ {v['teams']} équipes, {v['projects']} projets, "
          f"{v['tickets']} tickets, {v['comments']} commentaires")
    print(f"2. Actif depuis ...... {w['months']} mois ({w['years']} ans)")
    print(f"3. % avec commentaire  {r['pct_tickets_with_comments']} %")
    print(f"4. Médiane comm./ticket {r['median_comments_per_ticket']}")
    print(f"5. % résolus ......... {r['pct_tickets_solved']} %")
    print(f"6. % mentionnant outil {r['pct_tickets_mentioning_tools']} %")
    print(f"7. Caractères ........ {r['text_characters']:,} "
          f"(~{r['text_tokens_estimate']:,} tokens)".replace(",", " "))
    print(f"8. Golden tickets .... {r['golden_tickets']}")
    print("\nJSON écrit dans linear_estimate.json")


if __name__ == "__main__":
    main()
