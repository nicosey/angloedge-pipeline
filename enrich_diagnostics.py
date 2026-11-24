"""
One-off diagnostics for the enrichment blocklist change. Writes nothing to the DB.

Usage:
  python3 enrich_diagnostics.py --rate
      Seconds per enrichment call, measured from recent enriched_at timestamps.

  python3 enrich_diagnostics.py --compare-category [--n 20] [--repeats 2] [--seed 1]
      Score N uncategorised, non-routine, unenriched rows with the live prompt
      two ways — "Category: Other" and with the Category line omitted — each
      REPEATS times, and compare. Uses the live model, prompt text, temperature
      and token limit.

  python3 enrich_diagnostics.py --compare-prompt [--n 30] [--seed 1]
      Re-score already-enriched rows with the CURRENT prompt and print old vs new
      score and insight side by side: the known fabrication cases (KNOWN_CASES)
      plus N random enriched rows. Flags names, numbers and dates in each insight
      that are not in the company name or headline (a heuristic, for reading —
      not a verdict). Writes nothing.

  python3 enrich_diagnostics.py --audit [--n 50] [--seed 11]
      Audit N random insights produced by the CURRENT prompt version. Each is
      labelled by the local model as judge, using the same three labels as the
      hand-labelled baseline (audits/insights_v1_baseline.json):
        unsupported  states a specific fact not in the headline or company name
        outside      leans on outside knowledge or speculation, nothing checkable invented
        clean        reasons only from the headline / what the announcement type means
      Prints the breakdown next to the baseline's, plus the outside-term heuristic,
      and writes output/audit/insights_v<version>_<date>.json with every row, the
      judge's label and reason, and an empty "human_label" for a manual read.
      Reads the DB only.

  python3 enrich_diagnostics.py --audit-calibrate
      Run the same judge over the baseline's 50 hand-labelled rows and report how
      often it agrees with the hand labels — how far to trust --audit's numbers.
"""
import json
import os
import random
import re
import sqlite3
import statistics as st
import sys
import time
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


# (company LIKE, headline LIKE) — insights found inventing facts under prompt v1
KNOWN_CASES = [
    ("%Elecosoft%", "%Court Sanction%"), ("%Distil%", "%Admin%"), ("%THG%", "%Capital Reduction%"),
    ("%Concurrent%", "%Production Orders%"), ("%Conduit%", "%Directorate%"),
    ("%BW Energy%", "%LTIP%"), ("%Oriole%", "%Senala%"), ("%Drax%", "%data centre%"),
    ("%Trufin%", "%Special Dividend%"), ("%Instalco%", "%Interim report%"),
    ("%Wilmington%", "%Preliminary Results%"), ("%Titon%", "%Acquisition%"),
]

_GENERIC = set(
    "UK US EU AIM FTSE CEO CFO COO NAV REIT REITs AGM EGM IPO M&A EPS ESG RNS TVR PDMR "
    "PDMRs LTIP H1 H2 Q1 Q2 Q3 Q4 FY Board The This That These It Its A An If As While "
    "Without Such Shareholders Investors".split())


def outside_terms(insight, source):
    """Proper-noun-like terms, amounts, percentages and years in `insight` absent from `source`.
    Sentence-initial single words are ignored (they are capitalised anyway)."""
    src = re.sub(r"[^a-z0-9]", "", source.lower())
    found = set()
    name = r"(?:[A-Z][A-Za-z&\-]{2,}|[a-z]+[A-Z][A-Za-z]+)"          # Wirecard, iSoftStone
    for m in re.finditer(rf"{name}(?:\s+{name})*", insight):
        before = insight[:m.start()].rstrip()
        sentence_start = not before or before.endswith((".", "!", "?", ";", ":"))
        if sentence_start and " " not in m.group(0):
            continue
        found.add(m.group(0))
    found |= set(re.findall(r"[£$€]\s?\d[\d,.]*\s?(?:m|bn|k)?|\b(?:19|20)\d\d\b|\b\d+(?:\.\d+)?%", insight))
    out = []
    for term in sorted(found):
        words = [w for w in re.split(r"\s+", term.replace("'s", "")) if w]
        if all(w in _GENERIC for w in words):
            continue
        if re.sub(r"[^a-z0-9]", "", term.replace("'s", "").lower()) in src:
            continue
        if any(len(w) > 3 and re.sub(r"[^a-z0-9]", "", w.lower()) in src for w in words):
            continue
        out.append(term)
    return out


