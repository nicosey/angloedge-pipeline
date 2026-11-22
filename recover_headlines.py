"""
Recover full headlines for RNS rows stored truncated ("...") from Investegate's list view.

Usage:
  python3 recover_headlines.py               Fetch each truncated row's page and store the full headline
  python3 recover_headlines.py --dry-run     Fetch and show old → new; write nothing
  python3 recover_headlines.py --limit N     Process at most N rows (newest first)
  python3 recover_headlines.py --force       Run even inside the RNS collector's window (testing only)

Only the headline column is ever written. Each row is committed as soon as it
is recovered, and a fixed row no longer ends in "...", so an interrupted run
loses nothing and a re-run skips rows already fixed.

Where the title lives (Investegate's announcement template, all wires):
  <h1 id="main-title">  Investegate's headline — complete for EQS, GNW, MFN, PRN,
                        BZW, UKN, FNW; truncated for RNS.
  first <h1> inside the announcement document (div.fr-view-element)
                        — the full title for RNS. Only that heading is read; no
                        other announcement text is parsed or kept.
A candidate is accepted only if it starts with the stored text (ignoring case,
quotes and spacing), is longer, and is not itself truncated.

Politeness: one request every REQUEST_INTERVAL seconds, the pipeline's usual
honest User-Agent, and no running while the RNS collector is active
(07:00–16:30) — a run refuses to start in that window and stops cleanly before
it opens.
"""
import html
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, time as dtime

import db
from config import log

USER_AGENT = "AngloEdgePipeline/1.0 (research aggregator; see angloedge.com)"
REQUEST_INTERVAL = 2.0            # seconds between request starts
REQUEST_TIMEOUT = 20
RETRY_WAIT = 30                   # seconds before the single retry on 429/5xx
MAX_CONSECUTIVE_FAILURES = 10     # stop: probably blocked or site down
URL_PREFIX = "https://www.investegate.co.uk/"
ELLIPSIS = "..."

# The RNS collector runs 07:00–16:30; stay clear of it.
QUIET_START = dtime(6, 55)        # stop before the collector's first run
QUIET_END = dtime(16, 35)         # earliest start after its last run


class FetchError(Exception):
    def __init__(self, kind, detail=""):
        super().__init__(f"{kind} {detail}".strip())
        self.kind = kind


# ── extraction ──────────────────────────────────────────────

_MAIN_TITLE_RE = re.compile(r'<h1[^>]*\bid="main-title"[^>]*>(.*?)</h1>', re.S | re.I)
_DOC_START_RE = re.compile(r'<div[^>]*class="[^"]*\bfr-view-element\b[^"]*"', re.I)
_H1_RE = re.compile(r'<h1[^>]*>(.*?)</h1>', re.S | re.I)


def clean_text(fragment):
    """Strip tags, decode entities, collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def comparable(text):
    """Normalised form for prefix comparison: case, quotes, dashes, spacing."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().casefold()


def candidate_titles(page):
    """Possible full titles, best first: main-title, then the document's first <h1>."""
    out = []
    m = _MAIN_TITLE_RE.search(page)
    if m:
        out.append(clean_text(m.group(1)))
    d = _DOC_START_RE.search(page)
    if d:
        h = _H1_RE.search(page, d.end())
        if h:
            out.append(clean_text(h.group(1)))
    return [t for t in out if t]


def stored_stem(headline):
    """The stored headline without its trailing ellipsis."""
    text = headline.rstrip()
    return text[: -len(ELLIPSIS)].rstrip() if text.endswith(ELLIPSIS) else text


def extract_full_headline(page, stored):
    """
    Return (headline, None) for the first candidate that genuinely extends the
    stored text, else (None, reason) with reason in: unparseable, mismatch,
    still_truncated.
    """
    candidates = candidate_titles(page)
    if not candidates:
        return None, "unparseable"
    stem = comparable(stored_stem(stored))
    reason = "mismatch"
    for title in candidates:
        c = comparable(title)
        if not c.startswith(stem):
            continue
        if c.endswith(ELLIPSIS) or len(c) <= len(stem):
            reason = "still_truncated"
            continue
        return title, None
    return None, reason


# ── HTTP ────────────────────────────────────────────────────

def _ssl_context():
    """
    A verifying SSL context. python.org builds on macOS often ship without a CA
    bundle (CERTIFICATE_VERIFY_FAILED), so fall back to certifi, then to the
    system bundle — never to an unverified context.
    """
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca"):
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    if os.path.exists("/etc/ssl/cert.pem"):
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ctx


_SSL_CONTEXT = None

