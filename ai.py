import json
import re
import urllib.request

from config import OLLAMA_URL, OLLAMA_BRIEFING_MODEL, OLLAMA_ENRICH_MODEL, log


# ── prompt builders ──────────────────────────────────────────

def _results_text(results_data):
    out = ""
    for section in results_data:
        out += f"\n{section['section']}:\n"
        for r in section["results"]:
            out += f"- {r['title']}"
            if r["snippet"]:
                out += f": {r['snippet']}"
            out += "\n"
    return out


def _narrative_prompt(output_cfg, results_data, cfg, previous_narratives):
    history_block = ""
    if previous_narratives:
        history_block = "\nPREVIOUS SUMMARIES (for context and trend continuity):\n"
        for ts, text in previous_narratives:
            history_block += f"\n[{ts[:16].replace('T', ' ')}]\n{text}\n"
        history_block += "\n---\n"

    agg_instruction = (
        "- Where relevant, note how today's news continues or diverges from the previous summaries above\n"
        if previous_narratives else ""
    )

    max_words = output_cfg.get("max_words", 500)

    briefing_instruction = cfg.get("briefing_instruction", "")
    if briefing_instruction:
        briefing_instruction = f"- {briefing_instruction}\n"

    return f"""You are {cfg['ai_persona']}.
Based on today's news below, write a concise, engaging narrative summary called "{cfg['ai_topic']}".

Rules:
- Write 3-4 short paragraphs maximum
- Lead with the most important story
- Mention key companies and deals
- Add brief analysis on what trends you see
- Keep it under {max_words} words
- Write in a professional but accessible tone
- Do NOT use markdown formatting, just plain text
- Do NOT refer to "articles", "sources", "reports" or the data you were given — write as if from your own knowledge
- End with one sentence on what to watch next
{briefing_instruction}{agg_instruction}
{history_block}TODAY'S RAW NEWS DATA:
{_results_text(results_data)}

Write the narrative now:"""


def _thread_prompt(output_cfg, results_data, cfg, previous_tweets=None, source_content=None):
    num_posts  = output_cfg.get("num_posts", 4)
    max_chars  = output_cfg.get("max_chars_per_post", 280)
    numbered   = output_cfg.get("numbered", True)
    numbering  = f"- Start each post with its position, e.g. '1/{num_posts}', '2/{num_posts}'\n" if numbered else ""

    briefing_instruction = cfg.get("briefing_instruction", "")
    if briefing_instruction:
        briefing_instruction = f"- {briefing_instruction}\n"

    if source_content:
        input_block = f"BRIEFING TO THREAD:\n{source_content}"
        task = "Break the briefing below into an X (Twitter) thread"
    else:
        latest_sections = [s for s in results_data if "latest" in s["section"].lower()]
        feed = latest_sections if latest_sections else results_data
        input_block = f"LATEST NEWS:\n{_results_text(feed)}"
        task = "Write an X (Twitter) thread covering the most important current stories"

    return f"""You are {cfg['ai_persona']}.
{task} of exactly {num_posts} posts.

Rules:
- Write exactly {num_posts} posts separated by a line containing only: ---
- Each post must be under {max_chars} characters including the number prefix
{numbering}- Each post must be self-contained and make sense on its own
- Cover the key points in order — do not skip or invent content
- End the final post with 2-3 relevant hashtags
- Write in present tense, professional but accessible tone
- Do NOT use markdown formatting
{briefing_instruction}
{input_block}

Write the thread now:"""


def _tweet_prompt(output_cfg, results_data, cfg, previous_tweets=None):
    max_chars = output_cfg.get("max_chars", 280)

    # Prefer a section explicitly marked as latest news, otherwise use all
    latest_sections = [s for s in results_data if "latest" in s["section"].lower()]
    feed = latest_sections if latest_sections else results_data

    recent_block = ""
    if previous_tweets:
        recent_block = "\nRECENT TWEETS ALREADY POSTED (do NOT repeat these stories):\n"
        for t in previous_tweets:
            recent_block += f"- {t}\n"
        recent_block += "\n"

    return f"""You are {cfg['ai_persona']}.
Based on the latest news below, write a single tweet-sized update about the most breaking or recent story.

Rules:
- Maximum {max_chars} characters including spaces
- Focus on the single most recent or newsworthy headline
- One or two sentences maximum
- Write in present tense
- End with 1-2 relevant hashtags
- Pick a DIFFERENT story from any recently posted tweets listed below
{recent_block}
LATEST NEWS:
{_results_text(feed)}

Write the tweet now:"""


