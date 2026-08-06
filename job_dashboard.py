#!/usr/bin/env python3
"""
JOB DASHBOARD — single-file version
====================================
Pulls entry-level jobs from Adzuna, RemoteOK, and Greenhouse/Lever boards,
filters out senior roles, dedupes against what you've already seen, and
writes dashboard.html.

SETUP (one time):
  1. Put this file in a folder by itself, e.g. ~/job-dashboard/
  2. In that folder:
         python3 -m venv venv
         source venv/bin/activate          (Windows: venv\\Scripts\\activate)
         pip install requests
  3. Set your Adzuna credentials as environment variables:
         export ADZUNA_APP_ID="your_id"
         export ADZUNA_APP_KEY="your_key"
     (Windows PowerShell: $env:ADZUNA_APP_ID="your_id")
     Keeping them out of this file means you can safely push it to GitHub,
     which you'll need to do for Claude Code Routines.
  4. Run it:
         python job_dashboard.py
  5. Open dashboard.html in your browser.

Creates seen_jobs.json automatically so tomorrow only shows NEW listings.
"""

import json
import os
import re
import time
from pathlib import Path
from datetime import datetime

try:
    import requests
except ImportError:
    raise SystemExit("Missing dependency. Run:  pip install requests")


# =============================================================================
# YOUR SETTINGS — edit this section only
# =============================================================================

# --- Adzuna credentials: read from environment, never hardcoded ---
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")

# --- Where you want to work ---
LOCATIONS = [
    "San Francisco, CA",
    "New York, NY",
    "Los Angeles, CA",
]

# --- What you want to do ---
KEYWORD_GROUPS = {
    "AI / Agentforce": [
        "AI Agent Developer",
        "Agentforce Developer",
        "AI Solutions Engineer",
        "Forward Deployed Engineer",
        "AI Implementation Analyst",
        "Enterprise AI Analyst",
    ],
    "Data / Analytics": [
        "Data Analyst",
        "Business Analyst",
        "Commercial Analytics Analyst",
        "Pricing Analyst",
        "Demand Planning Analyst",
        "Revenue Operations Analyst",
        "Business Intelligence Analyst",
    ],
    "Strategy / GTM": [
        "GTM Analyst",
        "Strategy and Operations Analyst",
        "Sales Strategy Analyst",
        "BizOps Analyst",
        "Go To Market Analyst",
        "Commercial Operations Analyst",
    ],
    "Consulting / PM": [
        "Associate Product Manager",
        "Management Consulting Analyst",
        "Project Manager",
        "Business Operations Associate",
    ],
    "Pharma / Healthcare": [
        "Market Access Analyst",
        "Life Sciences Analyst",
        "Healthcare Data Analyst",
        "Commercial Analytics Pharma",
    ],
}

# --- Seniority filter (regex, matched against lowercased title) ---
# Word-boundary anchored so "sr" doesn't match "srings" and "ii" catches
# "Analyst II" regardless of trailing punctuation.
SENIORITY_EXCLUDE = [
    r"\bsenior\b", r"\bsr\.?\b", r"\bstaff\b", r"\bprincipal\b",
    r"\bdirector\b", r"\bvp\b", r"\bvice president\b", r"\bhead of\b",
    r"\bchief\b", r"\bexecutive\b", r"\bpresident\b", r"\blead\b",
    r"\bi{2,3}\b",          # Analyst II / III
    r"\blevel\s*[2-9]\b",   # Level 2, Level 3
    r"\bl[2-9]\b",          # L2, L3
]

# "Manager" usually means too senior, EXCEPT for these entry-level titles
# that happen to contain the word — all of which are on your target list.
MANAGER_ALLOWED = [
    "associate product manager",
    "project manager",
    "assistant manager",
    "program manager",
]

# --- Positive entry-level signals (used to filter RemoteOK's feed) ---
ENTRY_LEVEL_HINTS = [
    "entry", "associate", "analyst", "junior", "jr.", "new grad", "graduate",
]

# --- Company boards to check (no global search exists for these platforms) ---
# Find a token by visiting boards.greenhouse.io/<token> or jobs.lever.co/<token>
# Salesforce has no public Greenhouse board — add real tokens as you find them.
# Verify one works by opening boards.greenhouse.io/<token> in your browser first.
GREENHOUSE_COMPANIES = []
LEVER_COMPANIES = []

