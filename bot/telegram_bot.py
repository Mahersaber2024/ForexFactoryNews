import os
import sys
import signal
import asyncio
import datetime as dt
import requests
import pathlib
import re
import html
import hashlib
import json
import io
import time
from contextlib import closing

# Make the project root importable (for bot_settings / bot package)
ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import bot_settings as cfg
from bot import backup_manager as bk
from bot import ea_admin
from bot import channels
from bot import channel_style
from bot import calendar_image
from bot import font_setup
from bot import tz
from bot import reminders
from db import postgres as database
from telegram.error import Forbidden, BadRequest

from datetime import datetime
import pytz
from collections import defaultdict
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
    BotCommand,
    MenuButtonCommands,
    InputMediaPhoto,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    TypeHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ChatAction, ParseMode

FA_COMMANDS = [
    BotCommand("start", "منوی اصلی"),
    BotCommand("help", "راهنما"),
]

EN_COMMANDS = [
    BotCommand("start", "Main menu"),
    BotCommand("help", "Help"),
]

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cid = q.message.chat.id
    language = q.data.split("_")[1]  # setlang_fa → fa
    if language not in LANGS:
        await q.answer()
        return
    set_user_language(cid, language)
    toast = {'fa': "✓ زبان: فارسی", 'en': "✓ Language: English"}
    await q.answer(toast[language] + " · 🌍 " + tz.status_label(language, cid))
    await safe_edit(q.message, main_menu_text(language, cid), main_menu_keyboard(language, cid))

import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)   # no log file: messages only go to the console (systemd's journal)
logger = logging.getLogger(__name__)

# در توابعی که خطا را نادیده می‌گیرید، لاگ کنید
LANGS = ("fa", "en")

def _db():
    return database.connect()

def get_stored_language(chat_id: int) -> str | None:
    """The language the user picked, or None if they never picked one."""
    try:
        with closing(_db()) as conn:
            row = conn.execute("SELECT language FROM subscribers WHERE chat_id = ?", (chat_id,)).fetchone()
        return row[0] if row and row[0] in LANGS else None
    except Exception as e:
        logger.error(f"Error in get_stored_language for chat_id {chat_id}: {e}")
        return None

def get_user_language(chat_id: int, tg_language_code: str | None = None) -> str:
    """Stored language; if none yet, guess from the Telegram app language instead of forcing English."""
    stored = get_stored_language(chat_id)
    if stored:
        return stored
    return "fa" if (tg_language_code or "").lower().startswith("fa") else "en"

def set_user_language(chat_id: int, language: str):
    # Picking a language must NOT switch the Daily High-Impact News on (it used to insert a "subscribed" row)
    try:
        with closing(_db()) as conn, conn:
            conn.execute(
                """
                INSERT INTO subscribers (chat_id, language, digest_on)
                VALUES (?, ?, 0)
                ON CONFLICT(chat_id) DO UPDATE SET language = excluded.language
                """,
                (chat_id, language)
            )
        tz.on_language_change(chat_id, language)      # an automatic time zone just moved with the language
    except Exception as e:
        logger.error(f"Error in set_user_language for chat_id {chat_id}: {e}")
    
def _all_languages() -> dict[int, str]:
    """{chat_id: stored language} in ONE read (the 30-second loops look at every user)."""
    try:
        with closing(_db()) as conn:
            return {int(c): l for c, l in conn.execute("SELECT chat_id, language FROM subscribers") if l in LANGS}
    except Exception as e:
        logger.error(f"Error in _all_languages: {e}")
        return {}

def get_now_nyt() -> str:
    ny_tz = pytz.timezone("America/New_York")
    return datetime.now(ny_tz).strftime("%H:%M")

# ───────────────  Config  ────────────────
# All settings come from bot_settings.py (which reads .env)
BOT_TOKEN = cfg.TELEGRAM_BOT_TOKEN
DEFAULT_API_BASE = cfg.API_BASE_URL
ADMIN_CHAT_ID = cfg.ADMIN_CHAT_ID
BASE_DIR = cfg.BASE_DIR
DB_PATH = cfg.DB_PATH
SEND_RESTART_MSG = cfg.SEND_RESTART_MSG

# Admin can switch the active Flask server at runtime (see /setapi below).
# Initial value is loaded from the DB if the admin already set one, else falls back to .env.
API_BASE = DEFAULT_API_BASE

# ───────────────  DB helpers  ─────────────
def ensure_db():
    database.initialize()
    if font_setup.missing():   # ✎ Persian font for the pictures: fetch Vazirmatn in the background (never blocks / breaks the bot)
        import threading
        threading.Thread(target=font_setup.ensure_persian_fonts, daemon=True).start()

# ───────────────  Runtime API server config  ─────────────
def get_config(key: str, default: str | None = None) -> str | None:
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT value FROM bot_config WHERE key = ?", (key,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else default
    except Exception as e:
        logger.error(f"Error in get_config for key {key}: {e}")
        return default

def set_config(key: str, value: str):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO bot_config (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value)
    )
    conn.commit()
    conn.close()

def load_api_base_from_db():
    """Load the admin's last saved API_BASE on startup, if any."""
    global API_BASE
    saved = get_config("api_base_url")
    if saved:
        API_BASE = saved
        logger.info(f"Loaded API_BASE from DB: {API_BASE}")
    else:
        API_BASE = DEFAULT_API_BASE
        logger.info(f"Using API_BASE from .env: {API_BASE}")

def restart_notice_on() -> bool:
    """'Bot is back online' message to all users on restart. Admin toggle (DB) wins over .env."""
    v = get_config("restart_notice")
    return SEND_RESTART_MSG if v is None else v == "1"

def is_admin(chat_id: int) -> bool:
    return ADMIN_CHAT_ID != 0 and chat_id == ADMIN_CHAT_ID

def list_api_servers() -> dict[str, str]:
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT name, url FROM api_servers ORDER BY name")
        rows = cur.fetchall()
        conn.close()
        return {name: url for name, url in rows}
    except Exception:
        return {}

def save_api_server(name: str, url: str):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO api_servers (name, url) VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET url = excluded.url
        """,
        (name, url)
    )
    conn.commit()
    conn.close()

def delete_api_server(name: str) -> bool:
    conn = _db()
    cur = conn.cursor()
    cur.execute("DELETE FROM api_servers WHERE name = ?", (name,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

DEFAULT_BRIEF_TIME = "07:00"

def load_subs() -> dict[int, str]:
    """chat_id → time, only for users whose Daily High-Impact News is on."""
    try:
        with closing(_db()) as conn:
            rows = conn.execute("SELECT chat_id, digest_time FROM subscribers WHERE digest_on = 1").fetchall()
        return {int(cid): (t or DEFAULT_BRIEF_TIME) for cid, t in rows}
    except Exception as e:
        logger.error(f"Error in load_subs: {e}")
        return {}

def get_brief(chat_id: int) -> tuple[bool, str]:
    """(is_on, time) for one user."""
    try:
        with closing(_db()) as conn:
            row = conn.execute("SELECT digest_on, digest_time FROM subscribers WHERE chat_id = ?",
                               (chat_id,)).fetchone()
    except Exception as e:
        logger.error(f"Error in get_brief for chat_id {chat_id}: {e}")
        row = None
    if not row:
        return False, DEFAULT_BRIEF_TIME
    return bool(row[0]), (row[1] or DEFAULT_BRIEF_TIME)

def set_brief_on(chat_id: int, on: bool, language: str | None = None):
    language = language or get_user_language(chat_id)
    with closing(_db()) as conn, conn:
        conn.execute(
            """INSERT INTO subscribers (chat_id, language, digest_on) VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET digest_on = excluded.digest_on,
                   pre_news_on = CASE WHEN excluded.digest_on = 1 THEN 0 ELSE subscribers.pre_news_on END""",
            (int(chat_id), language, 1 if on else 0))

def set_brief_time(chat_id: int, hhmm: str, language: str | None = None):
    language = language or get_user_language(chat_id)
    with closing(_db()) as conn, conn:
        conn.execute(
            """INSERT INTO subscribers (chat_id, language, digest_on, digest_time) VALUES (?, ?, 1, ?)
               ON CONFLICT(chat_id) DO UPDATE SET digest_time = excluded.digest_time, digest_on = 1, pre_news_on = 0""",
            (int(chat_id), language, hhmm))

def get_pre_news(chat_id: int) -> bool:
    with closing(_db()) as conn:
        row = conn.execute("SELECT pre_news_on FROM subscribers WHERE chat_id = ?", (chat_id,)).fetchone()
    return bool(row and row[0])

def set_pre_news(chat_id: int, on: bool, language: str):
    with closing(_db()) as conn, conn:
        conn.execute(
            """INSERT INTO subscribers (chat_id, language, digest_on, pre_news_on) VALUES (?, ?, 0, ?)
               ON CONFLICT(chat_id) DO UPDATE SET pre_news_on = excluded.pre_news_on""",
            (chat_id, language, int(on)))

def update_sub_time(chat_id: int, digest_time: str):   # kept for compatibility
    set_brief_time(chat_id, digest_time)

def save_sub(chat_id: int, digest_time: str | None = None):   # kept for compatibility
    if digest_time:
        set_brief_time(chat_id, digest_time)
    set_brief_on(chat_id, True)

def remove_sub(chat_id: int):
    # Turn it off, keep the row: deleting it threw away the user's language (→ English) and time
    set_brief_on(chat_id, False)

# ───────────────  Decor & Utils  ─────────────
def build_event_block(ev: dict) -> str:
    ny_tz = pytz.timezone("America/New_York")
    date_str = ev.get("Date", "")
    time_str = html.escape(str(ev.get("Time", "—")), quote=False)
    currency = html.escape(str(ev.get("Currency", "???")), quote=False)
    event_name = html.escape(str(ev.get("Event", "—")), quote=False)
    prev = html.escape(str(ev.get("Previous", "—")), quote=False)
    fcst = html.escape(str(ev.get("Forecast", "—")), quote=False)
    act  = html.escape(str(ev.get("Actual", "—")), quote=False)

    try:
        date_obj = datetime.strptime(date_str + f" {datetime.now().year}", "%a, %b %d %Y")
        date_obj = ny_tz.localize(date_obj)
        date_formatted = date_obj.strftime("%b %d")
        weekday_en = date_obj.strftime("%A")
        weekday_fa = {
            "Saturday": "شنبه", "Sunday": "یک‌شنبه", "Monday": "دوشنبه",
            "Tuesday": "سه‌شنبه", "Wednesday": "چهارشنبه", "Thursday": "پنج‌شنبه", "Friday": "جمعه"
        }.get(weekday_en, weekday_en)
    except:
        date_formatted = date_str
        weekday_fa = "—"

    return (
        f"➤ {weekday_fa}\n"
        f"{currency}\n"
        f"{IMPACT_DOT.get(ev.get('Impact', 'High'), '🔴')} {event_name}\n"
        f"◷ {time_str} | {date_formatted}\n"
        f"⌖ Prev: {prev} | Fcst: {fcst} | Act: {act}"
    )

async def send_typing(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, delay: float = 0):
    """Shows 'typing…' without blocking. (The old version slept 1s on every command = the /start lag.)"""
    try:
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

# ───────────────  UI texts  ─────────────
BRIEF_NAME = {'fa': "دریافت روزانه اخبار مهم", 'en': "Daily High-Impact News"}

UI = {
    'fa': {
        'welcome': "<b>به ربات اخبار اقتصادی خوش اومدی</b>\n\n"
                   "این ربات خبرهای مهم بازار رو نشونت میده و می‌تونه هر روز خودکار برات بفرسته.\n\n"
                   "برای شروع، «امروز» رو بزن.\n\n",
        'welcome_back': "<b>منوی اصلی</b>\n\nبرای دیدن خبرها یکی از دکمه‌ها رو بزن.\n\n",
        'status_on': "◉ <b>دریافت روزانه اخبار مهم</b> روشنه: هر روز کاری ساعت <b>{time}</b>.",
        'status_off': "◌ <b>دریافت روزانه اخبار مهم</b> خاموشه. از دکمه‌ی پایین روشنش کن.",
        'currencies': "⇄ انتخاب ارز و اهمیت",
        'yesterday': "دیروز", 'today': "امروز", 'tomorrow': "فردا", 'week': "این هفته",
        'brief_btn_on': "◉ ارسال خودکار: {time}",
        'brief_btn_off': "◌ ارسال خودکار: خاموش",
        'channel': "◈ ارسال به کانال من",
        'other_lang': ("◍ English", "setlang_en"),
        # panel
        'p_title': "▤ <b>ارسال خودکار خبرهای روز</b>",
        'p_about': "ساعت ارسال روزانه و هشدار ۳۰ دقیقه قبل از انتشار رو جداگانه تنظیم کن.",
        'p_status_on': "وضعیت: <b>● روشن</b>",
        'p_status_off': "وضعیت: <b>○ خاموش</b>",
        'p_time': "◷ ساعت: <b>{time}</b>",
        'p_cur': "⇄ ارزها: <b>{cur}</b>",
        'p_imp': "≡ اهمیت خبر: {imp}",
        'p_dest': "⌖ ارسال می‌شه به: <b>{dest}</b>",
        'p_dest_chat': "همین چت",
        'p_toggle_on': "● روشنه · بزن تا خاموش شه",
        'p_toggle_off': "○ خاموشه · بزن تا روشن شه",
        'p_change_time': "◷ تغییر ساعت ارسال · {time}",
        'back': "‹ بازگشت",
        'toast_on': "◉ ارسال خودکار روشن شد · {time}",
        'toast_off': "◌ ارسال خودکار خاموش شد",
        'toast_time': "◷ ساعت: {time}",
        # time picker
        't_title': "◷ <b>ساعت ارسال خبرها</b>\n\n{now}\n\n▾ ساعت دلخواهت رو بنویس و بفرست، مثلاً <code>7:23</code> یا <code>17:00</code>\nیا یکی از دکمه‌های پایین رو بزن.\n\n△ ساعت <b>{zone}</b> حساب می‌شه.",
        't_bad': "✕ این ساعت نامعتبره.\nساعت باید بین <code>0</code> تا <code>23</code> و دقیقه بین <code>0</code> تا <code>59</code> باشه.\nمثال درست: <code>7:23</code> یا <code>17:00</code> یا <code>13:05</code>",
        'p_line_on': "● <b>روشن</b>: هر روز کاری (دوشنبه تا جمعه) ساعت <b>{time}</b>{hint}",
        'p_line_off': "○ <b>خاموش</b>: خبری خودکار نمیاد. برای روشن کردن «تغییر ساعت» رو بزن.",
        'p_all_cur': "همه ارزها",
        'p_help': "ⓘ هر روز کاری یک پیام با خبرهای همون روز برات میاد.",
        't_done': "✓ تأیید",
    },
    'en': {
        'welcome': "<b>Welcome to the Economic News bot</b>\n\n"
                   "It shows you the important market news and can send it to you automatically every day.\n\n"
                   "To start, tap “Today”.\n\n",
        'welcome_back': "<b>Main menu</b>\n\nTap a button to see the news.\n\n",
        'status_on': "◉ <b>Daily High-Impact News</b> is on: every weekday at <b>{time}</b> New York time.",
        'status_off': "◌ <b>Daily High-Impact News</b> is off. Turn it on with the button below.",
        'currencies': "⇄ Choose currencies & importance",
        'yesterday': "Yesterday", 'today': "Today", 'tomorrow': "Tomorrow", 'week': "This week",
        'brief_btn_on': "◉ Auto-send: {time}",
        'brief_btn_off': "◌ Auto-send: Off",
        'channel': "◈ Send to my channel",
        'other_lang': ("◍ فارسی", "setlang_fa"),
        'p_title': "▤ <b>Daily news auto-send</b>",
        'p_about': "Set daily delivery and 30-minute pre-release alerts independently.",
        'p_status_on': "Status: <b>● On</b>",
        'p_status_off': "Status: <b>○ Off</b>",
        'p_time': "◷ Time: <b>{time}</b> (New York)",
        'p_cur': "⇄ Currencies: <b>{cur}</b>",
        'p_imp': "≡ Importance: {imp}",
        'p_dest': "⌖ Sent to: <b>{dest}</b>",
        'p_dest_chat': "this chat",
        'p_toggle_on': "● On · tap to turn off",
        'p_toggle_off': "○ Off · tap to turn on",
        'p_change_time': "◷ Change sending time · {time}",
        'back': "‹ Back",
        'toast_on': "◉ Auto-send is on · {time}",
        'toast_off': "◌ Auto-send is off",
        'toast_time': "◷ Time: {time}",
        't_title': "◷ <b>When should I send the news?</b>\n\n{now}\n\n▾ Type a time and send it, e.g. <code>7:23</code> or <code>17:00</code>\nor tap one of the buttons below.\n\n△ The time must be <b>{zone}</b>.",
        't_bad': "✕ That's not a valid time.\nHour must be 0-23 and minute 0-59.\nExamples: <code>7:23</code>, <code>17:00</code>, <code>13:05</code>",
        'p_line_on': "● <b>On</b>: sent every weekday (Mon–Fri) at <b>{time}</b> New York time{hint}",
        'p_line_off': "○ <b>Off</b>: nothing is sent automatically. Tap “Change sending time” to turn it on.",
        'p_all_cur': "All currencies",
        'p_help': "ⓘ Every weekday you get one message with that day's news.",
        't_done': "✓ Done",
    },
}

def without_date_entities(text: str) -> str:
    """Keep event names if Telegram rejects date entities inside a quote."""
    return re.sub(r'<tg-time unix="\d+">(.*?)</tg-time>', r'\1', text, flags=re.DOTALL)

def is_date_entity_rejection(error: Exception, text: str) -> bool:
    message = str(error).lower()
    return '<tg-time unix="' in text and (
        "can't parse entities" in message or "cannot parse entities" in message
        or "unsupported start tag" in message or "unsupported tag" in message
        or "entity" in message or "entities" in message
    )

async def safe_edit(message, text: str, kb: InlineKeyboardMarkup | None = None):
    """edit_text that ignores Telegram's 'message is not modified' (tapping the selected option)."""
    try:
        await message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        if is_date_entity_rejection(e, text):
            logger.warning("Telegram rejected the date entity in a quote; retrying without it: %s", e)
            try:
                await message.edit_text(without_date_entities(text), reply_markup=kb, parse_mode=ParseMode.HTML)
            except BadRequest as retry_error:
                if "not modified" not in str(retry_error).lower():
                    raise
            return
        raise

