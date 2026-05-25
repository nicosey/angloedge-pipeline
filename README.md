# angloedge-pipeline

A lightweight daily news briefing pipeline powering [AngloEdge](https://angloedge.com). Searches for recent news via [SearXNG](https://github.com/searxng/searxng), generates AI outputs via [Ollama](https://ollama.com), saves to SQLite, and publishes to Telegram and GitHub.

## How it works

1. **Collect** (`collect.py`) — runs hourly, searches SearXNG and saves raw results to SQLite. No AI, no delivery.
2. **Briefing** (`briefing.py`) — runs at scheduled times (e.g. 7am, 12pm, 5pm), reads collected data, generates AI outputs, queues them in the outbox.
3. **Publish** (`publish.py`) — reads the outbox and delivers queued messages to Telegram.

`run.sh` wraps steps 2 and 3 together for launchd. `collect.sh` wraps step 1.

Separating collection from generation means each briefing draws on several hours of accumulated news rather than a single search snapshot.

### Live session mode

`session.py` adds a fourth mode for monitoring a story as it develops in real time:

1. **Interim cycles** — every N minutes, runs `collect.py` then `briefing.py --save-only`. Each mini-briefing is saved to the DB but not delivered.
2. **Final aggregation** — after all cycles complete (or on `Ctrl+C`), runs `briefing.py --lookback` which reads all interim narratives as context, generates a single synthesised output, and publishes it.

This is useful for watching a fast-moving story over a morning or afternoon without flooding Telegram with every update — only the final summary is delivered.

### Briefing types

Each run can be given a `--briefing-type` that changes both the message title and the AI's writing instruction:

| Type | Title | AI instruction |
| --- | --- | --- |
| `morning` | Morning Start | Set the scene for the trading day ahead |
| `midday` | Mid Day Update | Summarise how markets have developed through the morning |
| `eod` | End of Day Summary | Wrap up the full trading day |

Types are configured per topic in the JSON config under `briefing_types`.

### Full-day mode

The `--full-day` flag tells the briefing to use all collections since midnight rather than just since the last briefing. Used for the end-of-day run so it covers the complete trading day.

## Requirements

- Python 3.8+
- [SearXNG](https://github.com/searxng/searxng) running locally
- [Ollama](https://ollama.com) running locally with a model pulled
- For Telegram delivery: a bot token and chat ID

No third-party Python packages required for the core pipeline — uses only the standard library.

RNS collection (`collect.py --rns`) additionally requires:

```bash
pip install requests beautifulsoup4
```

## Setup

```bash
cp .env.example .env
# edit .env with your values
```

### .env

```ini
SEARXNG_URL=http://localhost:8888
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=qwen3-coder:30b

# Default Telegram bot — used by dest: "telegram"
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id

# Named bots — used by dest: "telegram_<name>"
# Add one pair per bot; the suffix must match the dest name in upper case
TELEGRAM_BOT_TOKEN_ROBOTICS_JN=your_robotics_token
TELEGRAM_CHAT_ID_ROBOTICS_JN=your_robotics_chat_id

# Default outbox destination for outputs without an explicit dest (default: console)
BRIEFING_DEST=telegram

# DB paths (optional — defaults shown)
BRIEFING_DB=output/briefings.db
BRIEFING_ARCHIVE_DB=output/archive.db
```

## Usage

```bash
# Collect latest news for a topic (run hourly)
python3 collect.py <topic>

# Collect RNS announcements from Investegate
python3 collect.py --rns

# Collect both SearXNG news and RNS in one run
python3 collect.py --all <topic>

# Generate a briefing from collected data and queue for delivery
python3 briefing.py <topic> [options]

# Publish all queued outputs to Telegram
python3 publish.py

# Or run briefing + publish together
./run.sh <topic> [briefing flags...]

# Live session: collect + brief at intervals, then aggregate and publish
python3 session.py <topic> [options]

# Research: query archive/DB or run live searches, optionally analyse with Ollama
python3 research.py [options]
```

### collect.py options

| Option | Description |
| --- | --- |
| `--mock` | Fake SearXNG — no services needed |
| `--rns` | Collect RNS announcements from Investegate instead of SearXNG news |
| `--all` | Collect SearXNG news for `<topic>` then collect RNS (requires a topic argument) |

### briefing.py options

| Option | Description |
| --- | --- |
| `--mock` | Fake SearXNG + Ollama — no services needed |
| `--dry-run` | Skip DB write and outbox, print to terminal instead |
| `--save-only` | Save to DB (for aggregation context) but skip delivery — used by session.py for interim cycles |
| `--briefing-type <name>` | Set the briefing type (e.g. `morning`, `midday`, `eod`) — controls title and AI instruction |
| `--full-day` | Use all collections since midnight (for end-of-day summary) |
| `--lookback N` | Include last N minutes of saved narratives as AI context |

### session.py options

| Option | Description |
| --- | --- |
| `--interval N` | Minutes between interim cycles (default: 60) |
| `--count N` | Number of interim cycles before the final aggregation (default: 3) |
| `--duration N` | Total session duration in minutes — alternative to `--count` |
| `--briefing-type <name>` | Briefing type applied to the final aggregated output |
| `--mock` | Fake SearXNG + Ollama — no services needed |
| `--dry-run` | Skip DB writes and delivery |

### research.py options

| Option | Description |
| --- | --- |
| `--topic <name>` | Query archived and live DB collections for this topic |
| `--keyword <word>` | Filter articles by keyword in title or snippet |
| `--from <date>` | Start date for DB query (e.g. `2026-04-01`) |
| `--to <date>` | End date for DB query |
| `--live "<query>"` | Run a live SearXNG search (repeatable) |
| `--count N` | Results per live search query (default: 5) |
| `--analyse` | Send results to Ollama for a research summary |
| `--time-range <range>` | Time window for live searches: `day` (default), `week`, `month`, `year` |
| `--save` | Save live search results to `archive.db` |

### Examples

```bash
# Test locally with no services
python3 briefing.py robotics --mock --dry-run

# Real run using collected data
python3 briefing.py uk_capital_markets --briefing-type morning

# End of day summary covering all day's collections
python3 briefing.py uk_capital_markets --briefing-type eod --full-day

# Collect + brief + publish in one step
./run.sh uk_capital_markets --briefing-type morning

# Live session: 3 cycles every 10 min, then aggregate and publish
python3 session.py robotics --interval 10 --count 3

# Live session: run for 2 hours every 30 min, EOD final summary
python3 session.py uk_capital_markets --duration 120 --interval 30 --briefing-type eod

# Test the session loop with no services
python3 session.py robotics --interval 1 --count 2 --mock --dry-run

# Research: query archive for a keyword since a date
python3 research.py --topic uk_capital_markets --keyword "Barclays" --from 2026-04-01

# Research: live search with Ollama analysis
python3 research.py --live "UK gilts today" --live "Bank of England rates" --analyse

# Research: combined archive + live + analysis, save live results to archive
python3 research.py --topic uk_capital_markets --keyword "gilts" --live "UK gilts today" --analyse --save
```

**Available topics:** `robotics`, `uk_capital_markets`, `data_centres`, `bjj`

## Tests

```bash
python3 tests.py
```

No external services required — all tests use mocks or temp files. Covers format, delivery, search, AI mocks, and research (article extraction, keyword filtering, DB queries, live search time range).

## Delivery destinations

| Destination | Description |
| --- | --- |
| `console` | Print to terminal |
| `telegram` | Send via the default bot (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`) |
| `telegram_<name>` | Send via a named bot — reads `TELEGRAM_BOT_TOKEN_<NAME>` and `TELEGRAM_CHAT_ID_<NAME>` from `.env` |
| `markdown` | Write a `.md` file to `MARKDOWN_OUTPUT_DIR` (default: `output/markdown/`) |
| `github` | Write a `.md` file and `git push` to the Astro repo at `GITHUB_REPO_PATH` |

Set `BRIEFING_DEST` in `.env` as the default, or set `dest` per output in the topic config. Multiple destinations can be comma-separated:

```json
"outputs": [
  {"type": "narrative", "name": "Daily Digest", "dest": "telegram,github"},
  {"type": "thread",    "name": "X Thread",     "dest": "telegram_robotics_jn"}
]
```

### Markdown / GitHub publishing

Outputs sent to `markdown` or `github` are formatted as Markdown with Astro-compatible frontmatter:

```markdown
---
title: "Morning Start"
date: 2026-04-08
time: "07:00"
topic: uk_capital_markets
model: qwen3-coder:30b
---

Narrative content here...
```

Files are named `{date}T{time}-{topic}.md` and written to `GITHUB_MD_DIR` inside the repo (default: `src/content/briefings`). After writing, `GitHubDelivery` runs `git add / commit / pull --rebase / push` — Netlify (or any CI) picks up the push and deploys automatically. GitHub publishing is skipped automatically on weekends (Saturday and Sunday).

**Setup:**

1. Clone your Astro repo to the Mac Mini
2. Ensure git credentials are configured (SSH key or HTTPS token)
3. Set in `.env`:

   ```ini
   GITHUB_REPO_PATH=/path/to/your/astro-site
   GITHUB_MD_DIR=src/content/briefings
   GITHUB_BRANCH=main
   ```

4. Test the connection: `python3 test_github.py`

To add a new Telegram bot: create via `@BotFather`, add `TELEGRAM_BOT_TOKEN_<NAME>` and `TELEGRAM_CHAT_ID_<NAME>` to `.env`, use `"dest": "telegram_<name>"` in config. No code changes needed.

## Output types

Each topic config defines an `outputs` array controlling what the AI generates per run:

```json
"outputs": [
  {"type": "narrative", "name": "Daily Digest",  "max_words": 500},
  {"type": "tweet",     "name": "Tweet Summary", "max_chars": 280, "tweet_lookback_hours": 6}
]
```

| Type | Description |
| --- | --- |
| `narrative` | 3–4 paragraph analysis, up to `max_words` |
| `tweet` | Single breaking-news sentence, up to `max_chars` |
| `thread` | Multi-post X thread — delivered as separate Telegram messages, one per post, ready to copy-paste |

### Thread options

| Field | Default | Description |
| --- | --- | --- |
| `max_chars_per_post` | `280` | Max characters per post including the number prefix |
| `numbered` | `true` | Prefix each post with its position (e.g. `1/7`) |

Thread posts are derived from the narrative output — each paragraph becomes a post. Paragraphs longer than `max_chars_per_post` are hard-split at sentence then word boundaries. The post count is determined by the content, not a fixed number. The first post gets the briefing title in Unicode bold (e.g. 𝗠𝗼𝗿𝗻𝗶𝗻𝗴 𝗦𝘁𝗮𝗿𝘁) so it renders on X. Each post arrives as a separate Telegram message.

**Deduplication:** `tweet` looks back at recent outputs to avoid repeating the same stories.

**Latest news focus:** Name a search section with "latest" in the title (e.g. `LATEST UK CAPITAL MARKETS NEWS`) and the tweet/thread prompt draws exclusively from that section.

## Adding a topic

Create a JSON file in `config/`:

```json
{
  "title": "My Briefing",
  "header_emoji": "📋",
  "footer_emoji": "🔍",
  "ai_persona": "an analyst writing a daily briefing",
  "ai_topic": "Today's Update",
  "lookback_minutes": 120,
  "briefing_types": {
    "morning": {
      "title": "Morning Start",
      "ai_instruction": "Set the scene for the day ahead."
    },
    "midday": {
      "title": "Mid Day Update",
      "ai_instruction": "Summarise how things have developed through the morning."
    },
    "eod": {
      "title": "End of Day Summary",
      "ai_instruction": "Wrap up the full day's key developments."
    }
  },
  "outputs": [
    {"type": "narrative", "name": "Daily Digest",  "max_words": 500, "dest": "telegram"},
    {"type": "tweet",     "name": "Tweet Summary", "max_chars": 280, "dest": "telegram", "tweet_lookback_hours": 6}
  ],
  "searches": [
    {"emoji": "📰", "title": "LATEST MY TOPIC NEWS", "query": "my topic news today", "count": 5, "category": "news"},
    {"emoji": "📊", "title": "SECTION NAME",          "query": "your search query",  "count": 4, "category": "news"}
  ]
}
```

Then test:

```bash
python collect.py my_topic --mock
python briefing.py my_topic --briefing-type morning --mock --dry-run
```

## Database

Results are stored in `output/briefings.db` (SQLite — no server required):

| Table | Contents |
| --- | --- |
| `collections` | Raw search results saved by collect.py — one row per hourly run per topic. Archived to `archive.db` after 48 hours. |
| `runs` | One row per briefing run: topic, timestamp, raw headlines, aggregation state |
| `outputs` | One row per AI output per run: type, name, content — kept permanently |
| `outbox` | Delivery queue: one row per output×destination. Undelivered entries are retried on every publish run. Published entries older than 24 hours are cleaned up automatically. |
| `rns_announcements` | RNS announcements scraped from Investegate — one row per announcement, deduplicated by Investegate's numeric ID (`rns_number`). Kept permanently. |

## Scheduling on macOS (launchd)

### UK Capital Markets

| Job | Schedule | Command |
| --- | --- | --- |
| `com.angloedge.uk_capital_markets_collect` | Hourly 7am–5pm | `collect.sh uk_capital_markets` |
| `com.angloedge.uk_capital_markets` | 7am daily | `run.sh uk_capital_markets --briefing-type morning` |
| `com.angloedge.uk_capital_markets_digest` | 12pm daily | `run.sh uk_capital_markets --briefing-type midday` |
| `com.angloedge.uk_capital_markets_eod` | 5pm daily | `run.sh uk_capital_markets --briefing-type eod --full-day` |

### Robotics

| Job | Schedule | Command |
| --- | --- | --- |
| `com.angloedge.robotics` | 6pm daily | `briefing.py robotics && publish.py` |

### RNS announcements

| Job | Schedule | Command |
| --- | --- | --- |
| `com.angloedge.rns` | Every 30 min, 7am–4:30pm | `collect.py --rns` |

`collect.py --rns` scrapes the [Investegate](https://www.investegate.co.uk) announcement listing, parses each row into structured fields, and inserts new entries into the `rns_announcements` table. Duplicates are skipped automatically using Investegate's numeric announcement ID as a unique key, so the job is safe to run frequently.

Fields collected per announcement: `timestamp`, `company_name`, `ticker`, `headline`, `url`, `category` (inferred from headline keywords), and `rns_number` (Investegate's internal numeric ID, used for deduplication).

**Install dependencies before first run:**

```bash
pip install requests beautifulsoup4
```

**Load the job:**

```bash
cp launchd/com.angloedge.rns.plist ~/Library/LaunchAgents/
# Edit the plist to replace the project path if not using /Users/m4server/projects/angloedge-pipeline
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.angloedge.rns.plist
launchctl list | grep angloedge.rns
```

**Query the collected data:**

```bash
sqlite3 output/briefings.db "SELECT timestamp, ticker, company_name, category, headline FROM rns_announcements ORDER BY timestamp DESC LIMIT 20;"
```

### Smart RNS enrichment

Raw RNS data is just a headline and a category. The enrichment layer sends high-signal announcements to Ollama and writes three additional fields back to the database:

| Field | Type | Description |
| --- | --- | --- |
| `impact_score` | INTEGER 1–10 | Market significance (8–10 = transformative, 5–7 = notable, 1–4 = routine) |
| `insight` | TEXT | 1–2 sentence sharp commentary: why it matters, sector context, precedents |
| `key_themes` | TEXT (JSON) | List of tags, e.g. `["M&A", "Insurance", "Foreign Takeover"]` |

Only **high-signal categories** are enriched (Results, Acquisition, Trading Update, Placing, Director Dealing, Share Buyback). Routine categories (AGM/EGM, Dividend) are skipped to keep Ollama load low.

**Run enrichment standalone:**

```bash
python3 process_rns.py             # enrich up to 50 unenriched announcements
python3 process_rns.py --limit 10  # process at most 10
python3 process_rns.py --dry-run   # preview without calling Ollama
```

**Collect and enrich in one step:**

```bash
python3 collect.py --rns --enrich
```

**Query enriched announcements:**

```bash
sqlite3 output/briefings.db "SELECT ticker, impact_score, key_themes, insight FROM rns_announcements WHERE impact_score IS NOT NULL ORDER BY impact_score DESC, timestamp DESC LIMIT 10;"
```

### Plist template

Save as `~/Library/LaunchAgents/com.angloedge.LABEL.plist`, replacing `YOUR_USER`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.angloedge.LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOUR_USER/projects/angloedge-pipeline/run.sh</string>
        <string>TOPIC</string>
        <string>--briefing-type</string>
        <string>morning</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>7</integer>
        <key>Minute</key><integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>/Users/YOUR_USER/projects/angloedge-pipeline/output/LABEL.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOUR_USER/projects/angloedge-pipeline/output/LABEL.error.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

### Load and test

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.angloedge.LABEL.plist
launchctl list | grep angloedge
launchctl start com.angloedge.LABEL
tail -f ~/projects/angloedge-pipeline/output/LABEL.log
```

### Notes

- launchd runs jobs as your user, so credentials in `.env` are picked up normally
- If the machine is asleep at the scheduled time, the job is skipped — it does not catch up on wake
- publish.py is safe to run independently at any time; it skips already-published outbox entries
- To clear stale outbox entries: `sqlite3 output/briefings.db "UPDATE outbox SET published_at=datetime('now') WHERE published_at IS NULL;"`

## Output structure

```text
output/
  briefings.db                          # SQLite — live DB: collections (48h), runs, outputs, outbox
  archive.db                            # SQLite — collections older than 48h, kept permanently
  2026-03-29_07-00-00_uk_capital_markets/
    raw_briefing.txt                    # formatted headlines (HTML)
    narrative.txt                       # AI narrative output
    tweet.txt                           # AI tweet output
```
