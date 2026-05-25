"""
RNS enrichment processor — adds LLM intelligence to raw Investegate announcements.

Usage:
  python3 process_rns.py                 Enrich up to 50 unenriched high-signal announcements
  python3 process_rns.py --limit N       Process at most N announcements
  python3 process_rns.py --dry-run       Show what would be enriched without calling Ollama
"""
import sys

from config import log
from db import init_db, get_unenriched_rns, update_rns_enrichment
from ai import enrich_rns_with_llm


def main():
    args     = sys.argv[1:]
    dry_run  = "--dry-run" in args
    limit    = 50
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                print(f"Error: --limit requires an integer, got {args[i + 1]!r}")
                sys.exit(1)

    init_db()
    announcements = get_unenriched_rns(limit=limit)

    if not announcements:
        log("✅ No unenriched high-signal RNS announcements found.")
        return

    log(f"🔬 Enriching {len(announcements)} RNS announcement(s)" + (" [DRY RUN]" if dry_run else "") + "...")
    log("")
    enriched = enrich_rns_with_llm(announcements, dry_run=dry_run)

    if not dry_run:
        for rns_number, data in enriched:
            update_rns_enrichment(
                rns_number,
                data["impact_score"],
                data["insight"],
                data["key_themes"],
            )
        log("")
        log(f"✅ Enriched {len(enriched)}/{len(announcements)} announcement(s).")
    else:
        log("")
        log(f"[DRY RUN] Would have enriched {len(announcements)} announcement(s).")


if __name__ == "__main__":
    main()
