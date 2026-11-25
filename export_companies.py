"""
Export per-company RNS JSON files into the Astro site repo.

Usage:
  python3 export_companies.py                 Export, write changed files, commit and push
  python3 export_companies.py --dry-run       Report what would be written; write nothing
  python3 export_companies.py --limit N       Export only the N most recently active companies
                                              (writes files, but skips index, pruning and git)

Writes, inside GITHUB_REPO_PATH:
  <GITHUB_COMPANIES_DIR>/<slug>.json          One file per company (default dir: src/data/companies)
  <parent of that dir>/companies-index.json   Index of every exported company
  <parent of that dir>/company-slugs.json     Sticky ticker → slug map (never rewritten, only appended)

Slugs are permanent: once a ticker has a slug it is reused forever, even if the
company name changes, so a published URL never moves.

Excluded accounts (excluded_accounts.py: newswire and commentary feeds) get no
page. Only our own stored fields are exported (timestamp, category, headline, url and
our enrichment). Announcement body text is never stored or exported.
"""
import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime

from config import log
from db import init_db, get_rns_for_export
from routine_filings import routine_type
from excluded_accounts import is_excluded


MIN_ANNOUNCEMENTS   = 3
DEFAULT_COMPANIES_DIR = os.path.join("src", "data", "companies")
INDEX_FILENAME      = "companies-index.json"
SLUG_MAP_FILENAME   = "company-slugs.json"
MAX_TIMESTAMP_WARNINGS = 20   # cap per-row log lines; the summary has the full count

# Trailing tokens removed from company names before slugifying (lowercase,
# punctuation-stripped). Affects NEW slugs only — existing slugs are sticky.
# Names read '<name> <legal form> <share-class notation>', so stripping runs
# from the end in two phases: share-class tokens first, then legal forms. Once a
# legal form is stripped, share-class tokens are no longer removed — so
# 'Leverage Shares PLC' keeps 'Shares'.
SHARE_CLASS_TOKENS = {
    "ord", "ords", "ordinary", "shs", "sh", "shares", "share", "npv", "red", "redeemable",
    "cls", "class", "clas", "pref", "preference", "prf", "pfd", "cnv", "conv", "di",
    "dr", "adr", "gdr", "sdr", "reg", "registered", "units", "unit", "wi", "vtg", "ptg",
    "gbp", "usd", "eur",   # bare currency designations ('BH Macro Ltd. GBP Shares')
}
LEGAL_FORM_TOKENS = {
    "plc", "limited", "ltd", "inc", "incorporated", "corp", "corporation", "llc", "lp",
    "ag", "sa", "nv", "ab", "publ", "asa", "oyj", "spa", "se", "icav", "bv",
    "a/s", "s.a", "s.a.e", "s.a.r.l", "sarl", "n.v", "s.p.a", "kgaa", "co",
}
SLUG_STRIP_TOKENS = SHARE_CLASS_TOKENS | LEGAL_FORM_TOKENS
# Nominal values such as '0.1p', '10p', 'gbp0.01', 'usd1', '€0.05'
_NOMINAL_VALUE_RE = re.compile(r"^(?:(?:gbp|usd|eur|sek|nok|dkk|chf|£|\$|€)\d[\d.]*p?|\d+(?:\.\d+)?p)$")

# Issuers whose announcements ALL match one of these headline patterns are
# debt-programme issuers (sovereigns, bond SPVs) and get no company page.
DEBT_PROGRAMME_HEADLINE_PATTERNS = (
    r"^\s*(?:publication\s+of\s+)?final\s+terms\b",
)
_DEBT_PROGRAMME_RES = [re.compile(p, re.I) for p in DEBT_PROGRAMME_HEADLINE_PATTERNS]


def is_debt_programme_issuer(rows):
    """True when every announcement headline is a debt-programme filing."""
    return bool(rows) and all(
        any(rx.search(r["headline"] or "") for rx in _DEBT_PROGRAMME_RES) for r in rows
    )