async def send_news_message(bot, target, text: str, kb=None):
    try:
        await bot.send_message(target, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except BadRequest as e:
        if not is_date_entity_rejection(e, text):
            raise
        logger.warning("Telegram rejected the date entity in a quote; retrying without it: %s", e)
        await bot.send_message(target, without_date_entities(text), parse_mode=ParseMode.HTML, reply_markup=kb)

# ───────────────  ▭ Keyboard under the input box: ▥ Economic News · ▦ Calendar · ◷ Reminders  ─────────────
# ▭ Keyboard under the input box. Changed the buttons? Bump KB_VERSION by 1:
# every user gets the new keyboard the first time they tap any button / send anything.
KB_VERSION = 4

KB_LABELS = {
    'fa': {'admin': "✱ پنل ادمین", 'menu': "▥ اخبار اقتصادی", 'cal': "▦ تقویم", 'rem': "◷ یادآورها"},
    'en': {'admin': "✱ Admin Panel", 'menu': "▥ Economic News", 'cal': "▦ Calendar", 'rem': "◷ Reminders"},
}
MENU_BUTTON = "⌂ Main Menu"          # old labels, still on some users' keyboards: keep working
KB_ACTIONS = {MENU_BUTTON: 'menu', "⌂ منو": 'menu', "⌂ Menu": 'menu'}
for _lbls in KB_LABELS.values():
    for _act, _lbl in _lbls.items():
        KB_ACTIONS[_lbl] = _act
# keyboards already open in users' chats still carry the old coloured-emoji labels: keep them working
for _old, _act in (("\U0001F4CA اخبار اقتصادی", 'menu'), ("\U0001F4CA Economic News", 'menu'),
                   ("\U0001F4C6 تقویم", 'cal'), ("\U0001F4C6 Calendar", 'cal'),
                   ("\u23F0 یادآورها", 'rem'), ("\u23F0 Reminders", 'rem'),
                   ("\U0001F6E0 پنل ادمین", 'admin'), ("\U0001F6E0 Admin Panel", 'admin')):
    KB_ACTIONS[_old] = _act

def menu_kb(language: str = 'en', cid: int | None = None) -> ReplyKeyboardMarkup:
    """Keyboard under the input box. It rides along on messages that have no inline buttons.
    The admin (and only the admin) also gets ✱ Admin Panel on the first row."""
    l = KB_LABELS.get(language, KB_LABELS['en'])
    rows = [
        [KeyboardButton(l['menu'])],                              # ▥ news, full width
        [KeyboardButton(l['cal']), KeyboardButton(l['rem'])],     # then two columns
    ]
    if cid is not None and is_admin(cid):
        rows.insert(0, [KeyboardButton(l['admin'])])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

MENU_KB = menu_kb('en')   # old name

def _kb_stamp(language: str, cid: int | None = None) -> int:
    """users.menu_kb stores which keyboard the user has: version * 10 + language (1 = fa, 2 = en),
    + 1000 for the admin keyboard. A version bump, a language switch or becoming admin all trigger a refresh."""
    return KB_VERSION * 10 + (1 if language == 'fa' else 2) + (1000 if cid is not None and is_admin(cid) else 0)

def _menu_kb_sent(cid: int, language: str | None = None) -> bool:
    """True if this user already has the current keyboard."""
    try:
        with closing(_db()) as conn:
            row = conn.execute("SELECT menu_kb FROM users WHERE chat_id = ?", (cid,)).fetchone()
        return bool(row) and row[0] == _kb_stamp(language or get_user_language(cid), cid)
    except Exception:
        return False

def _mark_menu_kb(cid: int, language: str | None = None):
    stamp = _kb_stamp(language or get_user_language(cid), cid)
    try:
        with closing(_db()) as conn, conn:
            conn.execute("INSERT INTO users (chat_id, menu_kb) VALUES (?, ?) "
                         "ON CONFLICT(chat_id) DO UPDATE SET menu_kb = excluded.menu_kb", (cid, stamp))
    except Exception as e:
        logger.error(f"Error in _mark_menu_kb for {cid}: {e}")

# Telegram can only swap the keyboard together with a message, so one short general note goes out
KB_UPDATED = {
    'fa': "✓ ربات به‌روز شد.",
    'en': "✓ The bot has been updated.",
}

def kb_updated_text(language: str, cid: int) -> str:
    return KB_UPDATED.get(language, KB_UPDATED['en'])

async def keyboard_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs AFTER every handler (group 1): if the user's keyboard is older than KB_VERSION
    (or in the other language), send it once with a short note. Telegram can only set this keyboard with a message."""
    # Inline menu interactions must never create a second message.
    if update.callback_query:
        return
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return
    language = get_stored_language(chat.id)
    if language is None or _menu_kb_sent(chat.id, language):   # new user still picking a language: wait
        return
    try:
        await context.bot.send_message(chat.id, kb_updated_text(language, chat.id), reply_markup=menu_kb(language, chat.id),
                                       parse_mode=ParseMode.HTML)
        _mark_menu_kb(chat.id, language)
    except Forbidden:
        _mark_menu_kb(chat.id, language)
    except Exception as e:
        logger.warning(f"Keyboard refresh for {chat.id} failed: {e}")

async def ensure_menu_button(context, cid: int, language: str, force: bool = False):
    """No more ⌂ carrier message (send + delete). The native Telegram Menu button is set once,
    globally, in post_init, so it exists for every user without /start. Kept as a no-op for old callers."""
    return

async def menu_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reuse the last economic-news menu when Telegram still allows it to be edited."""
    cid = update.effective_chat.id
    context.user_data.pop("awaiting", None)
    language = get_stored_language(cid)
    if language is None:   # never picked a language: same as /start
        await start(update, context)
        return
    await show_main_menu_in_place(update.message, context, cid, language)

def remember_news_menu(cid: int, message_id: int):
    with closing(_db()) as conn, conn:
        conn.execute("INSERT INTO users (chat_id, news_menu_message_id) VALUES (?, ?) "
                     "ON CONFLICT(chat_id) DO UPDATE SET news_menu_message_id = excluded.news_menu_message_id",
                     (cid, message_id))

def last_news_menu(cid: int) -> int | None:
    with closing(_db()) as conn:
        row = conn.execute("SELECT news_menu_message_id FROM users WHERE chat_id = ?", (cid,)).fetchone()
    return row[0] if row else None

async def show_main_menu_in_place(message, context, cid: int, language: str):
    """/start and the ▥ button always put a FRESH menu at the bottom of the chat.
    (Old behaviour edited the previous menu message in place: when its text was identical Telegram
    answered "message is not modified" and the user saw nothing, or the edit happened far up the chat.)"""
    text, kb = main_menu_text(language, cid), main_menu_keyboard(language, cid)
    old_id = last_news_menu(cid)
    sent = await message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    remember_news_menu(cid, sent.message_id)
    if old_id and old_id != sent.message_id:
        context.chat_data.setdefault("news_revisions", {})[old_id] = \
            context.chat_data.get("news_revisions", {}).get(old_id, 0) + 1
        try:
            await context.bot.delete_message(chat_id=cid, message_id=old_id)   # best effort: keep one live menu
        except Exception:
            pass

def main_menu_text(language: str, cid: int, back: bool = False) -> str:
    on, time_ = get_brief(cid)
    currencies = html.escape(" · ".join(get_user_currencies(cid)), quote=False)
    clock, zone_line = tz.clock_note(language, cid), tz.status_label(language, cid)
    if language == "fa":
        status = (f"روزانه، ساعت {html.escape(time_, quote=False)} ({clock})" if on
                  else "۳۰ دقیقه قبل از هر خبر" if get_pre_news(cid) else "خاموش")
        return ("▥ <b>اخبار اقتصادی</b>\n"
                "برای دیدن خبرها یکی از دکمه‌های پایین رو بزن.\n"
                f"<blockquote>⇄ ارزهای تو: {currencies}\n"
                f"◉ ارسال خودکار: {status}\n"
                f"🌍 منطقه زمانی: {zone_line}</blockquote>")
    status = (f"once a day, {html.escape(time_, quote=False)} ({clock})" if on
              else "30 min before each news" if get_pre_news(cid) else "Off")
    return ("▥ <b>Economic News</b>\n"
            "Tap a button below to see the news.\n"
            f"<blockquote>⇄ Your currencies: {currencies}\n"
            f"◉ Auto-send: {status}\n"
            f"🌍 Time zone: {zone_line}</blockquote>")

def main_menu_keyboard(language: str = 'en', cid: int | None = None,
                       selected: str | None = None) -> InlineKeyboardMarkup:
    t = UI[language]
    on, time_ = get_brief(cid) if cid is not None else (False, DEFAULT_BRIEF_TIME)
    before = get_pre_news(cid) if cid is not None else False
    other_label, other_cb = t['other_lang']
    def period_label(key: str) -> str:
        return f"{t[key]} •" if selected == key else t[key]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(period_label('yesterday'), callback_data="yesterday"),
            InlineKeyboardButton(period_label('today'), callback_data="today"),
            InlineKeyboardButton(period_label('tomorrow'), callback_data="tomorrow"),
        ],
        [InlineKeyboardButton(period_label('week'), callback_data="week")],
        [
            InlineKeyboardButton(t['brief_btn_on'].format(time=time_) if on
                                 else (("◴ ارسال: ۳۰ دقیقه قبل از خبر" if language == "fa" else "◴ Auto-send: 30 min before")
                                       if before else t['brief_btn_off']),
                                 callback_data="brief"),
            InlineKeyboardButton(t['currencies'], callback_data="choose_currencies"),
        ],
        [
            InlineKeyboardButton(t['channel'], callback_data="my_channel"),
            InlineKeyboardButton(other_label, callback_data=other_cb),   # one tap language switch
        ],
        [InlineKeyboardButton("🌍 " + (tz.status_label(language, cid) if cid is not None
                                       else ("منطقه زمانی" if language == "fa" else "Time zone")),
                              callback_data="tzs")],      # language + time zone settings
    ])

def default_keyboard(language: str = 'en', cid: int | None = None) -> InlineKeyboardMarkup:   # old name
    return main_menu_keyboard(language, cid)

