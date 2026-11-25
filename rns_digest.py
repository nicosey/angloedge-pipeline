"""
Daily digest of the day's highest-impact RNS announcements, published to the site.

Usage:
  python3 rns_digest.py                    Write the digest into the site repo, commit and push
  python3 rns_digest.py --dry-run          Print the posts; write nothing
  python3 rns_digest.py --date 2026-11-23  Digest for another day
  python3 rns_digest.py --top N            Number of announcements (default DIGEST_TOP, 5)
  python3 rns_digest.py --min-score S      Lowest score included (default DIGEST_MIN_SCORE, 7)
  python3 rns_digest.py --telegram         Also send the digest to Telegram (once per day)

Writes, inside GITHUB_REPO_PATH:
  <data dir>/rns-today.json    (data dir = parent of GITHUB_COMPANIES_DIR, as for
                                export_companies.py). The site renders it at
                                /rns/today (page) and /rns/today.txt (posts).

rns-today.json holds the date, the items, and "posts": the ready-to-post blocks
for the X bot, each at most POST_LIMIT characters as X counts them (URLs 23,
emoji and most symbols 2). /rns/today.txt is the posts joined by a line holding
only "---". A day with nothing scored DIGEST_MIN_SCORE+ (weekends) writes an
empty digest for that date, so the bot never reposts a previous day.

Only our own stored fields are used: ticker, company name, headline, score and
the generated insight. No announcement text and no outbound announcement links.
"Today" is the announcement's own timestamp, not when it was enriched.
"""
import html
import json
import os
import re
import sys
from datetime import date, datetime

import db
from excluded_accounts import is_excluded
from routine_filings import routine_type
from config import log
from export_companies import (DEFAULT_COMPANIES_DIR, INDEX_FILENAME, commit_and_push,
                              parse_timestamp, read_text, serialise, write_if_changed)

DIGEST_TOP = int(os.environ.get("DIGEST_TOP", "5"))
DIGEST_MIN_SCORE = int(os.environ.get("DIGEST_MIN_SCORE", "7"))
DIGEST_FILENAME = "rns-today.json"
SENT_DIR = os.path.join("output", "digest")

POST_LIMIT = 280
POST_DELIMITER = "---"
TITLE = "Highest-impact UK RNS today 📋"
FOOTER = "Scored filing history for {n} UK companies → angloedge.com/companies"


# ── selection ───────────────────────────────────────────────

def rows_for_day(con, day, min_score):
    """Enriched, non-routine rows announced on `day` with score >= min_score, best first.
    Routine filings (routine_filings.py) never make the digest, whatever their score."""
    cur = con.execute(
        "SELECT rns_number, timestamp, company_name, ticker, headline, impact_score, insight, url, category "
        "FROM rns_announcements WHERE impact_score IS NOT NULL AND impact_score >= ?",
        (min_score,))
    keys = ("rns_number", "timestamp", "company_name", "ticker", "headline", "impact_score", "insight", "url",
            "category")
    out = []
    for r in cur.fetchall():
        row = dict(zip(keys, r))
        if is_excluded(row["ticker"], row["url"]) or routine_type(row["headline"], row["category"]):
            continue
        ts = parse_timestamp(row["timestamp"])
        if ts is not None and ts.date() == day:
            row["_ts"] = ts
            out.append(row)
    out.sort(key=lambda r: (-r["impact_score"], -r["_ts"].timestamp(), r["rns_number"]))
    return out


def pick(rows, top):
    """Top rows, at most one per company (its highest-scoring, then latest)."""
    seen, out = set(), []
    for r in rows:
        key = (r["ticker"] or r["company_name"] or "").upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) == top:
            break
    return out


# ── text ────────────────────────────────────────────────────

def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()


def display_headline(headline, company):
    """Drop a leading 'Company Name:' that repeats the company (as the site does)."""
    headline = (headline or "").strip()
    m = re.match(r"^\s*([^:]{2,80}?)\s*:\s+(.+)$", headline, re.S)
    if m and company:
        prefix, core = _norm(m.group(1)), _norm(company)
        for suffix in (" plc", " limited", " ltd", " group plc", " group"):
            core = core[: -len(suffix)] if core.endswith(suffix) else core
            prefix = prefix[: -len(suffix)] if prefix.endswith(suffix) else prefix
        if prefix and (prefix == core or (core.startswith(prefix) and len(prefix.split()[0]) >= 3)):
            return m.group(2).strip()
    return headline


def company_count_label(n):
    """1722 -> '1,700+'; small numbers stay exact."""
    if n < 100:
        return str(n)
    return f"{n // 100 * 100:,}+"