# ── prompt registry ──────────────────────────────────────────
# To add a new output type: add an entry here returning a prompt string.

_PROMPT_BUILDERS = {
    "narrative": _narrative_prompt,
    "tweet":     _tweet_prompt,
    "thread":    _thread_prompt,
}


# ── public API ───────────────────────────────────────────────

def _ollama_call(prompt, max_tokens=500, model=None, timeout=60):
    """Single Ollama call, returns stripped text or None."""
    payload = json.dumps({
        "model": model or OLLAMA_BRIEFING_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": max_tokens}
    }).encode("utf-8")
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=payload)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data.get("message", {}).get("content", "").strip() or None
    except Exception as e:
        log(f"  ⚠ Ollama call failed: {e}")
        return None


def _hard_split(text, max_len):
    """Split text into chunks ≤ max_len at sentence then word boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while len(text) > max_len:
        window = text[:max_len]
        cut = window.rfind("\n\n")
        if cut <= 0:
            cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "),
                      window.rfind(".\n"), window.rfind("!\n"), window.rfind("?\n"))
            if cut > 0:
                cut += 1
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = max_len
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def _generate_thread_from_paragraphs(output_cfg, source_content, cfg):
    """Split narrative into posts ≤ max_chars, numbered after all splitting."""
    max_chars = output_cfg.get("max_chars_per_post", 280)
    numbered  = output_cfg.get("numbered", True)

    # Split on double or single newlines for resilience against inconsistent AI output
    raw = [p.strip() for p in re.split(r'\n\n+|\n', source_content) if p.strip()]
    if not raw:
        return None

    # Split any chunk that still exceeds max_chars (before numbering overhead)
    posts = []
    for chunk in raw:
        posts.extend(_hard_split(chunk, max_chars))

    # Number after all splitting so the total is accurate
    n = len(posts)
    if numbered:
        numbered_posts = []
        for i, post in enumerate(posts):
            prefix = f"{i+1}/{n} "
            # Trim post if prefix pushes it over limit
            available = max_chars - len(prefix)
            if len(post) > available:
                post = _hard_split(post, available)[0]
            numbered_posts.append(prefix + post)
        posts = numbered_posts

    log(f"  ✅ thread: {n} posts from narrative paragraphs")
    return "\n---\n".join(posts)


def generate_title(narrative, cfg):
    """Ask the AI to generate a short headline title based on today's narrative."""
    prompt = (
        f"You are {cfg['ai_persona']}.\n"
        f"Based on the briefing below, write a short punchy headline title (5-8 words) "
        f"that captures today's defining story or theme. "
        f"Output only the title — no quotes, no punctuation at the end.\n\n"
        f"BRIEFING:\n{narrative}\n\nTitle:"
    )
    title = _ollama_call(prompt, max_tokens=30)
    if title:
        # Strip any surrounding quotes the model might add
        title = title.strip().strip('"').strip("'")
        log(f"  ✅ Generated title: {title}")
    return title


def generate_output(output_cfg, results_data, cfg, previous_narratives=None, source_content=None):
    output_type = output_cfg.get("type", "narrative")

    # Thread without fixed num_posts: split narrative paragraphs
    if output_type == "thread" and "num_posts" not in output_cfg and source_content:
        return _generate_thread_from_paragraphs(output_cfg, source_content, cfg)

    builder = _PROMPT_BUILDERS.get(output_type)
    if not builder:
        log(f"  ⚠ Unknown output type: {output_type}")
        return None

    if source_content is not None:
        prompt = builder(output_cfg, results_data, cfg, previous_narratives, source_content=source_content)
    else:
        prompt  = builder(output_cfg, results_data, cfg, previous_narratives)
    payload = json.dumps({
        "model": OLLAMA_BRIEFING_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.3, "num_predict": 4000}
    }).encode("utf-8")

    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=payload)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=300) as resp:
            data    = json.loads(resp.read().decode())
            content = data.get("message", {}).get("content", "").strip()
            if content:
                log(f"  ✅ {output_type}: {len(content)} chars")
                return content
            log(f"  ⚠ AI returned empty response for {output_type}")
            return None
    except Exception as e:
        log(f"  ⚠ AI {output_type} failed: {e}")
        return None





