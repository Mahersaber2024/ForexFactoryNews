"""
tz.py: ONE time zone per user, used by everything in the bot.

    AUTOMATIC (default)   Persian (fa) -> Tehran (Asia/Tehran)      anything else -> New York (America/New_York)
    MANUAL                the user picks a city in the language settings (language + time zone live together)

Where the zone is used: the times a user sets (daily news, daily date, reminders), every event time shown in a
message or a picture, the dates, and the "which Forex Factory day is my today" question. Forex Factory itself always
speaks New York time, so event rows are converted only for display (`localize_event`); the absolute timestamps used
for the 30-minute alerts are never touched.

The choice is stored in the table `reminder_tz` (the same table the reminders always used, so nobody loses a zone
they already picked). No row = automatic. Because "automatic" is computed from the language on every call, switching
the language changes the zone by itself, and `on_language_change` re-times the user's reminders at that moment.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from contextlib import closing
from datetime import datetime

import pytz

import bot_settings as cfg

NY_TZ = pytz.timezone("America/New_York")
TEHRAN_TZ = pytz.timezone("Asia/Tehran")
NY_NAME = "America/New_York"
TEHRAN_NAME = "Asia/Tehran"
AUTO = "auto"

# name, flag, city (fa), city (en), abbreviation (None = ask pytz: GMT/BST, CET/CEST ...)
_ZONE_INFO = [
    ("Asia/Tehran",         "🇮🇷", "تهران",      "Tehran",      "IRST"),
    ("Asia/Kabul",          "🇦🇫", "کابل",       "Kabul",       "AFT"),
    ("Asia/Dubai",          "🇦🇪", "دبی",        "Dubai",       "GST"),
    ("Asia/Baghdad",        "🇮🇶", "بغداد",      "Baghdad",     "AST"),
    ("Europe/Istanbul",     "🇹🇷", "استانبول",   "Istanbul",    "TRT"),
    ("Asia/Baku",           "🇦🇿", "باکو",       "Baku",        "AZT"),
    ("Asia/Tashkent",       "🇺🇿", "تاشکند",     "Tashkent",    "UZT"),
    ("Europe/Moscow",       "🇷🇺", "مسکو",       "Moscow",      "MSK"),
    ("Europe/London",       "🇬🇧", "لندن",       "London",      None),
    ("Europe/Berlin",       "🇩🇪", "برلین",      "Berlin",      None),
    ("America/New_York",    "🇺🇸", "نیویورک",    "New York",    "ET"),
    ("America/Chicago",     "🇺🇸", "شیکاگو",     "Chicago",     "CT"),
    ("America/Los_Angeles", "🇺🇸", "لس‌آنجلس",   "Los Angeles", "PT"),
    ("America/Toronto",     "🇨🇦", "تورنتو",     "Toronto",     "ET"),
    ("Asia/Kolkata",        "🇮🇳", "هند",        "India",       "IST"),
    ("Asia/Singapore",      "🇸🇬", "سنگاپور",    "Singapore",   "SGT"),
    ("Asia/Hong_Kong",      "🇭🇰", "هنگ‌کنگ",    "Hong Kong",   "HKT"),
    ("Asia/Tokyo",          "🇯🇵", "توکیو",      "Tokyo",       "JST"),
    ("Australia/Sydney",    "🇦🇺", "سیدنی",      "Sydney",      None),
    ("UTC",                 "🌍", "UTC",        "UTC",         "UTC"),
]
_INFO = {name: (flag, fa, en, abbr) for name, flag, fa, en, abbr in _ZONE_INFO}
# same shape the reminders always used: (name, "🏙 fa label", "🏙 en label")
TIMEZONES = [(name, f"{flag} {fa}", f"{flag} {en}") for name, flag, fa, en, _ in _ZONE_INFO]

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_FA_WEEKDAYS = {"mon": "دوشنبه", "tue": "سه‌شنبه", "wed": "چهارشنبه", "thu": "پنج‌شنبه",
                "fri": "جمعه", "sat": "شنبه", "sun": "یک‌شنبه"}
_FA_MONTHS = {"jan": "ژانویه", "feb": "فوریه", "mar": "مارس", "apr": "آوریل", "may": "مه", "jun": "ژوئن",
              "jul": "ژوئیه", "aug": "اوت", "sep": "سپتامبر", "oct": "اکتبر", "nov": "نوامبر", "dec": "دسامبر"}
_TIME_FORMATS = ("%H:%M", "%I:%M%p", "%I%p")


# ───────────────  who is Persian  ────────────────
def is_fa(language: str) -> bool:
    return language == "fa"


def fa_num(x) -> str:
    return str(x).translate(_FA_DIGITS)


# ───────────────  the stored choice  ────────────────
def _db():
    return sqlite3.connect(cfg.DB_PATH, timeout=10)


def _ensure(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS reminder_tz (chat_id INTEGER PRIMARY KEY, tz TEXT NOT NULL)")


def auto_name(language: str) -> str:
    """The zone 'automatic' means for this language."""
    return TEHRAN_NAME if is_fa(language) else NY_NAME


def override(chat_id: int | None) -> str | None:
    """The zone the user picked by hand, or None (= automatic)."""
    if chat_id is None:
        return None
    try:
        with closing(_db()) as conn:
            _ensure(conn)
            row = conn.execute("SELECT tz FROM reminder_tz WHERE chat_id = ?", (chat_id,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] in pytz.all_timezones_set else None


def is_auto(chat_id: int | None) -> bool:
    return override(chat_id) is None


def zone_name(language: str, chat_id: int | None = None) -> str:
    """Effective zone name: the user's pick, else the one that goes with the language."""
    return override(chat_id) or auto_name(language)


