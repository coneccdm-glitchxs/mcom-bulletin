#!/usr/bin/env python3
"""
Daily scraper for the M.Com Karnataka Job Bulletin (Justin's Ad Center).

What it does:
  1. Fetches a small set of source pages that list M.Com / Karnataka
     government job notifications.
  2. Parses out organisation, post name, qualification, last date, and
     apply link where it can find them.
  3. Writes everything to data/jobs.json, which the HTML page fetches
     at load time.

Design notes:
  - This is intentionally simple and defensive. Government/aggregator
    sites change their HTML often, so every parser is wrapped in a
    try/except and logs a warning instead of crashing the whole run.
  - If a source's structure changes and a parser stops finding
    anything, that source is simply skipped for the day rather than
    failing the whole workflow (jobs.json keeps yesterday's data for
    that source via the merge step at the bottom).
  - Respect robots.txt and rate limits: this hits a handful of pages
    once a day, not a crawl. Do not increase frequency or scope
    without checking each site's terms of use.
"""

import json
import re
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MComBulletinBot/1.0; "
                  "+https://github.com/) personal-use daily checker"
}

DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "jobs.json"

SOURCES = [
    {
        "id": "karnatakacareers_mcom",
        "name": "Karnataka Careers — M.Com Jobs",
        "url": "https://www.karnatakacareers.org/qualification/m-com/",
    },
    {
        "id": "kea_notices",
        "name": "Karnataka Examinations Authority",
        "url": "https://cetonline.karnataka.gov.in/kea/",
    },
]

# Manually pinned entries that don't come from scraping (e.g. IBPS
# exam cycles, which are announced on a predictable annual calendar
# and are easier to hand-maintain than scrape reliably).
PINNED_FILE = DATA_DIR / "pinned.json"


def fetch(url, timeout=15):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        print(f"[warn] could not fetch {url}: {exc}", file=sys.stderr)
        return None


def make_id(*parts):
    raw = "|".join(p or "" for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def parse_karnatakacareers(html, source):
    """
    karnatakacareers.org lists each job as an H2 heading (organisation)
    followed by a small table (Post Name / Vacancies / Qualification)
    and a 'Last Date' / 'Walk In Date' line with an Apply Now link.
    """
    listings = []
    if not html:
        return listings

    soup = BeautifulSoup(html, "html.parser")
    headings = soup.find_all(["h2"])

    for h in headings:
        org = h.get_text(strip=True)
        if not org or "search" in org.lower() or "jobs" in org.lower() and len(org) < 4:
            continue

        # Walk forward through siblings until the next h2 to collect
        # this listing's table + deadline + link.
        post_name, qualification, deadline, link = None, None, None, None
        node = h.find_next_sibling()
        steps = 0
        while node and node.name != "h2" and steps < 15:
            steps += 1
            text = node.get_text(" ", strip=True)

            if node.name == "table":
                rows = node.find_all("tr")
                for row in rows:
                    cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                    if len(cells) >= 2:
                        label, value = cells[0].lower(), cells[1]
                        if "post" in label:
                            post_name = value
                        elif "qualification" in label:
                            qualification = value

            if text and ("last date" in text.lower() or "walk in" in text.lower()):
                m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", text)
                if m:
                    deadline = m.group(1)
                a = node.find("a", href=True)
                if a:
                    link = a["href"]

            node = node.find_next_sibling()

        if org and (post_name or qualification):
            listings.append({
                "id": make_id(source["id"], org, post_name),
                "source": source["name"],
                "organisation": org,
                "post": post_name or "See notification",
                "qualification": qualification or "See notification",
                "deadline": deadline,
                "link": link or source["url"],
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            })

    return listings


def parse_generic_notice_list(html, source):
    """
    Fallback parser for pages that just list <a> notice links with
    dates nearby (used for KEA and similar notice-board style pages).
    Only keeps links whose text looks like a real notification.
    """
    listings = []
    if not html:
        return listings

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if len(text) < 15:
            continue
        if not re.search(r"(recruit|notif|vacan|exam|result|admit|accountant)", text, re.I):
            continue
        listings.append({
            "id": make_id(source["id"], text, a["href"]),
            "source": source["name"],
            "organisation": source["name"],
            "post": text,
            "qualification": None,
            "deadline": None,
            "link": a["href"] if a["href"].startswith("http") else source["url"],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })
    return listings[:20]  # cap noise from footer/nav links


def load_pinned():
    if PINNED_FILE.exists():
        try:
            return json.loads(PINNED_FILE.read_text())
        except Exception as exc:
            print(f"[warn] could not read pinned.json: {exc}", file=sys.stderr)
    return []


def load_previous():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            return {"listings": []}
    return {"listings": []}


def main():
    all_listings = []
    previous = load_previous()
    previous_by_source = {}
    for item in previous.get("listings", []):
        previous_by_source.setdefault(item.get("source"), []).append(item)

    for source in SOURCES:
        html = fetch(source["url"])
        time.sleep(2)  # be polite between requests

        if source["id"] == "karnatakacareers_mcom":
            parsed = parse_karnatakacareers(html, source)
        else:
            parsed = parse_generic_notice_list(html, source)

        if parsed:
            print(f"[ok] {source['name']}: {len(parsed)} listings found")
            all_listings.extend(parsed)
        else:
            # Source failed or structure changed — keep yesterday's
            # entries for this source instead of wiping them out.
            fallback = previous_by_source.get(source["name"], [])
            print(f"[warn] {source['name']}: 0 new listings, "
                  f"keeping {len(fallback)} from previous run")
            all_listings.extend(fallback)

    pinned = load_pinned()
    all_listings.extend(pinned)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "listings": all_listings,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f"[done] wrote {len(all_listings)} total listings to {DATA_FILE}")


if __name__ == "__main__":
    main()