_MOCK_NARRATIVE = (
    "In a real run, Ollama would generate several paragraphs of analysis here. "
    "Lead story: a major development has emerged with significant market implications.\n\n"
    "A second paragraph would discuss trends and key players involved in today's story. "
    "Several companies are affected and analysts are watching closely.\n\n"
    "A third paragraph would add context and historical perspective. "
    "This follows a pattern seen in previous quarters and may signal a broader shift.\n\n"
    "Watch for follow-up developments over the next 24 hours as the situation evolves."
)


# Bump when the enrichment prompt or scoring changes: rows enriched under an
# older version are re-enriched by enrich_backlog.py (column enrichment_version).
RNS_PROMPT_VERSION = 3

# Company history given with each announcement (v3): the company's previous
# non-routine announcements, as date + headline only — never our own earlier
# insights or scores, so an error can't feed on itself. db.get_unenriched_rns
# attaches it as ann["history"] (oldest first).
HISTORY_MAX_ITEMS = 5
HISTORY_MAX_DAYS = 365
HISTORY_HEADLINE_CHARS = 90

_RNS_ENRICH_PROMPT = """\
You are a careful UK equities analyst writing a short note on a regulatory
announcement (RNS). You know only what is below: the company, the announcement's
title and category, and the company's earlier announcements on record.

Company: {company} ({ticker})
Category: {category}
Announcement: {headline}

Earlier announcements by this company (oldest first):
{history}

The category was assigned automatically from the title's wording and may be wrong.

Respond with ONLY a valid JSON object — no explanation, no preamble, no markdown:
{{
  "impact_score": <integer 1-10>,
  "insight": "<at most 2 sentences and 40 words on what this means for shareholders>",
  "key_themes": ["<tag1>", "<tag2>"]
}}

Rules for the insight — follow all of them:
- Use only the information above. Where the earlier announcements explain this one
  (an offer followed by a court meeting, then a court sanction), say so, citing them
  by month.
- Never name a company, person, product, project, place, amount, percentage or date
  that does not appear above.
- Do not describe what the company does, its sector, strategy or finances unless the
  information above says so. Do not mention competitors, peers, precedents, market
  conditions or other companies' events.
- Do not guess the outcome, size, terms or reasons for anything not stated above.
- Write about the announcement, never about its title or wording, and never about
  what is missing: do not write "the headline", "the title", "does not specify",
  "not disclosed" or "no details".
- Start with the company's name or with what happened, never with "This
  announcement", "This headline", "This filing" or "This signifies".
- Say something specific to THIS company, not a definition of the announcement type.
  Use what is above: the amounts, names and terms in the title, and how it fits the
  earlier announcements (the next step in a sequence, a repeat, a change of course, the
  first such filing in the past year). Never explain what a category of announcement
  generally means. If nothing specific can be said, write one short plain sentence on
  what shareholders should watch for next.

key_themes: 1-3 short generic tags (for example "M&A", "Capital raise", "Governance",
"Results"). No names that are not above.

Scoring guide — how much the announcement could matter to the company's shareholders:
- 9-10: a firm or recommended offer for the company, a transformative merger,
  administration or insolvency, suspension of shares, a severe profit warning
- 7-8:  a possible offer or formal sale process, a material acquisition or disposal,
  a large fundraise, departure of the CEO or chair, a strategic review, a guidance change
- 5-6:  scheduled results or a trading update with no stated direction, a contract or
  partnership described without its size, a board change
- 3-4:  director dealings, a dividend declaration, a results-date notice, meeting
  results, small capital actions
- 1-2:  administrative housekeeping, and press releases about other organisations or
  marketing material with no bearing on the company's shareholders\
"""


