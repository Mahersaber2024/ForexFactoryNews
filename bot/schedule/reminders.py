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
from bot.schedule import tz as zones
from bot.i18n import image_locale
from bot.i18n import language as i18n
from bot.i18n import i18n_extra
from bot.ui_edit import edit_or_resend

logger = logging.getLogger(__name__)

AWAIT_DETAILS = "rem_details"   # waiting for "HH:MM text" of a new reminder
AWAIT_TEXT = "rem_text"         # the time is chosen (chip / quick time): waiting for the text only
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
for _lang in image_locale.SUPPORTED:
    WEEKDAYS[_lang] = image_locale.DAYS[_lang]
    WEEK_ORDER[_lang] = WEEK_ORDER['en']
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
    elif lang in image_locale.SUPPORTED:     # month names in the user's own language
        s = f"{d.day} {image_locale.MONTHS[lang][d.month - 1]} {d.year}"
    else:
        s = f"{d.day} {G_MONTHS['en'][d.month - 1]} {d.year}"
    return f"{wd} {s}" if weekday else s


def short_date(d: date, lang: str) -> str:
    """Button label: 'شنبه ۱۸ مهر' / 'Sat 10 Oct'."""
    if lang == 'fa':
        _, jm, jd = g2j(d.year, d.month, d.day)
        return f"{WEEKDAYS['fa'][d.weekday()]} {fa_num(jd)} {J_MONTHS['fa'][jm - 1]}"
    if lang in image_locale.SUPPORTED:
        short = (lambda w: w) if lang in ("hi", "vi") else (lambda w: w[:3])   # Devanagari / Vietnamese: no cutting
        return f"{short(WEEKDAYS[lang][d.weekday()])} {d.day} {short(image_locale.MONTHS[lang][d.month - 1])}"
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
            return zones.label(name, lang)
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
        if kind == "days" and not (int(weekday or 0) >> d.weekday()) & 1:   # custom days: weekday = bitmask (Mon = bit 0)
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


WORK_MASK = 0b0011111      # Mon-Fri
ALL_MASK = 0b1111111


def counts(chat_id: int) -> tuple[int, int]:
    """(active, paused). A paused reminder is one whose next_run is NULL."""
    try:
        with closing(_db()) as conn:
            a = conn.execute("SELECT COUNT(*) FROM user_reminders WHERE chat_id = ? AND next_run IS NOT NULL", (chat_id,)).fetchone()[0]
            p = conn.execute("SELECT COUNT(*) FROM user_reminders WHERE chat_id = ? AND next_run IS NULL", (chat_id,)).fetchone()[0]
        return int(a), int(p)
    except Exception:
        return 0, 0


def upcoming(chat_id: int) -> dict | None:
    """The reminder that fires next (paused ones are skipped)."""
    items = [r for r in list_reminders(chat_id) if r["next_run"] is not None]
    return items[0] if items else None


def set_paused(chat_id: int, rid: int, paused: bool) -> bool:
    r = get_reminder(chat_id, rid)
    if not r:
        return False
    nr = None if paused else next_run(r["kind"], r["hhmm"], r["tz"], r["on_date"], r["weekday"])
    if not paused and nr is None:
        return False                      # a one-time reminder whose time has passed cannot be resumed
    with closing(_db()) as conn, conn:
        conn.execute("UPDATE user_reminders SET next_run = ? WHERE id = ? AND chat_id = ?", (nr, rid, chat_id))
    return True