# ── pure helpers ─────────────────────────────────────────────

def slugify(text):
    """Lowercase ASCII, runs of anything non-alphanumeric collapsed to '-'."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def slug_name(company_name):
    """Company name with trailing share-class and legal-form tokens removed.

    Used only for the slug; the display name is never altered. Strips trailing
    parenthesised groups ('(DI)', '(Publ)'), then trailing tokens from
    SLUG_STRIP_TOKENS / nominal values ('0.1p', 'GBP0.01'), and a class letter
    ('A', 'B') when it sits next to share-class notation. Never strips to empty.
    """
    name = (company_name or "").strip()
    while True:
        stripped = re.sub(r"\s*\([^()]*\)\s*$", "", name)
        if stripped == name or not stripped:
            break
        name = stripped
    name = re.sub(r"\bpublic\s+limited\s+company\b", "plc", name, flags=re.I)
    tokens = _strip_trailing(name.split())
    tokens = _drop_repeated_short_name(tokens)
    return " ".join(tokens)


def _norm_token(t):
    return t.lower().strip(".,;:&-")


def _is_strip_token(t):
    n = _norm_token(t)
    return n in SLUG_STRIP_TOKENS or bool(_NOMINAL_VALUE_RE.match(n))


def _is_share_class_token(t):
    n = _norm_token(t)
    return n in SHARE_CLASS_TOKENS or bool(_NOMINAL_VALUE_RE.match(n))


def _strip_trailing(tokens, stripped_any=False):
    """Pop trailing share-class, then legal-form tokens. Never strips to empty."""
    tokens = list(tokens)
    seen_legal = False
    while len(tokens) > 1:
        last = _norm_token(tokens[-1])
        if not last:
            tokens.pop()   # bare '&' in 'AG & Co KGaA'
        elif last in LEGAL_FORM_TOKENS:
            tokens.pop()
            stripped_any = seen_legal = True
        elif seen_legal:
            break
        elif _is_share_class_token(tokens[-1]):
            tokens.pop()
            stripped_any = True
        elif len(last) == 1 and last.isalpha() and (stripped_any or _is_strip_token(tokens[-2])):
            tokens.pop()   # class letter: 'Nolato AB B Ord Shs', 'Brookfield Corp Cls A'
            stripped_any = True
        elif stripped_any and re.fullmatch(r"(19|20)\d\d", last):
            tokens.pop()   # share-class year: 'Fair Oaks Income Limited 2021 Shs NPV'
        else:
            break
    return tokens


def _drop_repeated_short_name(tokens):
    """'Siemens AG Siemens' -> 'Siemens'; 'X REIT P X' -> 'X REIT'.

    Investegate often lists '<legal name> <legal form> <short name>'. Drop the
    trailing repeat only when the word before it is a legal form or a truncated
    single letter, so genuine repeats like 'Mahindra & Mahindra' survive.
    """
    norm = [_norm_token(t) for t in tokens]
    for k in range(len(tokens) // 2, 0, -1):
        head, tail = norm[:-k], norm[-k:]
        if len(head) <= k or head[:k] != tail:
            continue
        before = tokens[len(head) - 1]
        nb = _norm_token(before)
        if _is_strip_token(before) or (len(nb) == 1 and nb.isalpha()):
            return _strip_trailing(tokens[:len(head)], stripped_any=True)
    return tokens


def make_slug(company_name, ticker):
    name_part   = slugify(slug_name(company_name))
    ticker_part = slugify(ticker)
    return f"{name_part}-{ticker_part}" if name_part else ticker_part


# Non-ISO formats seen in rns_announcements.timestamp, tried in order after ISO.
# collect.py parses Investegate's '21 Sep 2026 07:32 AM' with %B (full month
# name), which only matches months whose full and short names coincide (May);
# every other month is stored raw. Full-name and 24h variants are accepted too.
_TIMESTAMP_FORMATS = (
    "%d %b %Y %I:%M %p",    # 21 Sep 2026 07:32 AM  (Investegate native)
    "%d %B %Y %I:%M %p",    # 21 September 2026 07:32 AM
    "%d %b %Y %H:%M",       # 21 Sep 2026 07:32
    "%d %B %Y %H:%M",       # 21 September 2026 07:32
)


def parse_timestamp(raw):
    """Return a naive datetime for any known timestamp format, else None."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        pass
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\bSept\b", "Sep", text, flags=re.I)   # %b only accepts 'Sep'
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def normalise_timestamp(raw):
    """ISO-8601 (seconds precision) for parseable values; the raw value otherwise."""
    dt = parse_timestamp(raw)
    return dt.isoformat(timespec="seconds") if dt else raw


