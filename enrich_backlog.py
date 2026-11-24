"""
Enrich the backlog: unenriched non-routine RNS announcements, plus every row
enriched under an older prompt (ai.RNS_PROMPT_VERSION), which is re-enriched in
place with the current prompt.

Usage:
  python3 enrich_backlog.py              Guard the backlog from Telegram, then enrich it
  python3 enrich_backlog.py --dry-run    Report what would be guarded and enriched; write nothing,
                                         call no model
  python3 enrich_backlog.py --limit N    Enrich at most N rows (the Telegram guard still covers
                                         the whole backlog)
  python3 enrich_backlog.py --force      Run inside the RNS collector's window (testing only)

Selection and enrichment are the live path: db.get_unenriched_rns with
include_stale=True (routine filings excluded unless they already carry an
old-version enrichment), ai.enrich_rns_with_llm and db.update_rns_enrichment,
which stamps the current version. Newest first. A re-enriched row keeps its old
enrichment until the new one is saved, so a failed call loses nothing.

Telegram guard: before any enrichment, every eligible row older than
GUARD_AGE_HOURS (or with an unparseable timestamp) that has never been sent gets
telegram_published_at set to the run time, in one transaction. It covers the
whole backlog even under --limit, because once the blocklist is live the
half-hourly job would otherwise enrich old rows and post them as news.

Resumable: enriched rows drop out of the selection, so an interrupted run loses
at most the call in progress. Rows that fail are not retried within a run.
Like recover_headlines.py, it refuses to start during the collector's
07:00–16:30 window and stops cleanly before it opens.
"""
import sys
import time
from collections import Counter
from datetime import datetime, timedelta

import ai
import db
from config import OLLAMA_ENRICH_MODEL, log
from export_companies import parse_timestamp
from recover_headlines import in_collector_window, QUIET_START, QUIET_END

GUARD_AGE_HOURS = 24
BATCH_SIZE = 10
MAX_CONSECUTIVE_FAILURES = 20     # model down or unparseable output: stop


def eligible_rows(con):
    """Rows to (re-)enrich: rns_number, timestamp, category, telegram_published_at, stale."""
    con.create_function("routine_type", 2, db.routine_type, deterministic=True)
    cur = con.execute(
        "SELECT rns_number, timestamp, category, telegram_published_at, "
        "       impact_score IS NOT NULL FROM rns_announcements "
        f"WHERE {db.ENRICH_WORK_SQL} OR {db.STALE_ENRICHMENT_SQL}", (db.RNS_PROMPT_VERSION,))
    return [dict(zip(("rns_number", "timestamp", "category", "telegram_published_at", "stale"), r))
            for r in cur.fetchall()]


def _get_work(limit, skip):
    return db.get_unenriched_rns(limit=limit, skip=skip, include_stale=True)


def rows_to_guard(rows, now):
    cutoff = now - timedelta(hours=GUARD_AGE_HOURS)
    out = []
    for r in rows:
        if r["telegram_published_at"] is not None:
            continue
        ts = parse_timestamp(r["timestamp"])
        if ts is None or ts < cutoff:
            out.append(r["rns_number"])
    return out


def apply_guard(con, rns_numbers, run_time):
    with con:
        con.executemany(
            "UPDATE rns_announcements SET telegram_published_at=? "
            "WHERE rns_number=? AND telegram_published_at IS NULL",
            [(run_time, n) for n in rns_numbers])


def enrich(limit=None, force=False, now=datetime.now, get=None, enrich_fn=None, save=None):
    """Enrich newest-first in batches; returns a Counter of outcomes."""
    get = get or _get_work
    enrich_fn = enrich_fn or ai.enrich_rns_with_llm
    save = save or db.update_rns_enrichment
    counts, failed, consecutive = Counter(), set(), 0
    started = time.monotonic()
    while limit is None or counts["enriched"] + counts["failed"] < limit:
        if not force and in_collector_window(now()):
            log(f"⏸  Stopping before the RNS collector's window ({QUIET_START:%H:%M}). "
                f"Progress is saved — re-run after {QUIET_END:%H:%M} to continue.")
            counts["stopped_for_window"] = 1
            break
        size = BATCH_SIZE if limit is None else min(BATCH_SIZE, limit - counts["enriched"] - counts["failed"])
        batch = get(limit=size, skip=frozenset(failed))
        if not batch:
            break
        results = dict(enrich_fn(batch))
        for ann in batch:
            e = results.get(ann["rns_number"])
            if e:
                save(ann["rns_number"], e["impact_score"], e["insight"], e["key_themes"])
                counts["enriched"] += 1
                counts[f"score_{e['impact_score']}"] += 1
                consecutive = 0
            else:
                failed.add(ann["rns_number"])
                counts["failed"] += 1
                consecutive += 1
        done = counts["enriched"] + counts["failed"]
        rate = (time.monotonic() - started) / done
        log(f"  … {counts['enriched']} enriched, {counts['failed']} failed  ({rate:.1f}s/row)")
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            log(f"🛑 {consecutive} consecutive failures — is Ollama running? Stopping; progress is saved.")
            counts["aborted"] = 1
            break
    return counts


def main(argv):
    dry_run, force = "--dry-run" in argv, "--force" in argv
    limit = None
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
            if limit < 1:
                raise ValueError
        except (IndexError, ValueError):
            raise SystemExit("--limit needs a positive integer")
    if not dry_run and not force and in_collector_window(datetime.now()):
        log(f"The RNS collector runs until 16:30 — start after {QUIET_END:%H:%M} "
            f"(or pass --force for a small test).")
        return 1

    db.init_db()
    con = db._connect()
    try:
        rows = eligible_rows(con)
        guard = rows_to_guard(rows, datetime.now())
        by_cat = Counter((r["category"] or "(none)") for r in rows)
        stale = sum(1 for r in rows if r["stale"])
        log(f"🔎 To enrich: {len(rows)}  ({len(rows) - stale} new, {stale} re-enriched from an older "
            f"prompt)  — model {OLLAMA_ENRICH_MODEL}, prompt v{db.RNS_PROMPT_VERSION}")
        for cat, n in by_cat.most_common():
            log(f"    {n:6}  {cat}")
        log(f"🛡  Telegram guard: {len(guard)} row(s) older than {GUARD_AGE_HOURS}h "
            + ("would be marked" if dry_run else "to mark") + " as published")
        if dry_run:
            log(f"Would enrich {min(len(rows), limit) if limit else len(rows)} row(s). Dry run — nothing written.")
            return 0
        run_time = datetime.now().isoformat(timespec="seconds")
        apply_guard(con, guard, run_time)
        log(f"   guarded at {run_time}")
    finally:
        con.close()

    counts = enrich(limit=limit, force=force)
    log("")
    log(f"Enriched: {counts['enriched']}")
    log(f"Failed:   {counts['failed']}  (left unenriched; retried on the next run)")
    scores = {k[6:]: v for k, v in counts.items() if k.startswith("score_")}
    if scores:
        log("Scores:   " + "  ".join(f"{s}:{n}" for s, n in sorted(scores.items(), key=lambda x: int(x[0]))))
    con = db._connect()
    try:
        log(f"Remaining: {len(eligible_rows(con))}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
