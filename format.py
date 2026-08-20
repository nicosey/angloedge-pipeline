import re
from datetime import datetime

from config import OLLAMA_BRIEFING_MODEL

# Destinations that receive markdown-formatted content instead of HTML
MARKDOWN_DESTS = {"markdown", "github"}


def _unicode_bold(text):
    """Convert ASCII letters/digits to Unicode bold sans-serif — renders on X."""
    result = []
    for c in text:
        if 'A' <= c <= 'Z':
            result.append(chr(0x1D5D4 + ord(c) - ord('A')))
        elif 'a' <= c <= 'z':
            result.append(chr(0x1D5EE + ord(c) - ord('a')))
        elif '0' <= c <= '9':
            result.append(chr(0x1D7EC + ord(c) - ord('0')))
        else:
            result.append(c)
    return ''.join(result)


def truncate(text, max_len=120):
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "…"


def build_raw_briefing(results_data, cfg):
    now = datetime.now()
    header = (
        f'{cfg["header_emoji"]} <b>{cfg["title"].upper()}</b>\n'
        f'📅 {now.strftime("%A, %d %B %Y")} • {now.strftime("%H:%M")}\n'
        f'{"─" * 30}'
    )
    sections = [header]
    for s in results_data:
        section = f'\n{s["emoji"]} <b>{s["section"]}</b>\n'
        if s["results"]:
            for r in s["results"]:
                section += f'• <b>{r["title"]}</b>'
                if r["snippet"]:
                    section += f'\n  <i>{truncate(r["snippet"])}</i>'
                if r["url"]:
                    section += f'\n  <a href="{r["url"]}">→ Read</a>'
                section += "\n"
        else:
            section += "  <i>No results found today</i>\n"
        sections.append(section)
    sections.append(f'\n{"─" * 30}\n{cfg["footer_emoji"]} <i>SearXNG • {len(results_data)} searches</i>')
    return "\n".join(sections)


THREAD_DELIMITER = "\n---\n"


def _split_to_limit(text, max_len=280):
    """Split text into chunks not exceeding max_len, breaking at paragraph, sentence, then word boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while len(text) > max_len:
        window = text[:max_len]
        # Prefer paragraph break, then sentence boundary (period/!/? followed by space or newline), then word
        cut = window.rfind("\n\n")
        if cut <= 0:
            cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "),
                      window.rfind(".\n"), window.rfind("!\n"), window.rfind("?\n"))
            if cut > 0:
                cut += 1  # include the punctuation
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = max_len
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def split_thread(content, output_cfg, cfg):
    """Split thread content into individual post strings, applying bold title to the first."""
    max_len = output_cfg.get("max_post_length", 280)
    raw_posts = [p.strip() for p in content.split(THREAD_DELIMITER) if p.strip()]
    posts = []
    for post in raw_posts:
        posts.extend(_split_to_limit(post, max_len))
    title = cfg.get("briefing_title", "")
    if title and posts:
        bold_title = f"{_unicode_bold(title)}\n\n"
        first_limit = max_len - len(bold_title)
        first_chunks = _split_to_limit(posts[0], first_limit)
        posts = [bold_title + first_chunks[0]] + first_chunks[1:] + posts[1:]
    return posts


def parse_frontmatter(text):
    """Extract key/value pairs from a YAML frontmatter block."""
    m = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
    if not m:
        return {}
    result = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            result[k.strip()] = v.strip().strip('"')
    return result


def validate_astro_frontmatter(text):
    """Check frontmatter satisfies the Astro briefings collection schema.
    Returns a list of error strings — empty list means valid.
    """
    from datetime import datetime as dt
    errors = []
    meta = parse_frontmatter(text)
    if not meta:
        errors.append("No frontmatter block found")
        return errors
    if not meta.get("title"):
        errors.append("Missing required field: title")
    if not meta.get("description"):
        errors.append("Missing required field: description")
    pubdate = meta.get("pubDate", "")
    if not pubdate:
        errors.append("Missing required field: pubDate")
    else:
        try:
            dt.fromisoformat(pubdate)
        except ValueError:
            errors.append(f"Invalid pubDate: '{pubdate}' — expected ISO format e.g. 2026-04-08T18:00:00")
    return errors


def build_markdown_message(content, output_cfg, cfg):
    """Format a generated output as Markdown with Astro-compatible frontmatter."""
    now   = datetime.now()
    title = cfg.get("briefing_title", cfg.get("ai_topic", cfg["title"]))
    topic = cfg.get("_topic", "briefing")
    # First sentence as description (up to 160 chars)
    first_sentence = content.strip().split(". ")[0].strip()
    description = first_sentence[:160] + ("…" if len(first_sentence) > 160 else "")
    return (
        f"---\n"
        f'title: "{title}"\n'
        f'description: "{description}"\n'
        f"pubDate: {now.strftime('%Y-%m-%dT%H:%M:%S')}\n"
        f"topic: {topic}\n"
        f'model: "{OLLAMA_BRIEFING_MODEL}"\n'
        f"---\n\n"
        f"{content}\n"
    )


def build_output_message(content, output_cfg, cfg):
    """Format a generated output for delivery. Dispatches by output type."""
    output_type = output_cfg.get("type", "narrative")

    if output_type in ("tweet", "thread"):
        title = cfg.get("briefing_title", "")
        if title:
            return f'{_unicode_bold(title)}\n\n{content}'
        return content

    # narrative (default)
    now   = datetime.now()
    title = cfg.get("briefing_title", cfg["ai_topic"])
    return (
        f'{cfg["header_emoji"]} <b>{title.upper()}</b>\n'
        f'📅 {now.strftime("%A, %d %B %Y")} • {now.strftime("%H:%M")}\n'
        f'{"─" * 30}\n\n'
        f'{content}\n\n'
        f'{"─" * 30}\n'
        f'🧠 <i>Written by {OLLAMA_BRIEFING_MODEL}</i>'
    )