ALL_CURRENCIES = ("USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "CNY")
AWAIT_BRIEF_TIME = "brief_time"
QUICK_TIMES = ("07:00", "08:00", "09:00", "12:00", "15:00", "17:00")

def parse_hhmm(text: str) -> str | None:
    """'7:23', '13:5', '17:00', '7', '1723', '5pm', '07.30', '13 5', '13/05' (also Persian/Arabic digits) → 'HH:MM', or None."""
    text = (text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    text = text.strip().lower()
    for sep in ("٫", "：", "؛", ";", "/", ",", "،", "-", "_", "h"):
        text = text.replace(sep, ":")
    text = re.sub(r"\s*:\s*", ":", text)
    m = (re.fullmatch(r"(\d{1,2})(?:[:.\s](\d{1,2}))?\s*(am|pm)?", text)
         or re.fullmatch(r"(\d{1,2})(\d{2})()", text))
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap == "pm" else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return f"{h:02d}:{mi:02d}"

def local_hint(language: str, hhmm: str) -> str:
    """Kept for old callers: Persian users now set and see Tehran time directly, so there is nothing to convert."""
    return ""

def brief_panel(language: str, cid: int, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    t = UI[language]
    fa = language == "fa"
    clock = tz.clock_note(language, cid)
    daily, time_ = get_brief(cid)
    before = get_pre_news(cid)
    if daily:
        now = (f"● <b>روشنه:</b> هر روز کاری ساعت <b>{time_}</b> ({clock})" if fa
               else f"● <b>On:</b> every weekday at <b>{time_}</b> ({clock})")
    elif before:
        now = "● <b>روشنه:</b> ۳۰ دقیقه قبل از انتشار هر خبر" if fa else "● <b>On:</b> 30 minutes before each news"
    else:
        now = "○ <b>خاموشه</b> (ساعتی تنظیم نشده)" if fa else "○ <b>Off</b> (no time set)"
    if fa:
        how = ("▭ <b>ساعت رو تایپ کن</b> و بفرست (" + clock + ")\nمثلاً <code>7:23</code> یا <code>17:00</code>\n\n"
               "یا دکمه‌ی ◴ پایین رو بزن تا ۳۰ دقیقه قبل از هر خبر برات بیاد.\n"
               "<i>فقط یکی از این دو فعاله.</i>")
    else:
        how = ("▭ <b>Type the time</b> and send it (" + clock + ")\nExample: <code>7:23</code> or <code>17:00</code>\n\n"
               "or tap ◴ below to get each news 30 minutes before it is released.\n"
               "<i>Only one of the two can be on.</i>")
    head = ["▤ <b>ارسال خودکار خبرها</b>" if fa else "▤ <b>Automatic news</b>", ""]
    text = "\n".join(([notice, ""] if notice else []) + head + [now, "", how])
    state = ("روشن ✓" if before else "خاموش") if fa else ("On ✓" if before else "Off")
    rows = [[InlineKeyboardButton((f"◴ ۳۰ دقیقه قبل از هر خبر · {state}" if fa else f"◴ 30 min before each news · {state}"),
                                  callback_data="brief_mode_before")]]
    if daily:
        rows.append([InlineKeyboardButton("◌ خاموش کردن ساعت" if fa else "◌ Turn off the time", callback_data="brief_off_all")])
    rows.append([InlineKeyboardButton("🌍 " + tz.status_label(language, cid), callback_data="tzs")])
    rows.append([InlineKeyboardButton("✕ انصراف" if fa else "✕ Cancel", callback_data="main_menu")])
    return text, InlineKeyboardMarkup(rows)

def brief_time_picker(language: str, cid: int) -> tuple[str, InlineKeyboardMarkup]:
    t = UI[language]
    on, time_ = get_brief(cid)
    rows = [[InlineKeyboardButton(f"✓ {q}" if on and q == time_ else q, callback_data=f"brief_set_{q}")
             for q in QUICK_TIMES[i:i + 3]] for i in (0, 3)]
    rows.append([InlineKeyboardButton(("✓ " if not on else "") + ("خاموش کردن ارسال خودکار" if language == "fa" else "Turn off auto-send"),
                                      callback_data="brief_none")])
    rows.append([InlineKeyboardButton(t['back'], callback_data="brief")])
    clock = tz.clock_note(language, cid)
    if language == "fa":
        now = (f"الان: <b>{time_}</b> ({clock})" if on
               else "الان: <b>خاموش</b> (خبری خودکار نمیاد)")
    else:
        now = f"Now: <b>{time_}</b> ({clock})" if on else "Now: <b>Off</b> (no automatic news)"
    return t['t_title'].format(now=now, zone=clock), InlineKeyboardMarkup(rows)


# ───────────────  ▦ Calendar: Persian (Jalali) + Gregorian, with its own daily date message  ─────────────
# No extra package needed: the Jalali conversion is done right here.
NY_TZ = tz.NY_TZ
DEFAULT_CAL_TIME = "07:00"
CAL_KINDS = ("jalali", "gregorian")

JALALI_MONTHS = {
    'fa': ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
           "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"],
    'en': ["Farvardin", "Ordibehesht", "Khordad", "Tir", "Mordad", "Shahrivar",
           "Mehr", "Aban", "Azar", "Dey", "Bahman", "Esfand"],
}
GREGORIAN_MONTHS = {
    'fa': ["ژانویه", "فوریه", "مارس", "آوریل", "مه", "ژوئن",
           "ژوئیه", "اوت", "سپتامبر", "اکتبر", "نوامبر", "دسامبر"],
    'en': ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"],
}
WEEKDAYS = {   # Python weekday(): Monday = 0
    'fa': ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یک‌شنبه"],
    'en': ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

def fa_num(x) -> str:
    return str(x).translate(_FA_DIGITS)

def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
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
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd

def jalali_str(d: dt.date, language: str) -> str:
    jy, jm, jd = gregorian_to_jalali(d.year, d.month, d.day)
    wd = WEEKDAYS[language][d.weekday()]
    if language == 'fa':
        return f"{wd} {fa_num(jd)} {JALALI_MONTHS['fa'][jm - 1]} {fa_num(jy)}"
    return f"{wd}, {jd} {JALALI_MONTHS['en'][jm - 1]} {jy}"

def gregorian_str(d: dt.date, language: str) -> str:
    wd = WEEKDAYS[language][d.weekday()]
    if language == 'fa':
        return f"{wd} {fa_num(d.day)} {GREGORIAN_MONTHS['fa'][d.month - 1]} {fa_num(d.year)}"
    return f"{wd}, {d.day} {GREGORIAN_MONTHS['en'][d.month - 1]} {d.year}"

def ny_today() -> dt.date:
    return datetime.now(NY_TZ).date()

CAL_UI = {
    'fa': {
        'menu_off': "▦ تقویم",
        'menu_on': "▦ تقویم · {time}",
        'title': "▦ <b>تاریخ امروز</b>",
        'jalali': "☼ شمسی", 'gregorian': "◍ میلادی",
        'daily_title': "◫ <b>ارسال روزانه تاریخ</b>",
        'daily_about': "هر روز (حتی آخر هفته) سر ساعتی که خودت تعیین می‌کنی، تاریخ امروز رو برات می‌فرستم.",
        'status_on': "وضعیت: <b>● روشن</b> · ساعت <b>{time}</b>",
        'status_off': "وضعیت: <b>○ خاموش</b>",
        'change_time': "◷ تغییر ساعت · {time}",
        'toast_on': "◫ ارسال روزانه تاریخ روشن شد · {time}",
        'toast_off': "◫ ارسال روزانه تاریخ خاموش شد",
        'toast_time': "◷ ساعت: {time}",
        'dest_chat': "همین چت", 'add_channel': "+ افزودن کانال", 'style': "❖ استایل و لوگو",
        'keep_one': "حداقل یک مقصد باید انتخاب باشه",
        'toast_dest_on': "✓ {name} اضافه شد", 'toast_dest_off': "{name} برداشته شد",
        'added': "✓ کانال {ch} وصل شد و به مقصدهای تقویم اضافه شد.\n\n",
        't_title': "◷ <b>ساعت ارسال تاریخ</b>\n\nالان روی <b>{time}</b> ({zone}) تنظیمه.\nاول ساعت رو بزن، بعد دقیقه.",
        'daily_msg': "▦ <b>تاریخ امروز</b>",
        'back': "‹ بازگشت", 'done': "✓ تأیید",
    },
    'en': {
        'menu_off': "▦ Calendar",
        'menu_on': "▦ Calendar · {time}",
        'title': "▦ <b>Today's date</b>",
        'jalali': "☼ Persian", 'gregorian': "◍ Gregorian",
        'daily_title': "◫ <b>Daily date message</b>",
        'daily_about': "Every day (weekends too), at the time you pick, I send you today's date.",
        'status_on': "Status: <b>● On</b> · at <b>{time}</b> (New York time)",
        'status_off': "Status: <b>○ Off</b>",
        'change_time': "◷ Change time · {time}",
        'toast_on': "◫ Daily date on · {time}",
        'toast_off': "◫ Daily date off",
        'toast_time': "◷ Time: {time}",
        'dest_chat': "This chat", 'add_channel': "+ Add channel", 'style': "❖ Style & logo",
        'keep_one': "Keep at least one destination",
        'toast_dest_on': "✓ {name} added", 'toast_dest_off': "{name} removed",
        'added': "✓ Channel {ch} connected and added to the calendar destinations.\n\n",
        't_title': "◷ <b>Date delivery time</b> ({zone})\n\nCurrently <b>{time}</b>.\nTap an hour, then the minutes.",
        'daily_msg': "▦ <b>Today's date</b>",
        'back': "‹ Back", 'done': "✓ Done",
    },
}

def ensure_calendar_table():
    database.initialize()

def get_cal(chat_id: int) -> dict:
    try:
        with closing(_db()) as conn:
            row = conn.execute("SELECT is_on, send_time, kinds, last_sent, to_channel FROM date_daily WHERE chat_id = ?",
                               (chat_id,)).fetchone()
    except Exception as e:
        logger.error(f"Error in get_cal for {chat_id}: {e}")
        row = None
    if not row:
        return {"on": False, "time": DEFAULT_CAL_TIME, "kinds": list(CAL_KINDS), "last_sent": None, "to_channel": False}
    kinds = [k for k in (row[2] or "").split(",") if k in CAL_KINDS] or list(CAL_KINDS)
    return {"on": bool(row[0]), "time": row[1] or DEFAULT_CAL_TIME, "kinds": kinds, "last_sent": row[3],
            "to_channel": bool(row[4])}

def _set_cal(chat_id: int, column: str, value):
    assert column in ("is_on", "send_time", "kinds", "last_sent", "to_channel")
    with closing(_db()) as conn, conn:
        conn.execute("INSERT INTO date_daily (chat_id) VALUES (?) ON CONFLICT DO NOTHING", (int(chat_id),))
        conn.execute(f"UPDATE date_daily SET {column} = ? WHERE chat_id = ?", (value, int(chat_id)))

def get_date_dests(chat_id: int) -> list[int]:
    """Ids the daily date is sent to: the chat itself and/or connected channels (own chat first). Never empty."""
    connected = [ch["id"] for ch in channels.get_channels(chat_id)]
    try:
        with closing(_db()) as conn:
            saved = {r[0] for r in conn.execute("SELECT dest_id FROM date_dest WHERE chat_id = ?", (chat_id,))}
    except Exception as e:
        logger.error(f"get_date_dests for {chat_id}: {e}")
        saved = set()
    if not saved:     # not chosen yet: the old switch decides (channels) or the chat itself
        return connected if (get_cal(chat_id)["to_channel"] and connected) else [chat_id]
    return [d for d in [chat_id, *connected] if d in saved] or [chat_id]

def _save_date_dests(chat_id: int, dests: list[int]):
    with closing(_db()) as conn, conn:
        conn.execute("DELETE FROM date_dest WHERE chat_id = ?", (int(chat_id),))
        conn.executemany("INSERT INTO date_dest (chat_id, dest_id) VALUES (?, ?)", [(int(chat_id), int(d)) for d in dests])

def toggle_date_dest(chat_id: int, dest_id: int) -> bool | None:
    """Select / unselect one destination. Returns the new state, or None if it was the last one (refused)."""
    cur = get_date_dests(chat_id)
    if dest_id in cur:
        if len(cur) == 1:
            return None
        cur.remove(dest_id)
        _save_date_dests(chat_id, cur)
        return False
    _save_date_dests(chat_id, cur + [dest_id])
    return True

def calendar_menu_label(language: str, cid: int | None) -> str:
    c = CAL_UI[language]
    cal = get_cal(cid) if cid is not None else {"on": False}
    return c['menu_on'].format(time=cal['time']) if cal['on'] else c['menu_off']

def date_lines(language: str, kinds: list[str], d: dt.date | None = None, cid: int | None = None) -> list[str]:
    """Compact dates: «پنج‌شنبه ۷ آبان ۱۴۰۵» + «[ میلادی: ۲۹ اکتبر ۲۰۲۶]»."""
    return caption_date_lines(language, kinds, d or tz.today_for(language, cid))

# ───────────────  ▦ date card model (picture version of the daily date)  ────────────────
def _jalali_month_info(d: dt.date) -> tuple[int, int, int, int, int]:
    """(jy, jm, jd, first weekday col [Sat=0], days in month) for the Jalali month containing d."""
    jy, jm, jd = gregorian_to_jalali(d.year, d.month, d.day)
    first = d - dt.timedelta(days=jd - 1)
    n = 29
    while True:
        nxt = first + dt.timedelta(days=n)
        if gregorian_to_jalali(nxt.year, nxt.month, nxt.day)[1] != jm:
            break
        n += 1
    return jy, jm, jd, (first.weekday() + 2) % 7, n          # Python Mon=0 -> Saturday=0

def build_date_model(language: str, kinds: list[str], d: dt.date | None = None, cid: int | None = None) -> dict:
    """Everything calendar_image.render_date_card needs. Persian text only if the server can draw it."""
    import calendar as _cal
    d = d or tz.today_for(language, cid)
    fa = language == "fa" and calendar_image.RTL_OK
    lang = "fa" if fa else "en"
    use_j = language == "fa" and "jalali" in kinds
    use_g = language != "fa" or "gregorian" in kinds
    jy, jm, jd, j_start, j_days = _jalali_month_info(d)
    num = fa_num if fa else str
    jal_hero = use_j                                            # Persian users: the Persian date leads (as in the text version)
    doy = d.timetuple().tm_yday
    ydays = 366 if _cal.isleap(d.year) else 365
    g_start, g_days = _cal.monthrange(d.year, d.month)

    g_parts = dict(wd=WEEKDAYS[lang][d.weekday()], day=num(d.day), month=GREGORIAN_MONTHS[lang][d.month - 1], year=num(d.year))
    j_parts = dict(wd=WEEKDAYS[lang][d.weekday()], day=num(jd), month=JALALI_MONTHS[lang][jm - 1], year=num(jy))
    season = (["بهار", "تابستان", "پاییز", "زمستان"] if fa else ["Spring", "Summer", "Autumn", "Winter"])[(jm - 1) // 3]
    j_doy = (jm - 1) * 31 + jd if jm <= 6 else 186 + (jm - 7) * 30 + jd
    j_ydays = 366 if (jy % 33) in (1, 5, 9, 13, 17, 22, 26, 30) else 365
    if jal_hero:
        hero, kind = j_parts, ("" if fa else "Persian")
        month_len_left = j_days - jd
        grid = dict(title=f"{j_parts['month']} {j_parts['year']}", start=j_start, days=j_days, today=jd,
                    heads=(["ش", "ی", "د", "س", "چ", "پ", "ج"] if fa else ["S", "S", "M", "T", "W", "T", "F"]),
                    weekend=[6])
        nums = [num(i) for i in range(1, j_days + 1)]
        second = (dict(label=("میلادی" if fa else "Gregorian"),
                       big=f"{g_parts['day']} {g_parts['month']} {g_parts['year']}", sub=g_parts["wd"]) if use_g else None)
        stats = ([("روز سال", f"{num(j_doy)}/{num(j_ydays)}"), ("فصل", season), ("تا پایان ماه", f"{num(month_len_left)} " + ("روز" if fa else "d"))]
                 if fa else [("Day of year", f"{j_doy}/{j_ydays}"), ("Season", season), ("Month ends in", f"{month_len_left} d")])
    else:
        hero, kind = g_parts, ("میلادی" if fa else "Gregorian")
        grid = dict(title=f"{g_parts['month']} {g_parts['year']}", start=g_start, days=g_days, today=d.day,
                    heads=(["د", "س", "چ", "پ", "ج", "ش", "ی"] if fa else ["M", "T", "W", "T", "F", "S", "S"]),
                    weekend=[5, 6] if not fa else [4])
        nums = [num(i) for i in range(1, g_days + 1)]
        second = (dict(label=("شمسی" if fa else "Persian"),
                       big=f"{j_parts['day']} {j_parts['month']} {j_parts['year']}", sub=j_parts["wd"]) if use_j else None)
        week = d.isocalendar()[1]
        stats = ([("روز سال", f"{num(doy)}/{num(ydays)}"), ("هفته", num(week)), ("فصل سال", "Q" + num((d.month - 1) // 3 + 1))]
                 if fa else [("Day of year", f"{doy}/{ydays}"), ("Week", str(week)), ("Quarter", f"Q{(d.month - 1) // 3 + 1}")])
    return dict(rtl=fa, title=("تقویم امروز" if fa else "Today's calendar"),
                sub=tz.zone_text(tz.zone_name(language, cid), lang)["sub"], kind=kind, hero=hero, grid=grid,
                nums=nums, second=second, stats=stats)

channel_style.date_model_provider = lambda language, cid=None: build_date_model(language, list(CAL_KINDS), None, cid)

async def _date_png(cid: int, language: str, kinds: list[str], day: dt.date, cache: dict):
    """The date as a picture in the CALENDAR look (own style, separate from the news). Rendered once per delivery."""
    try:
        if "png" not in cache:
            cache["png"] = await asyncio.to_thread(channel_style.render_date, cid, channel_style.CAL_ID,
                                                   build_date_model(language, kinds, day, cid))
        return cache["png"]
    except Exception:
        logger.exception("Date picture for %s failed; sending text instead", cid)
        return None

def calendar_panel(language: str, cid: int) -> tuple[str, InlineKeyboardMarkup]:
    """One simple screen: on/off + time, where to send (tap to select, any number of channels), style."""
    c, cal, fa = CAL_UI[language], get_cal(cid), language == "fa"
    connected = channels.get_channels(cid)
    dests = get_date_dests(cid)
    names = ([c['dest_chat']] if cid in dests else []) + \
            [html.escape(channels.label(ch), quote=False) for ch in connected if ch["id"] in dests]
    status = ("● روشن" if cal['on'] else "○ خاموش") if fa else ("● On" if cal['on'] else "○ Off")
    head = "▦ <b>تقویم امروز</b>" if fa else "▦ <b>Today's calendar</b>"
    labels = ("ارسال روزانه", f"ساعت ({tz.clock_note(language, cid)})", "مقصد") if fa else ("Daily delivery", tz.clock_note(language, cid), "Send to")
    text = ("\n".join([head, *date_lines(language, list(CAL_KINDS), cid=cid)])
            + f"\n\n<blockquote>◫ <b>{labels[0]}:</b> {status}\n"
            + f"◷ <b>{labels[1]}:</b> {html.escape(cal['time'])}\n"
            + f"⌖ <b>{labels[2]}:</b> {', '.join(names)}</blockquote>")
    toggle = (("ارسال روزانه · روشن" if cal['on'] else "ارسال روزانه · خاموش") if fa else
              ("Daily delivery · On" if cal['on'] else "Daily delivery · Off"))
    def dest_button(dest_id: int, name: str):
        return InlineKeyboardButton(f"{'●' if dest_id in dests else '○'} {name[:24]}", callback_data=f"cal_d_{dest_id}")
    dest_buttons = [dest_button(cid, c['dest_chat'])] + [
        dest_button(ch["id"], (ch.get("title") or channels.label(ch)).strip()) for ch in connected]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data="cal_toggle"),
         InlineKeyboardButton(f"◷ {cal['time']}", callback_data="cal_time")],
        *[dest_buttons[i:i + 2] for i in range(0, len(dest_buttons), 2)],
        [InlineKeyboardButton(c['add_channel'], callback_data="chan_add_cal"),
         InlineKeyboardButton(c['style'], callback_data=f"chs:c:{channel_style.CAL_ID}")],
        [InlineKeyboardButton(c['back'], callback_data="main_menu")],
    ])
    return text, kb

def calendar_after_connect(cid: int, channel_id: int, language: str) -> tuple[str, InlineKeyboardMarkup]:
    """A channel was just added from the calendar: select it as a destination and show the calendar again."""
    cur = get_date_dests(cid)
    if channel_id not in cur:
        _save_date_dests(cid, cur + [channel_id])
    ch = next((x for x in channels.get_channels(cid) if x["id"] == channel_id), {"id": channel_id})
    text, kb = calendar_panel(language, cid)
    return CAL_UI[language]['added'].format(ch=html.escape(channels.label(ch), quote=False)) + text, kb

def calendar_time_picker(language: str, cid: int) -> tuple[str, InlineKeyboardMarkup]:
    c, time_ = CAL_UI[language], get_cal(cid)['time']
    cur_h, cur_m = time_.split(":")
    rows, row = [], []
    for h in range(24):
        hh = f"{h:02d}"
        row.append(InlineKeyboardButton(f"✓{hh}" if hh == cur_h else hh, callback_data=f"cal_h_{hh}"))
        if len(row) == 6:
            rows.append(row)
            row = []
    rows.append([InlineKeyboardButton(f"✓:{mm}" if mm == cur_m else f":{mm}", callback_data=f"cal_m_{mm}")
                 for mm in ("00", "15", "30", "45")])
    rows.append([InlineKeyboardButton(c['done'], callback_data="cal")])
    return c['t_title'].format(time=time_, zone=tz.clock_note(language, cid)), InlineKeyboardMarkup(rows)

async def calendar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    language = get_user_language(cid, getattr(update.effective_user, "language_code", None))
    text, kb = calendar_panel(language, cid)
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

async def calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """All ▦ buttons: cal, cal_toggle, cal_d_<destination>, cal_time, cal_h_HH, cal_m_MM."""
    q = update.callback_query
    cid = q.message.chat.id
    language = get_user_language(cid, getattr(q.from_user, "language_code", None))
    c, data, cal = CAL_UI[language], q.data, get_cal(cid)
    toast = None
    if data == "cal_toggle":
        on = not cal['on']
        _set_cal(cid, "is_on", 1 if on else 0)
        toast = c['toast_on'].format(time=cal['time']) if on else c['toast_off']
    elif data.startswith("cal_d_"):
        try:
            dest_id = int(data[len("cal_d_"):])
        except ValueError:
            await q.answer()
            return
        known = {cid, *(ch["id"] for ch in channels.get_channels(cid))}
        if dest_id not in known:
            await q.answer("✕")
        else:
            state = toggle_date_dest(cid, dest_id)
            name = c['dest_chat'] if dest_id == cid else next(
                channels.label(ch) for ch in channels.get_channels(cid) if ch["id"] == dest_id)
            toast = c['keep_one'] if state is None else (c['toast_dest_on'] if state else c['toast_dest_off']).format(name=name)
    elif data == "cal_time" or data.startswith(("cal_h_", "cal_m_")):
        if data != "cal_time":
            _, part, val = data.split("_")
            h, m = cal['time'].split(":")
            new = f"{val}:{m}" if part == "h" else f"{h}:{val}"
            _set_cal(cid, "send_time", new)
            await q.answer(c['toast_time'].format(time=new))
        else:
            await q.answer()
        await safe_edit(q.message, *calendar_time_picker(language, cid))
        return
    await q.answer(toast)
    await safe_edit(q.message, *calendar_panel(language, cid))

async def reminders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await reminders.cmd(update, context, get_user_language(cid, getattr(update.effective_user, "language_code", None)))

async def reminders_tick(context: ContextTypes.DEFAULT_TYPE):
    await reminders.tick(context, get_user_language, menu_kb)

async def date_loop(context: ContextTypes.DEFAULT_TYPE):
    """Every 30 s: send today's date to users whose daily date time is now ON THEIR OWN CLOCK (their time zone:
    automatic from the language, or the one they picked), once per day."""
    try:
        with closing(_db()) as conn:
            cand = conn.execute("SELECT chat_id, send_time, last_sent FROM date_daily WHERE is_on = 1").fetchall()
    except Exception as e:
        logger.error(f"date_loop query failed: {e}")
        return
    if not cand:
        return
    langs = _all_languages()
    clocks = tz.bulk_now({c_: ("fa" if langs.get(c_) == "fa" else "en") for c_, _t, _l in cand})   # each user's own clock
    rows = []
    for cid_, send_time, last_sent in cand:
        now_ = clocks[cid_]
        if send_time == now_.strftime("%H:%M") and last_sent != now_.date().isoformat():
            rows.append((cid_,))
    for (cid,) in rows:
        try:
            language = get_user_language(cid)
            today_ = tz.today_for(language, cid)
            _set_cal(cid, "last_sent", today_.isoformat())   # mark first: never send twice even if sending is slow
            cal = get_cal(cid)
            text = "\n".join([CAL_UI[language]['daily_msg'], "", *date_lines(language, cal['kinds'], today_)])
            dests = get_date_dests(cid)
            successes, cache = 0, {}
            png = await _date_png(cid, language, cal['kinds'], today_, cache)
            for dest in dests + ([] if dests else [cid]):
                own = dest == cid
                kb = menu_kb(language, cid) if own else None
                try:
                    if png:
                        await context.bot.send_photo(dest, io.BytesIO(png), caption=text, parse_mode=ParseMode.HTML,
                                                     reply_markup=kb)
                    else:
                        await context.bot.send_message(dest, text, parse_mode=ParseMode.HTML, reply_markup=kb)
                    successes += 1
                    if own:
                        _mark_menu_kb(cid, language)
                except (Forbidden, BadRequest) as e:
                    logger.warning(f"Daily date to {dest} of {cid} failed: {e}")
                    if not own:
                        ch = next((x for x in channels.get_channels(cid) if x["id"] == dest), {"id": dest})
                        await channels.notify_failed(context, cid, language, ch)
            if not successes and cid not in dests:        # every channel failed: still deliver in the private chat
                if png:
                    await context.bot.send_photo(cid, io.BytesIO(png), caption=text, parse_mode=ParseMode.HTML,
                                                 reply_markup=menu_kb(language, cid))
                else:
                    await context.bot.send_message(cid, text, parse_mode=ParseMode.HTML, reply_markup=menu_kb(language, cid))
            await asyncio.sleep(0.05)
        except Forbidden:
            _set_cal(cid, "is_on", 0)   # user blocked the bot
        except Exception as e:
            logger.warning(f"Daily date for {cid} failed: {e}")


# ───────────────  Core fetch  ─────────────
def fill_missing_times(events: list[dict]) -> list[dict]:
    """Safety net if the API server still runs the old scraper: an event with an empty time
    happens at the same time as the event right before it on the same day (ForexFactory style)."""
    last_date, last_time = None, None
    for ev in events:
        date, time_ = ev.get("Date"), (ev.get("Time") or "").strip()
        if date != last_date:
            last_date, last_time = date, None
        if time_:
            last_time = time_
        elif last_time:
            ev["Time"] = last_time
    return events

IMPACTS = ("High", "Medium", "Low")
IMPACT_DOT = {"High": "🔴", "Medium": "🟠", "Low": "🟡", "Holiday": "⚪"}

def get_filtered_events(endpoint: str, allowed_currencies: list[str],
                        impacts: list[str] | None = None) -> list[dict]:
    impacts = impacts or ["High"]
    try:
        # Old API servers ignore ?impact= and return High only, which is still correct for High
        response = requests.get(f"{API_BASE}/{endpoint}", params={"impact": ",".join(impacts).lower()}, timeout=75)
        response.raise_for_status()  # بررسی خطاهای HTTP
        data = fill_missing_times(response.json())
        return [ev for ev in data
                if ev.get("Currency") in allowed_currencies and ev.get("Impact", "High") in impacts]
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching data from {API_BASE}/{endpoint}: {e}")
        raise

def get_events_with_retry(endpoint: str, allowed_currencies: list[str], impacts: list[str] | None = None,
                          attempts: int = 3, delays: tuple[float, ...] = (3, 8)) -> list[dict]:
    """get_filtered_events, tried up to `attempts` times (blocking: run it with asyncio.to_thread).
    Raises the last error if every attempt failed. An empty list is a real answer ("no news"), not a failure."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return get_filtered_events(endpoint, allowed_currencies, impacts)
        except Exception as e:      # network / HTTP 5xx / bad JSON
            last = e
            logger.warning("News fetch attempt %d/%d for '%s' failed: %s", i + 1, attempts, endpoint, e)
            if i < attempts - 1:
                time.sleep(delays[min(i, len(delays) - 1)])
    raise last

def group_by_currency(events: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for ev in events:
        cur = ev.get("Currency", "???")
        grouped.setdefault(cur, []).append(ev)
    return grouped

def format_message_clean(title: str, events: list[dict], language: str = 'en') -> str:
    if not events:
        return "☾ No hot news today." if language == 'en' else "☾ هیچ خبر داغی نیست."

    messages = {
        'fa': {
            'day_map': {
                "Monday": "دوشنبه", "Tuesday": "سه‌شنبه", "Wednesday": "چهارشنبه",
                "Thursday": "پنج‌شنبه", "Friday": "جمعه", "Saturday": "شنبه", "Sunday": "یکشنبه"
            }
        },
        'en': {
            'day_map': {
                "Monday": "Monday", "Tuesday": "Tuesday", "Wednesday": "Wednesday",
                "Thursday": "Thursday", "Friday": "Friday", "Saturday": "Saturday", "Sunday": "Sunday"
            }
        }
    }

    ny_tz = pytz.timezone("America/New_York")
    grouped = defaultdict(lambda: defaultdict(list))

    for ev in events:
        # Escape: names like "S&P Global PMI" are invalid Telegram HTML and made the whole message fail
        currency = html.escape(str(ev.get("Currency", "???")), quote=False)
        event_name = html.escape(str(ev.get("Event", "—")), quote=False)
        time_str = html.escape(str(ev.get("Time", "—")), quote=False)
        prev = html.escape(str(ev.get("Previous", "—")), quote=False)
        fcst = html.escape(str(ev.get("Forecast", "—")), quote=False)
        act = html.escape(str(ev.get("Actual", "—")), quote=False)
        date_str = ev.get("Date", "—")

        try:
            date_obj = datetime.strptime(date_str + f" {datetime.now().year}", "%a, %b %d %Y")
            date_obj = ny_tz.localize(date_obj)
            date_str = date_obj.strftime("%b %d")
            farsi_day = messages[language]['day_map'].get(date_obj.strftime("%A"), date_obj.strftime("%A"))
            date_display = farsi_day
            date_line = f"➤ {farsi_day}"
        except:
            date_display = date_str
            date_line = f"➤ {date_str}"

        event_text = (
            f"{currency}\n"
            f"{IMPACT_DOT.get(ev.get('Impact', 'High'), '🔴')} {event_name}\n"
            f"◷ {time_str} | {date_str}\n"
            f"⌖ Prev: {prev} | Fcst: {fcst} | Act: {act}\n"
        )
        grouped[currency][date_display].append(event_text)

    lines = [f"✦ {html.escape(title, quote=False)}\n"]
    for currency in grouped:
        for day, ev_list in grouped[currency].items():
            if language == 'en':
                lines.append(f"➤ {day}\n")
            else:
                lines.append(f"➤ {day}")
            lines.extend(ev_list)
            lines.append("")

    return "\n".join(lines).strip()

TG_LIMIT = 3900   # Telegram max is 4096 characters per message; keep a margin

def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split a long news message on blank lines (between events) so each part fits in one Telegram message.
    The weekly list, especially with 🟠/🟡 on, is often longer than 4096 chars and was silently dropped."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for block in text.split("\n\n"):
        while len(block) > limit:                      # a single huge block: hard cut
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(block[:limit])
            block = block[limit:]
        candidate = f"{cur}\n\n{block}" if cur else block
        if len(candidate) > limit:
            parts.append(cur)
            cur = block
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return parts

def news_header(endpoint: str, language: str, kinds: list[str], day: dt.date,
                include_calendar: bool = True, cid: int | None = None) -> str:
    c = tz.city(tz.zone_name(language, cid), language)       # the user's own zone, not a fixed city
    titles = {
        "fa": {"yesterday": f"خبرهای اقتصادی دیروز به وقت {c}", "today": f"خبرهای اقتصادی امروز به وقت {c}",
               "tomorrow": f"خبرهای اقتصادی فردا به وقت {c}", "weekly": f"خبرهای اقتصادی این هفته به وقت {c}"},
        "en": {"yesterday": f"Yesterday's Economic News, {c} Time", "today": f"Today's Economic News, {c} Time",
               "tomorrow": f"Tomorrow's Economic News, {c} Time", "weekly": f"This Week's Economic News, {c} Time"},
    }
    selected_day = day + dt.timedelta(days={"yesterday": -1, "tomorrow": 1}.get(endpoint, 0))
    lines = [f"<b>▤ {titles[language][endpoint]}</b>", ""]
    if endpoint == "weekly":
        start = day - dt.timedelta(days=day.weekday())
        end = start + dt.timedelta(days=6)
        label = "بازه هفته" if language == "fa" else "Week"
        lines.append(f"▦ <b>{label}:</b> {start.isoformat()}  →  {end.isoformat()}")
        if include_calendar:
            lines.extend(date_lines(language, kinds, day))
    elif include_calendar:
        lines.extend(date_lines(language, kinds, selected_day))
    return "\n".join(lines).rstrip()

def event_timestamp(ev: dict, reference_day: dt.date) -> int | None:
    """Resolve a Forex Factory date and clock time in New York, including year boundaries."""
    date_text = str(ev.get("Date") or "").strip()
    time_text = str(ev.get("Time") or "").strip().replace(" ", "").upper()
    time_obj = None
    for fmt in ("%H:%M", "%I:%M%p", "%I%p"):
        try:
            time_obj = datetime.strptime(time_text, fmt).time()
            break
        except ValueError:
            continue
    if not date_text or time_obj is None:
        return None
    candidates = []
    for year in (reference_day.year - 1, reference_day.year, reference_day.year + 1):
        try:
            parsed = datetime.strptime(f"{date_text} {year}", "%a, %b %d %Y").date()
            # strptime accepts mismatched weekday names, so verify the supplied day.
            if parsed.strftime("%a").lower() == date_text[:3].lower():
                candidates.append(parsed)
        except ValueError:
            continue
    if not candidates:
        return None
    event_day = min(candidates, key=lambda d: abs((d - reference_day).days))
    if abs((event_day - reference_day).days) > 7:
        return None
    try:
        return int(NY_TZ.localize(datetime.combine(event_day, time_obj), is_dst=None).timestamp())
    except (pytz.AmbiguousTimeError, pytz.NonExistentTimeError, OverflowError, ValueError):
        return None

def news_pages(endpoint: str, events: list[dict] | None, currencies: list[str],
               impacts: list[str], language: str, kinds: list[str],
               day: dt.date | None = None, limit: int = TG_LIMIT, cid: int | None = None) -> list[str]:
    """Build valid HTML pages on whole blockquote boundaries, each with its own calendar."""
    reference_day = day or ny_today()
    reference_day += dt.timedelta(days={"yesterday": -1, "tomorrow": 1}.get(endpoint, 0))
    has_selected_news = events is not None and any(ev.get("Currency") in currencies for ev in events)
    header_day = tz.today_for(language, cid) if cid is not None else (day or tz.today_for(language))
    header = news_header(endpoint, language, kinds, header_day, include_calendar=not has_selected_news, cid=cid)
    blocks = []
    if events is None:
        blocks.append("<blockquote>△ دریافت خبرها ممکن نشد. لطفاً بعداً دوباره امتحان کن.</blockquote>"
                      if language == "fa" else
                      "<blockquote>△ News is unavailable. Please try again later.</blockquote>")
    else:
        grouped = group_by_currency(events)
        period_fa = {"yesterday": "دیروز", "today": "امروز", "tomorrow": "فردا", "weekly": "این هفته"}
        period_en = {"yesterday": "yesterday", "today": "today", "tomorrow": "tomorrow", "weekly": "this week"}
        def clean(value, max_len=160):
            return html.escape(str(value or "—")[:max_len], quote=False)
        def clean_metric(value):
            text = str(value).strip() if value is not None else ""
            if text.lower() in ("", "—", "–", "-", "unknown", "n/a", "none", "null"):
                return ""
            return html.escape(text[:40], quote=False)
        if not any(grouped.get(currency) for currency in currencies):
            no_news = (f"برای ارزهای انتخابی {period_fa[endpoint]} خبری با اهمیت انتخاب‌شده نیست."
                       if language == "fa" else
                       f"No news for the selected currencies {period_en[endpoint]} at the selected impact levels.")
            blocks.append(f"<blockquote>{no_news}</blockquote>")
        for currency in currencies:
            cur = clean(currency, 12)
            found = grouped.get(currency, [])
            groups = {}
            for index, ev in enumerate(found):
                date_raw = str(ev.get("Date") or "").strip()
                time_raw = str(ev.get("Time") or "").strip()
                # Only definite clock times with a known date can be grouped. Preserve
                # the first appearance order and avoid grouping All Day / missing times.
                normalized = None
                for fmt in ("%H:%M", "%I:%M%p", "%I%p"):
                    try:
                        normalized = datetime.strptime(time_raw.replace(" ", "").upper(), fmt).strftime("%H:%M")
                        break
                    except ValueError:
                        pass
                key = (date_raw.casefold(), normalized) if date_raw and normalized else ("unmatched", index)
                groups.setdefault(key, []).append(ev)

            labels = ("قبلی", "پیش‌بینی", "واقعی") if language == "fa" else ("Prev", "Forecast", "Actual")
            for batch in groups.values():
                first = batch[0]
                shown_first = tz.localize_event(first, language, cid)      # date + time in the user's zone
                group_head = (f"<b>{cur}</b>\n"
                              f"◷ {clean(tz.display_date(str(shown_first.get('Date') or ''), language), 40)} | {clean(shown_first.get('Time'), 40)}")
                group_body = []
                def group_block(entries):
                    content = "\n\n".join(entries)
                    # Count displayed lines, including blank separator lines.
                    tag = "<blockquote expandable>" if len(content.split("\n")) > 6 else "<blockquote>"
                    return group_head + "\n" + tag + content + "</blockquote>"
                for ev in batch:
                    dot = IMPACT_DOT.get(ev.get("Impact", "High"), "🔴")
                    name = clean(ev.get("Event"))
                    timestamp = event_timestamp(ev, reference_day)
                    dated_name = f'<tg-time unix="{timestamp}">{name}</tg-time>' if timestamp is not None else name
                    entry = (
                        f"{dot} <b>{dated_name}</b>\n"
                        f"　<i>▥ {labels[0]}: {clean_metric(ev.get('Previous'))}  |  "
                        f"{labels[1]}: {clean_metric(ev.get('Forecast'))}  |  "
                        f"{labels[2]}: {clean_metric(ev.get('Actual'))}</i>"
                    )
                    candidate = group_block(group_body + [entry])
                    if group_body and len(header) + 2 + len(candidate) > limit:
                        blocks.append(group_block(group_body))
                        group_body = []
                    group_body.append(entry)
                if group_body:
                    blocks.append(group_block(group_body))
    pages, current = [], header
    for block in blocks:
        candidate = current + "\n\n" + block
        if len(candidate) > limit and current != header:
            pages.append(current)
            current = header + "\n\n" + block
        else:
            current = candidate
    pages.append(current)
    return pages

def news_page_keyboard(language: str, page: int, total: int, cid: int,
                       selected: str | None = None) -> InlineKeyboardMarkup:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◂", callback_data=f"news_page_{page - 1}"))
    if total > 1:
        row.append(InlineKeyboardButton(f"{page + 1} / {total}", callback_data=f"news_page_{page}"))
    if page + 1 < total:
        row.append(InlineKeyboardButton("▸", callback_data=f"news_page_{page + 1}"))
    rows = [list(r) for r in main_menu_keyboard(language, cid, selected).inline_keyboard]
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

async def fetch_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, endpoint: str, title: str):
    q = update.callback_query
    cid = q.message.chat.id
    message_id = q.message.message_id
    selected = "week" if endpoint == "weekly" else endpoint
    context.chat_data.setdefault("news_selected", {})[message_id] = selected
    context.chat_data.setdefault("news_pages", {}).pop(message_id, None)
    revisions = context.chat_data.setdefault("news_revisions", {})
    revision = revisions.get(message_id, 0)
    language = get_user_language(cid, getattr(q.from_user, "language_code", None))
    loading = "◴ در حال دریافت خبرها..." if language == "fa" else "◴ Fetching news..."
    dashboard = main_menu_text(language, cid)
    await safe_edit(q.message, dashboard + "\n\n" + loading, main_menu_keyboard(language, cid, selected))
    allowed, impacts = get_user_currencies(cid), get_user_impacts(cid)
    try:
        events = await asyncio.to_thread(get_events_with_retry, tz.endpoint_for(endpoint, language, cid), allowed, impacts,
                                          3, (1, 2))
    except Exception:
        logger.exception("Fetching %s news for %s failed", endpoint, cid)
        events = None
    if revisions.get(message_id, 0) != revision:
        return
    prefix = dashboard + "\n\n"
    pages = news_pages(endpoint, events, allowed, impacts, language, get_cal(cid)["kinds"],
                       limit=TG_LIMIT - len(prefix), cid=cid)
    pages = [prefix + page for page in pages]
    context.chat_data.setdefault("news_pages", {})[q.message.message_id] = pages
    await safe_edit(q.message, pages[0], news_page_keyboard(language, 0, len(pages), cid, selected))

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int, user_info: dict):
    """ارسال پیام به ادمین هنگام加入 کاربر جدید"""
    try:
        username = user_info.get('username', 'نامشخص')
        first_name = user_info.get('first_name', 'نامشخص')
        last_name = user_info.get('last_name', '')
        full_name = f"{first_name} {last_name}".strip()
        message = (
            f"◈ کاربر جدید به ربات اضافه شد!\n"
            f"ID شناسه: {user_id}\n"
            f"☻ نام: {full_name}\n"
            f"▭ نام کاربری: @{username if username != 'نامشخص' else 'ندارد'}\n"
            f"◷ زمان: {datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d %H:%M:%S')} NYT"
        )
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=message, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Error notifying admin: {e}")
# ───────────────  Admin: Flask server panel (/admin)  ─────────────
ADMIN_AWAIT_ADD_SERVER = "add_server"
ADMIN_AWAIT_RESTORE_FILE = "restore_file"

ADMIN_RULE = "──────────────"
_BK_NAME = re.compile(r"^backup-(\d{8})-(\d{6})-([a-z\-]+?)-*\.zip$")

def _backup_label(p: pathlib.Path) -> str:
    """backup-20261009-073550-manual.zip → Oct 09, 07:35 · manual"""
    m = _BK_NAME.match(p.name)
    if not m:
        return p.name
    when = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    return f"{when:%b %d, %H:%M} · {m.group(3)}"

def _active_server_name() -> str:
    for name, url in list_api_servers().items():
        if url == API_BASE:
            return name
    return "default (.env)"

def _user_stats() -> tuple[int, int]:
    try:
        with closing(_db()) as conn:
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            daily = conn.execute("SELECT COUNT(*) FROM subscribers WHERE digest_on = 1").fetchone()[0]
        return users, daily
    except Exception:
        return 0, 0

def build_admin_menu_text() -> str:
    backups = bk.list_backups()
    users, daily = _user_stats()
    last = f"{_backup_label(backups[0])} · {len(backups)} saved" if backups else "none yet"
    lines = [
        "<b>ADMIN PANEL</b>", ADMIN_RULE,
        f"<b>Server</b>     ● {html.escape(_active_server_name())}",
        f"<code>{html.escape(API_BASE)}</code>",
        "",
        f"<b>Users</b>      {users} · {daily} get daily news",
        f"<b>Backup</b>     {last}",
        f"<b>Restart msg</b>  {'● on' if restart_notice_on() else '○ off'}",
    ]
    if ea_admin.available():
        lines.append(f"<b>Downloads</b>  {ea_admin.summary()}")
    return "\n".join(lines)

def build_admin_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("⇄ Servers", callback_data="admin_servers")]]
    if ea_admin.available():
        rows[0].append(InlineKeyboardButton("▤ Downloads", callback_data="ea:list"))
    rows += [
        [
            InlineKeyboardButton("⤓ Backup now", callback_data="admin_backup"),
            InlineKeyboardButton("↺ Restore", callback_data="admin_restore"),
        ],
        [InlineKeyboardButton("▦ Saved backups", callback_data="admin_backups")],
        [InlineKeyboardButton("◴ API cache", callback_data="admin_cache")],
        [InlineKeyboardButton("● Restart message · On" if restart_notice_on() else "○ Restart message · Off",
                              callback_data="admin_restartmsg")],
    ]
    return InlineKeyboardMarkup(rows)

async def admin_restartmsg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-tap toggle for the 'Bot is back online' message sent to every user after a restart."""
    q = update.callback_query
    if not is_admin(q.message.chat.id):
        await q.answer("Admins only.", show_alert=True)
        return
    on = not restart_notice_on()
    set_config("restart_notice", "1" if on else "0")
    await q.answer("● Users get a message after each restart" if on else "○ Restarts are silent now")
    await safe_edit(q.message, build_admin_menu_text(), build_admin_menu_keyboard())

# ───────────────  Admin: API cache  ─────────────
ADMIN_AWAIT_CACHE_TIMES = "admin_cache_times"
CACHE_INTERVALS = (2, 5, 10, 15, 30, 60)
CACHE_LABELS = {"today": "Today", "tomorrow": "Tomorrow", "weekly": "Week", "yesterday": "Yesterday"}

def _cache_call(method: str, path: str = "", body: dict | None = None) -> dict:
    """Talks to the cache endpoints of the ACTIVE API server (blocking: call it with asyncio.to_thread)."""
    headers = {"X-Admin-Token": cfg.CACHE_ADMIN_TOKEN} if getattr(cfg, "CACHE_ADMIN_TOKEN", "") else {}
    r = requests.request(method, f"{API_BASE}/cache{path}", json=body, headers=headers, timeout=20)
    if r.status_code == 403:
        raise PermissionError("403")
    if r.status_code == 404:
        raise FileNotFoundError("404")
    r.raise_for_status()
    return r.json()

def _ago(seconds: int | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    return f"{seconds / 3600:.1f} h ago"

def _in(iso: str | None, now_iso: str) -> str:
    if not iso:
        return "—"
    secs = int((dt.datetime.fromisoformat(iso) - dt.datetime.fromisoformat(now_iso)).total_seconds())
    if secs <= 5:
        return "any moment"
    if secs < 90:
        return f"in {secs}s"
    if secs < 5400:
        return f"in {round(secs / 60)} min"
    return f"in {secs / 3600:.1f} h"

def _cache_error_text(err: Exception) -> str:
    if isinstance(err, PermissionError):
        return ("✕ The API server refused the change.\n"
                "Set the same <code>CACHE_ADMIN_TOKEN</code> in .env on the bot and on that server, then restart both.")
    if isinstance(err, FileNotFoundError):
        return "✕ This API server is too old to have cache settings. Update it first."
    return f"✕ Could not reach the API server.\n<code>{html.escape(str(err)[:150])}</code>"

def build_cache_view_sync(notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    back = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="admin_menu")]])
    head = [f"<b>API CACHE</b>", ADMIN_RULE, f"<b>Server</b>  ● {html.escape(_active_server_name())}", ""]
    try:
        st = _cache_call("GET")
    except Exception as e:
        return "\n".join(head + [_cache_error_text(e)]), back
    sett = st["settings"]
    iv, times = sett["interval_minutes"], sett["times"]
    lines = head
    if notice:
        lines += [notice, ""]
    lines += [
        f"<b>Every</b>      {f'{iv} min' if iv else '○ off'}",
        f"<b>At times</b>   {' · '.join(times) if times else '○ none'}  <i>(Tehran)</i>",
        f"<b>Next</b>       {_in(st.get('next_refresh'), st['now'])}",
        "",
    ]
    for key in ("today", "tomorrow", "weekly", "yesterday"):
        e = st["entries"][key]
        dot = "●" if e["current"] else "○"
        err = "  ✕ last try failed" if e.get("last_error") else ""
        lines.append(f"{dot} <b>{CACHE_LABELS[key]}</b>  {_ago(e['age_seconds'])} · {e['records']} events{err}")
    lines += ["", "<i>The bot always reads from this cache, so answers are instant. It survives restarts.</i>"]
    row = lambda items: [InlineKeyboardButton(("● " if n == iv else "") + f"{n}m", callback_data=f"admincache_i_{n}") for n in items]
    kb = [row(CACHE_INTERVALS[:3]), row(CACHE_INTERVALS[3:]) + [InlineKeyboardButton(("● " if not iv else "") + "Off", callback_data="admincache_i_0")],
          [InlineKeyboardButton("＋ Set times", callback_data="admincache_times"),
           InlineKeyboardButton("✕ Clear times", callback_data="admincache_clear")],
          [InlineKeyboardButton("⟳ Refresh now", callback_data="admincache_now"),
           InlineKeyboardButton("← Back", callback_data="admin_menu")]]
    return "\n".join(lines), InlineKeyboardMarkup(kb)

async def admin_cache_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """admin_cache (open) · admincache_i_<min> · admincache_times · admincache_clear · admincache_now"""
    q = update.callback_query
    if not is_admin(q.message.chat.id):
        await q.answer("Admins only.", show_alert=True)
        return
    data = q.data
    context.user_data.pop("awaiting", None)
    notice, toast = "", None
    try:
        if data.startswith("admincache_i_"):
            n = int(data.rsplit("_", 1)[1])
            await asyncio.to_thread(_cache_call, "POST", "/settings", {"interval_minutes": n})
            toast = f"✓ Every {n} min" if n else "○ Timed refresh is off"
        elif data == "admincache_clear":
            await asyncio.to_thread(_cache_call, "POST", "/settings", {"times": []})
            toast = "✓ Fixed times cleared"
        elif data == "admincache_now":
            await asyncio.to_thread(_cache_call, "POST", "/refresh")
            toast = "⟳ Refreshing… tap again in a few seconds"
        elif data == "admincache_times":
            context.user_data["awaiting"] = ADMIN_AWAIT_CACHE_TIMES
            await q.answer()
            await safe_edit(q.message,
                "<b>FIXED REFRESH TIMES</b>\n" + ADMIN_RULE +
                "\nSend the times (Tehran), any format, several at once:\n"
                "<code>8:30 14:00 20:15</code>\n\nThe cache refreshes at each of these times, every day.",
                InlineKeyboardMarkup([[InlineKeyboardButton("← Cancel", callback_data="admin_cache")]]))
            return
    except Exception as e:
        await q.answer()
        await safe_edit(q.message, _cache_error_text(e),
                        InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="admin_menu")]]))
        return
    await q.answer(toast)
    text, kb = await asyncio.to_thread(build_cache_view_sync, notice)
    await safe_edit(q.message, text, kb)

