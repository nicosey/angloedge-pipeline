import sqlite3
import json
import os
from datetime import datetime

from config import DB_PATH, ARCHIVE_DB_PATH, log


DB_TIMEOUT = 30  # seconds to wait before raising "locked" error


def _connect():
    return sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)


def _connect_archive():
    return sqlite3.connect(ARCHIVE_DB_PATH, timeout=DB_TIMEOUT)


def init_archive_db():
    os.makedirs(os.path.dirname(ARCHIVE_DB_PATH) or ".", exist_ok=True)
    def _():
        con = _connect_archive()
        con.execute("""
            CREATE TABLE IF NOT EXISTS collections (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                topic     TEXT NOT NULL,
                results   TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                timestamp       TEXT PRIMARY KEY,
                topic           TEXT NOT NULL,
                raw_briefing    TEXT,
                aggregated_from TEXT DEFAULT '[]',
                aggregated      INTEGER DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS outputs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_timestamp TEXT NOT NULL,
                output_type   TEXT NOT NULL,
                name          TEXT NOT NULL,
                content       TEXT
            )
        """)
        con.commit()
        con.close()
    _db_op(_)


def _db_op(fn):
    """Wrap a DB operation with a clear locked-error message."""
    try:
        return fn()
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            log("❌ DB locked — another briefing run is writing. Try again in a moment.")
        else:
            log(f"❌ DB error: {e}")
        raise


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    def _():
        con = _connect()
        con.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                timestamp       TEXT PRIMARY KEY,
                topic           TEXT NOT NULL,
                raw_briefing    TEXT,
                aggregated_from TEXT DEFAULT '[]',
                aggregated      INTEGER DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS outputs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_timestamp TEXT NOT NULL,
                output_type   TEXT NOT NULL,
                name          TEXT NOT NULL,
                content       TEXT,
                FOREIGN KEY(run_timestamp) REFERENCES runs(timestamp)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS outbox (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                output_id    INTEGER NOT NULL,
                dest         TEXT NOT NULL,
                message      TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                published_at TEXT,
                FOREIGN KEY(output_id) REFERENCES outputs(id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS collections (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                topic     TEXT NOT NULL,
                results   TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS rns_announcements (
                id           INTEGER PRIMARY KEY,
                rns_number   TEXT UNIQUE,
                timestamp    TEXT,
                company_name TEXT,
                ticker       TEXT,
                headline     TEXT,
                url          TEXT,
                category     TEXT,
                collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                impact_score INTEGER,
                insight      TEXT,
                key_themes   TEXT,
                enriched_at  TEXT
            )
        """)
        # Migration: add enrichment columns to existing DBs that predate this schema
        for col_def in (
            "ADD COLUMN impact_score INTEGER",
            "ADD COLUMN insight TEXT",
            "ADD COLUMN key_themes TEXT",
            "ADD COLUMN enriched_at TEXT",
        ):
            try:
                con.execute(f"ALTER TABLE rns_announcements {col_def}")
            except sqlite3.OperationalError:
                pass  # column already exists
        con.commit()
        con.close()
    _db_op(_)


def save_run(timestamp, topic, raw_briefing, aggregated_from=None):
    def _():
        con = _connect()
        con.execute(
            "INSERT OR REPLACE INTO runs "
            "(timestamp, topic, raw_briefing, aggregated_from) "
            "VALUES (?, ?, ?, ?)",
            (timestamp, topic, raw_briefing, json.dumps(aggregated_from or []))
        )
        con.commit()
        con.close()
        log(f"  🗄  DB: saved run {timestamp}")
    _db_op(_)


def save_output(run_timestamp, output_type, name, content):
    """Save an output and return its inserted id."""
    def _():
        con = _connect()
        cur = con.execute(
            "INSERT INTO outputs (run_timestamp, output_type, name, content) "
            "VALUES (?, ?, ?, ?)",
            (run_timestamp, output_type, name, content)
        )
        output_id = cur.lastrowid
        con.commit()
        con.close()
        log(f"  🗄  DB: saved {output_type} output for {run_timestamp}")
        return output_id
    return _db_op(_)


def add_to_outbox(output_id, dest, message):
    def _():
        con = _connect()
        con.execute(
            "INSERT INTO outbox (output_id, dest, message, created_at) "
            "VALUES (?, ?, ?, ?)",
            (output_id, dest, message, datetime.now().isoformat(timespec="seconds"))
        )
        con.commit()
        con.close()
        log(f"  📬 Outbox: queued for {dest}")
    _db_op(_)


def get_pending_outbox(dest=None):
    """Return [(outbox_id, dest, message)] for unpublished entries."""
    def _():
        con = _connect()
        if dest:
            rows = con.execute(
                "SELECT id, dest, message FROM outbox "
                "WHERE published_at IS NULL AND dest=? "
                "ORDER BY created_at",
                (dest,)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id, dest, message FROM outbox "
                "WHERE published_at IS NULL "
                "ORDER BY created_at"
            ).fetchall()
        con.close()
        return rows
    return _db_op(_)


def cleanup_outbox(max_age_hours=24):
    """Delete published outbox entries older than max_age_hours."""
    def _():
        con = _connect()
        cur = con.execute(
            "DELETE FROM outbox WHERE published_at IS NOT NULL AND created_at < datetime('now', ?)",
            (f"-{max_age_hours} hours",)
        )
        count = cur.rowcount
        con.commit()
        con.close()
        if count:
            log(f"  🗄  DB: cleaned up {count} outbox entr{'y' if count == 1 else 'ies'} older than {max_age_hours}h")
    _db_op(_)


def mark_outbox_published(outbox_id):
    def _():
        con = _connect()
        con.execute(
            "UPDATE outbox SET published_at=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), outbox_id)
        )
        con.commit()
        con.close()
    _db_op(_)


def find_recent_runs(topic, lookback_minutes):
    """Return [(timestamp, narrative_content)] for recent runs in chronological order.

    Uses the outputs table (type='narrative'). Stops as soon as it hits a run
    that already aggregated its predecessors — no need to look further back.
    """
    cutoff = datetime.fromtimestamp(
        datetime.now().timestamp() - lookback_minutes * 60
    ).isoformat()

    def _():
        con = _connect()
        rows = con.execute(
            "SELECT r.timestamp, o.content, r.aggregated_from "
            "FROM runs r "
            "JOIN outputs o ON o.run_timestamp = r.timestamp "
            "WHERE r.topic=? AND r.timestamp>=? AND o.output_type='narrative' "
            "ORDER BY r.timestamp DESC",
            (topic, cutoff)
        ).fetchall()
        con.close()
        return rows

    rows = _db_op(_)
    found = []
    for ts, content, agg_from in rows:
        found.append((ts, content))
        if json.loads(agg_from):  # this run already pulled in predecessors
            break

    return list(reversed(found))  # chronological order


def find_recent_tweets(topic, lookback_minutes):
    """Return list of recent tweet content for a topic, newest first."""
    cutoff = datetime.fromtimestamp(
        datetime.now().timestamp() - lookback_minutes * 60
    ).isoformat()

    def _():
        con = _connect()
        rows = con.execute(
            "SELECT o.content FROM runs r "
            "JOIN outputs o ON o.run_timestamp = r.timestamp "
            "WHERE r.topic=? AND r.timestamp>=? AND o.output_type='tweet' "
            "ORDER BY r.timestamp DESC",
            (topic, cutoff)
        ).fetchall()
        con.close()
        return [row[0] for row in rows]

    return _db_op(_)


def mark_aggregated(timestamps):
    def _():
        con = _connect()
        con.executemany(
            "UPDATE runs SET aggregated=1 WHERE timestamp=?",
            [(ts,) for ts in timestamps]
        )
        con.commit()
        con.close()
    _db_op(_)


def save_collection(topic, results):
    """Save a set of search results to the collections table."""
    def _():
        con = _connect()
        con.execute(
            "INSERT INTO collections (timestamp, topic, results) VALUES (?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), topic, json.dumps(results))
        )
        con.commit()
        con.close()
        log(f"  🗄  DB: saved collection for {topic}")
    _db_op(_)


def get_collections(topic, since_timestamp):
    """Return merged, deduplicated results_data from all collections since since_timestamp."""
    def _():
        con = _connect()
        rows = con.execute(
            "SELECT results FROM collections WHERE topic=? AND timestamp>=? ORDER BY timestamp",
            (topic, since_timestamp)
        ).fetchall()
        con.close()
        return [json.loads(row[0]) for row in rows]

    all_collections = _db_op(_)
    if not all_collections:
        return []

    # Merge sections across all collections, deduplicate by URL
    merged = {}
    seen_urls = set()
    for collection in all_collections:
        for section in collection:
            name = section["section"]
            if name not in merged:
                merged[name] = {"section": name, "emoji": section.get("emoji", ""), "results": []}
            for result in section["results"]:
                url = result.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    merged[name]["results"].append(result)
                elif not url:
                    merged[name]["results"].append(result)

    return list(merged.values())


def get_last_run_timestamp(topic):
    """Return the timestamp of the most recent briefing run for this topic, or None."""
    def _():
        con = _connect()
        row = con.execute(
            "SELECT timestamp FROM runs WHERE topic=? ORDER BY timestamp DESC LIMIT 1",
            (topic,)
        ).fetchone()
        con.close()
        return row[0] if row else None
    return _db_op(_)


def archive_collections(max_age_hours=48):
    """Copy collections older than max_age_hours to archive.db, then delete from live DB."""
    def _():
        live = _connect()
        rows = live.execute(
            "SELECT id, timestamp, topic, results FROM collections "
            "WHERE timestamp < datetime('now', ?)",
            (f"-{max_age_hours} hours",)
        ).fetchall()
        live.close()

        if not rows:
            return

        init_archive_db()
        arc = _connect_archive()
        arc.executemany(
            "INSERT OR IGNORE INTO collections (id, timestamp, topic, results) VALUES (?, ?, ?, ?)",
            rows
        )
        arc.commit()
        arc.close()

        live = _connect()
        cur = live.execute(
            "DELETE FROM collections WHERE timestamp < datetime('now', ?)",
            (f"-{max_age_hours} hours",)
        )
        count = cur.rowcount
        live.commit()
        live.close()
        log(f"  🗄  DB: archived {count} collection(s) older than {max_age_hours}h → archive.db")
    _db_op(_)


def cleanup_collections(max_age_hours=48):
    """Delete collection entries older than max_age_hours."""
    def _():
        con = _connect()
        cur = con.execute(
            "DELETE FROM collections WHERE timestamp < datetime('now', ?)",
            (f"-{max_age_hours} hours",)
        )
        count = cur.rowcount
        con.commit()
        con.close()
        if count:
            log(f"  🗄  DB: cleaned up {count} old collection(s)")
    _db_op(_)


# Categories considered high-signal for LLM enrichment (skip routine/admin ones)
_HIGH_SIGNAL_CATEGORIES = {"Results", "Acquisition", "Trading Update", "Placing", "Director Dealing", "Share Buyback"}


def save_rns_announcements(announcements):
    """
    Insert RNS announcements, skipping any whose rns_number already exists.
    Returns the count of newly inserted rows.
    """
    def _():
        con = _connect()
        saved = 0
        for ann in announcements:
            cur = con.execute(
                "INSERT OR IGNORE INTO rns_announcements "
                "(rns_number, timestamp, company_name, ticker, headline, url, category) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ann["rns_number"],
                    ann["timestamp"],
                    ann["company_name"],
                    ann["ticker"],
                    ann["headline"],
                    ann["url"],
                    ann["category"],
                ),
            )
            saved += cur.rowcount
        con.commit()
        con.close()
        if saved:
            log(f"  🗄  DB: saved {saved} new RNS announcement(s)")
        return saved
    return _db_op(_)


def get_unenriched_rns(limit=50):
    """Return high-signal RNS rows not yet enriched, newest first."""
    def _():
        con = _connect()
        placeholders = ",".join("?" * len(_HIGH_SIGNAL_CATEGORIES))
        rows = con.execute(
            f"SELECT rns_number, timestamp, company_name, ticker, headline, url, category "
            f"FROM rns_announcements "
            f"WHERE impact_score IS NULL AND category IN ({placeholders}) "
            f"ORDER BY timestamp DESC LIMIT ?",
            (*_HIGH_SIGNAL_CATEGORIES, limit),
        ).fetchall()
        con.close()
        return [
            {
                "rns_number":   r[0],
                "timestamp":    r[1],
                "company_name": r[2],
                "ticker":       r[3],
                "headline":     r[4],
                "url":          r[5],
                "category":     r[6],
            }
            for r in rows
        ]
    return _db_op(_)


def update_rns_enrichment(rns_number, impact_score, insight, key_themes):
    """Write LLM enrichment fields back to an rns_announcements row."""
    def _():
        con = _connect()
        con.execute(
            "UPDATE rns_announcements "
            "SET impact_score=?, insight=?, key_themes=?, enriched_at=? "
            "WHERE rns_number=?",
            (
                impact_score,
                insight,
                json.dumps(key_themes) if isinstance(key_themes, list) else key_themes,
                datetime.now().isoformat(timespec="seconds"),
                rns_number,
            ),
        )
        con.commit()
        con.close()
    _db_op(_)