def zone(language: str, chat_id: int | None = None):
    return pytz.timezone(zone_name(language, chat_id))


user_tz = zone   # old name


def retime_reminders(chat_id: int, name: str) -> None:
    """The reminders keep their clock time (09:30 stays 09:30) but now in the new zone."""
    try:
        from bot import reminders          # lazy: reminders imports this module
        with closing(_db()) as conn, conn:
            rows = conn.execute("SELECT id, kind, hhmm, on_date, weekday FROM user_reminders WHERE chat_id = ?",
                                (chat_id,)).fetchall()
            for rid, kind, hhmm, on_date, wd in rows:
                conn.execute("UPDATE user_reminders SET tz = ?, next_run = ? WHERE id = ?",
                             (name, reminders.next_run(kind, hhmm, name, on_date, wd), rid))
    except Exception:
        pass                                # no reminders table yet / nothing to re-time


def set_zone(chat_id: int, name: str | None, language: str) -> str:
    """name = a zone name, or None / 'auto' for automatic. Returns the effective zone name."""
    if name in (None, "", AUTO):
        name = None
    elif name not in pytz.all_timezones_set:
        raise ValueError(f"unknown time zone: {name}")
    with closing(_db()) as conn, conn:
        _ensure(conn)
        if name is None:
            conn.execute("DELETE FROM reminder_tz WHERE chat_id = ?", (chat_id,))
        else:
            conn.execute("INSERT INTO reminder_tz (chat_id, tz) VALUES (?, ?) "
                         "ON CONFLICT(chat_id) DO UPDATE SET tz = excluded.tz", (chat_id, name))
    effective = zone_name(language, chat_id)
    retime_reminders(chat_id, effective)
    return effective


def on_language_change(chat_id: int, language: str) -> None:
    """Call after the language changed: an automatic zone just moved with it, so re-time the reminders."""
    if is_auto(chat_id):
        retime_reminders(chat_id, auto_name(language))


# ───────────────  names for screens and pictures  ────────────────
def city(name: str, language: str) -> str:
    info = _INFO.get(name)
    if info:
        return info[1] if is_fa(language) else info[2]
    return name.split("/")[-1].replace("_", " ")


def abbr(name: str) -> str:
    info = _INFO.get(name)
    if info and info[3]:
        return info[3]
    try:
        return datetime.now(pytz.timezone(name)).tzname() or "UTC"
    except Exception:
        return "UTC"


def label(name: str, language: str, flag: bool = True) -> str:
    """'🇮🇷 تهران' / '🇺🇸 New York'."""
    info = _INFO.get(name)
    text = city(name, language)
    return f"{info[0]} {text}" if (flag and info) else text


def status_label(language: str, chat_id: int | None = None) -> str:
    """For a settings line: 'خودکار · تهران' / 'Automatic · New York' / 'Dubai'."""
    name = zone_name(language, chat_id)
    if is_auto(chat_id):
        return f"{'خودکار' if is_fa(language) else 'Automatic'} · {city(name, language)}"
    return city(name, language)


def clock_note(language: str, chat_id: int | None = None) -> str:
    """'به وقت تهران' / 'New York time': the short phrase put next to a time."""
    name = zone_name(language, chat_id)
    return f"به وقت {city(name, language)}" if is_fa(language) else f"{city(name, language)} time"


def zone_text(name: str, language: str) -> dict:
    """The four time-zone captions the pictures use: sub / caps / mono / plain."""
    c, a = city(name, language), abbr(name)
    if is_fa(language):
        sub = f"به وقت {c}"
        return dict(sub=sub, caps=sub, mono=f"T+0 {c} ({a})", plain=sub)
    return dict(sub=f"{c} time ({a})", caps=f"{c} time ({a})".upper(),
                mono=f"T+0 {c.upper()} ({a})", plain=f"{c} time")


