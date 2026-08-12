"""
LAYER 5 — DELIVERY
Sends a short, scannable digest to Telegram. Top roles only.
If it doesn't fit on a phone screen, it won't get read.
"""

import html
import json
import os
import urllib.parse
import urllib.request


def _esc(value):
    """
    Escape a scraped value for Telegram's HTML parse mode.

    Job titles are scraped, not authored — `C++ <Dev> & Co` is an
    ordinary listing. Telegram rejects a message whose HTML it cannot
    parse with a 400, which takes down the WHOLE digest, not just the
    offending line, and the failure is swallowed by the caller's except.
    The user simply never hears from the bot again.
    """
    return html.escape("" if value is None else str(value), quote=True)


def _score(value):
    """A missing or non-numeric score must not take the message down."""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "—"


def _send(text, chat_id=None, dry=False):
    """
    chat_id is passed explicitly by every multi-user caller.

    The environment fallback exists only for the single-user CLI. In the
    portal a missing chat id must SKIP, never fall back to the env var —
    that is how one user's roles end up in another user's Telegram.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    if dry:
        print("\n[dry — nothing sent]\n")
        print(text)
        return False

    if not token or not chat:
        print("\n[no telegram creds — printing instead]\n")
        print(text)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20)
        return True
    except Exception as e:
        print(f"[telegram] failed: {e}")
        return False


def send_scan_summary(user, jobs, portal_url, dry=False, top_n=3):
    """
    One short message per user after a scan. Detail lives in the portal.

    Takes the user row so the chat id can only ever come from the user
    the results belong to. A user with no chat id is skipped with a
    warning — never delivered to whatever TELEGRAM_CHAT_ID happens to be
    in the environment.
    """
    chat_id = user["telegram_chat_id"] if "telegram_chat_id" in user.keys() \
        else None
    username = user["username"]

    if not jobs:
        return False
    if not chat_id and not dry:
        print(f"  [telegram] no chat id for {username} — skipped")
        return False

    lines = [f"<b>Career Loop</b> — {len(jobs)} new roles scored\n"]
    for j in jobs[:top_n]:
        stretch = " ↗" if j.get("stretch") else ""
        lines.append(f"<b>{_score(j.get('score'))}</b> — "
                     f"{_esc(j.get('title'))}{stretch}\n"
                     f"{_esc(j.get('company'))}")
    lines.append(f"\n<a href=\"{_esc(portal_url)}\">Open the portal</a>")

    return _send("\n".join(lines), chat_id=chat_id, dry=dry)


def send_digest(jobs, target, top_n=5, chat_id=None):
    if not jobs:
        _send("<b>Career Loop</b>\n\nNothing new cleared the bar today.\n"
              "That's a signal, not a failure — the bar is set high on purpose.",
              chat_id=chat_id)
        return

    urgent = target.get("urgent_score", 8.5)
    lines = [f"<b>Career Loop</b> — {len(jobs)} roles cleared the bar\n"]

    for j in jobs[:top_n]:
        try:
            flag = "🔥 APPLY TODAY" if float(j.get("score")) >= urgent else ""
        except (TypeError, ValueError):
            flag = ""
        stretch = "  ↗ stretch" if j.get("stretch") else ""
        lines.append(
            f"\n<b>{_score(j.get('score'))}</b> — "
            f"{_esc(j.get('title'))}{stretch} {flag}\n"
            f"{_esc(j.get('company'))} · {_esc(j.get('location') or 'NA')}\n"
            f"<i>{_esc(j.get('verdict'))}</i>\n"
            f"→ <b>Your angle:</b> {_esc(j.get('angle', ''))}\n"
            f"<a href=\"{_esc(j.get('url'))}\">Open listing</a>"
        )

    if len(jobs) > top_n:
        lines.append(f"\n\n+{len(jobs) - top_n} more in data/latest.json")

    _send("\n".join(lines), chat_id=chat_id)