async def admin_cache_times_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The admin typed the fixed refresh times."""
    parts = [x for x in re.split(r"[\s,،;]+", (update.message.text or "").strip()) if x]
    times = [parse_hhmm(x) for x in parts]
    if not parts or None in times:
        bad = html.escape(parts[times.index(None)]) if parts and None in times else ""
        await update.message.reply_text(
            f"✕ Not a valid time{f': <code>{bad}</code>' if bad else ''}.\nSend like this: <code>8:30 14:00 20:15</code>",
            parse_mode=ParseMode.HTML)   # still waiting
        return
    try:
        await asyncio.to_thread(_cache_call, "POST", "/settings", {"times": sorted(set(times))})
    except Exception as e:
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(_cache_error_text(e), parse_mode=ParseMode.HTML)
        return
    context.user_data.pop("awaiting", None)
    text, kb = await asyncio.to_thread(build_cache_view_sync, "✓ <b>Saved.</b>")
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

def build_back_keyboard(to: str = "admin_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=to)]])

def build_servers_view() -> tuple[str, InlineKeyboardMarkup]:
    """One screen for servers: tap = make active, ✕ = delete, ＋ = add."""
    servers = list_api_servers()
    lines = ["<b>SERVERS</b>", ADMIN_RULE]
    kb = []
    if servers:
        lines.append("Tap a server to make it active.")
        for name, url in servers.items():
            active = url == API_BASE
            kb.append([
                InlineKeyboardButton(f"{'●' if active else '○'}  {name}", callback_data=f"adminuse_{name}"),
                InlineKeyboardButton("✕", callback_data=f"admindelq_{name}"),
            ])
        if API_BASE not in servers.values():
            lines.append(f"\n● default (.env)\n<code>{html.escape(API_BASE)}</code>")
    else:
        lines.append(f"Using the default from .env:\n<code>{html.escape(API_BASE)}</code>\n\nNo saved servers yet.")
    kb.append([
        InlineKeyboardButton("＋ Add server", callback_data="admin_add"),
        InlineKeyboardButton("← Back", callback_data="admin_menu"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(kb)



async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /admin opens the panel (buttons only, no other commands needed)."""
    cid = update.effective_chat.id
    if not is_admin(cid):
        return
    context.user_data.pop("awaiting", None)
    _clear_pending_restore(context)
    await update.message.reply_text(
        build_admin_menu_text(), reply_markup=build_admin_menu_keyboard(), parse_mode=ParseMode.HTML
    )

