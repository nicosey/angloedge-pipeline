"""
Research tool — query collected/archived articles and optionally analyse with Ollama.

Usage:
  # Query archive for a topic + keyword
  python3 research.py --topic uk_capital_markets --keyword "Barclays" --from 2026-04-01

  # Live search for specific queries
  python3 research.py --live "Barclays Q1 results UK" --live "Barclays FCA investigation"

  # Combined: archive query + live search + AI analysis
  python3 research.py --topic uk_capital_markets --keyword "gilts" --live "UK gilts today" --analyse

  # Save live results to archive for future queries
  python3 research.py --live "HSBC results UK" --save
"""
import json
import sys
import os
import sqlite3
import urllib.request
from datetime import datetime

from config import OLLAMA_URL, OLLAMA_BRIEFING_MODEL, ARCHIVE_DB_PATH, DB_PATH, log
from search import search_searxng
from db import init_archive_db, save_collection


# ── helpers ──────────────────────────────────────────────────

def _query_db(db_path, topic, from_dt, to_dt):
    """Return all articles from a DB file for a topic and date range."""
    if not os.path.isfile(db_path):
        return []
    try:
        con = sqlite3.connect(db_path, timeout=10)
        q   = "SELECT timestamp, topic, results FROM collections WHERE 1=1"
        params = []
        if topic:
            q += " AND topic=?"
            params.append(topic)
        if from_dt:
            q += " AND timestamp >= ?"
            params.append(from_dt)
        if to_dt:
            q += " AND timestamp <= ?"
            params.append(to_dt)
        q += " ORDER BY timestamp"
        rows = con.execute(q, params).fetchall()
        con.close()
        return rows
    except Exception as e:
        log(f"⚠ DB query failed ({db_path}): {e}")
        return []


def _extract_articles(rows, keyword=None):
    """Flatten collection rows into a list of article dicts, optionally filtered."""
    articles = []
    kw = keyword.lower() if keyword else None
    seen_urls = set()
    for ts, topic, results_json in rows:
        try:
            sections = json.loads(results_json)
        except Exception:
            continue
        for section in sections:
            for r in section.get("results", []):
                url = r.get("url", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                if kw:
                    haystack = (r.get("title", "") + " " + r.get("snippet", "")).lower()
                    if kw not in haystack:
                        continue
                articles.append({
                    "collected": ts,
                    "topic":     topic,
                    "section":   section.get("section", ""),
                    "title":     r.get("title", ""),
                    "url":       r.get("url", ""),
                    "snippet":   r.get("snippet", ""),
                    "published": r.get("published", ""),
                })
    return articles


def _articles_from_live(queries, count=5, time_range="day"):
    """Run live SearXNG searches and return articles."""
    articles = []
    seen_urls = set()
    for query in queries:
        log(f"  🔍 Live: {query}")
        results = search_searxng(query, count=count, category="news", time_range=time_range)
        for r in results:
            url = r.get("url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            articles.append({
                "collected": datetime.now().isoformat(timespec="seconds"),
                "topic":     "live",
                "section":   query,
                "title":     r.get("title", "").strip(),
                "url":       url,
                "snippet":   r.get("content", "").strip()[:200],
                "published": r.get("publishedDate", ""),
            })
    return articles


def _print_articles(articles):
    if not articles:
        log("📭 No articles found.")
        return
    log(f"\n📰 {len(articles)} article(s) found:\n")
    for i, a in enumerate(articles, 1):
        pub = f" | published: {a['published']}" if a.get("published") else ""
        print(f"[{i}] {a['title']}")
        print(f"     {a['url']}")
        print(f"     collected: {a['collected'][:16]}{pub}")
        if a.get("snippet"):
            print(f"     {a['snippet'][:120]}...")
        print()