def display_date(date_text: str, language: str) -> str:
    """'Thu, Oct 29' -> 'پنج‌شنبه ۲۹ اکتبر' for Persian users; anything else is returned unchanged."""
    if not is_fa(language):
        return date_text
    m = re.fullmatch(r"\s*([A-Za-z]{3})[A-Za-z]*,?\s+([A-Za-z]{3})[A-Za-z]*\s+(\d{1,2})\s*", str(date_text or ""))
    if not m:
        return date_text
    wd, mo, day = _FA_WEEKDAYS.get(m.group(1).lower()), _FA_MONTHS.get(m.group(2).lower()), int(m.group(3))
    return f"{wd} {fa_num(day)} {mo}" if wd and mo else date_text


# ───────────────  clocks  ────────────────
def now_for(language: str, chat_id: int | None = None) -> datetime:
    return datetime.now(zone(language, chat_id))


def today_for(language: str, chat_id: int | None = None) -> dt.date:
    return now_for(language, chat_id).date()


def hhmm_for(language: str, chat_id: int | None = None) -> str:
    return now_for(language, chat_id).strftime("%H:%M")


def ny_today() -> dt.date:
    return datetime.now(NY_TZ).date()


def endpoint_for(endpoint: str, language: str, chat_id: int | None = None) -> str:
    """The Forex Factory day to ask for so that the user's own 'today' is covered.

    The API's days are New York days. At 07:00 in Tehran it is still the evening before in New York, so the user's
    'today' is New York's 'tomorrow' whenever the user's date is ahead of New York's (and the other way round for
    zones west of New York after New York's midnight)."""
    order = ("yesterday", "today", "tomorrow")
    if endpoint not in order:
        return endpoint
    offset = (today_for(language, chat_id) - ny_today()).days
    if offset == 0:
        return endpoint
    return order[min(2, max(0, order.index(endpoint) + offset))]


def all_overrides() -> dict[int, str]:
    """Every hand-picked zone in ONE read (for the loops that look at all users every 30 seconds)."""
    try:
        with closing(_db()) as conn:
            _ensure(conn)
            rows = conn.execute("SELECT chat_id, tz FROM reminder_tz").fetchall()
    except sqlite3.Error:
        return {}
    return {int(c): n for c, n in rows if n in pytz.all_timezones_set}


def bulk_now(langs: dict[int, str]) -> dict[int, datetime]:
    """{chat_id: language} -> {chat_id: the current time in that user's zone}, with a single database read."""
    picks, cache, utc_now = all_overrides(), {}, datetime.now(pytz.utc)
    out = {}
    for cid, lang in langs.items():
        name = picks.get(cid) or auto_name(lang)
        if name not in cache:
            cache[name] = pytz.timezone(name)
        out[cid] = utc_now.astimezone(cache[name])
    return out


# ───────────────  event rows  ────────────────
def _event_ts(ev: dict) -> int | None:
    """Unix time of a Forex Factory row (its date + clock time are New York time); None for All Day / unknown."""
    date_text = str(ev.get("Date") or "").strip()
    time_text = str(ev.get("Time") or "").strip().replace(" ", "").upper()
    time_obj = None
    for fmt in _TIME_FORMATS:
        try:
            time_obj = datetime.strptime(time_text, fmt).time()
            break
        except ValueError:
            continue
    if not date_text or time_obj is None:
        return None
    ref = ny_today()
    best = None
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            parsed = datetime.strptime(f"{date_text} {year}", "%a, %b %d %Y").date()
        except ValueError:
            continue
        if parsed.strftime("%a").lower() != date_text[:3].lower():
            continue
        if best is None or abs((parsed - ref).days) < abs((best - ref).days):
            best = parsed
    if best is None or abs((best - ref).days) > 8:
        return None
    try:
        return int(NY_TZ.localize(datetime.combine(best, time_obj), is_dst=None).timestamp())
    except (pytz.AmbiguousTimeError, pytz.NonExistentTimeError, OverflowError, ValueError):
        return None


def localize_event(ev: dict, language: str, chat_id: int | None = None) -> dict:
    """Copy of the row with Date/Time in the user's zone (same text style as Forex Factory: 'Thu, Oct 29' / '08:30').
    New York users and rows without a clock time are returned unchanged."""
    target = zone(language, chat_id)
    if target.zone == NY_NAME:
        return ev
    ts = _event_ts(ev)
    if ts is None:
        return ev
    local = datetime.fromtimestamp(ts, target)
    out = dict(ev)
    out["Date"] = f"{local:%a}, {local:%b} {local.day}"
    out["Time"] = local.strftime("%H:%M")
    return out


def localize_events(events: list[dict] | None, language: str, chat_id: int | None = None) -> list[dict] | None:
    if events is None or zone(language, chat_id).zone == NY_NAME:
        return events
    return [localize_event(e, language, chat_id) for e in events]