# --- How many jobs to show per day ---
# The rest stay queued and surface on following days, best-first.
MAX_JOBS_PER_DAY = 10

# --- Location preference (higher = shown sooner) ---
LOCATION_PRIORITY = {
    "san francisco": 3,
    "new york": 3,
    "los angeles": 2,
    "remote": 1,
}

# --- How many Adzuna results per keyword+location combo ---
RESULTS_PER_PAGE = 20

# =============================================================================
# END OF SETTINGS — you shouldn't need to edit below here
# =============================================================================

HERE = Path(__file__).parent
SEEN_PATH = HERE / "seen_jobs.json"
DASHBOARD_PATH = HERE / "dashboard.html"

ALL_KEYWORDS = [kw for group in KEYWORD_GROUPS.values() for kw in group]
KEYWORD_TO_GROUP = {kw: name for name, kws in KEYWORD_GROUPS.items() for kw in kws}


def _infer_group(title):
    """Best-guess keyword group for sources that don't tell us the search term."""
    tl = title.lower()
    for name, kws in KEYWORD_GROUPS.items():
        if any(kw.lower() in tl for kw in kws):
            return name
    return "Other"


def _city_of(job):
    ll = job.get("location", "").lower()
    for city in LOCATION_PRIORITY:
        if city in ll:
            return city
    return "other"


def is_entry_level(title):
    """True if the title looks entry-level (i.e. should be kept)."""
    tl = title.lower()
    for pattern in SENIORITY_EXCLUDE:
        if re.search(pattern, tl):
            return False
    if re.search(r"\bmanagers?\b", tl):
        if not any(ok in tl for ok in MANAGER_ALLOWED):
            return False
    return True


def select_diverse(ranked_jobs, limit):
    """
    Pick `limit` jobs, round-robining across (keyword group, city) buckets so
    the dashboard shows variety instead of ten of the same title in one city.
    Falls back to plain best-first if there isn't enough variety to fill the day.
    """
    from collections import defaultdict, OrderedDict

    buckets = OrderedDict()
    for job in ranked_jobs:  # already sorted best-first
        key = (job.get("group", "Other"), _city_of(job))
        buckets.setdefault(key, []).append(job)

    picked, seen_titles = [], defaultdict(int)
    # Round-robin: one from each bucket per pass
    while len(picked) < limit and any(buckets.values()):
        progressed = False
        for key in list(buckets.keys()):
            if len(picked) >= limit:
                break
            queue = buckets[key]
            while queue:
                candidate = queue.pop(0)
                title_key = candidate["title"].lower().strip()
                # Cap repeats of the exact same title across the whole day
                if seen_titles[title_key] >= 2:
                    continue
                seen_titles[title_key] += 1
                picked.append(candidate)
                progressed = True
                break
        if not progressed:
            break

    # If variety constraints left us short, top up with the best remaining
    if len(picked) < limit:
        already = {id(j) for j in picked}
        for job in ranked_jobs:
            if len(picked) >= limit:
                break
            if id(job) not in already:
                picked.append(job)

    return picked[:limit]


def relevance_score(job):
    """Higher = shown sooner. Used to pick the daily top N."""
    score = 0
    tl = job["title"].lower().strip()

    # Title match quality: exact beats partial
    for kw in ALL_KEYWORDS:
        kwl = kw.lower()
        if tl == kwl:
            score += 50
            break
        if kwl in tl:
            score += 30
            break
    else:
        score += 5  # matched loosely via the API's own relevance

    # Explicit entry-level signals
    if any(h in tl for h in ENTRY_LEVEL_HINTS):
        score += 15

    # Location preference
    ll = job.get("location", "").lower()
    for city, pts in LOCATION_PRIORITY.items():
        if city in ll:
            score += pts * 5
            break

    return score


