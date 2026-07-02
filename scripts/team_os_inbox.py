#!/usr/bin/env python3
"""team_os_inbox.py — MJ's ONE screen: "what needs me, right now, in plain words."

Pulls every ticket waiting on MJ (Needs-MJ / Blocked / Approved-not-Done) across
all projects and renders a human-language decision list — no jargon, no JSON.
The CLI answer to "⚡ My Decisions". Cron-able to DM a daily/▶ on-demand digest.

Usage: python3 team_os_inbox.py [--plain]   (--plain = no ANSI, for Telegram)
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

PLAIN = "--plain" in sys.argv
B = "" if PLAIN else "\033[1m"
Y = "" if PLAIN else "\033[93m"
G = "" if PLAIN else "\033[92m"
R = "" if PLAIN else "\033[91m"
N = "" if PLAIN else "\033[0m"

# lanes that mean "MJ must act", with what the action IS
ACTION = {
    "Needs-MJ": "approve / reject the consequential step",
    "Blocked": "answer a question / grant access — agent is waiting",
    "Approved": "(should auto-close; if stuck here, Integrator didn't finish)",
}


def _key() -> str:
    k = os.environ.get("LINEAR_API_KEY", "").strip()
    if k:
        return k
    for line in open(Path("~/.hermes/.env").expanduser(), errors="ignore"):
        if line.startswith("LINEAR_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def gql(q: str) -> dict:
    req = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=json.dumps({"query": q}).encode(),
        headers={"Authorization": _key(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"_err": str(e)}


def humanize(title: str, desc: str) -> str:
    """One plain-language line: strip ticket prefixes, take the intent."""
    t = re.sub(r"^AGENTS-\d+[: ]*", "", title)
    t = re.sub(r"\bfollow-up:\s*", "", t, flags=re.I)
    return t.strip()[:90]


def main() -> int:
    states = '","'.join(ACTION)
    q = f'''query {{ issues(first:100, filter:{{ state:{{ name:{{ in:["{states}"]}} }} }}) {{
      nodes {{ identifier title priority url
        state {{ name }} project {{ name }}
        assignee {{ name }} }} }} }}'''
    data = gql(q)
    if "_err" in data:
        print(f"  (Linear unreachable: {data['_err']})")
        return 1
    nodes = data.get("data", {}).get("issues", {}).get("nodes", [])
    # group by lane, urgent first
    buckets: dict[str, list] = {k: [] for k in ACTION}
    for n in nodes:
        s = (n.get("state") or {}).get("name", "")
        if s in buckets:
            buckets[s].append(n)
    total = sum(len(v) for v in buckets.values())

    print(f"\n{B}⚡ MJ — WHAT NEEDS YOU{N}   ({total} item{'s' if total != 1 else ''})")
    if not total:
        print(f"  {G}✓ nothing waiting — the machine is handling everything.{N}\n")
        return 0
    for lane, items in buckets.items():
        if not items:
            continue
        items.sort(key=lambda x: x.get("priority") or 5)
        head = {"Needs-MJ": f"{Y}APPROVE / REJECT{N}", "Blocked": f"{R}ANSWER (agent waiting){N}",
                "Approved": f"{G}should be closing{N}"}[lane]
        print(f"\n  {head} — {ACTION[lane]}")
        for n in items:
            urg = "🔴 " if (n.get("priority") or 5) <= 1 else ""
            proj = (n.get("project") or {}).get("name", "")
            print(f"    {urg}{n['identifier']}  {humanize(n['title'], '')}")
            print(f"        {proj} · {n['url']}")
    print(f"\n  {B}How to act:{N} in Linear move the card → Approved / Rejected, or comment a question.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