async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.message.chat.id):
        await q.answer("Admins only.", show_alert=True)
        return
    context.user_data.pop("awaiting", None)
    await q.answer()
    await safe_edit(q.message, build_admin_menu_text(), build_admin_menu_keyboard())

async def admin_servers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, toast: str | None = None):
    q = update.callback_query
    if not is_admin(q.message.chat.id):
        await q.answer("Admins only.", show_alert=True)
        return
    context.user_data.pop("awaiting", None)
    await q.answer(toast)
    await safe_edit(q.message, *build_servers_view())

async def admin_use_server_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global API_BASE
    q = update.callback_query
    if not is_admin(q.message.chat.id):
        await q.answer("Admins only.", show_alert=True)
        return
    name = q.data.split("_", 1)[1]
    servers = list_api_servers()
    if name not in servers:
        await admin_servers_callback(update, context, "That server no longer exists.")
        return
    already = servers[name] == API_BASE
    API_BASE = servers[name]
    set_config("api_base_url", API_BASE)
    await admin_servers_callback(update, context, f"{name} is already active" if already else f"● Switched to {name}")

async def admin_delete_ask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.message.chat.id):
        await q.answer("Admins only.", show_alert=True)
        return
    name = q.data.split("_", 1)[1]
    servers = list_api_servers()
    if name not in servers:
        await admin_servers_callback(update, context, "That server no longer exists.")
        return
    if servers[name] == API_BASE:
        await q.answer("This server is active. Switch to another one first.", show_alert=True)
        return
    await q.answer()
    await safe_edit(
        q.message,
        f"<b>DELETE SERVER</b>\n{ADMIN_RULE}\n{html.escape(name)}\n<code>{html.escape(servers[name])}</code>",
        InlineKeyboardMarkup([[
            InlineKeyboardButton("✕ Delete", callback_data=f"admindel_{name}"),
            InlineKeyboardButton("← Cancel", callback_data="admin_servers"),
        ]]),
    )

async def admin_delete_server_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.message.chat.id):
        await q.answer("Admins only.", show_alert=True)
        return
    name = q.data.split("_", 1)[1]
    if list_api_servers().get(name) == API_BASE:
        await q.answer("This server is active. Switch to another one first.", show_alert=True)
        return
    deleted = delete_api_server(name)
    await admin_servers_callback(update, context, f"✕ Deleted {name}" if deleted else "Not found")

async def admin_add_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.message.chat.id):
        await q.answer("Admins only.", show_alert=True)
        return
    await q.answer()
    context.user_data["awaiting"] = ADMIN_AWAIT_ADD_SERVER
    await safe_edit(
        q.message,
        f"<b>ADD SERVER</b>\n{ADMIN_RULE}\n"
        "Send it as one message:\n<code>name https://url</code>\n\n"
        "<i>Example</i>\n<code>iran https://iran.heysolo.ir/api/forex</code>",
        build_back_keyboard("admin_servers"),
    )

async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the admin's reply after tapping ＋ Add server."""
    cid = update.effective_chat.id
    if not is_admin(cid) or context.user_data.get("awaiting") != ADMIN_AWAIT_ADD_SERVER:
        return

    parts = update.message.text.strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[1].startswith(("http://", "https://")) or " " in parts[1]:
        await update.message.reply_text(
            "✕ Not recognised. Send it like this:\n<code>name https://url</code>",
            reply_markup=build_back_keyboard("admin_servers"), parse_mode=ParseMode.HTML
        )
        return   # still waiting, the admin can just send it again
    name, url = parts[0], parts[1].rstrip("/")
    if len(f"admindelq_{name}".encode()) > 64:
        await update.message.reply_text("✕ Name is too long, keep it short (e.g. iran, de1).",
                                        reply_markup=build_back_keyboard("admin_servers"))
        return
    context.user_data.pop("awaiting", None)
    save_api_server(name, url)
    text, kb = build_servers_view()
    await update.message.reply_text(f"✓ Saved {html.escape(name)}\n\n" + text, reply_markup=kb,
                                    parse_mode=ParseMode.HTML)

# ───────────────  Admin: backup & restore  ─────────────
def _admin_only(q) -> bool:
    return is_admin(q.message.chat.id)

def _fmt_counts(counts: dict) -> str:
    keys = ["subscribers", "users", "user_currencies", "api_servers", "bot_config"]
    parts = [f"{k}: <b>{counts[k]}</b>" for k in keys if k in counts]
    parts += [f"{k}: <b>{v}</b>" for k, v in counts.items() if k not in keys]
    return "\n".join(f"• {p}" for p in parts) or "• (empty)"

def _clear_pending_restore(context: ContextTypes.DEFAULT_TYPE):
    plan = context.user_data.pop("pending_restore", None)
    if plan is not None:
        plan.cleanup()