def compare_prompt(con, n, seed):
    cols = "rns_number, company_name, ticker, headline, category, impact_score, insight"
    keys = cols.split(", ")
    known = []
    for company, headline in KNOWN_CASES:
        r = con.execute(f"SELECT {cols} FROM rns_announcements WHERE impact_score IS NOT NULL "
                        "AND company_name LIKE ? AND headline LIKE ? ORDER BY timestamp DESC LIMIT 1",
                        (company, headline)).fetchone()
        if r:
            known.append(dict(zip(keys, r)))
    pool = [dict(zip(keys, r)) for r in con.execute(
        f"SELECT {cols} FROM rns_announcements WHERE impact_score IS NOT NULL "
        "ORDER BY timestamp DESC LIMIT 5000")]
    random.Random(seed).shuffle(pool)
    seen = {r["rns_number"] for r in known}
    rows = known + [r for r in pool if r["rns_number"] not in seen][:n]
    log(f"Re-scoring {len(known)} known cases + {len(rows) - len(known)} random rows with prompt "
        f"v{ai.RNS_PROMPT_VERSION} on {OLLAMA_ENRICH_MODEL}. Nothing is written.\n")

    old_flagged = new_flagged = fails = 0
    pairs = []
    started = time.monotonic()
    for i, row in enumerate(rows, 1):
        prompt = ai._RNS_ENRICH_PROMPT.format(
            company=row["company_name"] or "Unknown", ticker=row["ticker"] or "—",
            category=row["category"] or db.PROMPT_CATEGORY_FOR_BLANK, headline=row["headline"])
        parsed = ai._parse_enrichment_json(ai._ollama_call(prompt, max_tokens=300, model=OLLAMA_ENRICH_MODEL) or "")
        source = f"{row['company_name']} {row['ticker']} {row['headline']}"
        old_terms = outside_terms(row["insight"] or "", source)
        old_flagged += bool(old_terms)
        tag = "known" if i <= len(known) else "random"
        log(f"[{i}/{len(rows)} {tag}] {row['company_name']} — {row['headline']}")
        log(f"   old {row['impact_score']:>2}: {row['insight']}")
        if old_terms:
            log(f"          outside terms: {old_terms}")
        if not parsed:
            fails += 1
            log("   new  —: (no parseable response)\n")
            continue
        new_terms = outside_terms(parsed["insight"], source)
        new_flagged += bool(new_terms)
        pairs.append((row["impact_score"], parsed["impact_score"]))
        log(f"   new {parsed['impact_score']:>2}: {parsed['insight']}")
        if new_terms:
            log(f"          outside terms: {new_terms}")
        log("")

    done = len(rows) - fails
    per_call = (time.monotonic() - started) / max(len(rows), 1)
    log(f"Rows: {len(rows)}  parse failures: {fails}  ({per_call:.1f}s/call — "
        f"16,500 rows ≈ {16_500 * per_call / 3600:.1f} h; window 16:35–06:55 is 14.3 h)")
    log(f"Insights with outside terms: old {old_flagged}/{len(rows)}, new {new_flagged}/{done}")
    if pairs:
        diffs = [b - a for a, b in pairs]
        log(f"Score new − old: mean {st.mean(diffs):+.2f}; higher {sum(d > 0 for d in diffs)}, "
            f"lower {sum(d < 0 for d in diffs)}, same {sum(d == 0 for d in diffs)}")
        log(f"Old scores {dict(sorted(Counter(a for a, _ in pairs).items()))}")
        log(f"New scores {dict(sorted(Counter(b for _, b in pairs).items()))}")


# ── insight audit ──────────────────────────────────────────

AUDIT_LABELS = ("unsupported", "outside", "clean")
BASELINE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audits", "insights_v1_baseline.json")
AUDIT_DIR = os.path.join("output", "audit")

_JUDGE_PROMPT = """\
You are auditing a one-or-two-sentence "insight" written about a UK regulatory
announcement. The writer saw ONLY the company name, category and headline below —
not the announcement, and nothing else about the company.

Company: {company}
Category: {category}
Headline: {headline}
Insight: {insight}

Label the insight with exactly one of:
- "unsupported": it states a specific fact that is not in the headline or company
  name — a company, person, product, project, place, amount, percentage or date;
  or a claim about what the company does, its sector, history, strategy, finances,
  plans or recent events.
- "outside": nothing specific is invented, but it leans on outside knowledge or
  speculation — market conditions, sector trends, precedents, other companies,
  or guesses about causes or outcomes the headline does not state.
- "clean": it reasons only from the headline, or explains what this type of
  announcement generally means for shareholders.

Respond with ONLY a JSON object: {{"label": "<unsupported|outside|clean>", "reason": "<one short sentence>"}}\
"""


def judge(row):
    """(label, reason) from the local model, or (None, raw text) if unparseable."""
    prompt = _JUDGE_PROMPT.format(company=row["company_name"] or "", category=row["category"] or "Other",
                                  headline=row["headline"] or "", insight=row["insight"] or "")
    raw = ai._ollama_call(prompt, max_tokens=150, model=OLLAMA_ENRICH_MODEL) or ""
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(m.group()) if m else {}
    except ValueError:
        data = {}
    label = str(data.get("label", "")).strip().lower()
    return (label, str(data.get("reason", "")).strip()) if label in AUDIT_LABELS else (None, raw[:120])


def breakdown(labels):
    """'unsupported 11 (22%) · outside 8 (16%) · clean 31 (62%)' over labelled rows."""
    done = [l for l in labels if l in AUDIT_LABELS]
    if not done:
        return "no labels"
    c = Counter(done)
    return " · ".join(f"{k} {c[k]} ({c[k] / len(done):.0%})" for k in AUDIT_LABELS)


