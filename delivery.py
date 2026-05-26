import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime

from config import log
from format import parse_frontmatter, validate_astro_frontmatter


# ── base ─────────────────────────────────────────────────────

class Delivery:
    """Base class for briefing delivery destinations."""

    def send(self, message):
        raise NotImplementedError

    def send_long(self, message):
        """Send a message that may exceed the destination's size limit.
        Default: single send. Override if chunking is needed."""
        return self.send(message)


# ── telegram ─────────────────────────────────────────────────

class TelegramDelivery(Delivery):
    MAX_CHARS = 4000

    def __init__(self, token, chat_id):
        self.token   = token
        self.chat_id = chat_id

    def send(self, message):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.chat_id, "text": message,
            "parse_mode": "HTML", "disable_web_page_preview": True
        }).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=payload)
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                if result.get("ok"):
                    log("  ✅ Telegram: sent")
                    return True
                log(f"  ❌ Telegram error: {result}")
                return False
        except Exception as e:
            log(f"  ❌ Telegram send failed: {e}")
            return False

    def send_long(self, message):
        if len(message) <= self.MAX_CHARS:
            return self.send(message)
        chunks  = message.split("\n\n")
        current = ""
        for chunk in chunks:
            if len(current) + len(chunk) + 2 > self.MAX_CHARS:
                if current:
                    self.send(current)
                current = chunk
            else:
                current = current + "\n\n" + chunk if current else chunk
        if current:
            self.send(current)


# ── console ──────────────────────────────────────────────────

class ConsoleDelivery(Delivery):
    """Prints to stdout — used for --dry-run."""

    def send(self, message):
        print(message)


# ── multi ────────────────────────────────────────────────────

class MultiDelivery(Delivery):
    """Fans out to multiple delivery destinations."""

    def __init__(self, deliveries):
        self.deliveries = deliveries

    def send(self, message):
        for d in self.deliveries:
            d.send(message)

    def send_long(self, message):
        for d in self.deliveries:
            d.send_long(message)


# ── markdown ─────────────────────────────────────────────────

class MarkdownDelivery(Delivery):
    """Writes the output as a .md file to a local directory."""

    def __init__(self, output_dir):
        self.output_dir = output_dir

    def _filename(self, message):
        meta    = parse_frontmatter(message)
        topic   = meta.get("topic", "briefing")
        pubdate = meta.get("pubDate", "")
        if pubdate:
            # pubDate: 2026-04-08T14:49:38 → 2026-04-08T14-49
            dt = pubdate[:16].replace(":", "-")
            return f"{dt}-{topic}.md"
        return f"{topic}.md"

    def send(self, message):
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            path = os.path.join(self.output_dir, self._filename(message))
            with open(path, "w") as f:
                f.write(message)
            log(f"  ✅ Markdown: {path}")
            return True
        except Exception as e:
            log(f"  ❌ Markdown write failed: {e}")
            return False


# ── github ────────────────────────────────────────────────────

class GitHubDelivery(MarkdownDelivery):
    """Writes a .md file into a local git repo clone and pushes to GitHub."""

    def __init__(self, repo_path, md_dir, branch):
        self.repo_path = repo_path
        self.branch    = branch
        super().__init__(os.path.join(repo_path, md_dir))

    def _git(self, *args):
        result = subprocess.run(
            ["git", "-C", self.repo_path] + list(args),
            capture_output=True, text=True
        )
        if result.returncode != 0:
            msg = (result.stderr.strip() or result.stdout.strip() or
                   f"git {' '.join(args)} exited with code {result.returncode}")
            raise RuntimeError(msg)
        return result.stdout.strip()

    def send(self, message):
        if datetime.now().weekday() >= 5:  # 5=Saturday, 6=Sunday
            log("  ℹ GitHub: skipping publish — weekend")
            return True
        errors = validate_astro_frontmatter(message)
        if errors:
            for e in errors:
                log(f"  ❌ Frontmatter validation failed: {e}")
            return False
        if not super().send(message):
            return False
        filename = self._filename(message)
        rel_path = os.path.join(os.path.relpath(self.output_dir, self.repo_path), filename)
        meta     = parse_frontmatter(message)
        title    = meta.get("title", filename)
        try:
            self._git("add", rel_path)
            try:
                self._git("commit", "-m", f"briefing: {title}")
            except RuntimeError as e:
                if "nothing to commit" in str(e).lower() or "nothing added" in str(e).lower():
                    log(f"  ℹ GitHub: no changes to commit for {rel_path}")
                    return True
                raise
            self._git("pull", "--rebase", "origin", self.branch)
            self._git("push", "origin", self.branch)
            log(f"  ✅ GitHub: pushed {rel_path} → {self.branch}")
            return True
        except RuntimeError as e:
            log(f"  ❌ GitHub push failed: {e}")
            return False


# ── registry ─────────────────────────────────────────────────

def _make_telegram(name):
    """Resolve token and chat_id for a telegram dest name.
    'telegram'          -> TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
    'telegram_robotics' -> TELEGRAM_BOT_TOKEN_ROBOTICS / TELEGRAM_CHAT_ID_ROBOTICS
    """
    suffix = name[len("telegram_"):].upper() if name != "telegram" else ""
    token_key   = f"TELEGRAM_BOT_TOKEN_{suffix}" if suffix else "TELEGRAM_BOT_TOKEN"
    chat_key    = f"TELEGRAM_CHAT_ID_{suffix}"   if suffix else "TELEGRAM_CHAT_ID"
    token   = os.environ.get(token_key, "")
    chat_id = os.environ.get(chat_key, "")
    if not token or not chat_id:
        log(f"❌ Missing env vars for '{name}': need {token_key} and {chat_key}")
        sys.exit(1)
    return TelegramDelivery(token=token, chat_id=chat_id)


def _make_markdown():
    output_dir = os.environ.get("MARKDOWN_OUTPUT_DIR", "output/markdown")
    return MarkdownDelivery(output_dir)


def _make_github():
    repo_path = os.environ.get("GITHUB_REPO_PATH", "")
    md_dir    = os.environ.get("GITHUB_MD_DIR", "src/content/briefings")
    branch    = os.environ.get("GITHUB_BRANCH", "main")
    if not repo_path:
        log("❌ GITHUB_REPO_PATH not set in .env")
        sys.exit(1)
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        log(f"❌ GITHUB_REPO_PATH '{repo_path}' is not a git repository")
        sys.exit(1)
    return GitHubDelivery(repo_path=repo_path, md_dir=md_dir, branch=branch)


def make_delivery(dest, dry_run, cfg=None):
    """Build a Delivery for a dest string (comma-separated names).
    Supports: 'console', 'telegram', 'telegram_<name>', 'markdown', 'github'.
    """
    if dry_run:
        return ConsoleDelivery()

    names = [d.strip() for d in dest.split(",") if d.strip()]
    deliveries = []
    for name in names:
        if name == "console":
            deliveries.append(ConsoleDelivery())
        elif name == "telegram" or name.startswith("telegram_"):
            deliveries.append(_make_telegram(name))
        elif name == "markdown":
            deliveries.append(_make_markdown())
        elif name == "github":
            deliveries.append(_make_github())
        else:
            log(f"❌ Unknown delivery destination: '{name}'. Use 'telegram', 'telegram_<name>', 'markdown', 'github', or 'console'.")
            sys.exit(1)

    return deliveries[0] if len(deliveries) == 1 else MultiDelivery(deliveries)


