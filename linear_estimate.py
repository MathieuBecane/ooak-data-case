#!/usr/bin/env python3
"""
linear_estimate.py — Extracts volume and structure metrics from a Linear
workspace in order to price a dataset.

Two ways to use it:
  1. From the command line:
       LINEAR_API_KEY="lin_api_xxx" python3 linear_estimate.py
  2. As a module:
       from linear_estimate import run_scan
       result = run_scan("lin_api_xxx")   # returns a dict

The key only needs the "Read" scope. The script never writes.
No ticket text is ever written to disk: we read, we count, we discard.

Metrics returned:
  1. Volume: teams, projects, tickets, comments
  2. How long the workspace has been active (months / years)
  3. Share of tickets carrying at least one comment
  4. Share of resolved tickets
  5. Share of tickets referencing another tool
  6. Total character count (titles, descriptions, comments)
  7. Absolute number of golden tickets

A golden ticket meets all four conditions:
  - description longer than 200 characters
  - at least 3 comments
  - at least 2 distinct participants (creator + assignee + comment authors)
  - resolved, and reached through at least 2 state transitions
"""

import os
import re
import sys
import json
import time
from collections import Counter
from datetime import datetime, timezone

import requests

API = "https://api.linear.app/graphql"
PAGE = 25  # tickets per request, keeps the GraphQL complexity budget in check

TOOL_DOMAINS = (
    "slack.com", "github.com", "gitlab.com", "notion.so", "figma.com",
    "docs.google.com", "drive.google.com", "sheets.google.com",
    "atlassian.net", "jira", "sentry.io", "zendesk.com", "intercom.com",
    "loom.com", "vercel.com", "stripe.com",
)
URL_RE = re.compile(r"https?://[^\s)]+")

# Tickets Linear creates automatically in a fresh workspace. They contain
# links to other tools and would otherwise inflate the cross-tool metric.
DEFAULT_ONBOARDING = {
    "Get familiar with Linear", "Connect your tools",
    "Import your data", "Set up your teams",
}