_FORMULAIC_RE = re.compile(
    r"^\s*(this|the)\s+(announcement|headline|filing|news|update|release)\b"
    r"|^\s*this\s+(signifies|indicates|suggests|means)\b"
    r"|\b(typically|generally|usually)\s+(signifies|indicates|signals|means|suggests)\b", re.I)


def is_formulaic(insight):
    """True for the stock openings and category-definition phrasing the prompt forbids
    (measured by enrich_diagnostics.py; the insight is never rewritten)."""
    return bool(_FORMULAIC_RE.search(insight or ""))


def _parse_enrichment_json(text, category=""):
    """Extract the first JSON object from model output. Returns dict or None.
    `category` is accepted for compatibility and no longer used: there is no
    per-category score floor (the category is a keyword guess, and clamping let
    that guess overrule the model — e.g. PR-wire filler held at 6)."""
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group())
        score = data.get("impact_score")
        insight = data.get("insight", "")
        themes = data.get("key_themes", [])
        if not isinstance(score, int) or not (1 <= score <= 10):
            return None
        if not isinstance(themes, list):
            themes = [themes] if themes else []
        return {
            "impact_score": score,
            "insight":      str(insight).strip(),
            "key_themes":   [str(t).strip() for t in themes if t],
        }
    except (json.JSONDecodeError, TypeError):
        return None


def format_history(history):
    """Prompt lines for ann["history"]: [{"date": "14 Sep 2026", "headline": ..., "count": n}]."""
    if not history:
        return "(none on record)"
    lines = []
    for h in history:
        text = h["headline"]
        if len(text) > HISTORY_HEADLINE_CHARS:
            text = text[:HISTORY_HEADLINE_CHARS].rstrip() + "…"
        times = f" (×{h['count']})" if h.get("count", 1) > 1 else ""
        lines.append(f"- {h['date']}: {text}{times}")
    return "\n".join(lines)


def build_rns_prompt(ann):
    """The enrichment prompt for one announcement dict (history optional)."""
    return _RNS_ENRICH_PROMPT.format(
        company=ann.get("company_name") or "Unknown",
        ticker=ann.get("ticker") or "—",
        category=ann.get("category") or "Other",
        headline=ann["headline"],
        history=format_history(ann.get("history")),
    )


ENRICH_TIMEOUT = 180    # seconds; generous so parallel requests queued at Ollama don't time out


def enrich_rns_with_llm(announcements, dry_run=False):
    """
    Enrich RNS announcement dicts with LLM-generated impact_score, insight, and key_themes.
    Returns list of (rns_number, enrichment_dict) tuples for successfully enriched entries.
    """
    results = []
    for ann in announcements:
        prompt = build_rns_prompt(ann)
        if dry_run:
            log(f"  [DRY RUN] Would enrich: {ann['headline'][:70]}")
            continue

        log(f"  🔍 {ann['company_name']} ({ann['ticker']}) — {ann['headline'][:55]}...")
        raw = _ollama_call(prompt, max_tokens=300, model=OLLAMA_ENRICH_MODEL, timeout=ENRICH_TIMEOUT)
        if not raw:
            log("    ⚠ No response, skipping")
            continue

        enrichment = _parse_enrichment_json(raw, category=ann.get("category", ""))
        if not enrichment:
            log(f"    ⚠ Could not parse JSON: {raw[:80]!r}")
            continue

        log(f"    ✅ score={enrichment['impact_score']} themes={enrichment['key_themes']}")
        results.append((ann["rns_number"], enrichment))

    return results


def mock_output(output_cfg, cfg, source_content=None):
    output_type = output_cfg.get("type", "narrative")
    if output_type == "tweet":
        return (
            f"[MOCK] Major development in {cfg['ai_topic']}: key story emerging with broad implications. "
            f"#{cfg['ai_topic'].replace(' ', '')} #MockData"
        )
    if output_type == "thread":
        # Use the same paragraph-splitting path as real mode
        narrative = source_content or _MOCK_NARRATIVE
        return _generate_thread_from_paragraphs(output_cfg, narrative, cfg)
    # narrative
    return f"[MOCK] {_MOCK_NARRATIVE}"
