"""
⏰ Reminders (opened from the ⏰ button under the input box): one-time, daily and weekly reminders, 100% free (no VIP, no coins, no limit).
Ported from the old Reminder_Bot, without payments / VIP / bans and without extra packages
(no apscheduler, no telegram_bot_calendar): everything is stored in the bot's DB and a small
job (tick) sends due reminders, so nothing is lost on restart.
Dates can be picked with buttons or typed in Persian (1405/07/20) or Gregorian (2026/10/12).
"""
from __future__ import annotations

import calendar
import html
import re
from db import postgres as database
import time as _time
from contextlib import closing
from datetime import date, datetime, time, timedelta

import pytz
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as KB, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes

import logging

import bot_settings as cfg
from bot import tz as zones

logger = logging.getLogger(__name__)

AWAIT_DETAILS = "rem_details"   # waiting for "HH:MM text" of a new reminder
AWAIT_DATE = "rem_date"         # waiting for a typed date (one-time reminder)
AWAIT_EDIT = "rem_edit"         # waiting for "HH:MM new text" of an existing reminder
MAX_TEXT = 500
PAGE = 8
LATE_SKIP = 3600                # a repeating reminder more than 1h late (bot was down) is skipped, not spammed

TIMEZONES = zones.TIMEZONES      # one list for the whole bot (see tz.py)

# ───────────────  dates (Persian ⇄ Gregorian, no extra package)  ────────────────
J_MONTHS = {'fa': ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                   "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]}
G_MONTHS = {'en': ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]}
WEEKDAYS = {   # Python weekday(): Monday = 0
    'fa': ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یک‌شنبه"],
    'en': ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}
WEEK_ORDER = {'fa': [5, 6, 0, 1, 2, 3, 4], 'en': [0, 1, 2, 3, 4, 5, 6]}   # Persian week starts on Saturday
_TO_LATIN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_TO_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_num(x) -> str:
    return str(x).translate(_TO_FA)