# X's weighted length (twitter-text v3): code points in these ranges weigh 1,
# everything else (most symbols, CJK, emoji) 2; any URL weighs 23.
_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
_URL_RE = re.compile(r"(?:https?://)?(?:[a-z0-9-]+\.)+(?:com|co\.uk|uk|org|net|io)(?:/[^\s]*)?", re.I)
_EMOJI_SEQ_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿][️‍\U0001F3FB-\U0001F3FF"
                           r"\U0001F000-\U0001FAFF☀-➿]*")


def x_length(text):
    """Length of `text` as X counts it."""
    n, pos = 0, 0
    for m in _URL_RE.finditer(text):
        n += _plain_length(text[pos:m.start()]) + 23
        pos = m.end()
    return n + _plain_length(text[pos:])


def _plain_length(text):
    text = _EMOJI_SEQ_RE.sub("\x00\x00", text)          # each emoji sequence weighs 2
    return sum(1 if c == "\x00" or any(a <= ord(c) <= b for a, b in _LIGHT_RANGES) else 2
               for c in text)


def _fit(text, budget):
    """Trim text to `budget` (X length): whole sentences if possible, else words + '…'."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if x_length(text) <= budget:
        return text
    kept = ""
    for s in re.split(r"(?<=[.!?])\s+", text):
        candidate = f"{kept} {s}".strip()
        if x_length(candidate) > budget:
            break
        kept = candidate
    if kept:
        return kept
    out = ""
    for w in text.split(" "):
        candidate = f"{out} {w}".strip()
        if x_length(candidate + "…") > budget:
            break
        out = candidate
    return (out.rstrip(",;:—- ") + "…") if out else ""


def item_post(item):
    """One post for one announcement, within POST_LIMIT."""
    lead = f"${item['ticker']} {item['company']}" if item["ticker"] else item["company"]
    lead = _fit(lead, POST_LIMIT - 8) + f" — {item['score']}/10"
    headline = _fit(item["headline"], POST_LIMIT - x_length(lead) - 1)
    post = f"{lead}\n{headline}"
    budget = POST_LIMIT - x_length(post) - 1
    insight = _fit(item["insight"], budget) if item["insight"] and budget > 20 else ""
    return f"{post}\n{insight}" if insight else post


def _no_delimiter_lines(post):
    return "\n".join(line for line in post.split("\n") if line.strip() != POST_DELIMITER)


def build_posts(items, company_count, day):
    posts = [f"{TITLE}\n{day.strftime('%a')} {day.day} {day.strftime('%B %Y')}"]
    posts += [item_post(i) for i in items]
    posts.append(FOOTER.format(n=company_count_label(company_count)))
    posts = [_no_delimiter_lines(p) for p in posts]
    too_long = [x_length(p) for p in posts if x_length(p) > POST_LIMIT]
    if too_long:
        raise ValueError(f"post over {POST_LIMIT}: {too_long}")
    return posts


def items_from_rows(rows, slugs):
    return [{
        "ticker":    r["ticker"] or "",
        "company":   r["company_name"] or r["ticker"] or "",
        "slug":      slugs.get(r["ticker"]),
        "score":     r["impact_score"],
        "headline":  display_headline(r["headline"], r["company_name"]),
        "insight":   (r["insight"] or "").strip(),
        "timestamp": r["_ts"].isoformat(timespec="seconds"),
    } for r in rows]


def build_digest(rows, slugs, company_count, day, now=None):
    items = items_from_rows(rows, slugs)
    return {
        "date":         day.isoformat(),
        "generated_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "items":        items,
        "posts":        build_posts(items, company_count, day) if items else [],
    }


def format_telegram(digest):
    """The full digest as one Telegram HTML message (insights untrimmed)."""
    e = lambda t: html.escape(t, quote=False)       # Telegram HTML: only &, <, >
    parts = [e(TITLE)]
    for i in digest["items"]:
        lead = f"${e(i['ticker'])} {e(i['company'])}" if i["ticker"] else e(i["company"])
        block = [f"{lead} — {i['score']}/10", e(i["headline"])]
        if i["insight"]:
            block.append(e(i["insight"]))
        parts.append("\n".join(block))
    parts.append(e(digest["posts"][-1]))
    return f"\n{POST_DELIMITER}\n".join(parts)


# ── site repo ───────────────────────────────────────────────

def site_paths(repo_path, companies_rel):
    data_rel = os.path.dirname(companies_rel.rstrip("/\\"))
    return {
        "digest_rel": os.path.join(data_rel, DIGEST_FILENAME),
        "index":      os.path.join(repo_path, data_rel, INDEX_FILENAME),
    }


def load_index(index_path):
    """(ticker → slug for companies with a page, company count) from companies-index.json."""
    raw = read_text(index_path)
    if raw is None:
        return {}, None
    try:
        index = json.loads(raw)
    except ValueError:
        return {}, None
    return {c["ticker"]: c["slug"] for c in index if c.get("ticker") and c.get("slug")}, len(index)


def write_digest(digest, repo_path, digest_rel, branch, dry_run, git=None):
    """Write rns-today.json if its content changed, then commit and push it."""
    path = os.path.join(repo_path, digest_rel)
    existing = read_text(path)
    if existing is not None:
        try:
            old = json.loads(existing)
            if {k: v for k, v in old.items() if k != "generated_at"} == \
               {k: v for k, v in digest.items() if k != "generated_at"}:
                log(f"  ℹ {digest_rel}: unchanged")
                return "unchanged"
        except ValueError:
            pass
    status = write_if_changed(path, serialise(digest), dry_run)
    if dry_run:
        log(f"  ℹ {digest_rel}: would be {status} [DRY RUN]")
        return status
    message = f"rns: digest for {digest['date']} ({len(digest['items'])} announcement(s))"
    kwargs = {"git": git} if git else {}
    commit_and_push(repo_path, branch, [digest_rel], message, **kwargs)
    return status


# ── run ─────────────────────────────────────────────────────

def sent_marker(day):
    return os.path.join(SENT_DIR, f"{day.isoformat()}.sent")


def parse_args(argv):
    def value(name, cast):
        i = argv.index(name)
        try:
            return cast(argv[i + 1])
        except (IndexError, ValueError):
            raise SystemExit(f"{name} needs a value")
    opts = {"dry_run": "--dry-run" in argv, "telegram": "--telegram" in argv, "day": date.today(),
            "top": DIGEST_TOP, "min_score": DIGEST_MIN_SCORE}
    if "--date" in argv:
        opts["day"] = value("--date", date.fromisoformat)
    if "--top" in argv:
        opts["top"] = value("--top", int)
    if "--min-score" in argv:
        opts["min_score"] = value("--min-score", int)
    if opts["top"] < 1:
        raise SystemExit("--top needs a positive integer")
    return opts


def send_telegram(digest, day, deliver=None):
    if not digest["items"]:
        log("  ℹ Telegram: nothing to send")
        return True
    if os.path.exists(sent_marker(day)):
        log(f"⏭  Telegram digest for {day} already sent ({sent_marker(day)}). Delete it to resend.")
        return False
    if deliver is None:
        from delivery import make_delivery
        deliver = make_delivery("telegram", dry_run=False).send
    if not deliver(format_telegram(digest)):
        log("❌ Telegram digest not sent.")
        return False
    os.makedirs(SENT_DIR, exist_ok=True)
    with open(sent_marker(day), "w") as f:
        f.write(datetime.now().isoformat(timespec="seconds") + "\n")
    log(f"  ✅ Telegram digest for {day} sent")
    return True


def main(argv, deliver=None, git=None):
    opts = parse_args(argv)
    day = opts["day"]
    repo_path = os.environ.get("GITHUB_REPO_PATH", "")
    companies_rel = os.environ.get("GITHUB_COMPANIES_DIR", DEFAULT_COMPANIES_DIR)
    branch = os.environ.get("GITHUB_BRANCH", "main")
    if not repo_path or not os.path.isdir(os.path.join(repo_path, ".git")):
        log("❌ GITHUB_REPO_PATH must point at the site repo")
        return 1
    paths = site_paths(repo_path, companies_rel)
    slugs, count = load_index(paths["index"])

    db.init_db()
    con = db._connect()
    try:
        rows = pick(rows_for_day(con, day, opts["min_score"]), opts["top"])
        if count is None:     # no export yet: distinct tickers (an overcount)
            count = con.execute("SELECT COUNT(DISTINCT ticker) FROM rns_announcements "
                                "WHERE ticker IS NOT NULL AND ticker != ''").fetchone()[0]
    finally:
        con.close()

    digest = build_digest(rows, slugs, count, day)
    log(f"📋 Digest for {day}: {len(digest['items'])} announcement(s) scored {opts['min_score']}+")
    for p in digest["posts"]:
        log(f"\n[{x_length(p)}/{POST_LIMIT}]\n{p}")
    try:
        write_digest(digest, repo_path, paths["digest_rel"], branch, opts["dry_run"], git=git)
    except RuntimeError as e:
        log(f"❌ Digest publish failed: {e}")
        return 1
    if opts["telegram"] and not opts["dry_run"]:
        if not send_telegram(digest, day, deliver):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