def http_get(url):
    """Fetch a page as text. Raises FetchError(not_found | http_<code> | network)."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = _ssl_context()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=_SSL_CONTEXT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        raise FetchError("not_found" if e.code in (404, 410) else f"http_{e.code}", str(e.code))
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            raise FetchError("ssl", str(e.reason))   # not retried: it won't fix itself
        raise FetchError("network", str(e))
    except (TimeoutError, OSError) as e:
        raise FetchError("network", str(e))


def _retryable(kind):
    return kind == "network" or kind == "http_429" or kind.startswith("http_5")


class Fetcher:
    """Rate-limited fetch with one retry on 429/5xx/network errors."""

    def __init__(self, get=http_get, interval=REQUEST_INTERVAL, retry_wait=RETRY_WAIT,
                 sleep=time.sleep, clock=time.monotonic):
        self.get, self.interval, self.retry_wait = get, interval, retry_wait
        self.sleep, self.clock = sleep, clock
        self._last = None

    def _wait_turn(self):
        if self._last is not None:
            remaining = self.interval - (self.clock() - self._last)
            if remaining > 0:
                self.sleep(remaining)
        self._last = self.clock()

    def fetch(self, url):
        self._wait_turn()
        try:
            return self.get(url)
        except FetchError as e:
            if not _retryable(e.kind):
                raise
            log(f"    ↻ {e.kind} — retrying in {self.retry_wait}s")
            self.sleep(self.retry_wait)
            self._wait_turn()
            return self.get(url)


# ── schedule guard ──────────────────────────────────────────

def in_collector_window(now):
    """True between QUIET_START and QUIET_END, when the RNS collector may be running."""
    return QUIET_START <= now.time() < QUIET_END


# ── DB ──────────────────────────────────────────────────────

def load_truncated_rows(con, limit=None):
    sql = ("SELECT rns_number, headline, url FROM rns_announcements "
           "WHERE headline LIKE ? AND url LIKE ? ORDER BY timestamp DESC, rns_number")
    args = [f"%{ELLIPSIS}", f"{URL_PREFIX}%"]
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return [{"rns_number": r[0], "headline": r[1], "url": r[2]} for r in con.execute(sql, args)]


def save_headline(con, rns_number, old, new):
    """Update only the headline, and only if it still holds the text we fetched for."""
    cur = con.execute(
        "UPDATE rns_announcements SET headline=? WHERE rns_number=? AND headline=?",
        (new, rns_number, old))
    con.commit()
    return cur.rowcount == 1


# ── run ─────────────────────────────────────────────────────

def recover(con, rows, fetcher, dry_run=False, now=datetime.now, force=False):
    """Process rows; returns a Counter of outcomes."""
    counts = Counter()
    consecutive = 0
    for i, row in enumerate(rows, 1):
        if not force and in_collector_window(now()):
            log(f"⏸  Stopping before the RNS collector's window ({QUIET_START:%H:%M}). "
                f"Progress is saved — re-run after {QUIET_END:%H:%M} to continue.")
            counts["stopped_for_window"] = len(rows) - i + 1
            break

        prefix = f"[{i}/{len(rows)}] {row['rns_number']}"
        try:
            page = fetcher.fetch(row["url"])
        except FetchError as e:
            outcome, full = e.kind if e.kind == "not_found" else "fetch_failed", None
            log(f"  ✗ {prefix} {e} — {row['url']}")
        else:
            full, reason = extract_full_headline(page, row["headline"])
            outcome = "fixed" if full else reason
            if not full:
                log(f"  ✗ {prefix} {reason}: {row['headline']!r} — {row['url']}")

        if full:
            log(f"  ✓ {prefix} {row['headline']!r}\n      → {full!r}")
            if not dry_run and not save_headline(con, row["rns_number"], row["headline"], full):
                outcome = "changed_meanwhile"
                log(f"    row changed since it was read — left as is")

        counts[outcome] += 1
        consecutive = 0 if outcome in ("fixed", "mismatch", "still_truncated") else consecutive + 1
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            log(f"🛑 {consecutive} consecutive fetch failures — stopping (blocked or site down?). "
                f"Progress is saved; re-run later.")
            counts["aborted_remaining"] = len(rows) - i
            break
    return counts


def log_summary(counts, total, dry_run):
    verb = "Would fix" if dry_run else "Fixed"
    log("")
    log(f"Truncated rows selected: {total}")
    log(f"  {verb}: {counts['fixed']}")
    failures = {k: v for k, v in counts.items() if k not in ("fixed", "stopped_for_window", "aborted_remaining")}
    log(f"  Failed: {sum(failures.values())}" + (f"  ({', '.join(f'{k} {v}' for k, v in sorted(failures.items()))})" if failures else ""))
    for k in ("stopped_for_window", "aborted_remaining"):
        if counts[k]:
            log(f"  Not attempted ({k.replace('_', ' ')}): {counts[k]}")
    if dry_run:
        log("Dry run — nothing written.")


def parse_args(argv):
    opts = {"dry_run": "--dry-run" in argv, "force": "--force" in argv, "limit": None}
    if "--limit" in argv:
        i = argv.index("--limit")
        try:
            opts["limit"] = int(argv[i + 1])
            if opts["limit"] < 1:
                raise ValueError
        except (IndexError, ValueError):
            raise SystemExit("--limit needs a positive integer")
    return opts


def main(argv):
    opts = parse_args(argv)
    if not opts["force"] and in_collector_window(datetime.now()):
        log(f"The RNS collector runs until 16:30 — start after {QUIET_END:%H:%M} "
            f"(or pass --force for a small test).")
        return 1
    db.init_db()
    con = db._connect()
    try:
        rows = load_truncated_rows(con, opts["limit"])
        hours = len(rows) * REQUEST_INTERVAL / 3600
        log(f"🔎 {len(rows)} truncated headline(s) to recover, ~{hours:.1f}h at one request every "
            f"{REQUEST_INTERVAL:g}s" + (" [DRY RUN]" if opts["dry_run"] else ""))
        counts = recover(con, rows, Fetcher(), dry_run=opts["dry_run"], force=opts["force"])
        log_summary(counts, len(rows), opts["dry_run"])
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