def parse_key_themes(raw):
    """Decode the stored key_themes value into a list of strings.

    update_rns_enrichment stores json.dumps(list) normally, but passes non-list
    values through unchanged, so be defensive.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return [raw.strip()]
    if isinstance(value, list):
        return [str(t).strip() for t in value if t is not None and str(t).strip()]
    if value is None or value == "":
        return []
    return [str(value).strip()]


def _rns_key(rns_number):
    """Sort key for rns_number tie-breaks: numeric IDs numerically, others after."""
    s = str(rns_number or "")
    return (1, int(s), s) if s.isdigit() else (0, 0, s)


def format_announcement(row):
    """Shape one DB row for export. Unenriched rows get explicit nulls.

    `enriched` is the unambiguous marker: false means never scored, true means
    scored (impact_score is then an int 1-10, however low).
    `routine_type` is the routine filing label from routine_filings.py, or null;
    the site groups on it and holds no patterns of its own.
    """
    enriched = row["impact_score"] is not None
    return {
        "timestamp":    normalise_timestamp(row["timestamp"]),
        "category":     row["category"] or None,
        "headline":     row["headline"],
        "url":          row["url"] or None,
        "routine_type": routine_type(row["headline"], row["category"]),
        "enriched":     enriched,
        "impact_score": row["impact_score"] if enriched else None,
        "insight":      (row["insight"] or None) if enriched else None,
        "key_themes":   parse_key_themes(row["key_themes"]) if enriched else None,
    }


def group_rows(rows):
    """Group rows by ticker. Returns (groups, no_ticker_count, invalid_ts_rows)."""
    groups        = {}
    no_ticker     = 0
    invalid_ts    = []
    for row in rows:
        ticker = (row["ticker"] or "").strip()
        if not ticker:
            no_ticker += 1
            continue
        if parse_timestamp(row["timestamp"]) is None:
            invalid_ts.append(row)
        groups.setdefault(ticker, []).append(row)
    return groups, no_ticker, invalid_ts


def sort_company_rows(rows):
    """Newest first; rows with unparseable timestamps go last."""
    valid   = [r for r in rows if parse_timestamp(r["timestamp"]) is not None]
    invalid = [r for r in rows if parse_timestamp(r["timestamp"]) is None]
    valid.sort(key=lambda r: (parse_timestamp(r["timestamp"]), _rns_key(r["rns_number"])), reverse=True)
    invalid.sort(key=lambda r: _rns_key(r["rns_number"]), reverse=True)
    return valid + invalid


def build_company(ticker, rows, slug):
    """Build the exported dict for one company. rows must already be sorted."""
    valid_rows = [r for r in rows if parse_timestamp(r["timestamp"]) is not None]
    latest     = normalise_timestamp(valid_rows[0]["timestamp"]) if valid_rows else None
    # Display name: from the most recent row with a valid timestamp, else any row
    name_row   = valid_rows[0] if valid_rows else rows[0]
    return {
        "name":                (name_row["company_name"] or "").strip() or ticker,
        "ticker":              ticker,
        "slug":                slug,
        "announcement_count":  len(rows),
        "latest_announcement": latest,
        "announcements":       [format_announcement(r) for r in rows],
    }


def assign_slug(ticker, company_name, slug_map):
    """Return the sticky slug for ticker, adding a new entry to slug_map if needed."""
    if ticker in slug_map:
        return slug_map[ticker]
    base  = make_slug(company_name, ticker)
    used  = set(slug_map.values())
    slug  = base
    n     = 2
    while slug in used:
        slug = f"{base}-{n}"
        n += 1
    slug_map[ticker] = slug
    return slug


def _latest_sort_key(company):
    """Most recent first, companies with no valid date last, then by slug."""
    latest = parse_timestamp(company["latest_announcement"])
    return (latest is not None, latest or datetime.min)


def serialise(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


# ── file I/O ─────────────────────────────────────────────────

def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def write_if_changed(path, text, dry_run):
    """Write text to path only if it differs. Returns 'new', 'changed' or 'unchanged'."""
    existing = read_text(path)
    if existing == text:
        return "unchanged"
    status = "new" if existing is None else "changed"
    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return status


def load_slug_map(map_path, companies_dir):
    """Load the sticky ticker → slug map, recovering entries from existing files.

    A corrupt map is fatal: regenerating it could move live URLs.
    Any company file on disk whose ticker is missing from the map has its slug
    adopted, so losing the map file can never cause a page to be re-slugged.
    """
    slug_map = {}
    raw = read_text(map_path)
    if raw is not None:
        try:
            slug_map = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"slug map {map_path} is not valid JSON: {e}")
        if not isinstance(slug_map, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in slug_map.items()
        ):
            raise RuntimeError(f"slug map {map_path} must be an object of ticker → slug strings")

    if os.path.isdir(companies_dir):
        for fname in sorted(os.listdir(companies_dir)):
            if not fname.endswith(".json"):
                continue
            try:
                data = json.loads(read_text(os.path.join(companies_dir, fname)))
                ticker, slug = data["ticker"], data["slug"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if ticker and slug and ticker not in slug_map:
                log(f"  ⚠ Slug map had no entry for {ticker} — adopting existing slug '{slug}'")
                slug_map[ticker] = slug
    return slug_map


# ── git ──────────────────────────────────────────────────────

def _git(repo_path, *args):
    """Same subprocess pattern as delivery.GitHubDelivery._git."""
    result = subprocess.run(
        ["git", "-C", repo_path] + list(args),
        capture_output=True, text=True
    )
    if result.returncode != 0:
        msg = (result.stderr.strip() or result.stdout.strip() or
               f"git {' '.join(args)} exited with code {result.returncode}")
        raise RuntimeError(msg)
    return result.stdout.strip()


def commit_and_push(repo_path, branch, rel_paths, message, git=_git):
    """Stage and commit only rel_paths, then pull --rebase and push.

    Committing with an explicit pathspec means anything else already staged in
    the site repo is left alone. No weekend skip — this runs any day.
    """
    git(repo_path, "add", "-A", "--", *rel_paths)
    try:
        git(repo_path, "commit", "-m", message, "--", *rel_paths)
    except RuntimeError as e:
        if "nothing to commit" in str(e).lower() or "nothing added" in str(e).lower():
            log("  ℹ GitHub: no changes to commit")
            return
        raise
    git(repo_path, "pull", "--rebase", "origin", branch)
    git(repo_path, "push", "origin", branch)
    log(f"  ✅ GitHub: pushed company data → {branch}")


# ── export ───────────────────────────────────────────────────

def export_companies(rows, repo_path, companies_rel, branch="main",
                     dry_run=False, limit=None, min_announcements=None, git=_git):
    """Run the export against already-fetched rows. Returns a summary dict."""
    min_n         = MIN_ANNOUNCEMENTS if min_announcements is None else min_announcements
    companies_dir = os.path.join(repo_path, companies_rel)
    data_rel      = os.path.dirname(companies_rel.rstrip("/\\"))
    index_rel     = os.path.join(data_rel, INDEX_FILENAME)
    map_rel       = os.path.join(data_rel, SLUG_MAP_FILENAME)
    index_path    = os.path.join(repo_path, index_rel)
    map_path      = os.path.join(repo_path, map_rel)
    tag           = " [DRY RUN]" if dry_run else ""

    # Start from origin's current state so the slug map and files we compare
    # against are the latest (e.g. hand-corrected slugs). A stale clone would
    # otherwise write old-slug files and hit rebase conflicts on push.
    # Skipped for --dry-run (no side effects) and --limit (local test mode).
    if not dry_run and limit is None:
        git(repo_path, "pull", "--rebase", "--autostash", "origin", branch)

    # Newswire / commentary feeds get no company page (excluded_accounts.py);
    # an existing page is pruned below like any company that no longer qualifies.
    excluded = [r for r in rows if is_excluded(r.get("ticker"), r.get("url"))]
    rows = [r for r in rows if not is_excluded(r.get("ticker"), r.get("url"))]

    groups, no_ticker, invalid_ts = group_rows(rows)
    for r in invalid_ts[:MAX_TIMESTAMP_WARNINGS]:
        log(f"  ⚠ Unparseable timestamp {r['timestamp']!r} — {r['ticker']} rns {r['rns_number']} (sorted last)")
    if len(invalid_ts) > MAX_TIMESTAMP_WARNINGS:
        log(f"  ⚠ ...and {len(invalid_ts) - MAX_TIMESTAMP_WARNINGS} more unparseable timestamp(s)")

    eligible  = {t: sort_company_rows(rs) for t, rs in groups.items() if len(rs) >= min_n}
    below_min = len(groups) - len(eligible)

    slug_map     = load_slug_map(map_path, companies_dir)

    # Build provisional companies (slug filled in below) and order by recency
    debt_issuers = {t for t, rs in eligible.items() if is_debt_programme_issuer(rs)}
    companies = [build_company(t, rs, None) for t, rs in eligible.items() if t not in debt_issuers]
    companies.sort(key=lambda c: c["ticker"])                       # deterministic base order
    companies.sort(key=_latest_sort_key, reverse=True)
    if limit is not None:
        companies = companies[:limit]

    # Assign sticky slugs in ticker order so new-slug collisions resolve deterministically
    for c in sorted(companies, key=lambda c: c["ticker"]):
        c["slug"] = assign_slug(c["ticker"], c["name"], slug_map)

    if not dry_run:
        os.makedirs(companies_dir, exist_ok=True)

    counts = {"new": 0, "changed": 0, "unchanged": 0}
    for c in companies:
        status = write_if_changed(os.path.join(companies_dir, f"{c['slug']}.json"), serialise(c), dry_run)
        counts[status] += 1
        if status != "unchanged":
            log(f"  📝 {status:<7} {c['slug']}.json ({c['announcement_count']} announcements){tag}")

    # Slug map: append-only; written whenever new tickers were assigned
    map_status = write_if_changed(map_path, serialise(dict(sorted(slug_map.items()))), dry_run)
    if map_status != "unchanged":
        log(f"  📝 {map_status:<7} {map_rel}{tag}")

    summary = {
        "exported":          len(companies),
        "new":               counts["new"],
        "changed":           counts["changed"],
        "unchanged":         counts["unchanged"],
        "pruned":            0,
        "below_min":         below_min,
        "debt_programme":    len(debt_issuers),
        "excluded_rows":     len(excluded),
        "no_ticker_rows":    no_ticker,
        "invalid_timestamps": len(invalid_ts),
        "index_status":      "skipped",
        "map_status":        map_status,
        "committed":         False,
    }

    if limit is not None:
        log(f"  ℹ --limit set: index, pruning and git skipped")
        return summary

    # Index
    index = [
        {k: c[k] for k in ("name", "ticker", "slug", "announcement_count", "latest_announcement")}
        for c in companies
    ]
    summary["index_status"] = write_if_changed(index_path, serialise(index), dry_run)
    if summary["index_status"] != "unchanged":
        log(f"  📝 {summary['index_status']:<7} {index_rel}{tag}")

    # Prune stale files — never one whose slug belongs to a ticker that still qualifies
    exported_slugs  = {c["slug"] for c in companies}
    protected_slugs = {slug_map[t] for t in eligible if t in slug_map}
    if os.path.isdir(companies_dir):
        for fname in sorted(os.listdir(companies_dir)):
            if not fname.endswith(".json"):
                continue
            slug = fname[:-len(".json")]
            if slug in exported_slugs or slug in protected_slugs:
                continue
            try:
                json.loads(read_text(os.path.join(companies_dir, fname)))
            except json.JSONDecodeError:
                log(f"  ⚠ Not pruning unreadable file {fname} — check it by hand")
                continue
            log(f"  🗑  prune   {fname}{tag}")
            if not dry_run:
                os.remove(os.path.join(companies_dir, fname))
            summary["pruned"] += 1

    touched = (summary["new"] + summary["changed"] + summary["pruned"]
               + (summary["index_status"] != "unchanged")
               + (summary["map_status"] != "unchanged"))
    if touched and not dry_run:
        n = summary["new"] + summary["changed"]
        message = f"companies: {n} updated, {summary['pruned']} pruned ({summary['exported']} total)"
        commit_and_push(repo_path, branch, [companies_rel, index_rel, map_rel], message, git=git)
        summary["committed"] = True
    elif touched:
        log(f"  ℹ Would commit and push {touched} change(s){tag}")
    else:
        log("  ℹ No changes — nothing to commit")

    return summary


def log_summary(s, dry_run):
    tag = " [DRY RUN]" if dry_run else ""
    log("")
    log(f"✅ Company export complete{tag}")
    log(f"   Companies exported:          {s['exported']}")
    log(f"   Files changed:               {s['new'] + s['changed']} ({s['new']} new, {s['changed']} updated)")
    log(f"   Files unchanged:             {s['unchanged']}")
    log(f"   Files pruned:                {s['pruned']}")
    log(f"   Skipped (< {MIN_ANNOUNCEMENTS} announcements): {s['below_min']} companies")
    log(f"   Skipped (debt programme):    {s['debt_programme']} issuers")
    log(f"   Skipped (excluded accounts): {s.get('excluded_rows', 0)} announcements")
    log(f"   Skipped (no ticker):         {s['no_ticker_rows']} announcements")
    log(f"   Unparseable timestamps:      {s['invalid_timestamps']}")


def main():
    args    = sys.argv[1:]
    dry_run = "--dry-run" in args
    limit   = None
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                print(f"Error: --limit requires an integer, got {args[i + 1]!r}")
                sys.exit(1)
            if limit < 1:
                print("Error: --limit must be at least 1")
                sys.exit(1)

    repo_path     = os.environ.get("GITHUB_REPO_PATH", "")
    companies_rel = os.environ.get("GITHUB_COMPANIES_DIR", DEFAULT_COMPANIES_DIR)
    branch        = os.environ.get("GITHUB_BRANCH", "main")
    if not repo_path:
        log("❌ GITHUB_REPO_PATH not set in .env")
        sys.exit(1)
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        log(f"❌ GITHUB_REPO_PATH '{repo_path}' is not a git repository")
        sys.exit(1)

    init_db()
    rows = get_rns_for_export()
    log(f"🏢 Exporting companies from {len(rows)} RNS announcement(s)"
        + (" [DRY RUN]" if dry_run else "") + "...")

    try:
        summary = export_companies(rows, repo_path, companies_rel, branch=branch,
                                   dry_run=dry_run, limit=limit)
    except RuntimeError as e:
        log(f"❌ Company export failed: {e}")
        sys.exit(1)
    log_summary(summary, dry_run)


if __name__ == "__main__":
    main()
