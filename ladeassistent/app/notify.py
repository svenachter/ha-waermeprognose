"""When to send a notification: dedup, quiet hours, snooze. Pure logic, state is a plain dict."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ACTION_PREFIX = "LADEASSISTENT_"
ACTION_SNOOZE = ACTION_PREFIX + "SNOOZE"
ACTION_OK = ACTION_PREFIX + "OK"


def _t(s: str) -> time:
    h, m = s.split(":")[:2]
    return time(int(h), int(m))


def in_quiet(now: datetime, tz: ZoneInfo, start: str, end: str) -> bool:
    t = now.astimezone(tz).time()
    a, b = _t(start), _t(end)
    return (a <= t or t < b) if a > b else (a <= t < b)


def should_send(advice, now: datetime, tz: ZoneInfo, state: dict, *, quiet_start: str,
                quiet_end: str, lead_hours: float) -> tuple[bool, str]:
    """Return (send, reason). ``state`` holds ``sent`` {key: iso} and ``snooze_until`` iso."""
    if advice.status not in ("urgent", "opportunity") or not advice.key:
        return False, "kein Anlass"
    if advice.key in state.get("sent", {}):
        return False, "bereits gemeldet"
    snooze = state.get("snooze_until")
    if snooze and now < datetime.fromisoformat(snooze):
        return False, "pausiert"
    if in_quiet(now, tz, quiet_start, quiet_end):
        return False, "Ruhezeit"
    if advice.status == "opportunity" and advice.window:
        start = datetime.fromisoformat(advice.window["start"])
        if start - now > timedelta(hours=lead_hours):
            return False, "Fenster noch zu weit weg"
    return True, "ok"


def mark_sent(state: dict, key: str, now: datetime, keep_days: int = 14) -> None:
    sent = state.setdefault("sent", {})
    sent[key] = now.isoformat()
    cutoff = now - timedelta(days=keep_days)
    for k in [k for k, v in sent.items() if datetime.fromisoformat(v) < cutoff]:
        del sent[k]


def handle_action(state: dict, action: str, now: datetime, snooze_hours: float = 24) -> bool:
    if action == ACTION_SNOOZE:
        state["snooze_until"] = (now + timedelta(hours=snooze_hours)).isoformat()
        return True
    return action.startswith(ACTION_PREFIX)


def payload(advice, tag: str = "ladeassistent") -> dict:
    return {"title": advice.title, "message": advice.message,
            "data": {"tag": tag, "group": "ladeassistent", "clickAction": "/energy",
                     "actions": [{"action": ACTION_OK, "title": "OK"},
                                 {"action": ACTION_SNOOZE, "title": "Heute nicht"}]}}