Q_CONTEXT = """
query {
  organization { name urlKey }
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
    """Single GraphQL call with retry on rate limiting."""
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
    raise RuntimeError("Too many retries (rate limited).")


def _mentions_tool(issue):
    """True when the ticket links out to another tool, through a native
    Linear integration, an attachment URL, or a link in the text."""
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
    """A ticket worth turning into an RL task: substantial, discussed by
    several people, and carried through to resolution."""
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
    """Reusable core: takes a key, returns the metrics dict.
    The key is used here only, in memory, and is never logged."""
    headers = {"Authorization": api_key, "Content-Type": "application/json"}

    ctx = _gql(headers, Q_CONTEXT)
    org = ctx.get("organization") or {}
    n_teams = len(ctx["teams"]["nodes"])
    projects = ctx["projects"]["nodes"]
    n_projects = len(projects)

    # Walk every page of issues until the cursor runs out.
    issues, after = [], None
    while True:
        conn = _gql(headers, Q_ISSUES, {"after": after, "n": PAGE})["issues"]
        issues.extend(conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            break
        after = conn["pageInfo"]["endCursor"]

    n = len(issues)
    if n == 0:
        raise RuntimeError("Empty workspace, or key without access.")

    comment_counts, dates = [], []
    actors = set()
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
            if c.get("user"):
                actors.add(c["user"]["id"])
        if it.get("creator"):
            actors.add(it["creator"]["id"])
        if it.get("assignee"):
            actors.add(it["assignee"]["id"])
        if it.get("createdAt"):
            dates.append(it["createdAt"])

    first = min(dates)
    first_dt = datetime.fromisoformat(first.replace("Z", "+00:00"))
    months = (datetime.now(timezone.utc) - first_dt).days / 30.44
    pct = lambda x: round(100 * x / n, 1)

    result = {
        "organization": {"name": org.get("name"),
                         "url_key": org.get("urlKey")},
        "volume": {"teams": n_teams, "projects": n_projects,
                   "tickets": n, "comments": n_comments},
        "workflow_active": {"first_ticket": first[:10],
                            "months": round(months, 1),
                            "years": round(months / 12, 1)},
        "pct_tickets_with_comments": pct(n_with),
        "pct_tickets_solved": pct(n_solved),
        "pct_tickets_mentioning_tools": pct(n_tool),
        "text_characters": chars,
        "golden_tickets": n_golden,
    }

    # ---- Qualification flags -------------------------------------------
    # Each rule is informational: it should start a conversation with the
    # prospect, not silently discount the price.
    notes = []

    # 1. Linear's own onboarding tickets inflate the cross-tool metric.
    if n_default:
        notes.append(
            f"{n_default} default Linear onboarding ticket(s): inflates "
            "pct_tickets_mentioning_tools.")

    # 2. Visibility: a single visible team may mean the key cannot reach
    #    private teams. Every figure is then a floor, not a total.
    if n_teams <= 1:
        notes.append(
            "Only one team visible: check with the contact whether private "
            "teams exist beyond the reach of this key.")

    # 3. Too few actors means there is no collaboration to reconstruct.
    if len(actors) <= 2:
        notes.append(
            f"Only {len(actors)} distinct actor(s): near-solo workspace, "
            "little value for multi-actor tasks.")

    # 4. History too short to yield realistic trajectories.
    if months < 6:
        notes.append(
            f"Workspace active for {round(months, 1)} months only: history too "
            "short for long trajectories.")

    # 5. Volume below the point where processing pays for itself.
    if n < 50:
        notes.append(
            f"Only {n} tickets: volume below the exploitation threshold.")

    # 6. The most disqualifying signal: volume, but nothing usable.
    if n >= 200 and n_golden == 0:
        notes.append(
            "High volume but no golden ticket: Linear used as a to-do list "
            "rather than a collaboration surface.")

    # 7. The conversation happens elsewhere (Slack, meetings).
    if n_with / n < 0.15:
        notes.append(
            f"{pct(n_with)}% of tickets carry comments: discussion most likely "
            "happens outside Linear.")

    # 8. Resolution floor: without completed tickets there is no full
    #    trajectory to replay, hence no derivable RL task.
    if n_solved / n < 0.10:
        notes.append(
            f"{pct(n_solved)}% of tickets resolved, below the 10% floor: too "
            "few completed trajectories to build tasks from.")

    # 9. Bulk import: tickets created on the same day come from a migration
    #    (Jira, Asana) and usually arrive stripped of their history.
    if n >= 20 and dates:
        by_day = Counter(d[:10] for d in dates)
        day, cnt = by_day.most_common(1)[0]
        if cnt / n > 0.5:
            notes.append(
                f"{round(100 * cnt / n)}% of tickets created on {day}: likely "
                "bulk import, collaboration history missing.")

    if notes:
        result["_note"] = " | ".join(notes)
    return result


def main():
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        sys.exit("Set LINEAR_API_KEY in the environment.")
    print("Scanning...")
    r = run_scan(key)
    with open("linear_estimate.json", "w", encoding="utf-8") as f:
        json.dump(r, f, indent=2, ensure_ascii=False)

    v = r["volume"]
    w = r["workflow_active"]
    print("=" * 55)
    print(f"0. Organisation ......... {r['organization']['name']}")
    print(f"1. Volume ............... {v['teams']} teams, {v['projects']} projects, "
          f"{v['tickets']} tickets, {v['comments']} comments")
    print(f"2. Active for ........... {w['months']} months ({w['years']} years)")
    print(f"3. % with a comment ..... {r['pct_tickets_with_comments']} %")
    print(f"4. % resolved ........... {r['pct_tickets_solved']} %")
    print(f"5. % referencing a tool . {r['pct_tickets_mentioning_tools']} %")
    print(f"6. Characters ........... {r['text_characters']:,}".replace(",", " "))
    print(f"7. Golden tickets ....... {r['golden_tickets']}")
    if r.get("_note"):
        print(f"\nFlags: {r['_note']}")
    print("\nJSON written to linear_estimate.json")


if __name__ == "__main__":
    main()