def duplicate_reminder(chat_id: int, rid: int) -> int | None:
    r = get_reminder(chat_id, rid)
    if not r:
        return None
    nr = next_run(r["kind"], r["hhmm"], r["tz"], r["on_date"], r["weekday"])
    if nr is None:
        return None
    with closing(_db()) as conn, conn:
        conn.execute(
            "INSERT INTO user_reminders (chat_id, text, kind, hhmm, on_date, weekday, tz, next_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, r["text"], r["kind"], r["hhmm"], r["on_date"], r["weekday"], r["tz"], nr))
        return int(conn.execute("SELECT MAX(id) FROM user_reminders WHERE chat_id = ?", (chat_id,)).fetchone()[0])


def set_tz(chat_id: int, tz: str | None, lang: str = "en"):
    """New time zone for the whole bot; None = automatic (from the language). The user's reminders keep their
    clock time (9:00 stays 9:00 in the new zone)."""
    zones.set_zone(chat_id, tz, lang)


# ───────────────  texts  ────────────────
_X = i18n_extra.X


# (emoji, kind, hh:mm, weekday-or-mask-or-day, name key, text key)
TEMPLATES = [
    ("💧", "daily", "10:00", None, "tpl_water", "tpl_water_t"),
    ("💊", "daily", "08:00", None, "tpl_med", "tpl_med_t"),
    ("🏋️", "days", "18:00", 0b0010101, "tpl_gym", "tpl_gym_t"),        # Mon · Wed · Fri
    ("📚", "daily", "20:00", None, "tpl_study", "tpl_study_t"),
    ("📈", "days", "08:00", WORK_MASK, "tpl_trade", "tpl_trade_t"),
    ("🧘", "days", "15:00", WORK_MASK, "tpl_break", "tpl_break_t"),
    ("💼", "days", "17:00", 0b0010000, "tpl_review", "tpl_review_t"),   # Friday
    ("💳", "monthly", "10:00", 1, "tpl_bills", "tpl_bills_t"),          # the 1st of every month
]
TIME_CHIPS = ("07:00", "09:00", "12:00", "15:00", "18:00", "21:00")
QUICK_TONIGHT, QUICK_MORNING = "21:00", "09:00"

T = {
    'fa': {
        'menu': "⏰ یادآورها",
        'title': "⏰ <b>یادآورها</b>",
        'about': "هیچ کار مهمی از یادت نره. یادآور یک‌باره، روزانه، روزهای کاری، روزهای دلخواه یا ماهانه رو در چند ثانیه بساز؛ از همون پیام یادآوری عقبش بنداز یا تموم کن، هر وقت خواستی متوقفش کن، یا از قالب‌های آماده شروع کن.",
        'tz': "🌍 منطقه زمانی: <b>{tz}</b>",
        'count': "📋 یادآورهای فعال: <b>{n}</b>",
        'paused_part': "⏸ متوقف: <b>{p}</b>",
        'next_line': "⏭ بعدی: <b>{text}</b> · {when}",
        'next_none': "⏭ بعدی: چیزی برنامه‌ریزی نشده",
        'quick': "⚡ یادآور سریع", 'templates': "✨ قالب‌های آماده",
        'new': "➕ یادآور جدید", 'mine': "📋 یادآورهای من ({n})", 'mine_short': "📋 یادآورهای من", 'tz_btn': "🌍 منطقه زمانی · {tz}",
        'back': "‹ بازگشت", 'cancel': "✖️ انصراف", 'home': "📊 اخبار اقتصادی", 'another': "➕ یکی دیگه",
        'type_q': "📝 <b>نوع یادآور</b>\n\nچند وقت یه بار یادت بندازم؟",
        'once': "📅 یک‌بار", 'daily': "🔁 هر روز", 'weekly': "📆 هر هفته", 'monthly': "🗓 هر ماه",
        'weekdays': "💼 روزهای کاری", 'pick_days': "📆 انتخاب روزها",
        'days_q': "📆 <b>کدوم روزها؟</b>\n\nروزهای دلخواهت رو بزن، بعد «ادامه» رو بزن.",
        'work_days': "دوشنبه تا جمعه", 'days_all_btn': "هر روز", 'days_go': "ادامه ➜",
        'days_none': "حداقل یک روز رو انتخاب کن",
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
        'details_q': "{head}\n\n⏰ یه ساعت رو بزن، یا ساعت و متن رو با هم بفرست:\n\n<code>09:30 تماس با سارا</code>\n\n🌍 ساعت به وقت {tz}",
        'text_q': "{head}\n\nحالا متن یادآور رو بفرست:",
        'head_once': "📅 یک‌بار، <b>{d}</b>", 'head_daily': "🔁 <b>هر روز</b>", 'head_weekly': "📆 <b>هر {d}</b>",
        'head_days': "📆 <b>{d}</b>",
        'bad_details': "❌ فرمت درست نیست. اینطوری بفرست:\n<code>09:30 متن یادآور</code>",
        'past': "⚠️ این زمان گذشته! یه ساعت دیگه بفرست:\n<code>09:30 متن یادآور</code>",
        'saved': "✅ <b>یادآور ذخیره شد</b>\n\n{line}\n\n⏭ دفعه‌ی بعد: <b>{next}</b>",
        'list_title': "📋 <b>یادآورهای من</b>",
        'empty': "هنوز یادآوری نداری. با «➕ یادآور جدید» یکی بساز.",
        'edit_q': "✏️ <b>ویرایش یادآور</b>\n\n{line}\n\nمتن جدید رو بفرست، یا ساعت و متن جدید:\n<code>09:30 متن جدید</code>",
        'edited': "✅ <b>یادآور ویرایش شد</b>\n\n{line}\n\n⏭ دفعه‌ی بعد: <b>{next}</b>",
        'edited_paused': "✅ <b>یادآور ویرایش شد</b>\n\n{line}",
        'deleted': "🗑 حذف شد", 'gone': "این یادآور دیگه وجود نداره",
        'tz_q': "🌍 <b>منطقه زمانی</b>\n\nساعت خبرها، تاریخ و یادآورها همه به وقت همین منطقه‌ست.\n«خودکار» یعنی با زبان عوض میشه (فارسی ← تهران، انگلیسی ← نیویورک).",
        'tz_set': "🌍 {tz}",
        'fire': "⏰ <b>یادآور</b>\n\n{text}",
        'every_day': "هر روز", 'every': "هر {d}", 'at': "ساعت {t}",
        'prev': "◀️ قبلی", 'next_p': "بعدی ▶️",
        'quick_q': "⚡ <b>یادآور سریع</b>\n\nکی یادت بندازم؟",
        'in_min': "⏱ {n} دقیقه", 'in_h': "⏱ {n} ساعت", 'tonight': "🌙 امشب {t}", 'tomorrow_at': "🌅 فردا {t}",
        'item_title': "📌 <b>یادآور</b>", 'st_active': "▶️ فعال", 'st_paused': "⏸ متوقف",
        'edit': "✏️ ویرایش", 'pause': "⏸ توقف", 'resume': "▶️ ادامه", 'dup': "⧉ کپی", 'delete': "🗑 حذف",
        'confirm_q': "🗑 <b>این یادآور حذف بشه؟</b>\n\n{line}", 'yes_delete': "✓ بله، حذف کن",
        'resumed_toast': "▶️ دوباره فعال شد", 'dup_toast': "⧉ کپی شد",
        'resume_past': "این زمان گذشته. اول ویرایشش کن.",
        'done_btn': "✓ انجام شد",
        'fire_done': "✅ <b>انجام شد</b>\n\n<s>{text}</s>",
        'fire_snoozed': "⏱ <b>عقب افتاد</b> تا {when}\n\n{text}",
        'snoozed_toast': "⏱ عقب افتاد",
        'today_t': "امروز {t}", 'tomorrow_t': "فردا {t}", 'rel': "{x} دیگه",
        'u_d': "{n} روز", 'u_h': "{n} ساعت", 'u_min': "{n} دقیقه",
        'tpl_q': "✨ <b>قالب‌های آماده</b>\n\nروی هرکدوم بزنی همون لحظه ساخته می‌شه. بعدش می‌تونی ساعت یا متنش رو عوض کنی.",
        'tpl_water': "نوشیدن آب", 'tpl_water_t': "وقتشه یه لیوان آب بخوری.",
        'tpl_med': "دارو", 'tpl_med_t': "داروت رو بخور.",
        'tpl_gym': "تمرین", 'tpl_gym_t': "وقت تمرینه؛ با تمام انرژی.",
        'tpl_study': "مطالعه", 'tpl_study_t': "وقت مطالعه‌ست؛ ۴۵ دقیقه با تمرکز.",
        'tpl_trade': "چک اخبار قبل از معامله", 'tpl_trade_t': "قبل از معامله اخبار مهم امروز رو چک کن.",
        'tpl_break': "استراحت", 'tpl_break_t': "پاشو، بدنت رو کش بده و به چشم‌هات استراحت بده.",
        'tpl_review': "مرور هفته", 'tpl_review_t': "مرور هفته: چی خوب بود، چی رو بهتر کنم.",
        'tpl_bills': "پرداخت قبوض", 'tpl_bills_t': "وقت پرداخت قبض‌های این ماهه.",
    },
    'en': {
        'menu': "⏰ Reminders",
        'title': "⏰ <b>Reminders</b>",
        'about': _X("Never miss what matters. Set one-time, daily, weekday, custom-day or monthly reminders in seconds, snooze or finish them right from the alert, pause any time, or start from ready-made templates.",
                    "ज़रूरी बातें कभी न छूटें। एक बार, रोज़, कार्यदिवस, चुने हुए दिन या हर महीने के रिमाइंडर सेकंडों में बनाएँ; अलर्ट से ही स्नूज़ करें या पूरा करें, कभी भी रोकें, या तैयार टेम्पलेट से शुरू करें।",
                    "Не пропускайте важное. Создавайте одноразовые, ежедневные, по будням, по выбранным дням и ежемесячные напоминания за секунды, откладывайте или закрывайте их прямо из уведомления, ставьте на паузу и начинайте с готовых шаблонов.",
                    "Jangan lewatkan hal penting. Buat pengingat sekali, harian, hari kerja, hari pilihan, atau bulanan dalam hitungan detik, tunda atau selesaikan langsung dari notifikasinya, jeda kapan saja, atau mulai dari templat siap pakai.",
                    "Não perca o que importa. Crie lembretes únicos, diários, de dias úteis, de dias escolhidos ou mensais em segundos, adie ou conclua direto no aviso, pause quando quiser ou comece por modelos prontos.",
                    "Đừng bỏ lỡ điều quan trọng. Tạo nhắc nhở một lần, hằng ngày, ngày làm việc, ngày tùy chọn hoặc hằng tháng chỉ trong vài giây, hoãn hoặc hoàn tất ngay từ thông báo, tạm dừng bất cứ lúc nào, hoặc bắt đầu từ mẫu có sẵn."),
        'tz': "🌍 Time zone: <b>{tz}</b>",
        'count': "📋 Active reminders: <b>{n}</b>",
        'paused_part': _X("⏸ Paused: <b>{p}</b>", "⏸ रुके हुए: <b>{p}</b>", "⏸ На паузе: <b>{p}</b>", "⏸ Dijeda: <b>{p}</b>", "⏸ Pausados: <b>{p}</b>", "⏸ Tạm dừng: <b>{p}</b>"),
        'next_line': _X("⏭ Next: <b>{text}</b> · {when}", "⏭ अगला: <b>{text}</b> · {when}", "⏭ Далее: <b>{text}</b> · {when}", "⏭ Berikutnya: <b>{text}</b> · {when}", "⏭ Próximo: <b>{text}</b> · {when}", "⏭ Tiếp theo: <b>{text}</b> · {when}"),
        'next_none': _X("⏭ Next: nothing scheduled", "⏭ अगला: कुछ तय नहीं", "⏭ Далее: ничего не запланировано", "⏭ Berikutnya: belum ada jadwal", "⏭ Próximo: nada agendado", "⏭ Tiếp theo: chưa có lịch"),
        'quick': _X("⚡ Quick reminder", "⚡ झटपट रिमाइंडर", "⚡ Быстрое напоминание", "⚡ Pengingat cepat", "⚡ Lembrete rápido", "⚡ Nhắc nhanh"),
        'templates': _X("✨ Templates", "✨ टेम्पलेट", "✨ Шаблоны", "✨ Templat", "✨ Modelos", "✨ Mẫu"),
        'new': "➕ New reminder", 'mine': "📋 My reminders ({n})", 'mine_short': "📋 My reminders", 'tz_btn': "🌍 Time zone · {tz}",
        'back': "‹ Back", 'cancel': "✖️ Cancel", 'home': "📊 Economic News", 'another': "➕ Another one",
        'type_q': "📝 <b>Reminder type</b>\n\nHow often should I remind you?",
        'once': "📅 One-time", 'daily': "🔁 Daily", 'weekly': "📆 Weekly", 'monthly': "🗓 Monthly",
        'weekdays': _X("💼 Weekdays", "💼 कार्यदिवस", "💼 По будням", "💼 Hari kerja", "💼 Dias úteis", "💼 Ngày làm việc"),
        'pick_days': _X("📆 Pick days", "📆 दिन चुनें", "📆 Выбрать дни", "📆 Pilih hari", "📆 Escolher dias", "📆 Chọn ngày"),
        'days_q': _X("📆 <b>Which days?</b>\n\nTap the days you want, then tap Continue.",
                     "📆 <b>कौन-से दिन?</b>\n\nजो दिन चाहिए उन पर टैप करें, फिर आगे बढ़ें दबाएँ।",
                     "📆 <b>В какие дни?</b>\n\nНажмите нужные дни, затем «Далее».",
                     "📆 <b>Hari apa saja?</b>\n\nKetuk hari yang kamu mau, lalu ketuk Lanjut.",
                     "📆 <b>Quais dias?</b>\n\nToque nos dias desejados e depois em Continuar.",
                     "📆 <b>Những ngày nào?</b>\n\nChạm vào các ngày bạn muốn, rồi chạm Tiếp tục."),
        'work_days': _X("Mon–Fri", "सोम–शुक्र", "Пн–Пт", "Sen–Jum", "Seg–Sex", "T2–T6"),
        'days_all_btn': _X("Every day", "रोज़", "Каждый день", "Setiap hari", "Todo dia", "Mỗi ngày"),
        'days_go': _X("Continue ➜", "आगे बढ़ें ➜", "Далее ➜", "Lanjut ➜", "Continuar ➜", "Tiếp tục ➜"),
        'days_none': _X("Pick at least one day", "कम से कम एक दिन चुनें", "Выберите хотя бы один день", "Pilih minimal satu hari", "Escolha pelo menos um dia", "Hãy chọn ít nhất một ngày"),
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
        'details_q': _X("{head}\n\n⏰ Tap a time, or send the time and text together:\n\n<code>09:30 Call Sara</code>\n\n🌍 Time in {tz}",
                        "{head}\n\n⏰ कोई समय चुनें, या समय और टेक्स्ट एक साथ भेजें:\n\n<code>09:30 Call Sara</code>\n\n🌍 समय: {tz}",
                        "{head}\n\n⏰ Выберите время или отправьте время и текст вместе:\n\n<code>09:30 Call Sara</code>\n\n🌍 Время: {tz}",
                        "{head}\n\n⏰ Ketuk jam, atau kirim jam dan teks sekaligus:\n\n<code>09:30 Call Sara</code>\n\n🌍 Waktu {tz}",
                        "{head}\n\n⏰ Toque num horário ou envie horário e texto juntos:\n\n<code>09:30 Call Sara</code>\n\n🌍 Horário de {tz}",
                        "{head}\n\n⏰ Chạm vào một giờ, hoặc gửi cả giờ và nội dung cùng lúc:\n\n<code>09:30 Call Sara</code>\n\n🌍 Giờ {tz}"),
        'text_q': _X("{head}\n\nNow send the reminder text:",
                     "{head}\n\nअब रिमाइंडर का टेक्स्ट भेजें:",
                     "{head}\n\nТеперь отправьте текст напоминания:",
                     "{head}\n\nSekarang kirim teks pengingatnya:",
                     "{head}\n\nAgora envie o texto do lembrete:",
                     "{head}\n\nBây giờ hãy gửi nội dung nhắc nhở:"),
        'head_once': "📅 One-time, <b>{d}</b>", 'head_daily': "🔁 <b>Every day</b>", 'head_weekly': "📆 <b>Every {d}</b>",
        'head_days': "📆 <b>{d}</b>",
        'bad_details': "❌ Wrong format. Send it like this:\n<code>09:30 Reminder text</code>",
        'past': "⚠️ That time has passed! Send another time:\n<code>09:30 Reminder text</code>",
        'saved': "✅ <b>Reminder saved</b>\n\n{line}\n\n⏭ Next: <b>{next}</b>",
        'list_title': "📋 <b>My reminders</b>",
        'empty': "No reminders yet. Tap “➕ New reminder” to create one.",
        'edit_q': _X("✏️ <b>Edit reminder</b>\n\n{line}\n\nSend the new text, or a new time and text:\n<code>09:30 New text</code>",
                     "✏️ <b>रिमाइंडर बदलें</b>\n\n{line}\n\nनया टेक्स्ट भेजें, या नया समय और टेक्स्ट:\n<code>09:30 New text</code>",
                     "✏️ <b>Изменить напоминание</b>\n\n{line}\n\nОтправьте новый текст или новое время и текст:\n<code>09:30 New text</code>",
                     "✏️ <b>Ubah pengingat</b>\n\n{line}\n\nKirim teks baru, atau jam dan teks baru:\n<code>09:30 New text</code>",
                     "✏️ <b>Editar lembrete</b>\n\n{line}\n\nEnvie o novo texto, ou novo horário e texto:\n<code>09:30 New text</code>",
                     "✏️ <b>Sửa nhắc nhở</b>\n\n{line}\n\nGửi nội dung mới, hoặc giờ và nội dung mới:\n<code>09:30 New text</code>"),
        'edited': "✅ <b>Reminder updated</b>\n\n{line}\n\n⏭ Next: <b>{next}</b>",
        'edited_paused': _X("✅ <b>Reminder updated</b>\n\n{line}", "✅ <b>रिमाइंडर अपडेट हुआ</b>\n\n{line}", "✅ <b>Напоминание обновлено</b>\n\n{line}", "✅ <b>Pengingat diperbarui</b>\n\n{line}", "✅ <b>Lembrete atualizado</b>\n\n{line}", "✅ <b>Đã cập nhật nhắc nhở</b>\n\n{line}"),
        'deleted': "🗑 Deleted", 'gone': "This reminder no longer exists",
        'tz_q': "🌍 <b>Time zone</b>\n\nNews times, dates and reminders all use this zone.\n“Automatic” follows the language (Persian → Tehran, English → New York).",
        'tz_set': "🌍 {tz}",
        'fire': "⏰ <b>Reminder</b>\n\n{text}",
        'every_day': "every day", 'every': "every {d}", 'at': "at {t}",
        'prev': "◀️ Prev", 'next_p': "Next ▶️",
        'quick_q': _X("⚡ <b>Quick reminder</b>\n\nWhen should I remind you?",
                      "⚡ <b>झटपट रिमाइंडर</b>\n\nमैं आपको कब याद दिलाऊँ?",
                      "⚡ <b>Быстрое напоминание</b>\n\nКогда напомнить?",
                      "⚡ <b>Pengingat cepat</b>\n\nKapan aku harus mengingatkanmu?",
                      "⚡ <b>Lembrete rápido</b>\n\nQuando devo te lembrar?",
                      "⚡ <b>Nhắc nhanh</b>\n\nBạn muốn tôi nhắc lúc nào?"),
        'in_min': _X("⏱ {n} min", "⏱ {n} मिनट", "⏱ {n} мин", "⏱ {n} mnt", "⏱ {n} min", "⏱ {n} phút"),
        'in_h': _X("⏱ {n} h", "⏱ {n} घंटा", "⏱ {n} ч", "⏱ {n} jam", "⏱ {n} h", "⏱ {n} giờ"),
        'tonight': _X("🌙 Tonight {t}", "🌙 आज रात {t}", "🌙 Сегодня вечером {t}", "🌙 Malam ini {t}", "🌙 Hoje à noite {t}", "🌙 Tối nay {t}"),
        'tomorrow_at': _X("🌅 Tomorrow {t}", "🌅 कल {t}", "🌅 Завтра {t}", "🌅 Besok {t}", "🌅 Amanhã {t}", "🌅 Ngày mai {t}"),
        'item_title': _X("📌 <b>Reminder</b>", "📌 <b>रिमाइंडर</b>", "📌 <b>Напоминание</b>", "📌 <b>Pengingat</b>", "📌 <b>Lembrete</b>", "📌 <b>Nhắc nhở</b>"),
        'st_active': _X("▶️ Active", "▶️ सक्रिय", "▶️ Активно", "▶️ Aktif", "▶️ Ativo", "▶️ Đang bật"),
        'st_paused': _X("⏸ Paused", "⏸ रुका हुआ", "⏸ На паузе", "⏸ Dijeda", "⏸ Pausado", "⏸ Tạm dừng"),
        'edit': _X("✏️ Edit", "✏️ बदलें", "✏️ Изменить", "✏️ Ubah", "✏️ Editar", "✏️ Sửa"),
        'pause': _X("⏸ Pause", "⏸ रोकें", "⏸ Пауза", "⏸ Jeda", "⏸ Pausar", "⏸ Tạm dừng"),
        'resume': _X("▶️ Resume", "▶️ फिर शुरू", "▶️ Возобновить", "▶️ Lanjutkan", "▶️ Retomar", "▶️ Tiếp tục"),
        'dup': _X("⧉ Duplicate", "⧉ कॉपी बनाएँ", "⧉ Дублировать", "⧉ Duplikat", "⧉ Duplicar", "⧉ Nhân đôi"),
        'delete': _X("🗑 Delete", "🗑 हटाएँ", "🗑 Удалить", "🗑 Hapus", "🗑 Excluir", "🗑 Xóa"),
        'confirm_q': _X("🗑 <b>Delete this reminder?</b>\n\n{line}",
                        "🗑 <b>यह रिमाइंडर हटाएँ?</b>\n\n{line}",
                        "🗑 <b>Удалить это напоминание?</b>\n\n{line}",
                        "🗑 <b>Hapus pengingat ini?</b>\n\n{line}",
                        "🗑 <b>Excluir este lembrete?</b>\n\n{line}",
                        "🗑 <b>Xóa nhắc nhở này?</b>\n\n{line}"),
        'yes_delete': _X("✓ Yes, delete", "✓ हाँ, हटाएँ", "✓ Да, удалить", "✓ Ya, hapus", "✓ Sim, excluir", "✓ Có, xóa"),
        'resumed_toast': _X("▶️ Resumed", "▶️ फिर शुरू हुआ", "▶️ Возобновлено", "▶️ Dilanjutkan", "▶️ Retomado", "▶️ Đã tiếp tục"),
        'dup_toast': _X("⧉ Duplicated", "⧉ कॉपी बन गई", "⧉ Дубликат создан", "⧉ Diduplikat", "⧉ Duplicado", "⧉ Đã nhân đôi"),
        'resume_past': _X("That time has passed. Edit it first.", "वह समय बीत चुका है। पहले उसे बदलें।", "Это время уже прошло. Сначала измените его.", "Waktunya sudah lewat. Ubah dulu.", "Esse horário já passou. Edite primeiro.", "Thời gian đó đã qua. Hãy sửa trước."),
        'done_btn': _X("✓ Done", "✓ हो गया", "✓ Готово", "✓ Selesai", "✓ Feito", "✓ Xong"),
        'fire_done': _X("✅ <b>Done</b>\n\n<s>{text}</s>", "✅ <b>हो गया</b>\n\n<s>{text}</s>", "✅ <b>Готово</b>\n\n<s>{text}</s>", "✅ <b>Selesai</b>\n\n<s>{text}</s>", "✅ <b>Feito</b>\n\n<s>{text}</s>", "✅ <b>Xong</b>\n\n<s>{text}</s>"),
        'fire_snoozed': _X("⏱ <b>Snoozed</b> until {when}\n\n{text}", "⏱ <b>टाला गया</b> — {when} तक\n\n{text}", "⏱ <b>Отложено</b> до {when}\n\n{text}", "⏱ <b>Ditunda</b> sampai {when}\n\n{text}", "⏱ <b>Adiado</b> para {when}\n\n{text}", "⏱ <b>Đã hoãn</b> đến {when}\n\n{text}"),
        'snoozed_toast': _X("⏱ Snoozed", "⏱ टाल दिया", "⏱ Отложено", "⏱ Ditunda", "⏱ Adiado", "⏱ Đã hoãn"),
        'today_t': _X("Today {t}", "आज {t}", "Сегодня {t}", "Hari ini {t}", "Hoje {t}", "Hôm nay {t}"),
        'tomorrow_t': _X("Tomorrow {t}", "कल {t}", "Завтра {t}", "Besok {t}", "Amanhã {t}", "Ngày mai {t}"),
        'rel': _X("in {x}", "{x} में", "через {x}", "dalam {x}", "em {x}", "sau {x}"),
        'u_d': _X("{n} d", "{n} दिन", "{n} д", "{n} hr", "{n} d", "{n} ngày"),
        'u_h': _X("{n} h", "{n} घंटे", "{n} ч", "{n} jam", "{n} h", "{n} giờ"),
        'u_min': _X("{n} min", "{n} मिनट", "{n} мин", "{n} mnt", "{n} min", "{n} phút"),
        'tpl_q': _X("✨ <b>Templates</b>\n\nTap one to create it instantly. You can edit its time or text afterwards.",
                    "✨ <b>टेम्पलेट</b>\n\nबनाने के लिए किसी एक पर टैप करें। बाद में उसका समय या टेक्स्ट बदल सकते हैं।",
                    "✨ <b>Шаблоны</b>\n\nНажмите на шаблон — напоминание создастся сразу. Время и текст можно изменить потом.",
                    "✨ <b>Templat</b>\n\nKetuk salah satu untuk langsung membuatnya. Jam atau teksnya bisa diubah setelahnya.",
                    "✨ <b>Modelos</b>\n\nToque em um para criar na hora. Depois você pode editar o horário ou o texto.",
                    "✨ <b>Mẫu</b>\n\nChạm vào một mẫu để tạo ngay. Sau đó bạn có thể sửa giờ hoặc nội dung."),
        'tpl_water': _X("Drink water", "पानी पिएँ", "Пить воду", "Minum air", "Beber água", "Uống nước"),
        'tpl_water_t': _X("Time to drink a glass of water.", "एक गिलास पानी पीने का समय।", "Время выпить стакан воды.", "Saatnya minum segelas air.", "Hora de beber um copo d'água.", "Đến giờ uống một ly nước."),
        'tpl_med': _X("Medicine", "दवा", "Лекарство", "Obat", "Remédio", "Thuốc"),
        'tpl_med_t': _X("Take your medicine.", "अपनी दवा लें।", "Примите лекарство.", "Minum obatmu.", "Tome seu remédio.", "Uống thuốc của bạn."),
        'tpl_gym': _X("Workout", "वर्कआउट", "Тренировка", "Olahraga", "Treino", "Tập luyện"),
        'tpl_gym_t': _X("Workout time: show up and give it your best.", "वर्कआउट का समय: पूरे मन से करें।", "Время тренировки: выложитесь по максимуму.", "Waktunya olahraga: lakukan yang terbaik.", "Hora do treino: dê o seu melhor.", "Đến giờ tập: hãy cố gắng hết mình."),
        'tpl_study': _X("Study", "पढ़ाई", "Учёба", "Belajar", "Estudo", "Học tập"),
        'tpl_study_t': _X("Study session: focus for 45 minutes.", "पढ़ाई का समय: 45 मिनट ध्यान लगाएँ।", "Время учёбы: сосредоточьтесь на 45 минут.", "Sesi belajar: fokus selama 45 menit.", "Hora de estudar: foque por 45 minutos.", "Giờ học: tập trung trong 45 phút."),
        'tpl_trade': _X("Pre-trade news check", "ट्रेड से पहले ख़बरें", "Новости перед торговлей", "Cek berita sebelum trading", "Notícias antes de operar", "Xem tin trước khi giao dịch"),
        'tpl_trade_t': _X("Check today's high-impact news before you trade.", "ट्रेड करने से पहले आज की अहम ख़बरें देखें।", "Проверьте важные новости дня перед торговлей.", "Cek berita berdampak tinggi hari ini sebelum trading.", "Veja as notícias de alto impacto de hoje antes de operar.", "Xem các tin tác động mạnh hôm nay trước khi giao dịch."),
        'tpl_break': _X("Stretch break", "स्ट्रेच ब्रेक", "Перерыв", "Istirahat", "Pausa", "Nghỉ giải lao"),
        'tpl_break_t': _X("Stand up, stretch and rest your eyes.", "उठें, स्ट्रेच करें और आँखों को आराम दें।", "Встаньте, потянитесь и дайте глазам отдохнуть.", "Berdiri, regangkan badan, dan istirahatkan mata.", "Levante, alongue-se e descanse os olhos.", "Đứng dậy, vươn vai và cho mắt nghỉ ngơi."),
        'tpl_review': _X("Weekly review", "साप्ताहिक समीक्षा", "Итоги недели", "Tinjauan mingguan", "Revisão semanal", "Tổng kết tuần"),
        'tpl_review_t': _X("Weekly review: what worked, what to improve.", "साप्ताहिक समीक्षा: क्या अच्छा रहा, क्या सुधारें।", "Итоги недели: что сработало, что улучшить.", "Tinjauan mingguan: apa yang berhasil, apa yang perlu diperbaiki.", "Revisão semanal: o que funcionou e o que melhorar.", "Tổng kết tuần: điều gì hiệu quả, điều gì cần cải thiện."),
        'tpl_bills': _X("Pay bills", "बिल भरना", "Оплата счетов", "Bayar tagihan", "Pagar contas", "Thanh toán hóa đơn"),
        'tpl_bills_t': _X("Time to pay this month's bills.", "इस महीने के बिल भरने का समय।", "Пора оплатить счета за этот месяц.", "Saatnya membayar tagihan bulan ini.", "Hora de pagar as contas do mês.", "Đến lúc thanh toán hóa đơn tháng này."),
    },
}


i18n.fill(T)


def _t(lang: str) -> dict:
    return T.get(lang, T['en'])


def _num(lang: str):
    return fa_num if lang == 'fa' else str


def menu_label(lang: str, chat_id: int | None = None) -> str:
    n = counts(chat_id)[0] if chat_id is not None else 0
    return f"{_t(lang)['menu']} · {fa_num(n) if lang == 'fa' else n}" if n else _t(lang)['menu']


def _wd(lang: str, i: int) -> str:
    """Short weekday name for buttons and lists."""
    name = WEEKDAYS[lang][i]
    return name if lang in ("fa", "hi", "vi") else name[:3]


def _mask_days(mask: int) -> list[int]:
    return [i for i in range(7) if (int(mask or 0) >> i) & 1]


def days_label(mask: int, lang: str) -> str:
    t, mask = _t(lang), int(mask or 0) & ALL_MASK
    if mask == ALL_MASK:
        return t['every_day']
    if mask == WORK_MASK:
        return t['work_days']
    order = [i for i in WEEK_ORDER[lang] if (mask >> i) & 1]
    return ("، " if lang == 'fa' else ", ").join(_wd(lang, i) for i in order)


def _when(r: dict, lang: str) -> str:
    t = _t(lang)
    if r["kind"] == "once":
        return f"📅 {fmt_date(date.fromisoformat(r['on_date']), lang)}، {t['at'].format(t=r['hhmm'])}" if lang == 'fa' \
            else f"📅 {fmt_date(date.fromisoformat(r['on_date']), lang)}, {t['at'].format(t=r['hhmm'])}"
    if r["kind"] == "daily":
        return f"🔁 {t['every_day']} {t['at'].format(t=r['hhmm'])}"
    if r["kind"] == "monthly":
        return f"🗓 {_monthly_label(r['weekday'], r['on_date'], lang, 'every_month')} {t['at'].format(t=r['hhmm'])}"
    if r["kind"] == "days":
        mask = int(r["weekday"] or 0) & ALL_MASK
        if mask == ALL_MASK:
            return f"🔁 {t['every_day']} {t['at'].format(t=r['hhmm'])}"
        days = _mask_days(mask)
        if len(days) == 1:
            return f"📆 {t['every'].format(d=WEEKDAYS[lang][days[0]])} {t['at'].format(t=r['hhmm'])}"
        return f"📆 {days_label(mask, lang)} {t['at'].format(t=r['hhmm'])}"
    return f"📆 {t['every'].format(d=WEEKDAYS[lang][r['weekday']])} {t['at'].format(t=r['hhmm'])}"


def _monthly_label(day: int, cal: str, lang: str, key: str) -> str:
    t = _t(lang)
    if lang != 'fa' and cal == 'jalali':
        # Keep existing schedules intact; do not misrepresent them as Gregorian.
        return (i18n.t(lang, "🗓 <b>Monthly (existing schedule)</b>") if key == "head_monthly"
                else i18n.t(lang, "monthly (existing schedule)"))
    return t[key].format(d=fa_num(day) if lang == 'fa' else day, cal=t['cal_' + (cal or 'gregorian')])


def _line(r: dict, lang: str) -> str:
    return f"{_when(r, lang)}\n📝 {html.escape(r['text'])}"


def _snip(text: str, n: int = 26) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _next_str(ts: int, tz: str, lang: str) -> str:
    local = datetime.fromtimestamp(ts, pytz.timezone(tz))
    return f"{fmt_date(local.date(), lang)} {local.strftime('%H:%M')}"


def _rel(seconds: float, lang: str) -> str:
    """'in 2 h 10 min' / '۲ ساعت و ۱۰ دقیقه دیگه'."""
    t, num = _t(lang), _num(lang)
    mins = max(1, int((seconds + 59) // 60))
    d, rest = divmod(mins, 1440)
    h, m = divmod(rest, 60)
    if d:
        parts = [t['u_d'].format(n=num(d))] + ([t['u_h'].format(n=num(h))] if h else [])
    elif h:
        parts = [t['u_h'].format(n=num(h))] + ([t['u_min'].format(n=num(m))] if m else [])
    else:
        parts = [t['u_min'].format(n=num(m))]
    return t['rel'].format(x=(" و " if lang == 'fa' else " ").join(parts))


def _next_human(ts: int, tz: str, lang: str) -> str:
    """'Today 18:30' / 'Tomorrow 09:00' / 'Sat 10 Oct 09:00'."""
    t, zone = _t(lang), pytz.timezone(tz)
    local, now = datetime.fromtimestamp(ts, zone), datetime.now(zone)
    diff = (local.date() - now.date()).days
    hm = local.strftime('%H:%M')
    if diff == 0:
        return t['today_t'].format(t=hm)
    if diff == 1:
        return t['tomorrow_t'].format(t=hm)
    return f"{short_date(local.date(), lang)} {hm}"


def _ceil_minute(dt_: datetime) -> datetime:
    """Round up to the next full minute (reminders have minute precision, never fire early)."""
    return dt_.replace(second=0, microsecond=0) + (timedelta(minutes=1) if dt_.second or dt_.microsecond else timedelta())


def _quick_target(code: str, tz: str) -> tuple[str, str] | None:
    """(on_date ISO, hh:mm) for a quick-reminder code: minutes, 'tonight' or 'tom'."""
    now = datetime.now(pytz.timezone(tz))
    if code.isdigit():
        when = _ceil_minute(now + timedelta(minutes=int(code)))
    elif code == "tonight":
        when = now.replace(hour=int(QUICK_TONIGHT[:2]), minute=0, second=0, microsecond=0)
    elif code in ("tom", "tomorrow"):
        when = (now + timedelta(days=1)).replace(hour=int(QUICK_MORNING[:2]), minute=0, second=0, microsecond=0)
    else:
        return None
    return when.date().isoformat(), when.strftime("%H:%M")


# ───────────────  screens  ────────────────
def home_view(chat_id: int, lang: str) -> tuple[str, KB]:
    t, tz = _t(lang), get_tz(chat_id, lang)
    num = _num(lang)
    active, paused = counts(chat_id)
    nxt = upcoming(chat_id)
    if nxt:
        when = f"{_next_human(nxt['next_run'], nxt['tz'], lang)} · {_rel(nxt['next_run'] - _time.time(), lang)}"
        next_line = t['next_line'].format(text=html.escape(_snip(nxt['text'])), when=when)
    else:
        next_line = t['next_none']
    count_line = t['count'].format(n=num(active)) + (f" · {t['paused_part'].format(p=num(paused))}" if paused else "")
    text = "\n".join([t['title'], "",
                      f"<blockquote>{next_line}\n{count_line}\n{t['tz'].format(tz=tz_label(tz, lang))}</blockquote>",
                      "", f"<i>{html.escape(t['about'], quote=False)}</i>"])
    total = active + paused
    kb = KB([
        [B(t['quick'], callback_data="rem_quick"), B(t['new'], callback_data="rem_new")],
        [B(t['templates'], callback_data="rem_tpl"), B(t['mine'].format(n=num(total)), callback_data="rem_list_0")],
        [B(t['tz_btn'].format(tz=tz_label(tz, lang)), callback_data="rem_tz")],
        [B(t['back'], callback_data="main_menu")],
    ])
    return text, kb


def quick_view(chat_id: int, lang: str) -> tuple[str, KB]:
    t, num = _t(lang), _num(lang)
    now = datetime.now(pytz.timezone(get_tz(chat_id, lang)))
    chips = [B(t['in_min'].format(n=num(10)), callback_data="rem_q_10"),
             B(t['in_min'].format(n=num(30)), callback_data="rem_q_30"),
             B(t['in_h'].format(n=num(1)), callback_data="rem_q_60"),
             B(t['in_h'].format(n=num(2)), callback_data="rem_q_120")]
    last = []
    if (now.hour, now.minute) < (int(QUICK_TONIGHT[:2]) - 1, 30):
        last.append(B(t['tonight'].format(t=QUICK_TONIGHT), callback_data="rem_q_tonight"))
    last.append(B(t['tomorrow_at'].format(t=QUICK_MORNING), callback_data="rem_q_tom"))
    return t['quick_q'], KB([chips[:2], chips[2:], last, [B(t['back'], callback_data="rem")]])


def type_view(lang: str) -> tuple[str, KB]:
    t = _t(lang)
    return t['type_q'], KB([
        [B(t['once'], callback_data="rem_k_once"), B(t['daily'], callback_data="rem_k_daily")],
        [B(t['weekdays'], callback_data="rem_k_weekdays"), B(t['pick_days'], callback_data="rem_k_days")],
        [B(t['monthly'], callback_data="rem_k_monthly")],
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
    """Old single-day picker (kept for buttons in old messages)."""
    t = _t(lang)
    btns = [B(WEEKDAYS[lang][i], callback_data=f"rem_w_{i}") for i in WEEK_ORDER[lang]]
    rows = [btns[0:3], btns[3:6], btns[6:7], [B(t['back'], callback_data="rem_new")]]
    return t['day_q'], KB(rows)


def days_view(lang: str, mask: int) -> tuple[str, KB]:
    """Pick any days of the week: tap to select, presets, Continue."""
    t = _t(lang)
    btns = [B(("✅ " if (mask >> i) & 1 else "") + _wd(lang, i), callback_data=f"rem_dy_{i}") for i in WEEK_ORDER[lang]]
    rows = [btns[0:4], btns[4:7],
            [B(("✅ " if mask == WORK_MASK else "") + t['work_days'], callback_data=f"rem_dp_{WORK_MASK}"),
             B(("✅ " if mask == ALL_MASK else "") + t['days_all_btn'], callback_data=f"rem_dp_{ALL_MASK}")],
            [B(t['days_go'], callback_data="rem_dgo")],
            [B(t['back'], callback_data="rem_new")]]
    return t['days_q'], KB(rows)


def monthday_view(lang: str, cal: str) -> tuple[str, KB]:
    t = _t(lang)
    if lang != 'fa':
        cal = 'gregorian'
    rows = ([[B(("✅ " if c == cal else "") + t['cal_' + c], callback_data=f"rem_mc_{c}") for c in ("jalali", "gregorian")]]
            if lang == 'fa' else [])
    days = [B(fa_num(n) if lang == 'fa' else str(n), callback_data=f"rem_md_{cal}_{n}") for n in range(1, 32)]
    rows += [days[i:i + 7] for i in range(0, 31, 7)]
    rows.append([B(t['back'], callback_data="rem_new")])
    return t['mday_q'], KB(rows)


def _draft_head(draft: dict, lang: str) -> str:
    t = _t(lang)
    if draft["kind"] == "once":
        return t['head_once'].format(d=fmt_date(date.fromisoformat(draft["on_date"]), lang))
    if draft["kind"] == "weekly":
        return t['head_weekly'].format(d=WEEKDAYS[lang][draft["weekday"]])
    if draft["kind"] == "monthly":
        return _monthly_label(draft["weekday"], draft["on_date"], lang, 'head_monthly')
    if draft["kind"] == "days":
        mask = int(draft.get("weekday") or 0) & ALL_MASK
        if mask == ALL_MASK:
            return t['head_daily']
        days = _mask_days(mask)
        if len(days) == 1:
            return t['head_weekly'].format(d=WEEKDAYS[lang][days[0]])
        return t['head_days'].format(d=days_label(mask, lang))
    return t['head_daily']


def details_prompt(chat_id: int, lang: str, draft: dict) -> tuple[str, KB]:
    """Step before the text: tap a time, or send 'HH:MM text' in one message."""
    t = _t(lang)
    chips = [B(c, callback_data=f"rem_t_{c.replace(':', '')}") for c in TIME_CHIPS]
    return (t['details_q'].format(head=_draft_head(draft, lang), tz=tz_label(get_tz(chat_id, lang), lang)),
            KB([chips[0:3], chips[3:6], [B(t['cancel'], callback_data="rem")]]))


def text_prompt(chat_id: int, lang: str, draft: dict) -> tuple[str, KB]:
    """The time is chosen: now only the text is missing."""
    t = _t(lang)
    head = f"{_draft_head(draft, lang)}\n⏰ <b>{draft['hhmm']}</b> · 🌍 {tz_label(get_tz(chat_id, lang), lang)}"
    return t['text_q'].format(head=head), KB([[B(t['cancel'], callback_data="rem")]])


def list_view(chat_id: int, lang: str, page: int = 0) -> tuple[str, KB]:
    t, items, num = _t(lang), list_reminders(chat_id), _num(lang)
    if not items:
        return f"{t['list_title']}\n\n{t['empty']}", KB([[B(t['new'], callback_data="rem_new"), B(t['templates'], callback_data="rem_tpl")],
                                                         [B(t['back'], callback_data="rem")]])
    pages = (len(items) - 1) // PAGE + 1
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE:(page + 1) * PAGE]
    lines, rows = [t['list_title'], ""], []
    for i, r in enumerate(chunk, start=page * PAGE + 1):
        n = num(i)
        paused = r["next_run"] is None
        lines += [f"<b>{n}.</b> {'⏸ ' if paused else ''}{_line(r, lang)}", ""]
        rows.append([B(f"{'⏸ ' if paused else ''}{n} · {_snip(r['text'], 30)}", callback_data=f"rem_o_{r['id']}_{page}")])
    nav = []
    if page > 0:
        nav.append(B(t['prev'], callback_data=f"rem_list_{page - 1}"))
    if page < pages - 1:
        nav.append(B(t['next_p'], callback_data=f"rem_list_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B(t['new'], callback_data="rem_new"), B(t['back'], callback_data="rem")])
    return "\n".join(lines).strip(), KB(rows)


def item_view(chat_id: int, lang: str, rid: int, page: int = 0) -> tuple[str, KB]:
    t = _t(lang)
    r = get_reminder(chat_id, rid)
    if not r:
        return list_view(chat_id, lang, page)
    paused = r["next_run"] is None
    status = t['st_paused'] if paused else f"{t['st_active']} · {t['next_line'].split(':', 1)[0].strip()}: {_next_human(r['next_run'], r['tz'], lang)} · {_rel(r['next_run'] - _time.time(), lang)}"
    text = f"{t['item_title']}\n\n<blockquote>{_line(r, lang)}\n{status}</blockquote>"
    kb = KB([
        [B(t['edit'], callback_data=f"rem_e_{rid}"),
         B(t['resume'] if paused else t['pause'], callback_data=f"rem_p_{rid}_{page}")],
        [B(t['dup'], callback_data=f"rem_c_{rid}_{page}"), B(t['delete'], callback_data=f"rem_x_{rid}_{page}")],
        [B(t['back'], callback_data=f"rem_list_{page}")],
    ])
    return text, kb


def tpl_view(lang: str) -> tuple[str, KB]:
    t = _t(lang)
    btns = [B(f"{emoji} {t[name]}", callback_data=f"rem_tp_{i}") for i, (emoji, _k, _h, _w, name, _tx) in enumerate(TEMPLATES)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)] + [[B(t['back'], callback_data="rem")]]
    return t['tpl_q'], KB(rows)


def tz_view(chat_id: int, lang: str) -> tuple[str, KB]:
    t, cur = _t(lang), get_tz(chat_id, lang)
    auto_on = zones.is_auto(chat_id)
    auto_lbl = ("✅ " if auto_on else "") + ("🔄 خودکار · " if lang == 'fa' else i18n.t(lang, "🔄 Automatic · ")) \
        + zones.city(zones.auto_name(lang), lang)
    btns = [B(("✅ " if (name == cur and not auto_on) else "") + zones.label(name, lang), callback_data=f"rem_z_{i}")
            for i, (name, fa, en) in enumerate(TIMEZONES)]
    rows = [[B(auto_lbl, callback_data="rem_z_auto")]] + [btns[i:i + 2] for i in range(0, len(btns), 2)] \
        + [[B(t['back'], callback_data="rem")]]
    return t['tz_q'], KB(rows)


def after_save_kb(lang: str) -> KB:
    t = _t(lang)
    return KB([[B(t['mine_short'], callback_data="rem_list_0"),
                B(t['another'], callback_data="rem_new")],
               [B(t['templates'], callback_data="rem_tpl"), B(t['menu'], callback_data="rem")],
               [B(t['home'], callback_data="main_menu")]])


def fire_kb(lang: str) -> KB:
    t, num = _t(lang), _num(lang)
    return KB([[B(t['done_btn'], callback_data="rem_f_ok")],
               [B(t['in_min'].format(n=num(10)), callback_data="rem_f_s10"),
                B(t['in_h'].format(n=num(1)), callback_data="rem_f_s60"),
                B(t['tomorrow_at'].format(t=QUICK_MORNING), callback_data="rem_f_stom")]])


async def _edit(msg, text: str, kb: KB):
    try:
        await edit_or_resend(msg, text, kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def _save_new(chat_id: int, lang: str, draft: dict, hhmm: str, body: str) -> tuple[str, KB] | None:
    """Stores the reminder; the 'saved' card, or None when that time has already passed."""
    t, tz = _t(lang), get_tz(chat_id, lang)
    nr = add_reminder(chat_id, body, draft["kind"], hhmm, tz, draft.get("on_date"), draft.get("weekday"))
    if nr is None:
        return None
    r = {"kind": draft["kind"], "hhmm": hhmm, "on_date": draft.get("on_date"), "weekday": draft.get("weekday"), "text": body}
    return t['saved'].format(line=_line(r, lang), next=_next_str(nr, tz, lang)), after_save_kb(lang)


# ───────────────  handlers (wired from telegram_bot.py)  ────────────────
async def cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE, lang: str):
    """⏰ Reminders keyboard button: opens the reminders home as a new message."""
    ctx.user_data.pop("awaiting", None)
    text, kb = home_view(update.effective_chat.id, lang)
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _fired_action(q, ctx, lang: str, code: str):
    """Buttons under a delivered reminder: ✓ Done · snooze 10 min / 1 h / tomorrow."""
    t, cid = _t(lang), q.message.chat.id
    raw = q.message.text or ""
    body = (raw.split("\n\n", 1)[1] if "\n\n" in raw else raw).strip()[:MAX_TEXT]
    try:
        if code == "ok" or not body:
            await q.answer(t['done_btn'])
            await q.message.edit_text(t['fire_done'].format(text=html.escape(body)), parse_mode=ParseMode.HTML)
            return
        tz = get_tz(cid, lang)
        target = _quick_target({"s10": "10", "s60": "60", "stom": "tom"}.get(code, "10"), tz)
        nr = add_reminder(cid, body, "once", target[1], tz, target[0]) if target else None
        if nr is None:
            await q.answer(t['past'].split("\n")[0], show_alert=True)
            return
        await q.answer(t['snoozed_toast'])
        await q.message.edit_text(t['fire_snoozed'].format(when=_next_human(nr, tz, lang), text=html.escape(body)),
                                  parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"fired-reminder action failed: {e}")


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, lang: str):
    """Every button whose data is 'rem' or starts with 'rem_'."""
    q = update.callback_query
    cid, data, t = q.message.chat.id, q.data, _t(lang)
    if data.startswith("rem_f_"):
        await _fired_action(q, ctx, lang, data[6:])
        return
    if str(ctx.user_data.get("awaiting") or "").startswith("rem_"):
        ctx.user_data.pop("awaiting", None)   # any button cancels a pending text input
    toast, view = None, None

    def start_details(draft: dict):
        ctx.user_data["rem_draft"] = draft
        ctx.user_data["awaiting"] = AWAIT_DETAILS
        return details_prompt(cid, lang, draft)

    if data == "rem":
        view = home_view(cid, lang)
    elif data == "rem_new":
        ctx.user_data.pop("rem_draft", None)
        view = type_view(lang)
    elif data == "rem_quick":
        view = quick_view(cid, lang)
    elif data.startswith("rem_q_"):
        target = _quick_target(data[6:], get_tz(cid, lang))
        if not target:
            view = quick_view(cid, lang)
        else:
            draft = {"kind": "once", "on_date": target[0], "hhmm": target[1]}
            ctx.user_data["rem_draft"] = draft
            ctx.user_data["awaiting"] = AWAIT_TEXT
            when = _next_human(int(pytz.timezone(get_tz(cid, lang)).localize(
                datetime.combine(date.fromisoformat(target[0]), time(*map(int, target[1].split(":"))))).timestamp()),
                get_tz(cid, lang), lang)
            view = (t['text_q'].format(head=f"⚡ <b>{html.escape(when)}</b>"), KB([[B(t['cancel'], callback_data="rem")]]))
    elif data == "rem_k_once":
        view = date_view(cid, lang)
    elif data in ("rem_k_weekly", "rem_k_days"):
        mask = int((ctx.user_data.get("rem_draft") or {}).get("weekday") or 0) if (ctx.user_data.get("rem_draft") or {}).get("kind") == "days" else 0
        ctx.user_data["rem_draft"] = {"kind": "days", "weekday": mask}
        view = days_view(lang, mask)
    elif data == "rem_k_weekdays":
        view = start_details({"kind": "days", "weekday": WORK_MASK})
    elif data.startswith("rem_dy_"):
        draft = ctx.user_data.get("rem_draft") or {"kind": "days", "weekday": 0}
        draft = {"kind": "days", "weekday": int(draft.get("weekday") or 0) ^ (1 << int(data[7:]))}
        ctx.user_data["rem_draft"] = draft
        view = days_view(lang, draft["weekday"])
    elif data.startswith("rem_dp_"):
        draft = {"kind": "days", "weekday": int(data[7:]) & ALL_MASK}
        ctx.user_data["rem_draft"] = draft
        view = days_view(lang, draft["weekday"])
    elif data == "rem_dgo":
        draft = ctx.user_data.get("rem_draft") or {}
        if draft.get("kind") != "days" or not int(draft.get("weekday") or 0):
            await q.answer(t['days_none'], show_alert=True)
            return
        view = start_details(draft)
    elif data.startswith("rem_t_"):
        draft = ctx.user_data.get("rem_draft")
        hhmm = f"{data[6:8]}:{data[8:10]}"
        if not draft or not re.fullmatch(r"\d\d:\d\d", hhmm):
            view = home_view(cid, lang)
        else:
            draft = {**draft, "hhmm": hhmm}
            ctx.user_data["rem_draft"] = draft
            ctx.user_data["awaiting"] = AWAIT_TEXT
            view = text_prompt(cid, lang, draft)
    elif data == "rem_k_monthly" or data.startswith("rem_mc_"):
        cal = data[7:] if data.startswith("rem_mc_") else ("jalali" if lang == 'fa' else "gregorian")
        view = monthday_view(lang, cal if cal in ("jalali", "gregorian") else "gregorian")
    elif data.startswith("rem_md_"):
        _, _, cal, n = data.split("_")
        if lang != 'fa' and cal != 'gregorian':
            await q.answer(i18n.t(lang, "Please select a Gregorian day."))
            await _edit(q.message, *monthday_view(lang, 'gregorian'))
            return
        # monthly reuses the columns: on_date = calendar, weekday = day of month
        view = start_details({"kind": "monthly", "on_date": cal, "weekday": int(n)})
    elif data == "rem_k_daily":
        view = start_details({"kind": "daily"})
    elif data.startswith("rem_d_"):
        d = datetime.strptime(data[6:], "%Y%m%d").date()
        view = start_details({"kind": "once", "on_date": d.isoformat()})
    elif data == "rem_dtype":
        ctx.user_data["awaiting"] = AWAIT_DATE
        view = (t['type_date'], KB([[B(t['cancel'], callback_data="rem")]]))
    elif data.startswith("rem_w_"):
        view = start_details({"kind": "weekly", "weekday": int(data[6:])})
    elif data == "rem_tpl":
        view = tpl_view(lang)
    elif data.startswith("rem_tp_"):
        i = int(data[7:])
        if not 0 <= i < len(TEMPLATES):
            view = tpl_view(lang)
        else:
            emoji, kind, hhmm, wd, name_key, text_key = TEMPLATES[i]
            draft = {"kind": kind, "weekday": wd}
            if kind == "monthly":
                draft["on_date"] = "jalali" if lang == 'fa' else "gregorian"
            saved = _save_new(cid, lang, draft, hhmm, f"{emoji} {t[text_key]}")
            toast = f"{emoji} {t[name_key]}"
            view = saved or tpl_view(lang)
    elif data.startswith("rem_list_"):
        view = list_view(cid, lang, int(data[9:] or 0))
    elif data.startswith("rem_o_"):
        _, _, rid, page = data.split("_")
        view = item_view(cid, lang, int(rid), int(page))
    elif data.startswith("rem_p_"):
        _, _, rid, page = data.split("_")
        r = get_reminder(cid, int(rid))
        if not r:
            toast, view = t['gone'], list_view(cid, lang)
        else:
            was_paused = r["next_run"] is None
            if set_paused(cid, int(rid), not was_paused):
                toast = t['resumed_toast'] if was_paused else t['st_paused']
            else:
                toast = t['resume_past']
            view = item_view(cid, lang, int(rid), int(page))
    elif data.startswith("rem_c_"):
        _, _, rid, page = data.split("_")
        new_id = duplicate_reminder(cid, int(rid))
        if new_id:
            toast, view = t['dup_toast'], item_view(cid, lang, new_id, 0)
        else:
            toast, view = t['resume_past'], item_view(cid, lang, int(rid), int(page))
    elif data.startswith("rem_xy_"):
        _, _, rid, page = data.split("_")
        toast = t['deleted'] if delete_reminder(cid, int(rid)) else t['gone']
        view = list_view(cid, lang, int(page))
    elif data.startswith("rem_x_"):
        _, _, rid, page = data.split("_")
        r = get_reminder(cid, int(rid))
        if not r:
            toast, view = t['gone'], list_view(cid, lang, int(page))
        else:
            view = (t['confirm_q'].format(line=_line(r, lang)),
                    KB([[B(t['yes_delete'], callback_data=f"rem_xy_{rid}_{page}")],
                        [B(t['cancel'], callback_data=f"rem_o_{rid}_{page}")]]))
    elif data.startswith("rem_e_"):
        r = get_reminder(cid, int(data[6:]))
        if not r:
            toast, view = t['gone'], list_view(cid, lang)
        else:
            ctx.user_data["rem_edit_id"] = r["id"]
            ctx.user_data["awaiting"] = AWAIT_EDIT
            view = (t['edit_q'].format(line=_line(r, lang)), KB([[B(t['cancel'], callback_data=f"rem_o_{r['id']}_0")]]))
    elif data == "rem_tz":
        view = tz_view(cid, lang)
    elif data == "rem_z_auto":
        set_tz(cid, None, lang)
        toast = t['tz_set'].format(tz=("خودکار · " if lang == 'fa' else i18n.t(lang, "Automatic · ")) + zones.city(get_tz(cid, lang), lang))
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

    if awaiting == AWAIT_TEXT:                      # the time is already chosen (a chip / a quick time): only the text
        draft = ctx.user_data.get("rem_draft")
        if not draft or not draft.get("hhmm") or not text:
            ctx.user_data.pop("awaiting", None)
            text_, kb = home_view(cid, lang)
            await msg.reply_text(text_, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        saved = _save_new(cid, lang, draft, draft["hhmm"], text[:MAX_TEXT])
        if saved is None:                           # e.g. "today 09:00" tapped after 09:00: choose a time again
            ctx.user_data["awaiting"] = AWAIT_DETAILS
            p_text, p_kb = details_prompt(cid, lang, draft)
            await msg.reply_text(t['past'].split("\n")[0] + "\n\n" + p_text, reply_markup=p_kb, parse_mode=ParseMode.HTML)
            return
        ctx.user_data.pop("awaiting", None)
        ctx.user_data.pop("rem_draft", None)
        await msg.reply_text(saved[0], reply_markup=saved[1], parse_mode=ParseMode.HTML)
        return

    parsed = parse_details(text)

    if awaiting == AWAIT_EDIT:
        r = get_reminder(cid, ctx.user_data.get("rem_edit_id", -1))
        if not r:
            ctx.user_data.pop("awaiting", None)
            await msg.reply_text(t['gone'], reply_markup=after_save_kb(lang))
            return
        hhmm, body = parsed if parsed else (r["hhmm"], text[:MAX_TEXT])     # just text: the time stays
        if not body:
            await msg.reply_text(t['bad_details'], reply_markup=cancel_kb, parse_mode=ParseMode.HTML)
            return
        nr = next_run(r["kind"], hhmm, r["tz"], r["on_date"], r["weekday"])
        if nr is None:
            await msg.reply_text(t['past'], reply_markup=cancel_kb, parse_mode=ParseMode.HTML)
            return
        was_paused = r["next_run"] is None          # a paused reminder stays paused
        with closing(_db()) as conn, conn:
            conn.execute("UPDATE user_reminders SET text = ?, hhmm = ?, next_run = ? WHERE id = ? AND chat_id = ?",
                         (body, hhmm, None if was_paused else nr, r["id"], cid))
        ctx.user_data.pop("awaiting", None)
        ctx.user_data.pop("rem_edit_id", None)
        r.update(text=body, hhmm=hhmm)
        done = (t['edited_paused'].format(line=_line(r, lang)) if was_paused
                else t['edited'].format(line=_line(r, lang), next=_next_str(nr, r["tz"], lang)))
        await msg.reply_text(done, reply_markup=after_save_kb(lang), parse_mode=ParseMode.HTML)
        return

    if not parsed:
        await msg.reply_text(t['bad_details'], reply_markup=cancel_kb, parse_mode=ParseMode.HTML)
        return
    hhmm, body = parsed
    draft = ctx.user_data.get("rem_draft")
    if not draft:
        ctx.user_data.pop("awaiting", None)
        text_, kb = home_view(cid, lang)
        await msg.reply_text(text_, reply_markup=kb, parse_mode=ParseMode.HTML)
        return
    saved = _save_new(cid, lang, draft, hhmm, body)
    if saved is None:
        await msg.reply_text(t['past'], reply_markup=cancel_kb, parse_mode=ParseMode.HTML)
        return
    ctx.user_data.pop("awaiting", None)
    ctx.user_data.pop("rem_draft", None)
    await msg.reply_text(saved[0], reply_markup=saved[1], parse_mode=ParseMode.HTML)


async def tick(ctx: ContextTypes.DEFAULT_TYPE, get_language=None, keyboard_for=None, skip=None):
    """Job (every 30s): send due reminders, then schedule the next one / delete one-time ones.
    Paused reminders (next_run NULL) are never picked up. The alert carries ✓ Done and snooze buttons."""
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
        if skip and cid in skip:      # banned user: rescheduled above, nothing is sent
            continue
        lang = get_language(cid) if get_language else "fa"
        try:
            await ctx.bot.send_message(cid, _t(lang)['fire'].format(text=html.escape(text)), parse_mode=ParseMode.HTML,
                                       reply_markup=fire_kb(lang))
        except Forbidden:
            pass   # user blocked the bot; repeating ones simply try again next time
        except Exception as e:
            logger.warning(f"Reminder {rid} for {cid} failed: {e}")