async def _send_backup_file(context: ContextTypes.DEFAULT_TYPE, chat_id: int, path: pathlib.Path, caption: str):
    with open(path, "rb") as fh:
        await context.bot.send_document(
            chat_id=chat_id, document=fh, filename=path.name,
            caption=caption, parse_mode=ParseMode.HTML,
        )

async def admin_backup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """⤓ Backup now: snapshot DB + .env into a zip and send it to the admin."""
    q = update.callback_query
    if not _admin_only(q):
        await q.answer("Admins only.", show_alert=True)
        return
    await q.answer("Creating backup…")
    await context.bot.send_chat_action(chat_id=q.message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)
    try:
        path = await asyncio.to_thread(bk.create_backup, "manual")
        counts = await asyncio.to_thread(bk.live_table_counts)
    except bk.BackupError as e:
        await q.message.reply_text(f"✕ {e}")
        return
    except Exception as e:
        logger.exception("Backup failed")
        await q.message.reply_text(f"✕ Backup failed: {e}")
        return
    caption = (
        f"<b>BACKUP</b>\n{ADMIN_RULE}\n"
        f"<code>{path.name}</code>\n\n{_fmt_counts(counts)}\n\n"
        + (f"▤ Download files included: {len(bk._build_files())}\n" if bk._build_files() else "") +
        "△ Contains your bot token (.env). Keep it private.\n"
        "To restore: /admin → ↺ Restore → send this file."
    )
    try:
        await _send_backup_file(context, q.message.chat.id, path, caption)
    except Exception as e:
        logger.exception("Sending backup failed")
        await q.message.reply_text(f"△ Backup saved on server as <code>{path.name}</code> but sending failed: {e}",
                                   parse_mode=ParseMode.HTML)
    await q.message.reply_text(build_admin_menu_text(), reply_markup=build_admin_menu_keyboard(),
                               parse_mode=ParseMode.HTML)

async def admin_restore_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """↺ Restore: wait for the admin to upload a backup file."""
    q = update.callback_query
    if not _admin_only(q):
        await q.answer("Admins only.", show_alert=True)
        return
    await q.answer()
    _clear_pending_restore(context)
    context.user_data["awaiting"] = ADMIN_AWAIT_RESTORE_FILE
    await q.message.edit_text(
        f"<b>RESTORE</b>\n{ADMIN_RULE}\n"
        "Send the backup <b>.zip</b> file here (or an old <code>forexbot.db</code>).\n\n"
        "Nothing changes until you confirm. A safety backup of the current data is "
        "taken automatically before restoring.",
        reply_markup=build_back_keyboard(), parse_mode=ParseMode.HTML
    )

async def _show_restore_preview(message, context: ContextTypes.DEFAULT_TYPE, plan: "bk.RestorePlan"):
    context.user_data["pending_restore"] = plan
    created = plan.manifest.get("created_at", "unknown")
    kind = "raw database file" if plan.manifest.get("label") == "raw-db" else "full backup"
    rows = [[InlineKeyboardButton("✓ Restore data", callback_data="adminrs_data")]]
    if plan.has_settings:
        rows.append([InlineKeyboardButton("✓ Data + settings (.env, restarts bot)", callback_data="adminrs_all")])
    rows.append([InlineKeyboardButton("✕ Cancel", callback_data="adminrs_cancel")])
    await message.reply_text(
        f"<b>BACKUP CHECKED</b>\n{ADMIN_RULE}\n✓ Looks good\n\n"
        f"Type: {kind}\nCreated: <code>{created}</code>\n"
        f"Settings included: {'yes' if plan.has_settings else 'no'}\n"
        f"Download files included: {len(plan.builds)}\n\n"
        f"{_fmt_counts(plan.counts)}\n\n"
        "△ Restoring <b>replaces</b> all current users, subscriptions and servers.",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML
    )

async def admin_document_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives the backup file after tapping ↺ Restore, or EA builds (EA versions)."""
    cid = update.effective_chat.id
    if context.user_data.get("awaiting") == AWAIT_BRIEF_TIME and update.message.text:
        await brief_time_text(update, context)
        return
    if context.user_data.get("awaiting") == channels.AWAIT_CHANNEL:
        await channel_input(update, context)
        return
    if str(context.user_data.get("awaiting") or "").startswith("chs_"):
        await channel_style.on_photo(update, context, get_user_language(cid))
        return
    if not is_admin(cid):
        return
    if context.user_data.get("awaiting") != ADMIN_AWAIT_RESTORE_FILE:
        if ea_admin.wants_document(update, context):
            await ea_admin.handle_document(update, context)
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > bk.MAX_RESTORE_BYTES:
        await update.message.reply_text("✕ File is larger than 20 MB, Telegram bots can't download it.")
        return
    context.user_data.pop("awaiting", None)
    _clear_pending_restore(context)

    incoming_dir = cfg.DATA_DIR / "incoming"
    incoming_dir.mkdir(parents=True, exist_ok=True)
    target = incoming_dir / f"upload-{update.message.message_id}.bin"
    await context.bot.send_chat_action(chat_id=cid, action=ChatAction.TYPING)
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(custom_path=str(target))
        plan = await asyncio.to_thread(bk.prepare_restore, target)
    except bk.BackupError as e:
        await update.message.reply_text(f"✕ {e}\n\nOpen /admin → ↺ Restore to try again.")
        return
    except Exception as e:
        logger.exception("Restore upload failed")
        await update.message.reply_text(f"✕ Couldn't read that file: {e}")
        return
    finally:
        target.unlink(missing_ok=True)
    await _show_restore_preview(update.message, context, plan)

def _restart_process():
    """Exit cleanly; systemd (Restart=always) brings the bot back with the new .env."""
    os.kill(os.getpid(), signal.SIGTERM)

async def _restart_job(context: ContextTypes.DEFAULT_TYPE):
    _restart_process()

async def admin_restore_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _admin_only(q):
        await q.answer("Admins only.", show_alert=True)
        return
    action = q.data.split("_", 1)[1]
    plan = context.user_data.get("pending_restore")
    if action == "cancel":
        _clear_pending_restore(context)
        await q.answer("Cancelled")
        await q.message.edit_text(build_admin_menu_text(), reply_markup=build_admin_menu_keyboard(),
                                  parse_mode=ParseMode.HTML)
        return
    if plan is None:
        await q.answer("This restore expired. Start again from ↺ Restore.", show_alert=True)
        return
    context.user_data.pop("pending_restore", None)
    await q.answer("Restoring…")
    await q.message.edit_text("Restoring, hang on…")
    include_settings = action == "all"
    restored_build_count = len(plan.builds)
    try:
        safety, settings_changed = await asyncio.to_thread(bk.apply_restore, plan, include_settings)
        ensure_db()              # add any tables/columns the old backup didn't have
        load_api_base_from_db()  # pick up the restored active server
        counts = await asyncio.to_thread(bk.live_table_counts)
    except bk.BackupError as e:
        plan.cleanup()
        await q.message.edit_text(f"✕ Restore failed: {e}\nYour current data was not changed.")
        return
    except Exception as e:
        plan.cleanup()
        logger.exception("Restore failed")
        await q.message.edit_text(
            f"✕ Restore failed: {e}\n\nIf anything looks off, use ▦ Saved backups → the "
            "latest <i>pre-restore</i> backup to roll back.", parse_mode=ParseMode.HTML)
        return

    text = (
        f"<b>RESTORE COMPLETE</b>\n{ADMIN_RULE}\n"
        + (f"▤ Download files restored: {restored_build_count}\n\n" if restored_build_count else "") +
        f"{_fmt_counts(counts)}\n\n"
        f"Active server: <code>{API_BASE}</code>\n"
    )
    if safety:
        text += f"\nPrevious data saved as <code>{safety.name}</code> (▦ Saved backups)."
    if include_settings and settings_changed:
        text += "\n\n↻ Settings restored, restarting the bot in a few seconds…"
        await q.message.edit_text(text, parse_mode=ParseMode.HTML)
        context.job_queue.run_once(_restart_job, when=3)
        return
    await q.message.edit_text(text, reply_markup=build_admin_menu_keyboard(), parse_mode=ParseMode.HTML)

async def admin_backups_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """▦ Saved backups: backups stored on the server (manual + pre-restore)."""
    q = update.callback_query
    if not _admin_only(q):
        await q.answer("Admins only.", show_alert=True)
        return
    await q.answer()
    backups = bk.list_backups()
    if not backups:
        await q.message.edit_text("No backups on the server yet. Tap ⤓ Backup now first.",
                                  reply_markup=build_back_keyboard())
        return
    kb = [[InlineKeyboardButton(_backup_label(p), callback_data=f"adminbk_{p.name}")] for p in backups]
    kb.append([InlineKeyboardButton("← Back", callback_data="admin_menu")])
    await q.message.edit_text(
        f"<b>SAVED BACKUPS</b>\n{ADMIN_RULE}\nKeeping the last {cfg.BACKUP_KEEP}. Tap one to send or restore it.",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML
    )

async def admin_backup_item_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _admin_only(q):
        await q.answer("Admins only.", show_alert=True)
        return
    prefix, name = q.data.split("_", 1)
    path = bk.resolve_backup_name(name)
    if path is None:
        await q.answer("That backup no longer exists.", show_alert=True)
        return
    if prefix == "adminbksend":
        await q.answer("Sending…")
        await _send_backup_file(context, q.message.chat.id, path, f"<code>{path.name}</code>")
        return
    if prefix == "adminbkrs":
        await q.answer()
        _clear_pending_restore(context)
        try:
            plan = await asyncio.to_thread(bk.prepare_restore, path)
        except bk.BackupError as e:
            await q.message.reply_text(f"✕ {e}")
            return
        await _show_restore_preview(q.message, context, plan)
        return
    # adminbk_<name>: details
    await q.answer()
    await q.message.edit_text(
        f"<b>BACKUP</b>\n{ADMIN_RULE}\n{_backup_label(path)}\n<code>{bk.describe(path)}</code>",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⤒ Send to me", callback_data=f"adminbksend_{path.name}"),
                InlineKeyboardButton("↺ Restore this", callback_data=f"adminbkrs_{path.name}"),
            ],
            [InlineKeyboardButton("← Back", callback_data="admin_backups")],
        ]),
        parse_mode=ParseMode.HTML
    )

def _subscribe_and_get_time(chat_id: int) -> str:
    on, time_ = get_brief(chat_id)
    return time_ if on else ("تنظیم نشده" if get_user_language(chat_id) == "fa" else "None")

async def channel_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await channels.on_message(update, context, get_user_language(update.effective_chat.id),
                              _subscribe_and_get_time, on_connected=calendar_after_connect)

async def brief_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The user typed a delivery time (7:23, 17:00 ...)."""
    cid = update.effective_chat.id
    language = get_user_language(cid, getattr(update.effective_user, "language_code", None))
    new = parse_hhmm(update.message.text)
    if not new:
        await update.message.reply_text(UI[language]['t_bad'], parse_mode=ParseMode.HTML)   # still waiting
        return
    set_brief_time(cid, new, language)
    hint = local_hint(language, new)
    notice = (f"✓ <b>ذخیره شد.</b> از الان هر روز کاری ساعت <b>{new}</b> ({tz.clock_note(language, cid)}) خبرها برات میاد."
              if language == "fa" else
              f"✓ <b>Saved.</b> From now on you get the news every weekday at <b>{new}</b> ({tz.clock_note(language, cid)}).")
    text, kb = brief_panel(language, cid, notice)
    sent = await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    remember_news_menu(cid, sent.message_id)
    old = context.user_data.pop("brief_msg", None)
    context.user_data["awaiting"] = AWAIT_BRIEF_TIME
    context.user_data["brief_msg"] = sent.message_id
    if old:
        try:
            await context.bot.delete_message(chat_id=cid, message_id=old)   # remove the old time screen
        except Exception:
            pass

