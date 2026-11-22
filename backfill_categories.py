"""
Re-run headline categorisation over every stored RNS announcement.

Usage:
  python3 backfill_categories.py            Dry run: report what would change; write nothing
  python3 backfill_categories.py --apply    Back up the DB, then apply the changes in one transaction

For every row whose stored category differs from what collect._detect_category
now returns:
  * the category is updated;
  * any existing enrichment (impact_score, insight, key_themes, enriched_at) is
    cleared — it was generated for the wrong category (e.g. the score floor for
    Acquisition), so it is wrong, not merely stale. If the new category is
    high-signal the row is re-enriched on the normal schedule.

Telegram guard: rows that become eligible for enrichment only because of this
backfill (a new or changed category, the widened high-signal set, or cleared
enrichment), are older than GUARD_AGE_HOURS and have never been sent get
telegram_published_at set to the backfill run time, so process_rns.py --publish
never posts months-old announcements as news. Their enrichment still reaches
the site. Rows with an unparseable timestamp are guarded too.

Run outside the RNS job's schedule (07:00–16:30) so it cannot race it.
"""
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta

import db
from collect import _detect_category
from config import log
from export_companies import parse_timestamp

# Enrichment categories before this change. Rows already in one of these with
# no score were going to be enriched and published anyway, so are not guarded.
OLD_HIGH_SIGNAL_CATEGORIES = frozenset(
    {"Results", "Acquisition", "Trading Update", "Placing", "Director Dealing", "Share Buyback"})

GUARD_AGE_HOURS = 24


def plan_changes(rows, now, high_signal=None):
    """
    Decide the update for each row. rows: dicts with rns_number, timestamp,
    headline, category, impact_score, telegram_published_at.
    Returns a list of dicts: rns_number, old, new, clear, guard.
    """
    high_signal = db._HIGH_SIGNAL_CATEGORIES if high_signal is None else high_signal
    cutoff = now - timedelta(hours=GUARD_AGE_HOURS)
    changes = []
    for r in rows:
        old = r["category"] or ""
        new = _detect_category(r["headline"] or "") or ""
        enriched = r["impact_score"] is not None
        clear = old != new and enriched

        pending_after = new in high_signal and (not enriched or clear)
        pending_before = old in OLD_HIGH_SIGNAL_CATEGORIES and not enriched
        ts = parse_timestamp(r["timestamp"])
        guard = (pending_after and not pending_before
                 and r["telegram_published_at"] is None
                 and (ts is None or ts < cutoff))

        if old != new or guard:
            changes.append({"rns_number": r["rns_number"], "old": old, "new": new,
                            "clear": clear, "guard": guard,
                            "pending": pending_after and not pending_before,
                            "unparseable": ts is None})
    return changes


def load_rows(con):
    cur = con.execute(
        "SELECT rns_number, timestamp, headline, category, impact_score, telegram_published_at "
        "FROM rns_announcements")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def apply_changes(con, changes, run_time):
    with con:  # one transaction
        for c in changes:
            sets, args = ["category=?"], [c["new"]]
            if c["clear"]:
                sets.append("impact_score=NULL, insight=NULL, key_themes=NULL, enriched_at=NULL")
            if c["guard"]:
                sets.append("telegram_published_at=?")
                args.append(run_time)
            con.execute(f"UPDATE rns_announcements SET {', '.join(sets)} WHERE rns_number=?",
                        (*args, c["rns_number"]))


def backup_db(con):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = f"{db.DB_PATH}.pre-backfill-{stamp}"
    dest = sqlite3.connect(path)
    con.backup(dest)
    dest.close()
    return path


def report(rows, changes):
    label = lambda c: c or "(none)"
    moves = Counter((c["old"], c["new"]) for c in changes if c["old"] != c["new"])
    log(f"Rows scanned: {len(rows)}")
    log(f"Category changes: {sum(moves.values())}")
    for (old, new), n in moves.most_common():
        log(f"  {n:6}  {label(old):>16} → {label(new)}")
    before = Counter(label(r["category"] or "") for r in rows)
    after = Counter(before)
    for (old, new), n in moves.items():
        after[label(old)] -= n
        after[label(new)] += n
    log("Category totals (before → after):")
    for cat in sorted(set(before) | set(after), key=lambda k: -after[k]):
        log(f"  {label(cat):>16}  {before[cat]:6} → {after[cat]:6}")
    log(f"Enrichment cleared: {sum(c['clear'] for c in changes)}")
    log(f"Newly queued for enrichment: {sum(c['pending'] for c in changes)}")
    log(f"  of which Telegram-guarded (older than {GUARD_AGE_HOURS}h or unparseable): "
        f"{sum(c['guard'] for c in changes)}"
        f" — unparseable timestamps: {sum(c['guard'] and c['unparseable'] for c in changes)}")
    log(f"  of which recent enough to publish normally: "
        f"{sum(c['pending'] and not c['guard'] for c in changes)}")


def main(argv):
    apply = "--apply" in argv
    db.init_db()
    con = db._connect()
    try:
        rows = load_rows(con)
        run_time = datetime.now().isoformat(timespec="seconds")
        changes = plan_changes(rows, datetime.now())
        report(rows, changes)
        if not apply:
            log("Dry run — nothing written. Re-run with --apply to write.")
            return 0
        if not changes:
            log("Nothing to apply.")
            return 0
        log(f"Backup: {backup_db(con)}")
        apply_changes(con, changes, run_time)
        log(f"Applied {len(changes)} row update(s) at {run_time}.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
