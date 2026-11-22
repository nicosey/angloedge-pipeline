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
# Headlines arrive truncated (~50 chars), so patterns key on opening wording.
# Word stems spell out their endings: a stem followed by \b (e.g. "acqui[rs]\b")
# can never match "acquisition".
#
# Headlines matching _CATEGORY_EXCLUDE stay uncategorised whatever else they
# contain: third-party research, and Irish Takeover Panel Form 8 dealing
# disclosures (routine, despite the word "takeover").
_CATEGORY_EXCLUDE = re.compile(
    r'irish\s+takeover\s+panel|kepler\s+trust\s+intelligence|\bquoteddata\b', re.I)

_CATEGORY_RULES = [
    # Meeting outcomes ("Results of AGM") before Results
    ("AGM/EGM",          re.compile(r'\bresults?\s+of\s+(the\s+)?(agm|egm|annual\s+general|extraordinary\s+general|general|court)\b', re.I)),
    ("Results",          re.compile(r'\b(results|earnings|revenue|profit|loss|turnover)\b'
                                    r'|\b(annual|half[\s-]?year(ly)?)\s+financial\s+report\b'
                                    r'|\bhalf[\s-]?year(ly)?\s+report\b'
                                    r'|\binterim\s+(report|management\s+statement)\b'
                                    r'|\bquarterly\s+report\b', re.I)),
    # New listings only — a bare "Admission to Trading" is almost always a block
    # admission or debt issue by an existing issuer, so it is not matched
    ("New Listing",      re.compile(r'\bintention\s+to\s+float\b|\bfirst\s+day\s+of\s+(dealings|trading)\b'
                                    r'|\badmission\s+to\s+(aim|the\s+main\s+market|the\s+official\s+list|aquis)\b'
                                    r'|\bipo\b|\binitial\s+public\s+offering\b', re.I)),
    ("Acquisition",      re.compile(r'\b(acqui(re[sd]?|ring|sitions?)|merger|takeover|scheme\s+of\s+arrangement'
                                    r'|recommended\s+(cash\s+)?offer|possible\s+offer|firm\s+offer|rule\s+2\.[47]|pusu)\b', re.I)),
    # TR-1 is a major-shareholder notification, not a director dealing — not matched
    ("Director Dealing", re.compile(r'\b(pdmrs?|director.{0,15}(dealings?|interests?|purchase|sale|holding)'
                                    r'|managers.{0,2}\s+and\s+closely)\b', re.I)),
    ("Dividend",         re.compile(r'\b(dividend|distribution)\b', re.I)),
    ("AGM/EGM",          re.compile(r'\b(agm|egm|annual\s+general\s+meeting|extraordinary\s+general\s+meeting)\b', re.I)),
    ("Placing",          re.compile(r'\b(placing|fundrais(e|es|ed|ing)|subscription\s+for\s+shares|retail\s+offer|bookbuild(ing)?)\b', re.I)),
    ("Trading Update",   re.compile(r'\b(trading\s+update|trading\s+statement|business\s+update)\b', re.I)),
    ("Share Buyback",    re.compile(r'\b(buy.?back|repurchase)\b', re.I)),
    ("Appointment",      re.compile(r'\b(appoint(s|ment|ments|ed)?|resign(s|ed|ation)?|retire[sd]?|retirement'
                                    r'|board\s+(changes?|update)|directorate\s+(changes?|update|resignation))\b', re.I)),
]


def _detect_category(headline):
    """Return the first category label whose pattern matches the headline, or None."""
    if not headline or _CATEGORY_EXCLUDE.search(headline):
        return None
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