async def photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A picture: the user logo for the channel look (only while waiting for one)."""
    if update.message is None or update.effective_chat.type != "private":
        return
    if str(context.user_data.get("awaiting") or "").startswith("chs_"):
        await channel_style.on_photo(update, context, get_user_language(update.effective_chat.id))

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes plain text / forwarded posts to whichever flow is waiting for input."""
    if update.message is None or update.effective_chat.type != "private":
        return
    action = KB_ACTIONS.get((update.message.text or "").strip())
    if action:
        context.user_data.pop("awaiting", None)   # a keyboard button cancels any pending input
        if action == 'admin':
            await admin_cmd(update, context)   # does nothing for non-admins
        elif action == 'menu':
            await menu_button_pressed(update, context)
        elif action == 'cal':
            await calendar_cmd(update, context)
        else:
            await reminders_cmd(update, context)
        return
    if context.user_data.get("awaiting") == channels.AWAIT_CHANNEL:
        await channel_input(update, context)
        return
    if str(context.user_data.get("awaiting") or "").startswith("chs_"):
        await channel_style.on_wrong_input(update, context, get_user_language(update.effective_chat.id))
        return
    if str(context.user_data.get("awaiting") or "").startswith("rem_") and update.message.text:
        await reminders.on_text(update, context, get_user_language(update.effective_chat.id))
        return
    if (context.user_data.get("awaiting") == ADMIN_AWAIT_CACHE_TIMES and update.message.text
            and is_admin(update.effective_chat.id)):
        await admin_cache_times_input(update, context)
        return
    if context.user_data.get("awaiting") == AWAIT_BRIEF_TIME and update.message.text:
        await brief_time_text(update, context)   # waiting for a time: parse it, or explain the right format
        return
    if re.fullmatch(r"\s*\d{1,2}\s*[:.٫/،,]\s*\d{1,2}\s*", (update.message.text or "").translate(
            str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))) and parse_hhmm(update.message.text):
        await brief_time_text(update, context)   # a bare time like 16:53, even if the time screen was reset by a restart
        return
    if update.message.text:
        await admin_text_input(update, context)

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Returns the chat id — useful for setting ADMIN_CHAT_ID."""
    await update.message.reply_text(f"ID Chat ID: <code>{update.effective_chat.id}</code>", parse_mode=ParseMode.HTML)

# ───────────────  Commands  ─────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id

    # ---------- بررسی عضویت اولیه برای اطلاع به ادمین ----------
    conn = _db()
    cur  = conn.cursor()
    cur.execute("INSERT INTO users (chat_id) VALUES (?) ON CONFLICT DO NOTHING", (cid,))
    first_time = cur.rowcount == 1
    conn.commit()

    if first_time:
        # ارسال پیام به ادمین
        user = update.effective_user
        # in the background: the user shouldn't wait for the admin message
        context.application.create_task(notify_admin(context, cid, {
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name
        }))

    conn.close()

    # ---------- بررسی زبان کاربر ----------
    language = get_stored_language(cid)
    if language is None:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("◍ فارسی", callback_data="setlang_fa"),
                InlineKeyboardButton("◍ English", callback_data="setlang_en")
            ]
        ])
        await update.message.reply_text(
            "Please select your language:\nلطفاً زبان خود را انتخاب کنید:",
            reply_markup=keyboard
        )
        return

    await show_main_menu_in_place(update.message, context, cid, language)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_typing(context, update.effective_chat.id)
    cid = update.effective_chat.id
    language = get_user_language(cid)
    
    messages = {
        'fa': (
            "<b>▮ راهنما</b>\n\n"
            "این ربات خبرهای اقتصادی مهم بازار رو (از سایت Forex Factory) نشونت میده.\n\n"
            "<b>چطور استفاده کنم؟</b>\n"
            "1⃣ «امروز»، «فردا» یا «این هفته» رو بزن تا خبرها رو ببینی.\n"
            "2⃣ «انتخاب ارز و اهمیت» رو بزن و ارزهایی که برات مهمه رو روشن کن.\n"
            "3⃣ «ارسال خودکار» رو بزن تا هر روز کاری خبرها خودکار برات بیاد. ساعتش رو خودت می‌نویسی (مثلاً <code>7:23</code>).\n"
            "داخل «ارسال خودکار» یکی از دو روش رو انتخاب می‌کنی: «یک بار در روز» (ساعتش با خودته) یا «۳۰ دقیقه قبل از هر خبر».\n\n"
            "△ همه‌ی ساعت‌ها <b>{zone}</b> هستن (از «🌍 منطقه زمانی» عوضش کن).\n\n"
            "▦ «تقویم»: تاریخ امروز، شمسی و میلادی.\n"
            "◷ «یادآورها»: یه یادآور برای خودت بساز."
        ),
        'en': (
            "<b>▮ Help</b>\n\n"
            "This bot shows you the important economic news from Forex Factory.\n\n"
            "<b>How do I use it?</b>\n"
            "1⃣ Tap “Today”, “Tomorrow” or “This week” to see the news.\n"
            "2⃣ Tap “Choose currencies & importance” and turn on the currencies you care about.\n"
            "3⃣ Tap “Auto-send” to get the news automatically every weekday. You type the time yourself (e.g. <code>7:23</code>).\n"
            "Inside “Auto-send” you choose one way: “Once a day” (you pick the time) or “30 min before each news”.\n\n"
            "△ All times are <b>{zone}</b> (change it from “🌍 Time zone”).\n\n"
            "▦ “Calendar”: today's date.\n"
            "◷ “Reminders”: create a reminder for yourself."
        ),
    }
    
    labels = KB_LABELS[language]
    help_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(labels['menu'], callback_data="main_menu")],
        [InlineKeyboardButton(labels['cal'], callback_data="cal"),
         InlineKeyboardButton(labels['rem'], callback_data="rem")],
    ])
    await update.message.reply_text(messages[language].replace("{zone}", tz.clock_note(language, cid)),
                                    reply_markup=help_keyboard, parse_mode=ParseMode.HTML)

async def show_brief(update: Update, context: ContextTypes.DEFAULT_TYPE, toast: str | None = None):
    """Daily High-Impact News panel: edits the message when coming from a button, sends a new one for commands."""
    cid = update.effective_chat.id
    language = get_user_language(cid, getattr(update.effective_user, "language_code", None))
    text, kb = brief_panel(language, cid)
    q = update.callback_query
    context.user_data["awaiting"] = AWAIT_BRIEF_TIME      # the panel is waiting for a typed time
    if q:
        context.user_data["brief_msg"] = q.message.message_id
        await q.answer(toast)
        await safe_edit(q.message, text, kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    language = get_user_language(cid, getattr(update.effective_user, "language_code", None))
    set_brief_on(cid, True, language)
    await show_brief(update, context, UI[language]['toast_on'].format(time=get_brief(cid)[1]))

async def unsubscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    language = get_user_language(cid, getattr(update.effective_user, "language_code", None))
    set_brief_on(cid, False, language)
    await show_brief(update, context, UI[language]['toast_off'])


async def brief_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """All Daily High-Impact News buttons: brief, brief_toggle, brief_time, brief_h_HH, brief_m_MM."""
    q = update.callback_query
    cid = q.message.chat.id
    language = get_user_language(cid, getattr(q.from_user, "language_code", None))
    t = UI[language]
    data = q.data
    if data == "brief":
        context.user_data.pop("awaiting", None)
        await show_brief(update, context)
        return
    if data.startswith("brief_set_"):
        new = parse_hhmm(data[len("brief_set_"):])
        if new:
            set_brief_time(cid, new, language)
            context.user_data.pop("awaiting", None)
            await show_brief(update, context, t['toast_time'].format(time=new))
        else:
            await q.answer()
        return
    if data in ("brief_toggle", "brief_time"):
        await show_brief(update, context)
        return
    if data in ("brief_mode_before", "brief_alert_toggle"):
        if get_pre_news(cid) and not get_brief(cid)[0]:      # already chosen: tapping again turns it off
            set_pre_news(cid, False, language)
            await show_brief(update, context, "◌ خاموش شد" if language == "fa" else "◌ Turned off")
            return
        set_brief_on(cid, False, language)          # the two options exclude each other
        set_pre_news(cid, True, language)
        await show_brief(update, context, "◴ از الان ۳۰ دقیقه قبل از هر خبر برات میاد" if language == "fa"
                         else "◴ You'll get each news 30 minutes before it is released")
        return
    if data == "brief_off_all":
        set_brief_on(cid, False, language)
        set_pre_news(cid, False, language)
        await show_brief(update, context, "◌ خاموش شد" if language == "fa" else "◌ Turned off")
        return
    if data == "brief_none":
        set_brief_on(cid, False, language)
        await q.answer("◷ ارسال روزانه خاموش شد" if language == "fa" else "◷ Daily delivery off")
        context.user_data.pop("awaiting", None)
        await show_brief(update, context)
        return
    if data.startswith(("brief_h_", "brief_m_")):
        _, part, val = data.split("_")
        h, m = get_brief(cid)[1].split(":")
        new = f"{val}:{m}" if part == "h" else f"{h}:{val}"
        set_brief_time(cid, new, language)
        await q.answer(t['toast_time'].format(time=new))
    else:  # brief_time
        await q.answer()
    text, kb = brief_time_picker(language, cid)
    await safe_edit(q.message, text, kb)



def caption_date_lines(language: str, kinds: list[str], d: dt.date) -> list[str]:
    """Compact dates for the picture caption: «پنج‌شنبه ۷ آبان ۱۴۰۵» + «[ میلادی: ۲۹ اکتبر ۲۰۲۶]»."""
    if language != "fa":
        return [f"<b>{gregorian_str(d, language)}</b>"]
    if "jalali" in kinds:
        lines = [f"<b>{jalali_str(d, language)}</b>"]
        if "gregorian" in kinds:
            lines.append(f"[ {CAL_UI[language]['gregorian'].split(' ', 1)[-1]}: {fa_num(d.day)} "
                         f"{GREGORIAN_MONTHS['fa'][d.month - 1]} {fa_num(d.year)}]")
        return lines
    return [f"<b>{gregorian_str(d, language)}</b>"]

def news_caption(events: list[dict], language: str, kinds: list[str], limit: int = 1000,
                 cid: int | None = None) -> str:
    """Caption for the calendar picture: title + dates + ONLY the important (High) news in a quote block."""
    day = tz.today_for(language, cid)
    c = tz.city(tz.zone_name(language, cid), language)
    title = {"fa": f"خبرهای اقتصادی امروز به وقت {c}", "en": f"Today's Economic News, {c} Time"}[language]
    header = "\n".join([f"<b>▤ {title}</b>", "", *caption_date_lines(language, kinds, day)])
    high = [tz.localize_event(e, language, cid) for e in events if e.get("Impact", "High") == "High"]
    if not high:
        lines = ["بدون خبر مهم 🔴 برای ارزهای انتخابی." if language == "fa" else "No high-impact 🔴 news for your currencies."]
    else:
        lines = [f"🔴 {html.escape(str(e.get('Currency', '')), quote=False)} · "
                 f"{html.escape(str(e.get('Time', '')), quote=False)} — "
                 f"{html.escape(str(e.get('Event', ''))[:70], quote=False)}" for e in high]
    shown, used = [], len(header) + 2 + len("<blockquote expandable></blockquote>") + 2
    for line in lines:
        if used + len(line) + 1 > limit:
            shown.append("…")
            break
        shown.append(line)
        used += len(line) + 1
    tag = "<blockquote expandable>" if len(shown) > 6 else "<blockquote>"
    return header + "\n\n" + tag + "\n".join(shown) + "</blockquote>"

async def build_news_images(cid: int, events: list[dict], impacts: list[str], language: str, channel_id: int | None = None):
    """(png list, caption) in that channel's look, or None to fall back to the text news."""
    try:
        pngs = await asyncio.to_thread(channel_style.render_png, cid, tz.localize_events(events, language, cid), impacts,
                                       channel_id, tz.zone_name(language, cid), language)
    except Exception:
        logger.exception("Calendar picture for %s failed; sending text instead", cid)
        return None
    return pngs, news_caption(events, language, get_cal(cid)["kinds"], cid=cid)

async def send_news_images(bot, target, pngs: list[bytes], caption: str):
    if len(pngs) == 1:
        await bot.send_photo(target, io.BytesIO(pngs[0]), caption=caption, parse_mode=ParseMode.HTML)
        return
    media = [InputMediaPhoto(io.BytesIO(p), caption=caption if i == 0 else None,
                             parse_mode=ParseMode.HTML if i == 0 else None) for i, p in enumerate(pngs)]
    await bot.send_media_group(target, media)

async def digest_loop(context: ContextTypes.DEFAULT_TYPE):
    """Daily news: sent when the time the user set is now ON THEIR OWN CLOCK (their time zone: automatic from the
    language, or the one they picked), Monday to Friday of that clock."""
    subs_map = load_subs()
    if not subs_map:
        return
    langs = _all_languages()
    clocks = tz.bulk_now({c_: ("fa" if langs.get(c_) == "fa" else "en") for c_ in subs_map})   # each user's own clock
    recipients = []
    for cid_, t in subs_map.items():
        now_ = clocks[cid_]
        if t == now_.strftime("%H:%M") and now_.weekday() < 5:
            recipients.append(cid_)

    if not recipients:
        return

    for cid in recipients:
        try:
            language = get_user_language(cid)
            allowed = get_user_currencies(cid)
            impacts = get_user_impacts(cid)
            try:
                events = await asyncio.to_thread(get_events_with_retry, tz.endpoint_for("today", language, cid), allowed, impacts)
            except Exception:
                logger.exception("Digest news fetch for %s failed after 3 tries: nothing is sent", cid)
                continue      # fetch failed: send NOTHING (no error message to the channel or the chat)
            # events == [] is NOT a failure: news_pages() then builds the dedicated "no news" message
            texts = news_pages("today", events, allowed, impacts, language, get_cal(cid)["kinds"], cid=cid)

            connected = channels.get_channels(cid)
            succeeded = False
            images_by_channel = {}      # every channel has its own look; same look = rendered once
            if connected and events:
                rendered = {}
                for ch in connected:
                    look = channel_style.style_key(cid, ch["id"])
                    if look[0] != "image":
                        continue
                    if look not in rendered:
                        rendered[look] = await build_news_images(cid, events, impacts, language, ch["id"])   # None = text
                    images_by_channel[ch["id"]] = rendered[look]
            for ch in connected:
                try:
                    images = images_by_channel.get(ch["id"])
                    if images:
                        await send_news_images(context.bot, ch["id"], *images)
                    else:
                        for text in texts:
                            await send_news_message(context.bot, ch["id"], text)
                    succeeded = True
                except (Forbidden, BadRequest) as e:
                    logger.warning("Channel %s of %s failed: %s", ch["id"], cid, e)
                    await channels.notify_failed(context, cid, language, ch)
            if not connected or not succeeded:
                for i, text in enumerate(texts):
                    await send_news_message(context.bot, cid, text,
                                            menu_kb(language, cid) if i == len(texts) - 1 else None)
        except Exception as e:
            logger.warning(f"Digest for {cid} failed: {e}")

def pre_news_event_key(event: dict, release_ts: int) -> str:
    """Stable across value/forecast changes; recipient ID is stored separately."""
    identity = [release_ts, str(event.get("Currency", "")), str(event.get("Event", "")).strip()]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()

async def pre_news_loop(context: ContextTypes.DEFAULT_TYPE):
    """Send selected events in the minute beginning 30 minutes before release."""
    with closing(_db()) as conn:
        recipients = [row[0] for row in conn.execute(
            "SELECT chat_id FROM subscribers WHERE pre_news_on = 1").fetchall()]
    if not recipients:
        return
    now = datetime.now(NY_TZ)
    upcoming = now + dt.timedelta(minutes=30)
    endpoint = "tomorrow" if upcoming.date() != now.date() else "today"
    try:
        events = await asyncio.to_thread(get_events_with_retry, endpoint,
                                         ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "CNY"],
                                         list(IMPACTS), 3, (2, 4))
    except Exception:
        logger.exception("30-minute news alert fetch failed")
        return
    # Fetching can take time: use the actual time when checking the due window.
    now = datetime.now(NY_TZ)
    if (now + dt.timedelta(minutes=30)).date() != upcoming.date():
        return  # next tick fetches the right day
    due = []
    for event in events:
        release = event_timestamp(event, upcoming.date())
        if release is None:
            continue  # All Day / unknown time cannot safely be scheduled.
        seconds_to_release = release - now.timestamp()
        if 29 * 60 < seconds_to_release <= 30 * 60:
            due.append((event, release, pre_news_event_key(event, release)))
    if not due:
        return
    for cid in recipients:
        try:
            if not get_pre_news(cid):
                continue
            currencies, impacts = get_user_currencies(cid), get_user_impacts(cid)
            with closing(_db()) as conn:
                legacy_sent = {r[0] for r in conn.execute(
                    "SELECT event_key FROM pre_news_sent WHERE chat_id = ?", (cid,)).fetchall()}
            groups = {}
            for event, release, key in due:
                if event.get("Currency") in currencies and event.get("Impact", "High") in impacts and key not in legacy_sent:
                    groups.setdefault((event.get("Currency"), release), {})[key] = event
            language = get_user_language(cid)
            connected = channels.get_channels(cid)
            for (currency, _), keyed_events in groups.items():
                items = list(keyed_events.items())
                # Small batches stay within one Telegram message and can be marked independently.
                for start in range(0, len(items), 3):
                    chunk = items[start:start + 3]
                    title = ("◴ <b>۳۰ دقیقه تا انتشار خبر</b>" if language == "fa"
                             else "◴ <b>News in 30 minutes</b>")
                    pages = news_pages(endpoint, [event for _, event in chunk], [currency],
                                       impacts, language, get_cal(cid)["kinds"],
                                       day=now.date(), limit=TG_LIMIT - len(title) - 2, cid=cid)
                    if len(pages) != 1:
                        logger.error("Unexpected multi-page pre-news alert for %s; skipping to avoid duplicates", cid)
                        continue
                    text = title + "\n\n" + pages[0]
                    targets = [(ch["id"], ch) for ch in connected] or [(cid, None)]
                    successes = 0
                    for target, ch in targets:
                        with closing(_db()) as conn:
                            already = {r[0] for r in conn.execute(
                                "SELECT event_key FROM pre_news_deliveries WHERE chat_id = ? AND target_id = ?",
                                (cid, target)).fetchall()}
                        pending = [(key, ev) for key, ev in chunk if key not in already]
                        if not pending:
                            successes += 1
                            continue
                        if len(pending) != len(chunk):
                            partial = news_pages(endpoint, [ev for _, ev in pending], [currency],
                                                 impacts, language, get_cal(cid)["kinds"],
                                                 day=now.date(), limit=TG_LIMIT - len(title) - 2, cid=cid)
                            if len(partial) != 1:
                                continue
                            target_text = title + "\n\n" + partial[0]
                        else:
                            target_text = text
                        try:
                            await send_news_message(context.bot, target, target_text,
                                                    menu_kb(language, cid) if target == cid else None)
                            with closing(_db()) as conn, conn:
                                conn.executemany(
                                    "INSERT INTO pre_news_deliveries (chat_id, event_key, target_id, sent_date) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                                    [(cid, key, target, upcoming.date().isoformat()) for key, _ in pending])
                            successes += 1
                        except (Forbidden, BadRequest) as exc:
                            if not ch:
                                raise
                            logger.warning("30-minute alert channel %s failed: %s", target, exc)
                            await channels.notify_failed(context, cid, language, ch)
                    if connected and not successes:
                        # Fallback only once; keep each channel eligible for a later retry.
                        with closing(_db()) as conn:
                            already = {r[0] for r in conn.execute(
                                "SELECT event_key FROM pre_news_deliveries WHERE chat_id = ? AND target_id = ?",
                                (cid, cid)).fetchall()}
                        pending = [(key, ev) for key, ev in chunk if key not in already]
                        if pending:
                            private_pages = news_pages(endpoint, [ev for _, ev in pending], [currency],
                                                       impacts, language, get_cal(cid)["kinds"],
                                                       day=now.date(), limit=TG_LIMIT - len(title) - 2, cid=cid)
                            if len(private_pages) == 1:
                                await send_news_message(context.bot, cid, title + "\n\n" + private_pages[0],
                                                        menu_kb(language, cid))
                                with closing(_db()) as conn, conn:
                                    conn.executemany(
                                        "INSERT INTO pre_news_deliveries (chat_id, event_key, target_id, sent_date) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                                        [(cid, key, cid, upcoming.date().isoformat()) for key, _ in pending])
        except Exception:
            logger.exception("30-minute news alert delivery failed for %s", cid)
    with closing(_db()) as conn, conn:
        conn.execute("DELETE FROM pre_news_sent WHERE sent_date < ?",
                     ((now.date() - dt.timedelta(days=7)).isoformat(),))
        conn.execute("DELETE FROM pre_news_deliveries WHERE sent_date < ?",
                     ((now.date() - dt.timedelta(days=7)).isoformat(),))
        
def tz_settings_view(language: str, cid: int) -> tuple[str, InlineKeyboardMarkup]:
    """🌍 Language + time zone on one screen. 'Automatic' (the default) = the zone that goes with the language."""
    fa = language == "fa"
    auto_on, cur = tz.is_auto(cid), tz.zone_name(language, cid)
    if fa:
        text = ("🌍 <b>زبان و منطقه زمانی</b>\n\n"
                f"زبان: <b>فارسی</b>\nمنطقه زمانی: <b>{tz.status_label(language, cid)}</b>\n\n"
                "ساعت ارسال خبرها، ساعت خبرها، تاریخ و یادآورها همه به وقت همین منطقه‌ست.\n"
                "«خودکار» یعنی منطقه با زبان عوض می‌شه (فارسی ← تهران، English ← نیویورک).")
    else:
        text = ("🌍 <b>Language & time zone</b>\n\n"
                f"Language: <b>English</b>\nTime zone: <b>{tz.status_label(language, cid)}</b>\n\n"
                "Send times, news times, dates and reminders all use this zone.\n"
                "“Automatic” follows the language (Persian → Tehran, English → New York).")
    rows = [[InlineKeyboardButton(("✓ " if fa else "") + "◍ فارسی", callback_data="tzs_lang_fa"),
             InlineKeyboardButton(("" if fa else "✓ ") + "◍ English", callback_data="tzs_lang_en")],
            [InlineKeyboardButton(("✓ " if auto_on else "") + ("🔄 خودکار · " if fa else "🔄 Automatic · ")
                                  + tz.city(tz.auto_name(language), language), callback_data="tzs_auto")]]
    btns = [InlineKeyboardButton(("✓ " if (name == cur and not auto_on) else "") + (fa_ if fa else en_),
                                 callback_data=f"tzs_z_{i}")
            for i, (name, fa_, en_) in enumerate(tz.TIMEZONES)]
    rows += [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton("‹ بازگشت" if fa else "‹ Back", callback_data="main_menu")])
    return text, InlineKeyboardMarkup(rows)

async def tz_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cid = q.message.chat.id
    language = get_user_language(cid, getattr(q.from_user, "language_code", None))
    data, toast = q.data, None
    if data.startswith("tzs_lang_"):
        new = data.rsplit("_", 1)[1]
        if new in LANGS:
            set_user_language(cid, new)         # also moves an automatic zone with the language
            language = new
            toast = {"fa": "✓ زبان: فارسی", "en": "✓ Language: English"}[new] + " · 🌍 " + tz.status_label(new, cid)
    elif data == "tzs_auto":
        tz.set_zone(cid, None, language)
        toast = "✓ 🌍 " + tz.status_label(language, cid)
    elif data.startswith("tzs_z_"):
        try:
            i = int(data[len("tzs_z_"):])
        except ValueError:
            i = -1
        if 0 <= i < len(tz.TIMEZONES):
            tz.set_zone(cid, tz.TIMEZONES[i][0], language)
            toast = "✓ 🌍 " + tz.status_label(language, cid)
    await q.answer(toast)
    await safe_edit(q.message, *tz_settings_view(language, cid))

async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    if context.user_data.get("awaiting") == AWAIT_BRIEF_TIME and not data.startswith("brief_"):
        context.user_data.pop("awaiting", None)
    if (data in ("choose_currencies", "currencies_done", "main_menu", "my_channel",
                 "today", "yesterday", "tomorrow", "week", "brief", "subscribe", "unsubscribe")
            or data.startswith(("news_page_", "togglecur_", "toggleimp_", "brief_", "setlang_",
                                "tzs", "chan_", "chs"))):
        cid, message_id = q.message.chat.id, q.message.message_id
        remember_news_menu(cid, message_id)
        revisions = context.chat_data.setdefault("news_revisions", {})
        revisions[message_id] = revisions.get(message_id, 0) + 1
    if data == "tzs" or data.startswith("tzs_"):
        await tz_settings_callback(update, context)
        return
    if data.startswith("setlang_"):
        await set_language(update, context)
        return
    if data == "change_language":
        language = get_user_language(q.message.chat.id)
        messages = {
            'fa': "لطفاً زبان خود را انتخاب کنید:",
            'en': "Please select your language:"
        }
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("◍ فارسی", callback_data="setlang_fa"),
                InlineKeyboardButton("◍ English", callback_data="setlang_en")
            ]
        ])
        await q.message.edit_text(messages[language], reply_markup=keyboard)
        await q.answer()
        return
    if data == "choose_currencies":
        await choose_currency_keyboard(update, context)
        return
    if data.startswith("news_page_"):
        await q.answer()
        cid = q.message.chat.id
        language = get_user_language(cid, getattr(q.from_user, "language_code", None))
        pages = context.chat_data.get("news_pages", {}).get(q.message.message_id)
        selected = context.chat_data.get("news_selected", {}).get(q.message.message_id)
        try:
            page = int(data.rsplit("_", 1)[1])
        except ValueError:
            page = -1
        if pages and 0 <= page < len(pages):
            await safe_edit(q.message, pages[page], news_page_keyboard(language, page, len(pages), cid, selected))
        else:
            expired = "این صفحه منقضی شده؛ دوباره بازش کن." if language == "fa" else "This page expired; open it again."
            await safe_edit(q.message, main_menu_text(language, cid) + "\n\n" + expired,
                            news_page_keyboard(language, 0, 1, cid, selected))
        return
    if data.startswith("toggleimp_"):
        await toggle_impact(update, context)
        return
    if data.startswith("togglecur_"):
        await toggle_currency(update, context)
        return
    if data == "brief" or data.startswith("brief_"):
        await brief_callback(update, context)
        return
    if data == "cal":
        await q.answer()
        cid = q.message.chat.id
        if context.user_data.get("awaiting") == channels.AWAIT_CHANNEL:     # cancelled "add channel" from the calendar
            context.user_data.pop("awaiting", None)
            context.user_data.pop("chan_back", None)
        await safe_edit(q.message, *calendar_panel(get_user_language(cid, getattr(q.from_user, "language_code", None)), cid))
        return
    if data.startswith("cal_"):
        await calendar_callback(update, context)
        return
    if data == "rem" or data.startswith("rem_"):
        await reminders.on_callback(update, context,
                                    get_user_language(q.message.chat.id, getattr(q.from_user, "language_code", None)))
        return
    # Buttons from old menus still in users' chat history → new Daily High-Impact News panel
    if data == "subscribe":
        await subscribe_cmd(update, context)
        return
    if data == "unsubscribe":
        await unsubscribe_cmd(update, context)
        return
    if data in ("choose_time", "settime") or data.startswith("settime_"):
        if data.startswith("settime_"):
            _, h, m = data.split("_")
            set_brief_time(q.message.chat.id, f"{h}:{m}")
        await show_brief(update, context)
        return
    if data in ("currencies_done", "main_menu"):
        awaiting = str(context.user_data.get("awaiting") or "")
        if awaiting == channels.AWAIT_CHANNEL or awaiting.startswith(("rem_", "chs_")):
            context.user_data.pop("awaiting", None)
        await back_to_main_menu(update, context)
        return
    if data == "my_channel":
        await channels.show(update, context, get_user_language(q.message.chat.id))
        return
    if data in ("chan_remove", "chan_change") or data.startswith(("chan_add", "chan_remove_")):
        await channels.on_callback(update, context, get_user_language(q.message.chat.id))
        return
    if data == "chs" or data.startswith(("chs_", "chs:")):
        await channel_style.on_callback(update, context, get_user_language(q.message.chat.id))
        return
    if data == "admin_menu":
        await admin_menu_callback(update, context)
        return
    if data == "admin_restartmsg":
        await admin_restartmsg_callback(update, context)
        return
    if data.startswith("ea:"):
        if not is_admin(q.message.chat.id):
            await q.answer("Admins only.", show_alert=True)
            return
        await ea_admin.handle_callback(update, context)
        return
    if data == "admin_cache" or data.startswith("admincache_"):
        await admin_cache_callback(update, context)
        return
    if data in ("admin_servers", "admin_switch"):
        await admin_servers_callback(update, context)
        return
    if data == "admin_add":
        await admin_add_menu_callback(update, context)
        return
    if data == "admin_delete":   # button from old panels
        await admin_servers_callback(update, context)
        return
    if data.startswith("admindelq_"):
        await admin_delete_ask_callback(update, context)
        return
    if data.startswith("adminuse_"):
        await admin_use_server_callback(update, context)
        return
    if data.startswith("admindel_"):
        await admin_delete_server_callback(update, context)
        return
    if data == "admin_backup":
        await admin_backup_callback(update, context)
        return
    if data == "admin_restore":
        await admin_restore_menu_callback(update, context)
        return
    if data == "admin_backups":
        await admin_backups_list_callback(update, context)
        return
    if data.startswith(("adminbk_", "adminbksend_", "adminbkrs_")):
        await admin_backup_item_callback(update, context)
        return
    if data.startswith("adminrs_"):
        await admin_restore_confirm_callback(update, context)
        return
    await q.answer()
    mapping = {"yesterday": yesterday, "today": today, "tomorrow": tomorrow, "week": week}
    if data in mapping:
        fake_update = Update(update.update_id, message=q.message, callback_query=q)
        await mapping[data](fake_update, context)