def load_baseline(path=BASELINE_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def audit_sample(con, n, seed, version):
    keys = ("rns_number", "company_name", "ticker", "headline", "category", "impact_score", "insight")
    rows = [dict(zip(keys, r)) for r in con.execute(
        f"SELECT {', '.join(keys)} FROM rns_announcements "
        "WHERE insight IS NOT NULL AND insight != '' AND COALESCE(enrichment_version, 1) = ? "
        "ORDER BY rns_number", (version,))]
    return random.Random(seed).sample(rows, min(n, len(rows))), len(rows)


def audit(con, n, seed, judge_fn=judge, out_dir=AUDIT_DIR, today=None):
    version = ai.RNS_PROMPT_VERSION
    rows, population = audit_sample(con, n, seed, version)
    if not rows:
        log(f"No insights at prompt v{version} yet — run enrich_backlog.py first.")
        return None
    log(f"Auditing {len(rows)} of {population} v{version} insights (seed {seed}) with {OLLAMA_ENRICH_MODEL} as judge\n")
    for i, row in enumerate(rows, 1):
        row["judge_label"], row["judge_reason"] = judge_fn(row)
        row["outside_terms"] = outside_terms(row["insight"], f"{row['company_name']} {row['ticker']} {row['headline']}")
        row["human_label"] = ""
        log(f"[{i}/{len(rows)}] {row['judge_label'] or 'UNPARSED':>11}  {row['company_name']} — {row['headline']}")
        log(f"              {row['insight']}")
        if row["judge_label"] != "clean" and row["judge_reason"]:
            log(f"              ↳ {row['judge_reason']}")
    baseline = load_baseline()
    log("")
    log(f"Baseline v1 (hand-labelled, {len(baseline['rows'])}): {breakdown([r['label'] for r in baseline['rows']])}")
    log(f"This run v{version} (model judge, {len(rows)}):    {breakdown([r['judge_label'] for r in rows])}")
    unparsed = sum(r["judge_label"] is None for r in rows)
    if unparsed:
        log(f"Judge output unparseable for {unparsed} row(s) — left out of the percentages")
    log(f"Outside-term heuristic: {sum(bool(r['outside_terms']) for r in rows)}/{len(rows)} insights flagged")
    log("The judge is the same model that writes the insights; check --audit-calibrate for how "
        "far it agrees with the hand labels, and fill human_label in the sheet for a manual read.")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"insights_v{version}_{(today or datetime.now()).strftime('%Y-%m-%d')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"prompt_version": version, "seed": seed, "population": population,
                   "judge_model": OLLAMA_ENRICH_MODEL,
                   "judge_counts": dict(Counter(r["judge_label"] for r in rows)),
                   "rows": rows}, f, indent=2, ensure_ascii=False)
    log(f"Sheet: {path}")
    return rows


def audit_calibrate(judge_fn=judge):
    baseline = load_baseline()
    rows = baseline["rows"]
    log(f"Judging the {len(rows)} hand-labelled v1 insights with {OLLAMA_ENRICH_MODEL}\n")
    pairs = []
    for r in rows:
        got, reason = judge_fn(r)
        pairs.append((r["label"], got))
        mark = "✓" if got == r["label"] else "✗"
        log(f"{mark} hand {r['label']:>11}  judge {got or 'UNPARSED':>11}  {r['company_name']} — {r['headline']}")
        if got != r["label"] and reason:
            log(f"      ↳ {reason}")
    agree = sum(a == b for a, b in pairs)
    log("")
    log(f"Hand:  {breakdown([a for a, _ in pairs])}")
    log(f"Judge: {breakdown([b for _, b in pairs])}")
    log(f"Agreement: {agree}/{len(pairs)} ({agree / len(pairs):.0%})")
    flagged_hand = {i for i, (a, _) in enumerate(pairs) if a != "clean"}
    flagged_judge = {i for i, (_, b) in enumerate(pairs) if b in ("unsupported", "outside")}
    log(f"Not-clean caught by judge: {len(flagged_hand & flagged_judge)}/{len(flagged_hand)}; "
        f"clean rows the judge flagged: {len(flagged_judge - flagged_hand)}/{len(pairs) - len(flagged_hand)}")
    for want in AUDIT_LABELS:
        log(f"  hand {want:>11}: judge said {dict(Counter(b or 'UNPARSED' for a, b in pairs if a == want))}")
    return pairs


def main(argv):
    def opt(name, default):
        return int(argv[argv.index(name) + 1]) if name in argv else default
    con = sqlite3.connect(db.DB_PATH)
    try:
        if "--rate" in argv:
            measure_rate(con)
        if "--compare-category" in argv:
            compare_category(con, opt("--n", 20), opt("--repeats", 2), opt("--seed", 1))
        if "--compare-prompt" in argv:
            compare_prompt(con, opt("--n", 30), opt("--seed", 1))
        if "--audit" in argv:
            audit(con, opt("--n", 50), opt("--seed", 11))
        if "--audit-calibrate" in argv:
            audit_calibrate()
        if not {"--rate", "--compare-category", "--compare-prompt", "--audit", "--audit-calibrate"} & set(argv):
            print(__doc__)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