# ---------------------------------------------------------------- Adzuna ----
def fetch_adzuna():
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        print("  [adzuna] ADZUNA_APP_ID / ADZUNA_APP_KEY not set in environment "
              "— skipping. See SETUP at the top of this file.")
        return []

    base = "https://api.adzuna.com/v1/api/jobs/us/search/1"
    jobs = []
    for location in LOCATIONS:
        for keyword in ALL_KEYWORDS:
            params = {
                "app_id": ADZUNA_APP_ID,
                "app_key": ADZUNA_APP_KEY,
                "results_per_page": RESULTS_PER_PAGE,
                "what": keyword,
                "where": location,
                "content-type": "application/json",
            }
            try:
                resp = requests.get(base, params=params, timeout=15)
            except requests.RequestException as e:
                print(f"  [adzuna] {keyword} @ {location} -> {e}")
                continue

            if resp.status_code == 429:
                print("  [adzuna] Rate limited — stopping Adzuna early. "
                      "Trim KEYWORD_GROUPS if this keeps happening.")
                return jobs
            if resp.status_code != 200:
                print(f"  [adzuna] {keyword} @ {location} -> HTTP {resp.status_code}")
                continue

            for item in resp.json().get("results", []):
                jobs.append({
                    "source": "Adzuna",
                    "title": (item.get("title") or "").strip(),
                    "company": (item.get("company") or {}).get("display_name", "Unknown"),
                    "location": (item.get("location") or {}).get("display_name", location),
                    "url": item.get("redirect_url", ""),
                    "id": str(item.get("id") or item.get("redirect_url", "")),
                    "group": KEYWORD_TO_GROUP.get(keyword, "Other"),
                    "matched_keyword": keyword,
                })
            time.sleep(1.0)  # be polite to the free tier
    return jobs


# -------------------------------------------------------------- RemoteOK ----
def fetch_remoteok():
    try:
        resp = requests.get(
            "https://remoteok.com/api",
            headers={"User-Agent": "job-dashboard-script (personal use)"},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  [remoteok] {e}")
        return []

    if resp.status_code != 200:
        print(f"  [remoteok] HTTP {resp.status_code}")
        return []

    try:
        listings = resp.json()
    except ValueError:
        return []

    listings = [i for i in listings if isinstance(i, dict) and i.get("id")]
    kws = [k.lower() for k in ALL_KEYWORDS]
    hints = [h.lower() for h in ENTRY_LEVEL_HINTS]

    jobs = []
    for item in listings:
        title = (item.get("position") or "").strip()
        if not title:
            continue
        tl = title.lower()
        if not (any(k in tl for k in kws) and any(h in tl for h in hints)):
            continue
        jobs.append({
            "source": "RemoteOK",
            "title": title,
            "company": item.get("company", "Unknown"),
            "location": "Remote (US)",
            "url": item.get("url") or f"https://remoteok.com/remote-jobs/{item.get('id')}",
            "id": str(item.get("id")),
            "group": _infer_group(title),
        })
    return jobs


# ----------------------------------------------------- Greenhouse / Lever ----
def _location_match(loc_lower):
    cities = [l.lower().split(",")[0] for l in LOCATIONS]
    return any(c in loc_lower for c in cities) or "remote" in loc_lower


def fetch_greenhouse(token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    try:
        resp = requests.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"  [greenhouse:{token}] {e}")
        return []
    if resp.status_code != 200:
        print(f"  [greenhouse:{token}] HTTP {resp.status_code}")
        return []

    kws = [k.lower() for k in ALL_KEYWORDS]
    jobs = []
    for item in resp.json().get("jobs", []):
        title = item.get("title", "")
        loc = (item.get("location") or {}).get("name", "")
        if not (any(k in title.lower() for k in kws) and _location_match(loc.lower())):
            continue
        jobs.append({
            "source": f"Greenhouse ({token})",
            "title": title,
            "company": token.title(),
            "location": loc or "Unspecified",
            "group": _infer_group(title),
            "url": item.get("absolute_url", ""),
            "id": str(item.get("id")),
        })
    return jobs


def fetch_lever(token):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    try:
        resp = requests.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"  [lever:{token}] {e}")
        return []
    if resp.status_code != 200:
        print(f"  [lever:{token}] HTTP {resp.status_code}")
        return []

    kws = [k.lower() for k in ALL_KEYWORDS]
    jobs = []
    for item in resp.json():
        title = item.get("text", "")
        loc = (item.get("categories") or {}).get("location", "")
        if not (any(k in title.lower() for k in kws) and _location_match(loc.lower())):
            continue
        jobs.append({
            "source": f"Lever ({token})",
            "title": title,
            "company": token.title(),
            "location": loc or "Unspecified",
            "group": _infer_group(title),
            "url": item.get("hostedUrl", ""),
            "id": str(item.get("id")),
        })
    return jobs