async def yesterday(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await fetch_and_send(update, ctx, "yesterday", "")

async def today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    language = get_user_language(cid)
    await fetch_and_send(update, ctx, "today", "Today's Economic News" if language == 'en' else "خبرهای اقتصادی امروز")

async def tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    language = get_user_language(cid)
    await fetch_and_send(update, ctx, "tomorrow", "Tomorrow's Economic News" if language == 'en' else "خبرهای اقتصادی فردا")

async def week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    language = get_user_language(cid)
    await fetch_and_send(update, ctx, "weekly", "This Week's Economic News" if language == 'en' else "خبرهای اقتصادی این هفته")
    
IMPACT_LABEL = {
    'fa': {"High": "🔴 زیاد", "Medium": "🟠 متوسط", "Low": "🟡 کم"},
    'en': {"High": "🔴 High", "Medium": "🟠 Medium", "Low": "🟡 Low"},
}
CURRENCY_SCREEN = {
    'fa': "⇄ <b>ارزها</b>\nروی ارزهایی که برات مهمه بزن (✓ یعنی روشن). فقط خبر همین ارزها میاد.\n\n"
          "≡ <b>اهمیت خبر</b>\n🔴 زیاد = خبرهای خیلی مهم که بازار رو تکون میدن (پیشنهاد ما)\n"
          "🟠 متوسط · 🟡 کم\nهر کدوم رو روشن کنی، اون خبرها هم برات میاد.\n\n"
          "وقتی تموم شد، «ذخیره و بازگشت» رو بزن.",
    'en': "⇄ <b>Currencies</b>\nTap the currencies you care about (✓ = on). You only get news for these.\n\n"
          "≡ <b>Importance</b>\n🔴 High = very important news that moves the market (recommended)\n"
          "🟠 Medium · 🟡 Low\nTurn on any level to get those news too.\n\n"
          "When you're done, tap “Save and Return”.",
}

def build_currency_keyboard(selected: list[str], language: str = 'en',
                            impacts: list[str] | None = None) -> InlineKeyboardMarkup:
    impacts = impacts or ["High"]
    all_currencies = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "CNY"]
    kb = []
    row = []
    for i, cur in enumerate(all_currencies):
        label = f"✓ {cur}" if cur in selected else cur
        row.append(InlineKeyboardButton(label, callback_data=f"togglecur_{cur}"))
        if (i + 1) % 3 == 0:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([
        InlineKeyboardButton((f"✓ {IMPACT_LABEL[language][lvl]}" if lvl in impacts else IMPACT_LABEL[language][lvl]),
                             callback_data=f"toggleimp_{lvl}")
        for lvl in IMPACTS
    ])
    save_button = {
        'fa': "‹ ذخیره و بازگشت",
        'en': "‹ Save and Return"
    }
    kb.append([InlineKeyboardButton(save_button[language], callback_data="currencies_done")])
    return InlineKeyboardMarkup(kb)

def get_user_impacts(chat_id: int) -> list[str]:
    try:
        with closing(_db()) as conn:
            row = conn.execute("SELECT impacts FROM user_currencies WHERE chat_id = ?", (chat_id,)).fetchone()
        levels = [x for x in (row[0] or "").split(",") if x in IMPACTS] if row else []
        return levels or ["High"]
    except Exception as e:
        logger.error(f"Error in get_user_impacts for chat_id {chat_id}: {e}")
        return ["High"]

def set_user_impacts(chat_id: int, impacts: list[str]):
    value = ",".join(lvl for lvl in IMPACTS if lvl in impacts) or "High"
    with closing(_db()) as conn, conn:
        conn.execute(
            """INSERT INTO user_currencies (chat_id, currencies, impacts) VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET impacts = excluded.impacts""",
            (chat_id, ",".join(get_user_currencies(chat_id)), value))

async def toggle_impact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cid = q.message.chat.id
    language = get_user_language(cid, getattr(q.from_user, "language_code", None))
    level = q.data.split("_", 1)[1]
    if level not in IMPACTS:
        await q.answer()
        return
    impacts = get_user_impacts(cid)
    if level in impacts:
        if len(impacts) == 1:
            await q.answer("حداقل یکی باید روشن بمونه" if language == 'fa' else "Keep at least one on", show_alert=False)
            return
        impacts.remove(level)
    else:
        impacts.append(level)
    set_user_impacts(cid, impacts)
    await q.answer(IMPACT_LABEL[language][level] + (" ✓" if level in impacts else " ✕"))
    await safe_edit(q.message, CURRENCY_SCREEN[language],
                    build_currency_keyboard(get_user_currencies(cid), language, get_user_impacts(cid)))

def get_user_currencies(chat_id: int) -> list[str]:
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT currencies FROM user_currencies WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        conn.close()
        return row[0].split(",") if row and row[0] else ["USD"]
    except:
        return ["USD"]

def set_user_currencies(chat_id: int, currencies: list[str]):
    cur_str = ",".join(sorted(set(currencies)))
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO user_currencies (chat_id, currencies)
        VALUES (?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET currencies = excluded.currencies
        """,
        (chat_id, cur_str)
    )
    conn.commit()
    conn.close()

async def toggle_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cid = q.message.chat.id
    language = get_user_language(cid, getattr(q.from_user, "language_code", None))
    currency = q.data.split("_")[1]
    selected = get_user_currencies(cid)
    if currency in selected:
        if len(selected) == 1:
            await q.answer("حداقل یک ارز انتخاب کن" if language == "fa" else "Keep at least one currency selected")
            return
        selected.remove(currency)
    else:
        selected.append(currency)
    set_user_currencies(cid, selected)
    messages = {
        'fa': f"✓ {currency} {'حذف شد' if currency not in selected else 'اضافه شد'}",
        'en': f"✓ {currency} {'removed' if currency not in selected else 'added'}"
    }
    await q.answer(messages[language])
    await safe_edit(q.message, CURRENCY_SCREEN[language],
                    build_currency_keyboard(get_user_currencies(cid), language, get_user_impacts(cid)))

async def choose_currency_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cid = q.message.chat.id
    language = get_user_language(cid, getattr(q.from_user, "language_code", None))
    await q.answer()
    await safe_edit(q.message, CURRENCY_SCREEN[language],
                    build_currency_keyboard(get_user_currencies(cid), language, get_user_impacts(cid)))

async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cid = q.message.chat.id
    language = get_user_language(cid, getattr(q.from_user, "language_code", None))
    await q.answer()
    await safe_edit(q.message, main_menu_text(language, cid, back=True), main_menu_keyboard(language, cid))

# تغییر در تابع notify_restart:
async def notify_restart(application):
    """ارسال پیام «بات دوباره آنلاین شد» به همه کاربران قبلی"""
    # اگر تنظیمات غیرفعال باشد، هیچ کاری نکن
    if not restart_notice_on():
        logger.info("Restart notification is disabled globally.")
        return

    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT chat_id FROM users")
        users = cur.fetchall()
        conn.close()

        if not users:
            logger.info("No previous users found to notify on restart.")
            return

        restart_message_fa = (
            "✓ <b>بات دوباره آنلاین شد!</b>\n\n"
            "خوش اومدی دوباره ☺\n"
            "حالا می‌تونی مثل قبل از منوی اصلی استفاده کنی."
        )

        restart_message_en = (
            "✓ <b>Bot is back online!</b>\n\n"
            "Welcome back ☺\n"
            "You can use the main menu as before."
        )

        for (chat_id,) in users:
            try:
                language = get_user_language(chat_id)
                msg = restart_message_fa if language == 'fa' else restart_message_en
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode=ParseMode.HTML,
                    reply_markup=menu_kb(language, chat_id),   # existing users get the ▥ / ▦ / ◷ keyboard too
                )
                _mark_menu_kb(chat_id, language)
                await asyncio.sleep(0.3)  # جلوگیری از محدودیت سرعت
            except Exception as e:
                logger.warning(f"Could not send restart message to {chat_id}: {e}")

        logger.info(f"Restart notification sent to {len(users)} users.")

    except Exception as e:
        logger.error(f"Error in notify_restart: {e}")
        
async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    """Never fail silently: log the traceback, and tell the user something went wrong."""
    logger.error("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat and update.effective_chat.type == "private":
            await context.bot.send_message(update.effective_chat.id, "△ Something went wrong, please try again. / مشکلی پیش اومد، دوباره امتحان کن.")
    except Exception:
        pass

async def post_init(application):
    await application.bot.set_my_commands(EN_COMMANDS)
    await application.bot.set_my_commands(FA_COMMANDS, language_code="fa")
    # Native Telegram Menu button (next to the input box) for ALL users, independent of /start
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await notify_restart(application)

def main():
    if not BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is empty. Run: python bot_settings.py")
        sys.exit(1)
    ensure_db()
    load_api_base_from_db()  # use admin's saved server, if any
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)         # 3) ثبت post_init
        .build()
    )
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    # Admin-only: single /admin panel for switching the Flask server
    app.add_handler(CommandHandler("admin", admin_cmd))   # hidden, admin only (no button for it)
    app.add_handler(CommandHandler("myid", myid_cmd))     # hidden, needed once to find ADMIN_CHAT_ID
    app.add_handler(CallbackQueryHandler(button_router))
    app.add_handler(TypeHandler(Update, keyboard_refresh), group=1)   # ▭ new keyboard after KB_VERSION bump
    # Text replies: channel username for ◈ My channel, or the admin's reply when adding a server
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.FORWARDED) & ~filters.COMMAND & ~filters.Document.ALL, text_router))
    # Catches the backup file the admin uploads after tapping ↻ Restore
    app.add_handler(MessageHandler(filters.Document.ALL, admin_document_input))
    app.add_handler(MessageHandler(filters.PHOTO, photo_input))   # ❖ logo picture
    app.job_queue.run_repeating(digest_loop, interval=60, first=0)
    app.job_queue.run_repeating(pre_news_loop, interval=15, first=5)
    app.job_queue.run_repeating(date_loop, interval=30, first=0)   # 30s + last_sent: never misses / never doubles
    app.job_queue.run_repeating(reminders_tick, interval=30, first=5)   # ◷ due reminders
    print("● bot is online!")
    app.run_polling()

if __name__ == "__main__":
    if cfg.RUN_BOT:
        main()
    else:
        print("RUN_BOT is false. Telegram bot will not start.")
