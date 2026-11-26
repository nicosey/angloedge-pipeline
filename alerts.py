"""
Operational alerts for the unattended jobs, sent to a private Telegram chat.

  TELEGRAM_CHAT_ID_ALERTS    chat to alert (required; your own chat with the bot,
                             never the public channel)
  TELEGRAM_BOT_TOKEN_ALERTS  optional; defaults to TELEGRAM_BOT_TOKEN

Without TELEGRAM_CHAT_ID_ALERTS the alert is only logged, with a warning saying so.

Each job keeps a small state file in output/alerts/<job>.json, so a job failing
every run alerts once, again if the error changes, and again every
REPEAT_AFTER while it stays broken; the next success sends one "recovered".
"""
import html
import json
import os
import socket
from datetime import datetime, timedelta

from config import log

ALERT_DIR = os.path.join("output", "alerts")
REPEAT_AFTER = timedelta(hours=12)


def _send(text):
    chat = os.environ.get("TELEGRAM_CHAT_ID_ALERTS", "")
    token = os.environ.get("TELEGRAM_BOT_TOKEN_ALERTS") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not chat or not token:
        log("  ⚠ Alert not sent: set TELEGRAM_CHAT_ID_ALERTS in .env to receive job alerts")
        return False
    from delivery import TelegramDelivery
    return TelegramDelivery(token=token, chat_id=chat).send(text[:3900])


def _path(job):
    return os.path.join(ALERT_DIR, f"{job}.json")


def _load(job):
    try:
        with open(_path(job)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save(job, data):
    os.makedirs(ALERT_DIR, exist_ok=True)
    with open(_path(job), "w") as f:
        json.dump(data, f)


def _fmt(icon, job, body):
    host = socket.gethostname().split(".")[0]
    return f"{icon} <b>{html.escape(job)}</b> on {html.escape(host)}\n{html.escape(body)}"


def failure(job, message, now=None, send=None):
    """Record a failure; send an alert unless the same one was sent recently."""
    now = now or datetime.now()
    send = send or _send
    log(f"🚨 {job}: {message}")
    st = _load(job) or {"failing_since": now.isoformat(timespec="seconds"), "count": 0}
    st["count"] = st.get("count", 0) + 1
    last = st.get("last_alert")
    due = (st.get("message") != message or not last
           or now - datetime.fromisoformat(last) >= REPEAT_AFTER)
    if due:
        since = st["failing_since"][:16].replace("T", " ")
        body = f"{message}\n\nFailing since {since} ({st['count']} run(s))."
        if send(_fmt("🚨", job, body)):
            st["last_alert"] = now.isoformat(timespec="seconds")
    st["message"] = message
    _save(job, st)


def resolved(job, now=None, send=None):
    """Call after a successful run: one 'recovered' message if it had been failing."""
    st = _load(job)
    if not st:
        return
    send = send or _send
    since = st.get("failing_since", "?")[:16].replace("T", " ")
    send(_fmt("✅", job, f"Recovered (failing since {since}, {st.get('count', 0)} failed run(s))."))
    try:
        os.remove(_path(job))
    except OSError:
        pass


def notice(job, message, send=None):
    """Something was repaired automatically: worth knowing, not an outage."""
    log(f"🔧 {job}: {message}")
    (send or _send)(_fmt("🔧", job, message))
