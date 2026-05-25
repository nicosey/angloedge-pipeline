"""
RNS enrichment and Telegram publisher.

Usage:
  python3 process_rns.py                       Enrich up to 50 unenriched high-signal announcements
  python3 process_rns.py --limit N             Process at most N announcements
  python3 process_rns.py --publish             Send enriched, unpublished announcements to Telegram
  python3 process_rns.py --publish --dry-run   Preview Telegram messages without sending
  python3 process_rns.py --dry-run             Preview enrichment without calling Ollama
"""
import sys

from config import log
from db import init_db, get_unenriched_rns, update_rns_enrichment, get_unpublished_enriched_rns, mark_rns_published
from ai import enrich_rns_with_llm


def _format_telegram(ann):
    """Format an enriched RNS announcement as an HTML Telegram message."""
    score    = ann["impact_score"]
    themes   = ann.get("key_themes") or []
    insight  = ann.get("insight", "")
    ticker   = ann["ticker"]
    company  = ann["company_name"]
    category = ann["category"]
    url      = ann["url"]

    name_part = f"<b>{company} ({ticker})</b>" if ticker else f"<b>{company}</b>"
    theme_str = " · ".join(themes) if themes else ""

    lines = [
        f"📋 {name_part} — {category}",
        f"⚡ Impact: <b>{score}/10</b>",
        "",
    ]
    if insight:
        lines.append(insight)
        lines.append("")
    if theme_str:
        lines.append(f"🏷 {theme_str}")
    if url:
        lines.append(f'🔗 <a href="{url}">Full announcement →</a>')

    return "\n".join(lines)


def _run_enrich(limit, dry_run):
    announcements = get_unenriched_rns(limit=limit)
    if not announcements:
        log("✅ No unenriched high-signal RNS announcements found.")
        return

    log(f"🔬 Enriching {len(announcements)} RNS announcement(s)" + (" [DRY RUN]" if dry_run else "") + "...")
    log("")
    enriched = enrich_rns_with_llm(announcements, dry_run=dry_run)

    if not dry_run:
        for rns_number, data in enriched:
            update_rns_enrichment(rns_number, data["impact_score"], data["insight"], data["key_themes"])
        log("")
        log(f"✅ Enriched {len(enriched)}/{len(announcements)} announcement(s).")
    else:
        log(f"\n[DRY RUN] Would have enriched {len(announcements)} announcement(s).")


def _run_publish(dry_run):
    from delivery import make_delivery
    announcements = get_unpublished_enriched_rns()
    if not announcements:
        log("📭 No enriched RNS announcements pending publication.")
        return

    log(f"📬 Publishing {len(announcements)} RNS announcement(s) to Telegram" + (" [DRY RUN]" if dry_run else "") + "...")
    log("")
    delivery = make_delivery("telegram", dry_run)
    sent = 0
    for ann in announcements:
        message = _format_telegram(ann)
        log(f"  📨 {ann['company_name']} ({ann['ticker']}) — score {ann['impact_score']}/10")
        ok = delivery.send(message)
        if ok and not dry_run:
            mark_rns_published(ann["rns_number"])
            sent += 1
        elif dry_run:
            sent += 1

    log("")
    log(f"✅ Published {sent}/{len(announcements)} announcement(s).")


def main():
    args    = sys.argv[1:]
    dry_run = "--dry-run" in args
    publish = "--publish" in args
    limit   = 50
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                print(f"Error: --limit requires an integer, got {args[i + 1]!r}")
                sys.exit(1)

    init_db()

    if publish:
        _run_publish(dry_run)
    else:
        _run_enrich(limit, dry_run)


if __name__ == "__main__":
    main()
