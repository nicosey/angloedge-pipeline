"""
Accounts that are not issuers reporting on themselves: newswire feeds,
commentary feeds and platforms posting about other companies. Their rows are
kept in the DB but never enriched, posted, exported (no company page) or put in
the digest.

Found by auditing every exported account (2026-11-25): accounts with no RNS-wire
filings whose headlines are mostly about other organisations, plus the wires
they come in on. Add an account by ticker with the reason; add a wire when the
whole wire is a third-party feed.
"""
import re

EXCLUDED_TICKERS = {
    "FNEWS":    "FinanceWire News — a newswire's own feed of third-party press releases",
    "FROS.UKN": "Frostrow Capital — weekly market commentary for investment trust investors",
    "AMPX":     "Asset Match — trading platform announcing other companies' auctions and admissions",
}

# Investegate wire codes (…/announcement/<wire>/…) that carry only third-party feeds.
EXCLUDED_WIRES = {
    "fnw": "FinanceWire — no issuer announcements; every account on it is a feed",
}

_WIRE_RE = re.compile(r"investegate\.co\.uk/announcement/([a-z]+)/", re.I)


def wire_of(url):
    m = _WIRE_RE.search(url or "")
    return m.group(1).lower() if m else None


def is_excluded(ticker, url=None):
    """True if the row belongs to an excluded account or arrived on an excluded wire."""
    return (ticker or "").strip().upper() in EXCLUDED_TICKERS or wire_of(url) in EXCLUDED_WIRES


def excluded_sql(ticker, url):
    """SQLite function form (returns 0/1)."""
    return 1 if is_excluded(ticker, url) else 0