def g2j(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def j2g(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd
    days += (jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    months = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 12 and gd > months[gm]:
        gd -= months[gm]
        gm += 1
    return gy, gm + 1, gd


def fmt_date(d: date, lang: str, weekday: bool = True) -> str:
    wd = WEEKDAYS[lang][d.weekday()]
    if lang == 'fa':
        jy, jm, jd = g2j(d.year, d.month, d.day)
        s = f"{fa_num(jd)} {J_MONTHS['fa'][jm - 1]} {fa_num(jy)}"
    else:
        s = f"{d.day} {G_MONTHS['en'][d.month - 1]} {d.year}"
    return f"{wd} {s}" if weekday else s


def short_date(d: date, lang: str) -> str:
    """Button label: 'شنبه ۱۸ مهر' / 'Sat 10 Oct'."""
    if lang == 'fa':
        _, jm, jd = g2j(d.year, d.month, d.day)
        return f"{WEEKDAYS['fa'][d.weekday()]} {fa_num(jd)} {J_MONTHS['fa'][jm - 1]}"
    return f"{WEEKDAYS['en'][d.weekday()][:3]} {d.day} {G_MONTHS['en'][d.month - 1]}"


def parse_date(text: str) -> date | None:
    """'1405/07/20', '1405-7-20' (Persian) or '2026/10/12' (Gregorian), Persian digits are fine."""
    m = re.fullmatch(r"\s*(\d{4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*", text.translate(_TO_LATIN))
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        if y < 1700:   # Persian year
            if not (1 <= mo <= 12 and 1 <= d <= (31 if mo <= 6 else 30)):
                return None
            res = date(*j2g(y, mo, d))
            return res if g2j(res.year, res.month, res.day) == (y, mo, d) else None   # rejects 30 Esfand of non-leap
        return date(y, mo, d)
    except ValueError:
        return None


def parse_details(text: str) -> tuple[str, str] | None:
    """'09:30 Call Sara' → ('09:30', 'Call Sara'). Persian digits and 9.30 / ۹:۳۰ are fine."""
    m = re.match(r"^\s*(\d{1,2})\s*[:.٫]\s*(\d{2})\s+(.+)$", text.translate(_TO_LATIN), re.S)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    body = text[m.start(3):].strip()   # same positions (1:1 digit swap), keeps the user's own digits in the text
    return (f"{h:02d}:{mi:02d}", body[:MAX_TEXT]) if body else None


# ───────────────  DB  ────────────────
def _db():
    return database.connect()


def ensure_tables():
    database.initialize()


def get_tz(chat_id: int, lang: str) -> str:
    """The user's zone (their own pick, else automatic from the language): same one the whole bot uses."""
    return zones.zone_name(lang, chat_id)


def tz_label(tz: str, lang: str) -> str:
    for name, fa, en in TIMEZONES:
        if name == tz:
            return fa if lang == 'fa' else en
    return tz


def month_pos(d: date, cal: str) -> tuple[int, int]:
    """(day of month, days in that month) of date d in the Persian or Gregorian calendar."""
    if cal == "jalali":
        jy, jm, jd = g2j(d.year, d.month, d.day)
        first = date(*j2g(jy, jm, 1))
        nxt = date(*j2g(jy + (jm == 12), jm % 12 + 1, 1))
        return jd, (nxt - first).days
    return d.day, calendar.monthrange(d.year, d.month)[1]


def next_run(kind: str, hhmm: str, tz: str, on_date: str | None = None, weekday: int | None = None,
             after: datetime | None = None) -> int | None:
    """Unix time of the next send strictly after `after` (UTC now by default). None = in the past."""
    zone = pytz.timezone(tz)
    after = after or datetime.now(pytz.UTC)
    h, m = map(int, hhmm.split(":"))

    def at(d: date) -> datetime:
        return zone.normalize(zone.localize(datetime.combine(d, time(h, m))))

    if kind == "once":
        when = at(date.fromisoformat(on_date))
        return int(when.timestamp()) if when > after else None
    today = after.astimezone(zone).date()
    for i in range(70 if kind == "monthly" else 9):
        d = today + timedelta(days=i)
        if kind == "weekly" and d.weekday() != weekday:
            continue
        if kind == "monthly":
            dom, length = month_pos(d, on_date or "gregorian")
            if dom != min(weekday, length):   # day 31 in a 30-day month → the last day of that month
                continue
        when = at(d)
        if when > after:
            return int(when.timestamp())
    return None


def add_reminder(chat_id: int, text: str, kind: str, hhmm: str, tz: str,
                 on_date: str | None = None, weekday: int | None = None) -> int | None:
    nr = next_run(kind, hhmm, tz, on_date, weekday)
    if nr is None:
        return None
    with closing(_db()) as conn, conn:
        conn.execute(
            "INSERT INTO user_reminders (chat_id, text, kind, hhmm, on_date, weekday, tz, next_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (chat_id, text, kind, hhmm, on_date, weekday, tz, nr))
    return nr


def list_reminders(chat_id: int) -> list[dict]:
    with closing(_db()) as conn:
        rows = conn.execute(
            "SELECT id, text, kind, hhmm, on_date, weekday, tz, next_run FROM user_reminders "
            "WHERE chat_id = ? ORDER BY next_run IS NULL, next_run", (chat_id,)).fetchall()
    keys = ("id", "text", "kind", "hhmm", "on_date", "weekday", "tz", "next_run")
    return [dict(zip(keys, r)) for r in rows]


def get_reminder(chat_id: int, rid: int) -> dict | None:
    return next((r for r in list_reminders(chat_id) if r["id"] == rid), None)


def delete_reminder(chat_id: int, rid: int) -> bool:
    with closing(_db()) as conn, conn:
        return conn.execute("DELETE FROM user_reminders WHERE id = ? AND chat_id = ?", (rid, chat_id)).rowcount > 0


def count(chat_id: int) -> int:
    try:
        with closing(_db()) as conn:
            return conn.execute("SELECT COUNT(*) FROM user_reminders WHERE chat_id = ?", (chat_id,)).fetchone()[0]
    except Exception:
        return 0


def set_tz(chat_id: int, tz: str | None, lang: str = "en"):
    """New time zone for the whole bot; None = automatic (from the language). The user's reminders keep their
    clock time (9:00 stays 9:00 in the new zone)."""
    zones.set_zone(chat_id, tz, lang)


# ───────────────  texts  ────────────────
T = {
    'fa': {
        'menu': "⏰ یادآورها",
        'title': "⏰ <b>یادآورها</b>",
        'about': "یادآور یک‌باره، روزانه یا هفتگی بساز.",
        'tz': "🌍 منطقه زمانی: <b>{tz}</b>",
        'count': "📋 یادآورهای فعال: <b>{n}</b>",
        'new': "➕ یادآور جدید", 'mine': "📋 یادآورهای من ({n})", 'mine_short': "📋 یادآورهای من", 'tz_btn': "🌍 منطقه زمانی · {tz}",
        'back': "🔙 بازگشت", 'cancel': "✖️ انصراف", 'home': "📊 اخبار اقتصادی", 'another': "➕ یکی دیگه",
        'type_q': "📝 <b>نوع یادآور</b>\n\nچند وقت یه بار یادت بندازم؟",
        'once': "📅 یک‌بار", 'daily': "🔁 هر روز", 'weekly': "📆 هر هفته", 'monthly': "🗓 هر ماه",
        'mday_q': "🗓 <b>چندم هر ماه؟</b>\n\nاول تقویم رو انتخاب کن، بعد روز رو بزن.\n"
                  "اگه ماهی اون روز رو نداشت (مثلاً ۳۱ام)، آخرین روز همون ماه یادت میندازم.",
        'cal_jalali': "☀️ شمسی", 'cal_gregorian': "🌍 میلادی",
        'head_monthly': "🗓 <b>هر ماه، روز {d} {cal}</b>",
        'every_month': "هر ماه، روز {d} {cal}",
        'date_q': "📅 <b>کدوم روز؟</b>\n\nیه روز رو بزن، یا «تاریخ دیگه» رو بزن و تاریخ رو بنویس.",
        'other_date': "📝 تاریخ دیگه",
        'type_date': "📝 تاریخ رو بفرست، شمسی یا میلادی:\n\n<code>1405/07/25</code>\n<code>2026/10/17</code>",
        'bad_date': "❌ این تاریخ رو نفهمیدم. مثل <code>1405/07/25</code> یا <code>2026/10/17</code> بفرست.",
        'past_date': "⚠️ این تاریخ گذشته. یه روز از امروز به بعد بفرست.",
        'day_q': "📆 <b>کدوم روز هفته؟</b>",
        'details_q': "{head}\n\nحالا ساعت و متن یادآور رو بفرست:\n\n<code>09:30 تماس با سارا</code>\n\n🌍 ساعت به وقت {tz}",
        'head_once': "📅 یک‌بار، <b>{d}</b>", 'head_daily': "🔁 <b>هر روز</b>", 'head_weekly': "📆 <b>هر {d}</b>",
        'bad_details': "❌ فرمت درست نیست. اینطوری بفرست:\n<code>09:30 متن یادآور</code>",
        'past': "⚠️ این زمان گذشته! یه ساعت دیگه بفرست:\n<code>09:30 متن یادآور</code>",
        'saved': "✅ <b>یادآور ذخیره شد</b>\n\n{line}\n\n⏭ دفعه‌ی بعد: <b>{next}</b>",
        'list_title': "📋 <b>یادآورهای من</b>",
        'empty': "هنوز یادآوری نداری. با «➕ یادآور جدید» یکی بساز.",
        'edit_q': "✏️ <b>ویرایش یادآور</b>\n\n{line}\n\nساعت و متن جدید رو بفرست:\n<code>09:30 متن جدید</code>",
        'edited': "✅ <b>یادآور ویرایش شد</b>\n\n{line}\n\n⏭ دفعه‌ی بعد: <b>{next}</b>",
        'deleted': "🗑 حذف شد", 'gone': "این یادآور دیگه وجود نداره",
        'tz_q': "🌍 <b>منطقه زمانی</b>\n\nساعت خبرها، تاریخ و یادآورها همه به وقت همین منطقه‌ست.\n«خودکار» یعنی با زبان عوض میشه (فارسی ← تهران، انگلیسی ← نیویورک).",
        'tz_set': "🌍 {tz}",
        'fire': "⏰ <b>یادآور</b>\n\n{text}",
        'every_day': "هر روز", 'every': "هر {d}", 'at': "ساعت {t}",
        'prev': "◀️ قبلی", 'next_p': "بعدی ▶️",
    },
    'en': {
        'menu': "⏰ Reminders",
        'title': "⏰ <b>Reminders</b>",
        'about': "Create one-time, daily or weekly reminders.",
        'tz': "🌍 Time zone: <b>{tz}</b>",
        'count': "📋 Active reminders: <b>{n}</b>",
        'new': "➕ New reminder", 'mine': "📋 My reminders ({n})", 'mine_short': "📋 My reminders", 'tz_btn': "🌍 Time zone · {tz}",
        'back': "🔙 Back", 'cancel': "✖️ Cancel", 'home': "📊 Economic News", 'another': "➕ Another one",
        'type_q': "📝 <b>Reminder type</b>\n\nHow often should I remind you?",
        'once': "📅 One-time", 'daily': "🔁 Daily", 'weekly': "📆 Weekly", 'monthly': "🗓 Monthly",
        'mday_q': "🗓 <b>Which day of the month?</b>\n\nTap a Gregorian day.\n"
                  "If a month doesn't have that day (e.g. the 31st), I remind you on its last day.",
        'cal_jalali': "☀️ Persian", 'cal_gregorian': "🌍 Gregorian",
        'head_monthly': "🗓 <b>Monthly, day {d} ({cal})</b>",
        'every_month': "monthly, day {d} ({cal})",
        'date_q': "📅 <b>Which day?</b>\n\nTap a day, or tap “Other date” and type it.",
        'other_date': "📝 Other date",
        'type_date': "📝 Send a Gregorian date:\n\n<code>2026/10/17</code>",
        'bad_date': "❌ I couldn't read that date. Send it like <code>2026/10/17</code>.",
        'past_date': "⚠️ That date has passed. Send today or a later day.",
        'day_q': "📆 <b>Which day of the week?</b>",
        'details_q': "{head}\n\nNow send the time and the reminder text:\n\n<code>09:30 Call Sara</code>\n\n🌍 Time in {tz}",
        'head_once': "📅 One-time, <b>{d}</b>", 'head_daily': "🔁 <b>Every day</b>", 'head_weekly': "📆 <b>Every {d}</b>",
        'bad_details': "❌ Wrong format. Send it like this:\n<code>09:30 Reminder text</code>",
        'past': "⚠️ That time has passed! Send another time:\n<code>09:30 Reminder text</code>",
        'saved': "✅ <b>Reminder saved</b>\n\n{line}\n\n⏭ Next: <b>{next}</b>",
        'list_title': "📋 <b>My reminders</b>",
        'empty': "No reminders yet. Tap “➕ New reminder” to create one.",
        'edit_q': "✏️ <b>Edit reminder</b>\n\n{line}\n\nSend the new time and text:\n<code>09:30 New text</code>",
        'edited': "✅ <b>Reminder updated</b>\n\n{line}\n\n⏭ Next: <b>{next}</b>",
        'deleted': "🗑 Deleted", 'gone': "This reminder no longer exists",
        'tz_q': "🌍 <b>Time zone</b>\n\nNews times, dates and reminders all use this zone.\n“Automatic” follows the language (Persian → Tehran, English → New York).",
        'tz_set': "🌍 {tz}",
        'fire': "⏰ <b>Reminder</b>\n\n{text}",
        'every_day': "every day", 'every': "every {d}", 'at': "at {t}",
        'prev': "◀️ Prev", 'next_p': "Next ▶️",
    },
}


def _t(lang: str) -> dict:
    return T.get(lang, T['en'])


def menu_label(lang: str, chat_id: int | None = None) -> str:
    n = count(chat_id) if chat_id is not None else 0
    return f"{_t(lang)['menu']} · {fa_num(n) if lang == 'fa' else n}" if n else _t(lang)['menu']


def _when(r: dict, lang: str) -> str:
    t = _t(lang)
    if r["kind"] == "once":
        return f"📅 {fmt_date(date.fromisoformat(r['on_date']), lang)}، {t['at'].format(t=r['hhmm'])}" if lang == 'fa' \
            else f"📅 {fmt_date(date.fromisoformat(r['on_date']), lang)}, {t['at'].format(t=r['hhmm'])}"
    if r["kind"] == "daily":
        return f"🔁 {t['every_day']} {t['at'].format(t=r['hhmm'])}"
    if r["kind"] == "monthly":
        return f"🗓 {_monthly_label(r['weekday'], r['on_date'], lang, 'every_month')} {t['at'].format(t=r['hhmm'])}"
    return f"📆 {t['every'].format(d=WEEKDAYS[lang][r['weekday']])} {t['at'].format(t=r['hhmm'])}"


def _monthly_label(day: int, cal: str, lang: str, key: str) -> str:
    t = _t(lang)
    if lang == 'en' and cal == 'jalali':
        # Keep existing schedules intact; do not misrepresent them as Gregorian.
        return "🗓 <b>Monthly (existing schedule)</b>" if key == "head_monthly" else "monthly (existing schedule)"
    return t[key].format(d=fa_num(day) if lang == 'fa' else day, cal=t['cal_' + (cal or 'gregorian')])


def _line(r: dict, lang: str) -> str:
    return f"{_when(r, lang)}\n📝 {html.escape(r['text'])}"


def _next_str(ts: int, tz: str, lang: str) -> str:
    local = datetime.fromtimestamp(ts, pytz.timezone(tz))
    return f"{fmt_date(local.date(), lang)} {local.strftime('%H:%M')}"


# ───────────────  screens  ────────────────
def home_view(chat_id: int, lang: str) -> tuple[str, KB]:
    t, tz, n = _t(lang), get_tz(chat_id, lang), count(chat_id)
    text = "\n".join([t['title'], "", t['about'], "", t['tz'].format(tz=tz_label(tz, lang)),
                      t['count'].format(n=fa_num(n) if lang == 'fa' else n)])
    kb = KB([
        [B(t['new'], callback_data="rem_new")],
        [B(t['mine'].format(n=fa_num(n) if lang == 'fa' else n), callback_data="rem_list_0")],
        [B(t['tz_btn'].format(tz=tz_label(tz, lang)), callback_data="rem_tz")],
        [B(t['back'], callback_data="main_menu")],
    ])
    return text, kb


def type_view(lang: str) -> tuple[str, KB]:
    t = _t(lang)
    return t['type_q'], KB([
        [B(t['once'], callback_data="rem_k_once"), B(t['daily'], callback_data="rem_k_daily")],
        [B(t['weekly'], callback_data="rem_k_weekly"), B(t['monthly'], callback_data="rem_k_monthly")],
        [B(t['back'], callback_data="rem")],
    ])


def date_view(chat_id: int, lang: str) -> tuple[str, KB]:
    t = _t(lang)
    today = datetime.now(pytz.timezone(get_tz(chat_id, lang))).date()
    rows, row = [], []
    for i in range(14):
        d = today + timedelta(days=i)
        row.append(B(short_date(d, lang), callback_data=f"rem_d_{d.strftime('%Y%m%d')}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    rows.append([B(t['other_date'], callback_data="rem_dtype")])
    rows.append([B(t['back'], callback_data="rem_new")])
    return t['date_q'], KB(rows)


def weekday_view(lang: str) -> tuple[str, KB]:
    t = _t(lang)
    btns = [B(WEEKDAYS[lang][i], callback_data=f"rem_w_{i}") for i in WEEK_ORDER[lang]]
    rows = [btns[0:3], btns[3:6], btns[6:7], [B(t['back'], callback_data="rem_new")]]
    return t['day_q'], KB(rows)


def monthday_view(lang: str, cal: str) -> tuple[str, KB]:
    t = _t(lang)
    if lang == 'en':
        cal = 'gregorian'
    rows = ([[B(("✅ " if c == cal else "") + t['cal_' + c], callback_data=f"rem_mc_{c}") for c in ("jalali", "gregorian")]]
            if lang == 'fa' else [])
    days = [B(fa_num(n) if lang == 'fa' else str(n), callback_data=f"rem_md_{cal}_{n}") for n in range(1, 32)]
    rows += [days[i:i + 7] for i in range(0, 31, 7)]
    rows.append([B(t['back'], callback_data="rem_new")])
    return t['mday_q'], KB(rows)


def details_prompt(chat_id: int, lang: str, draft: dict) -> tuple[str, KB]:
    t = _t(lang)
    if draft["kind"] == "once":
        head = t['head_once'].format(d=fmt_date(date.fromisoformat(draft["on_date"]), lang))
    elif draft["kind"] == "weekly":
        head = t['head_weekly'].format(d=WEEKDAYS[lang][draft["weekday"]])
    elif draft["kind"] == "monthly":
        head = _monthly_label(draft["weekday"], draft["on_date"], lang, 'head_monthly')
    else:
        head = t['head_daily']
    return (t['details_q'].format(head=head, tz=tz_label(get_tz(chat_id, lang), lang)),
            KB([[B(t['cancel'], callback_data="rem")]]))


def list_view(chat_id: int, lang: str, page: int = 0) -> tuple[str, KB]:
    t, items = _t(lang), list_reminders(chat_id)
    if not items:
        return f"{t['list_title']}\n\n{t['empty']}", KB([[B(t['new'], callback_data="rem_new")],
                                                         [B(t['back'], callback_data="rem")]])
    pages = (len(items) - 1) // PAGE + 1
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE:(page + 1) * PAGE]
    lines, rows = [t['list_title'], ""], []
    for i, r in enumerate(chunk, start=page * PAGE + 1):
        n = fa_num(i) if lang == 'fa' else str(i)
        lines += [f"<b>{n}.</b> {_line(r, lang)}", ""]
        rows.append([B(f"✏️ {n}", callback_data=f"rem_e_{r['id']}"),
                     B(f"🗑 {n}", callback_data=f"rem_x_{r['id']}_{page}")])
    nav = []
    if page > 0:
        nav.append(B(t['prev'], callback_data=f"rem_list_{page - 1}"))
    if page < pages - 1:
        nav.append(B(t['next_p'], callback_data=f"rem_list_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B(t['new'], callback_data="rem_new"), B(t['back'], callback_data="rem")])
    return "\n".join(lines).strip(), KB(rows)


def tz_view(chat_id: int, lang: str) -> tuple[str, KB]:
    t, cur = _t(lang), get_tz(chat_id, lang)
    auto_on = zones.is_auto(chat_id)
    auto_lbl = ("✅ " if auto_on else "") + ("🔄 خودکار · " if lang == 'fa' else "🔄 Automatic · ") \
        + zones.city(zones.auto_name(lang), lang)
    btns = [B(("✅ " if (name == cur and not auto_on) else "") + (fa if lang == 'fa' else en), callback_data=f"rem_z_{i}")
            for i, (name, fa, en) in enumerate(TIMEZONES)]
    rows = [[B(auto_lbl, callback_data="rem_z_auto")]] + [btns[i:i + 2] for i in range(0, len(btns), 2)] \
        + [[B(t['back'], callback_data="rem")]]
    return t['tz_q'], KB(rows)


def after_save_kb(lang: str) -> KB:
    t = _t(lang)
    return KB([[B(t['mine_short'], callback_data="rem_list_0"),
                B(t['another'], callback_data="rem_new")],
               [B(t['home'], callback_data="main_menu")]])


async def _edit(msg, text: str, kb: KB):
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ───────────────  handlers (wired from telegram_bot.py)  ────────────────
async def cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE, lang: str):
    """⏰ Reminders keyboard button: opens the reminders home as a new message."""
    ctx.user_data.pop("awaiting", None)
    text, kb = home_view(update.effective_chat.id, lang)
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, lang: str):
    """Every button whose data is 'rem' or starts with 'rem_'."""
    q = update.callback_query
    cid, data, t = q.message.chat.id, q.data, _t(lang)
    if str(ctx.user_data.get("awaiting") or "").startswith("rem_"):
        ctx.user_data.pop("awaiting", None)   # any button cancels a pending text input
    toast, view = None, None

    if data == "rem":
        view = home_view(cid, lang)
    elif data == "rem_new":
        ctx.user_data.pop("rem_draft", None)
        view = type_view(lang)
    elif data == "rem_k_once":
        view = date_view(cid, lang)
    elif data == "rem_k_weekly":
        view = weekday_view(lang)
    elif data == "rem_k_monthly" or data.startswith("rem_mc_"):
        cal = data[7:] if data.startswith("rem_mc_") else ("jalali" if lang == 'fa' else "gregorian")
        view = monthday_view(lang, cal if cal in ("jalali", "gregorian") else "gregorian")
    elif data.startswith("rem_md_"):
        _, _, cal, n = data.split("_")
        if lang == 'en' and cal != 'gregorian':
            await q.answer("Please select a Gregorian day.")
            await _edit(q.message, *monthday_view(lang, 'gregorian'))
            return
        # monthly reuses the columns: on_date = calendar, weekday = day of month
        ctx.user_data["rem_draft"] = {"kind": "monthly", "on_date": cal, "weekday": int(n)}
        ctx.user_data["awaiting"] = AWAIT_DETAILS
        view = details_prompt(cid, lang, ctx.user_data["rem_draft"])
    elif data == "rem_k_daily":
        ctx.user_data["rem_draft"] = {"kind": "daily"}
        ctx.user_data["awaiting"] = AWAIT_DETAILS
        view = details_prompt(cid, lang, ctx.user_data["rem_draft"])
    elif data.startswith("rem_d_"):
        d = datetime.strptime(data[6:], "%Y%m%d").date()
        ctx.user_data["rem_draft"] = {"kind": "once", "on_date": d.isoformat()}
        ctx.user_data["awaiting"] = AWAIT_DETAILS
        view = details_prompt(cid, lang, ctx.user_data["rem_draft"])
    elif data == "rem_dtype":
        ctx.user_data["awaiting"] = AWAIT_DATE
        view = (t['type_date'], KB([[B(t['cancel'], callback_data="rem")]]))
    elif data.startswith("rem_w_"):
        ctx.user_data["rem_draft"] = {"kind": "weekly", "weekday": int(data[6:])}
        ctx.user_data["awaiting"] = AWAIT_DETAILS
        view = details_prompt(cid, lang, ctx.user_data["rem_draft"])
    elif data.startswith("rem_list_"):
        view = list_view(cid, lang, int(data[9:] or 0))
    elif data.startswith("rem_x_"):
        _, _, rid, page = data.split("_")
        toast = t['deleted'] if delete_reminder(cid, int(rid)) else t['gone']
        view = list_view(cid, lang, int(page))
    elif data.startswith("rem_e_"):
        r = get_reminder(cid, int(data[6:]))
        if not r:
            toast, view = t['gone'], list_view(cid, lang)
        else:
            ctx.user_data["rem_edit_id"] = r["id"]
            ctx.user_data["awaiting"] = AWAIT_EDIT
            view = (t['edit_q'].format(line=_line(r, lang)), KB([[B(t['cancel'], callback_data="rem_list_0")]]))
    elif data == "rem_tz":
        view = tz_view(cid, lang)
    elif data == "rem_z_auto":
        set_tz(cid, None, lang)
        toast = t['tz_set'].format(tz=("خودکار · " if lang == 'fa' else "Automatic · ") + zones.city(get_tz(cid, lang), lang))
        view = home_view(cid, lang)
    elif data.startswith("rem_z_"):
        i = int(data[6:])
        if 0 <= i < len(TIMEZONES):
            set_tz(cid, TIMEZONES[i][0], lang)
            toast = t['tz_set'].format(tz=tz_label(TIMEZONES[i][0], lang))
        view = home_view(cid, lang)
    else:
        view = home_view(cid, lang)

    await q.answer(toast)
    await _edit(q.message, *view)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, lang: str):
    """Text typed while a reminder screen is waiting for input."""
    cid, t = update.effective_chat.id, _t(lang)
    msg = update.message
    text = (msg.text or "").strip()
    awaiting = ctx.user_data.get("awaiting")
    cancel_kb = KB([[B(t['cancel'], callback_data="rem")]])
    tz = get_tz(cid, lang)

    if awaiting == AWAIT_DATE:
        date_text = text.translate(_TO_LATIN)
        year_match = re.match(r"\s*(\d{4})", date_text)
        d = parse_date(text) if lang != 'en' or (year_match and int(year_match.group(1)) >= 1700) else None
        if not d:
            await msg.reply_text(t['bad_date'], reply_markup=cancel_kb, parse_mode=ParseMode.HTML)
            return
        if d < datetime.now(pytz.timezone(tz)).date():
            await msg.reply_text(t['past_date'], reply_markup=cancel_kb, parse_mode=ParseMode.HTML)
            return
        ctx.user_data["rem_draft"] = {"kind": "once", "on_date": d.isoformat()}
        ctx.user_data["awaiting"] = AWAIT_DETAILS
        p_text, p_kb = details_prompt(cid, lang, ctx.user_data["rem_draft"])
        await msg.reply_text(p_text, reply_markup=p_kb, parse_mode=ParseMode.HTML)
        return

    parsed = parse_details(text)
    if not parsed:
        await msg.reply_text(t['bad_details'], reply_markup=cancel_kb, parse_mode=ParseMode.HTML)
        return
    hhmm, body = parsed

    if awaiting == AWAIT_EDIT:
        r = get_reminder(cid, ctx.user_data.get("rem_edit_id", -1))
        if not r:
            ctx.user_data.pop("awaiting", None)
            await msg.reply_text(t['gone'], reply_markup=after_save_kb(lang))
            return
        nr = next_run(r["kind"], hhmm, r["tz"], r["on_date"], r["weekday"])
        if nr is None:
            await msg.reply_text(t['past'], reply_markup=cancel_kb, parse_mode=ParseMode.HTML)
            return
        with closing(_db()) as conn, conn:
            conn.execute("UPDATE user_reminders SET text = ?, hhmm = ?, next_run = ? WHERE id = ? AND chat_id = ?",
                         (body, hhmm, nr, r["id"], cid))
        ctx.user_data.pop("awaiting", None)
        ctx.user_data.pop("rem_edit_id", None)
        r.update(text=body, hhmm=hhmm)
        await msg.reply_text(t['edited'].format(line=_line(r, lang), next=_next_str(nr, r["tz"], lang)),
                             reply_markup=after_save_kb(lang), parse_mode=ParseMode.HTML)
        return

    draft = ctx.user_data.get("rem_draft")
    if not draft:
        ctx.user_data.pop("awaiting", None)
        text_, kb = home_view(cid, lang)
        await msg.reply_text(text_, reply_markup=kb, parse_mode=ParseMode.HTML)
        return
    nr = add_reminder(cid, body, draft["kind"], hhmm, tz, draft.get("on_date"), draft.get("weekday"))
    if nr is None:
        await msg.reply_text(t['past'], reply_markup=cancel_kb, parse_mode=ParseMode.HTML)
        return
    ctx.user_data.pop("awaiting", None)
    ctx.user_data.pop("rem_draft", None)
    r = {"kind": draft["kind"], "hhmm": hhmm, "on_date": draft.get("on_date"), "weekday": draft.get("weekday"),
         "text": body}
    await msg.reply_text(t['saved'].format(line=_line(r, lang), next=_next_str(nr, tz, lang)),
                         reply_markup=after_save_kb(lang), parse_mode=ParseMode.HTML)


async def tick(ctx: ContextTypes.DEFAULT_TYPE, get_language=None, keyboard_for=None):
    """Job (every 30s): send due reminders, then schedule the next one / delete one-time ones."""
    now = int(_time.time())
    try:
        with closing(_db()) as conn:
            due = conn.execute(
                "SELECT id, chat_id, text, kind, hhmm, on_date, weekday, tz, next_run FROM user_reminders "
                "WHERE next_run IS NOT NULL AND next_run <= ? ORDER BY next_run LIMIT 200", (now,)).fetchall()
    except Exception as e:
        logger.error(f"reminders.tick query failed: {e}")
        return
    for rid, cid, text, kind, hhmm, on_date, wd, tz, nr in due:
        late = now - nr
        # reschedule FIRST, so a slow/failed send never makes it fire twice
        with closing(_db()) as conn, conn:
            if kind == "once":
                conn.execute("DELETE FROM user_reminders WHERE id = ?", (rid,))
            else:
                conn.execute("UPDATE user_reminders SET next_run = ? WHERE id = ?",
                             (next_run(kind, hhmm, tz, on_date, wd), rid))
        if kind != "once" and late > LATE_SKIP:
            continue
        lang = get_language(cid) if get_language else "fa"
        try:
            await ctx.bot.send_message(cid, _t(lang)['fire'].format(text=html.escape(text)), parse_mode=ParseMode.HTML,
                                       reply_markup=keyboard_for(lang, cid) if keyboard_for else None)
        except Forbidden:
            pass   # user blocked the bot; repeating ones simply try again next time
        except Exception as e:
            logger.warning(f"Reminder {rid} for {cid} failed: {e}")