# ------------------------------------------------------------- dashboard ----
def build_dashboard(jobs):
    if jobs:
        rows = "".join(
            f"<tr><td>{j['title']}</td><td>{j['company']}</td>"
            f"<td>{j['location']}</td><td><span class=\"tag\">{j.get('group','—')}</span></td>"
            f"<td><a href=\"{j['url']}\" target=\"_blank\" rel=\"noopener\">Open &rarr;</a></td></tr>"
            for j in jobs
        )
        body = ("<table><tr><th>Title</th><th>Company</th><th>Location</th>"
                "<th>Category</th><th>Apply</th></tr>" + rows + "</table>")
    else:
        body = '<div class="empty">No new matching jobs today.</div>'

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Job Dashboard — {datetime.now():%b %d, %Y}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:920px;
margin:40px auto;padding:0 20px;color:#1a1a1a}}
h1{{font-size:22px;margin-bottom:4px}}
.meta{{color:#666;margin-bottom:24px;font-size:14px}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:10px 8px;border-bottom:1px solid #eee;font-size:14px}}
th{{color:#666;font-weight:600}}
tr:hover td{{background:#fafafa}}
a{{color:#c15f3c;text-decoration:none;font-weight:600}}
a:hover{{text-decoration:underline}}
.empty{{color:#888;padding:48px 0;text-align:center}}
.tag{{background:#f0ede8;color:#5a544c;padding:2px 8px;border-radius:10px;font-size:12px;white-space:nowrap}}
</style></head><body>
<h1>New jobs — {datetime.now():%A, %B %d, %Y}</h1>
<div class="meta">{len(jobs)} new listing(s) since last run</div>
{body}
</body></html>"""
    DASHBOARD_PATH.write_text(html, encoding="utf-8")


# ------------------------------------------------------------------ main ----
def main():
    print("Fetching Adzuna...")
    adzuna = fetch_adzuna()
    print(f"  -> {len(adzuna)} results")

    print("Fetching RemoteOK...")
    remoteok = fetch_remoteok()
    print(f"  -> {len(remoteok)} results")

    if GREENHOUSE_COMPANIES or LEVER_COMPANIES:
        print("Fetching company boards...")
        boards = []
        for t in GREENHOUSE_COMPANIES:
            boards += fetch_greenhouse(t)
        for t in LEVER_COMPANIES:
            boards += fetch_lever(t)
        print(f"  -> {len(boards)} results")
    else:
        boards = []

    all_jobs = adzuna + remoteok + boards

    entry_level = [j for j in all_jobs if is_entry_level(j["title"])]
    dropped_senior = len(all_jobs) - len(entry_level)

    # Drop anything already shown on a previous day
    seen = set(json.loads(SEEN_PATH.read_text())) if SEEN_PATH.exists() else set()
    unseen, keys_this_run = [], set()
    for j in entry_level:
        key = (f"{j['source']}:{j['id']}" if j.get("id")
               else f"{j['source']}:{j['title']}:{j['company']}")
        if key in seen or key in keys_this_run:
            continue
        keys_this_run.add(key)
        j["_key"] = key
        unseen.append(j)

    # Rank, then spread the daily batch across keyword groups and cities so
    # one popular title in one city can't sweep every slot.
    unseen.sort(key=relevance_score, reverse=True)
    todays_jobs = select_diverse(unseen, MAX_JOBS_PER_DAY)
    backlog = len(unseen) - len(todays_jobs)

    build_dashboard(todays_jobs)

    # Only mark what you actually saw
    shown_keys = {j["_key"] for j in todays_jobs}
    SEEN_PATH.write_text(json.dumps(sorted(seen | shown_keys), indent=2))

    print(f"\n{len(all_jobs)} fetched | {dropped_senior} too senior | "
          f"{len(entry_level) - len(unseen)} already seen")
    print(f"Showing {len(todays_jobs)} today | {backlog} queued for tomorrow")
    print(f"Open: {DASHBOARD_PATH}")


if __name__ == "__main__":
    main()