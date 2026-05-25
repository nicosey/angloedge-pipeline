"""
News and RNS announcement collector.

Usage:
  python3 collect.py <topic> [--mock]   Collect SearXNG news for a topic (default)
  python3 collect.py --rns              Collect RNS announcements from Investegate
  python3 collect.py --all <topic>      Collect both SearXNG news and RNS
"""
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime

ssl._create_default_https_context = ssl._create_unverified_context

try:
    import requests
    from bs4 import BeautifulSoup
    _SCRAPING_AVAILABLE = True
except ImportError:
    _SCRAPING_AVAILABLE = False

from config import SEARXNG_URL, log, load_topic_config
from db import init_db, save_collection, archive_collections, save_rns_announcements


INVESTEGATE_URL = "https://www.investegate.co.uk/"

# Polite pause between HTTP requests (relevant if detail-page fetching is added)
_REQUEST_DELAY = 1.5

# Headline-to-category inference rules — first match wins.
# Patterns are intentionally broad so partial headlines still classify correctly.
_CATEGORY_RULES = [
    ("Results",          re.compile(r'\b(results|earnings|revenue|profit|loss|turnover)\b', re.I)),
    ("Acquisition",      re.compile(r'\b(acqui[rs]|merger|takeover|scheme\s+of\s+arrangement|recommended\s+offer)\b', re.I)),
    ("Director Dealing", re.compile(r'\b(pdmr|tr-?1|director.{0,15}(dealing|interest|purchase|sale|holding))\b', re.I)),
    ("Dividend",         re.compile(r'\b(dividend|distribution)\b', re.I)),
    ("AGM/EGM",          re.compile(r'\b(agm|egm|annual\s+general\s+meeting|extraordinary\s+general\s+meeting)\b', re.I)),
    ("Placing",          re.compile(r'\b(placing|fundrais|subscription\s+for\s+shares)\b', re.I)),
    ("Trading Update",   re.compile(r'\b(trading\s+update|trading\s+statement|business\s+update)\b', re.I)),
    ("Share Buyback",    re.compile(r'\b(buy.?back|repurchase)\b', re.I)),
    ("Appointment",      re.compile(r'\b(appoints?|resign|retire[sd]?|board\s+change)\b', re.I)),
]


def _detect_category(headline):
    """Return the first category label whose pattern matches the headline, or None."""
    for label, pattern in _CATEGORY_RULES:
        if pattern.search(headline):
            return label
    return None


def _parse_company(text):
    """
    Split 'Company Name (Possible Suffix) (TICKER)' into (company_name, ticker).
    The final parenthesised uppercase-alphanumeric group is treated as the ticker.
    Returns (company_name, ticker) where ticker may be None.
    """
    m = re.search(r'\(([A-Z0-9.]+)\)\s*$', text)
    if m:
        return text[:m.start()].strip(), m.group(1)
    return text.strip(), None


def _parse_timestamp(raw):
    """Parse Investegate timestamp '25 May 2026 05:45 PM' → ISO-8601 string."""
    try:
        return datetime.strptime(raw.strip(), "%d %B %Y %I:%M %p").isoformat()
    except ValueError:
        return raw.strip()