def _analyse(articles, persona=None):
    """Send articles to Ollama for research analysis."""
    if not articles:
        log("⚠ No articles to analyse.")
        return

    persona = persona or "a senior research analyst"
    articles_text = ""
    for i, a in enumerate(articles, 1):
        pub = f" (published: {a['published']})" if a.get("published") else ""
        articles_text += f"\n[{i}] {a['title']}{pub}\n{a['url']}\n{a['snippet']}\n"

    prompt = f"""You are {persona}.
Analyse the articles below and provide a concise research summary.

Rules:
- Identify the key themes and patterns across all articles
- Highlight the most significant developments
- Note any conflicting information or gaps
- Suggest what to watch or investigate further
- Keep it under 400 words
- Plain text, no markdown

ARTICLES:
{articles_text}

Research summary:"""

    log("\n🧠 Analysing with Ollama...\n")
    payload = json.dumps({
        "model":   OLLAMA_BRIEFING_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream":  False,
        "think":   False,
        "options": {"temperature": 0.2, "num_predict": 2000}
    }).encode("utf-8")
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=payload)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=120) as resp:
            data    = json.loads(resp.read().decode())
            content = data.get("message", {}).get("content", "").strip()
            if content:
                print("\n" + "=" * 40)
                print("ANALYSIS")
                print("=" * 40)
                print(content)
                print("=" * 40 + "\n")
            else:
                log("⚠ Ollama returned empty response.")
    except Exception as e:
        log(f"❌ Ollama call failed: {e}")


# ── main ─────────────────────────────────────────────────────

def main():
    raw_args = sys.argv[1:]

    # Parse flags
    topic   = None
    keyword = None
    from_dt = None
    to_dt   = None
    lives      = []
    analyse    = "--analyse" in raw_args
    save       = "--save"    in raw_args
    count      = 5
    time_range = "day"

    i = 0
    while i < len(raw_args):
        a = raw_args[i]
        if a == "--topic"      and i + 1 < len(raw_args): topic      = raw_args[i+1]; i += 2; continue
        if a == "--keyword"    and i + 1 < len(raw_args): keyword    = raw_args[i+1]; i += 2; continue
        if a == "--from"       and i + 1 < len(raw_args): from_dt    = raw_args[i+1]; i += 2; continue
        if a == "--to"         and i + 1 < len(raw_args): to_dt      = raw_args[i+1]; i += 2; continue
        if a == "--live"       and i + 1 < len(raw_args): lives.append(raw_args[i+1]); i += 2; continue
        if a == "--time-range" and i + 1 < len(raw_args): time_range = raw_args[i+1]; i += 2; continue
        if a == "--count"      and i + 1 < len(raw_args):
            try: count = int(raw_args[i+1])
            except ValueError: pass
            i += 2; continue
        i += 1

    if not topic and not lives:
        print(__doc__)
        sys.exit(1)

    log("=" * 40)
    log("🔬 Research Mode")
    log("=" * 40)

    articles = []

    # Archive + live DB query
    if topic:
        log(f"\n📦 Querying archive: topic={topic}"
            + (f" keyword={keyword}" if keyword else "")
            + (f" from={from_dt}" if from_dt else "")
            + (f" to={to_dt}" if to_dt else ""))
        archive_rows = _query_db(ARCHIVE_DB_PATH, topic, from_dt, to_dt)
        live_rows    = _query_db(DB_PATH, topic, from_dt, to_dt)
        all_rows     = archive_rows + live_rows
        db_articles  = _extract_articles(all_rows, keyword)
        log(f"  ✅ {len(db_articles)} article(s) from DB")
        articles.extend(db_articles)

    # Live search
    if lives:
        log(f"\n📡 Running {len(lives)} live search(es) [{time_range}]...")
        live_articles = _articles_from_live(lives, count=count, time_range=time_range)
        log(f"  ✅ {len(live_articles)} article(s) from live search")
        articles.extend(live_articles)

        if save and live_articles:
            log("\n💾 Saving live results to archive...")
            init_archive_db()
            sections = {}
            for a in live_articles:
                s = a["section"]
                if s not in sections:
                    sections[s] = {"section": s, "emoji": "🔍", "results": []}
                sections[s]["results"].append({
                    "title":     a["title"],
                    "url":       a["url"],
                    "snippet":   a["snippet"],
                    "published": a["published"],
                })
            from db import _connect_archive
            con = _connect_archive()
            con.execute(
                "INSERT INTO collections (timestamp, topic, results) VALUES (?, ?, ?)",
                (datetime.now().isoformat(timespec="seconds"),
                 topic or "research",
                 json.dumps(list(sections.values())))
            )
            con.commit()
            con.close()
            log(f"  ✅ Saved to archive.db")

    # Print results
    _print_articles(articles)

    # Analyse
    if analyse:
        _analyse(articles)

    log("Done!")


if __name__ == "__main__":
    main()
