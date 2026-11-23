"""
One-off diagnostics for the enrichment blocklist change. Writes nothing to the DB.

Usage:
  python3 enrich_diagnostics.py --rate
      Seconds per enrichment call, measured from recent enriched_at timestamps.

  python3 enrich_diagnostics.py --compare-category [--n 20] [--repeats 2] [--seed 1]
      Score N uncategorised, non-routine, unenriched rows with the live prompt
      two ways — "Category: Other" and with the Category line omitted — each
      REPEATS times, and compare. Uses the live model, prompt text, temperature
      and token limit; no score floors are applied (they are category-specific).
"""
import random
import sqlite3
import statistics as st
import sys
from collections import Counter
from datetime import datetime

import ai
import db
from config import OLLAMA_ENRICH_MODEL, log
from routine_filings import is_routine

VARIANTS = {
    "other":   lambda p: p.replace("Category: {category}", "Category: Other"),
    "omitted": lambda p: p.replace("Category: {category}\n", ""),
}


def measure_rate(con, days=3, max_gap=120):
    stamps = [datetime.fromisoformat(r[0]) for r in con.execute(
        "SELECT enriched_at FROM rns_announcements WHERE enriched_at >= date('now', ?) "
        "ORDER BY enriched_at", (f"-{days} day",)) if r[0]]
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    gaps = [g for g in gaps if 0 < g < max_gap]          # within a run, not between runs
    if not gaps:
        log("No consecutive enrichments in the period — nothing to measure.")
        return None
    med = st.median(gaps)
    log(f"{len(stamps)} enrichments in the last {days} days; {len(gaps)} in-run gaps")
    log(f"Seconds per call: median {med:.1f}, p90 {sorted(gaps)[int(len(gaps) * 0.9)]:.1f}")
    for n in (10_000, 11_000):
        log(f"  {n:,} rows ≈ {n * med / 3600:.1f} h  (overnight window 16:35–06:55 ≈ 14.3 h)")
    return med


def sample_rows(con, n, seed):
    rows = [dict(zip(("rns_number", "company_name", "ticker", "headline", "category"), r))
            for r in con.execute(
                "SELECT rns_number, company_name, ticker, headline, category FROM rns_announcements "
                "WHERE impact_score IS NULL AND (category IS NULL OR category = '') "
                "ORDER BY timestamp DESC LIMIT 3000")]
    rows = [r for r in rows if not is_routine(r["headline"], r["category"])]
    random.Random(seed).shuffle(rows)
    return rows[:n]


def score(template, row):
    prompt = template.format(company=row["company_name"] or "Unknown",
                             ticker=row["ticker"] or "—", category="", headline=row["headline"])
    raw = ai._ollama_call(prompt, max_tokens=300, model=OLLAMA_ENRICH_MODEL)
    parsed = ai._parse_enrichment_json(raw or "", category="")
    return parsed["impact_score"] if parsed else None


def compare_category(con, n, repeats, seed):
    rows = sample_rows(con, n, seed)
    log(f"Scoring {len(rows)} rows × {len(VARIANTS)} variants × {repeats} repeats with {OLLAMA_ENRICH_MODEL}")
    templates = {k: f(ai._RNS_ENRICH_PROMPT) for k, f in VARIANTS.items()}
    assert "Category:" not in templates["omitted"] and "Category: Other" in templates["other"]
    results = {k: [] for k in VARIANTS}                      # per row: list of scores
    for i, row in enumerate(rows, 1):
        for k, t in templates.items():
            results[k].append([score(t, row) for _ in range(repeats)])
        o, m = results["other"][-1], results["omitted"][-1]
        log(f"  [{i:2}/{len(rows)}] other={o} omitted={m}  {row['company_name']}: {row['headline'][:60]}")

    def mean_of(lst):
        vals = [s for s in lst if s is not None]
        return st.mean(vals) if vals else None

    per_row = {k: [mean_of(v) for v in results[k]] for k in VARIANTS}
    pairs = [(a, b) for a, b in zip(per_row["other"], per_row["omitted"]) if a is not None and b is not None]
    diffs = [b - a for a, b in pairs]
    noise = [abs(r[0] - r[1]) for k in VARIANTS for r in results[k]
             if repeats >= 2 and r[0] is not None and r[1] is not None]

    log("")
    for k in VARIANTS:
        flat = [s for r in results[k] for s in r if s is not None]
        fails = sum(s is None for r in results[k] for s in r)
        log(f"{k:>8}: mean {st.mean(flat):.2f}  median {st.median(flat)}  "
            f"distribution {dict(sorted(Counter(flat).items()))}  parse failures {fails}")
    if pairs:
        log(f"Paired (omitted − other), {len(pairs)} rows: mean {st.mean(diffs):+.2f}; "
            f"omitted higher on {sum(d > 0 for d in diffs)}, lower on {sum(d < 0 for d in diffs)}, "
            f"equal on {sum(d == 0 for d in diffs)}")
    if noise:
        log(f"Run-to-run noise (same prompt, same row): mean |Δ| {st.mean(noise):.2f}")


def main(argv):
    def opt(name, default):
        return int(argv[argv.index(name) + 1]) if name in argv else default
    con = sqlite3.connect(db.DB_PATH)
    try:
        if "--rate" in argv:
            measure_rate(con)
        if "--compare-category" in argv:
            compare_category(con, opt("--n", 20), opt("--repeats", 2), opt("--seed", 1))
        if not {"--rate", "--compare-category"} & set(argv):
            print(__doc__)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