def collect_rns():
    """
    Scrape the latest regulatory announcements from Investegate and persist
    new entries to the rns_announcements table.

    Duplicate rns_numbers (Investegate's numeric announcement ID) are silently
    ignored via INSERT OR IGNORE, so this is safe to run on any schedule.

    Requires: pip install requests beautifulsoup4

    Returns the number of newly inserted rows.
    """
    if not _SCRAPING_AVAILABLE:
        log("❌ RNS collection requires 'requests' and 'beautifulsoup4'.")
        log("   Install with: pip install requests beautifulsoup4")
        sys.exit(1)

    log("=" * 40)
    log("📋 Collecting RNS announcements from Investegate")
    log("=" * 40)

    session = requests.Session()
    session.headers.update({
        "User-Agent":      "AngloEdgePipeline/1.0 (research aggregator; see angloedge.com)",
        "Accept":          "text/html,application/xhtml+xml",
        "Accept-Language": "en-GB,en;q=0.9",
    })

    try:
        resp = session.get(INVESTEGATE_URL, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log(f"❌ Could not reach Investegate: {exc}")
        return 0

    soup  = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="table-investegate")
    if not table:
        log("❌ Announcement table not found — Investegate HTML may have changed")
        return 0

    tbody = table.find("tbody")
    rows  = tbody.find_all("tr") if tbody else []
    log(f"   Parsing {len(rows)} row(s)...")

    announcements = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        # --- Timestamp ---
        timestamp = _parse_timestamp(cells[0].get_text(strip=True))

        # --- Company name + ticker ---
        # Two anchors reference /company/TICKER — one wraps an img, one has the text.
        company_link = next(
            (a for a in cells[2].find_all("a", href=lambda h: h and "/company/" in h)
             if a.get_text(strip=True)),
            None,
        )
        if not company_link:
            continue
        company_name, ticker = _parse_company(company_link.get_text(strip=True))

        # --- Headline + URL ---
        ann_link = cells[3].find("a", class_="announcement-link")
        if not ann_link:
            continue
        headline = ann_link.get_text(strip=True)
        url      = ann_link.get("href", "")
        if url and not url.startswith("http"):
            url = "https://www.investegate.co.uk" + url

        # Unique ID: last path segment of the announcement URL is Investegate's
        # numeric ID (e.g. /announcement/rns/company-slug/headline-slug/9583514).
        # This serves as rns_number for deduplication.
        parts      = url.rstrip("/").split("/")
        rns_number = parts[-1] if parts[-1].isdigit() else url

        announcements.append({
            "rns_number":   rns_number,
            "timestamp":    timestamp,
            "company_name": company_name,
            "ticker":       ticker or "",
            "headline":     headline,
            "url":          url,
            "category":     _detect_category(headline) or "",
        })

    log(f"   Parsed {len(announcements)} announcement(s)")
    if not announcements:
        log("   Nothing to save.")
        return 0

    init_db()
    saved = save_rns_announcements(announcements)
    log(f"   💾 Saved {saved} new, skipped {len(announcements) - saved} duplicate(s)")
    log("")
    log("Done!")
    return saved


def _collect_topic(topic, mock=False):
    """Run SearXNG collection for one topic and persist the results."""
    from search import fetch_all_results, mock_fetch_results

    cfg = load_topic_config(topic)

    log("=" * 40)
    log(f"📡 Collecting: {cfg['title']}" + (" [MOCK]" if mock else ""))
    log("=" * 40)

    if not mock:
        try:
            req = urllib.request.Request(f"{SEARXNG_URL}/search?q=test&format=json")
            req.add_header("User-Agent", "AngloEdgePipeline/1.0")
            urllib.request.urlopen(req, timeout=10)
            log("✅ SearXNG: OK")
        except Exception:
            log("❌ SearXNG not reachable")
            sys.exit(1)

    log("")
    log("🔍 Fetching search results...")
    results = mock_fetch_results(cfg["searches"]) if mock else fetch_all_results(cfg["searches"])
    log("")

    init_db()
    save_collection(topic, results)
    archive_collections(max_age_hours=48)

    log("")
    log("Done!")


def _enrich_after_collect():
    from db import get_unenriched_rns, update_rns_enrichment
    from ai import enrich_rns_with_llm
    announcements = get_unenriched_rns()
    if not announcements:
        return
    log(f"\n🔬 Enriching {len(announcements)} new announcement(s)...")
    for rns_number, data in enrich_rns_with_llm(announcements):
        update_rns_enrichment(rns_number, data["impact_score"], data["insight"], data["key_themes"])


def _usage():
    available = [f[:-5] for f in os.listdir("config") if f.endswith(".json")]
    print("Usage:")
    print("  python3 collect.py <topic> [--mock]          Collect SearXNG news for a topic")
    print("  python3 collect.py --rns [--enrich]           Collect Investegate RNS announcements")
    print("  python3 collect.py --all <topic> [--enrich]   Collect both SearXNG news and RNS")
    print("  --enrich: run LLM enrichment on new high-signal announcements after collection")
    print(f"Available topics: {', '.join(sorted(available))}")


def main():
    raw_args = sys.argv[1:]
    flags    = {a for a in raw_args if a.startswith("--")}
    args     = [a for a in raw_args if not a.startswith("--")]
    mock     = "--mock" in flags

    if "--rns" in flags:
        collect_rns()
        if "--enrich" in flags:
            _enrich_after_collect()
        return

    if "--all" in flags:
        if not args:
            _usage()
            sys.exit(1)
        _collect_topic(args[0], mock=mock)
        time.sleep(_REQUEST_DELAY)
        collect_rns()
        if "--enrich" in flags:
            _enrich_after_collect()
        return

    # Default: SearXNG topic collection (original behaviour)
    if not args:
        _usage()
        sys.exit(1)
    _collect_topic(args[0], mock=mock)


if __name__ == "__main__":
    main()
