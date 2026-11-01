"""
◈ "Send to my channel": a user connects their own channel and the ▤ Daily High-Impact News is posted there
instead of in the private chat. The bot must be an admin of the channel (Post Messages).
"""
from __future__ import annotations

import html
import time

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as KB, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import bot_settings as cfg
from bot import language as i18n
from db import postgres as database

AWAIT_CHANNEL = "set_channel"
DEFAULT_BOT_USERNAME = "ForexFactoryN_Bot"
ADMIN_STATUSES = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
_last_failure_notice = {}


# ───────────────  DB  ────────────────
def ensure_table():
    database.initialize()


def get_channel(chat_id: int) -> dict | None:
    channels = get_channels(chat_id)
    return channels[0] if channels else None

def get_channels(chat_id: int) -> list[dict]:
    conn = database.connect()
    rows = conn.execute("SELECT channel_id, title, username FROM user_channels_multi WHERE chat_id = ? ORDER BY sort_order",
                        (chat_id,)).fetchall()
    conn.close()
    return [{"id": row[0], "title": row[1], "username": row[2]} for row in rows]


def set_channel(chat_id: int, channel_id: int, title: str, username: str | None):
    conn = database.connect()
    conn.execute(
        """INSERT INTO user_channels_multi (chat_id, channel_id, title, username) VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id, channel_id) DO UPDATE SET
               title = excluded.title, username = excluded.username""",
        (chat_id, channel_id, title, username))
    conn.commit()
    conn.close()


def remove_channel(chat_id: int, channel_id: int):
    conn = database.connect()
    conn.execute("DELETE FROM user_channels_multi WHERE chat_id = ? AND channel_id = ?", (chat_id, channel_id))
    for table in ("channel_style_multi", "date_dest"):     # its news look and its calendar destination go with it
        col = "dest_id" if table == "date_dest" else "channel_id"
        conn.execute(f"DELETE FROM {table} WHERE chat_id = ? AND {col} = ?", (chat_id, channel_id))
    conn.commit()
    conn.close()


def label(ch: dict) -> str:
    return f"@{ch['username']}" if ch.get("username") else (ch.get("title") or str(ch["id"]))


# ───────────────  texts  ────────────────
def _bot_name(ctx) -> str:
    return "@" + (getattr(ctx.bot, "username", None) or DEFAULT_BOT_USERNAME)


T = {
    "fa": {
        "intro": ("◈ <b>ارسال خبرها به کانال خودت</b>\n\n"
                  "۱) ربات {bot} رو به کانالت اضافه کن و <b>ادمین</b> کن (دسترسی «ارسال پیام» لازمه).\n"
                  "۲) بعد آیدی کانال رو بفرست، مثلاً <code>@mychannel</code>\n"
                  "   یا اگه کانال خصوصیه، یه پیام از کانال رو برام فوروارد کن.\n\n"
                  "از اون به بعد ▤ اخبار مهم روزانه‌ت (با همون ارزها و ساعتی که تنظیم کردی) توی کانالت ارسال میشه."),
        "current": "◈ کانال فعلی: <b>{ch}</b>\n▤ اخبار مهم روزانه اونجا ارسال میشه.",
        "change": "↻ تغییر کانال", "remove": "✕ قطع اتصال کانال", "cancel": "‹ بازگشت",
        "not_found": "✕ این کانال رو پیدا نکردم. مطمئن شو {bot} توی کانال ادمینه و آیدی رو درست فرستادی.",
        "not_channel": "✕ این یه کانال نیست. آیدی کانال (مثل @mychannel) رو بفرست.",
        "bot_not_admin": "✕ ربات {bot} هنوز ادمین کانال نیست. اول ادمینش کن (با دسترسی ارسال پیام)، بعد دوباره بفرست.",
        "bot_no_post": "✕ ربات ادمینه ولی دسترسی «ارسال پیام» نداره. این دسترسی رو روشن کن و دوباره بفرست.",
        "done": "✓ کانال <b>{ch}</b> وصل شد! ساعت ارسال روزانه: <b>{time}</b>. تنظیمات ارسال و هشدارها رو از پنل اخبار مشخص کن.",
        "removed": "✓ کانال جدا شد. ▤ اخبار مهم روزانه دوباره همین‌جا برات میاد.",
        "send_failed": "△ نتونستم توی کانال <b>{ch}</b> پست بذارم (احتمالاً ربات از کانال حذف شده یا ادمین نیست). "
                       "کانال جدا شد و خبرها از این به بعد همین‌جا میاد. برای وصل دوباره از منو «◈ کانال من» رو بزن.",
    },
    "en": {
        "intro": ("◈ <b>Get the news in your own channel</b>\n\n"
                  "1) Add {bot} to your channel and make it an <b>admin</b> (it needs “Post messages”).\n"
                  "2) Then send the channel username here, e.g. <code>@mychannel</code>\n"
                  "   or, for a private channel, forward any message from the channel to me.\n\n"
                  "From then on your ▤ Daily High-Impact News (same currencies and time you set) is posted in your channel."),
        "current": "◈ Current channel: <b>{ch}</b>\nYour ▤ Daily High-Impact News is posted there.",
        "change": "↻ Change channel", "remove": "✕ Disconnect channel", "cancel": "‹ Back",
        "not_found": "✕ I can't find that channel. Make sure {bot} is an admin there and the username is right.",
        "not_channel": "✕ That's not a channel. Send the channel username, like @mychannel.",
        "bot_not_admin": "✕ {bot} isn't an admin of that channel yet. Make it admin (with Post messages) and send it again.",
        "bot_no_post": "✕ The bot is admin but can't post messages. Enable “Post messages” and send it again.",
        "done": "✓ Channel <b>{ch}</b> connected! Daily delivery time: <b>{time}</b> (New York). Configure delivery and alerts in the news panel.",
        "removed": "✓ Channel disconnected. Your ▤ Daily High-Impact News comes here again.",
        "send_failed": "△ I couldn't post in your channel <b>{ch}</b> (the bot was probably removed or isn't admin). "
                       "It's disconnected now and the news comes here. Tap “◈ My channel” in the menu to connect again.",
    },
}


i18n.fill(T)


def _t(lang: str) -> dict:
    return T.get(lang, T["en"])

PANEL_EN = ("◈ <b>Publishing channels</b>\n\n{names}\n\n"
            "Add {bot} to the channel with <b>Post messages</b> permission, "
            "then send its username or forward a channel post.\n\n"
            "▾ Tap a channel's name to set its own look (text/picture, style, logo).")
NO_CHANNELS_EN = "No channels connected yet."


def channel_panel(cid: int, ctx, language: str) -> tuple[str, KB]:
    connected = get_channels(cid)
    bot = html.escape(_bot_name(ctx), quote=False)
    names = "\n".join(f"• {html.escape(label(ch), quote=False)}" for ch in connected)
    if language == "fa":
        text = ("◈ <b>کانال‌های انتشار</b>\n\n"
                f"{names if names else 'هنوز کانالی وصل نشده.'}\n\n"
                f"برای اتصال، {bot} رو به کانال اضافه کن و دسترسی <b>ارسال پیام</b> بده؛ "
                "بعد شناسه کانال رو بفرست یا یک پیام از کانال فوروارد کن.\n\n"
                "▾ روی اسم هر کانال بزن تا ظاهر ارسال (متن/تصویر، استایل، لوگو) همون کانال رو تنظیم کنی.")
        add, back = "+ افزودن کانال", "‹ بازگشت"
    else:
        text = i18n.t(language, PANEL_EN).format(
            names=names if names else i18n.t(language, NO_CHANNELS_EN), bot=bot)
        add, back = i18n.t(language, "+ Add channel"), i18n.t(language, "‹ Back")
    from bot import channel_style   # lazy: channel_style imports this module
    rows = [[B(add, callback_data="chan_add")]]
    btns = []
    for ch in connected:   # two columns: each button = that channel's name, opens ITS look settings
        icon = "▣" if channel_style.get_style(cid, ch["id"])["mode"] == "image" else "✎"
        name = (ch.get("title") or label(ch)).strip()[:22]
        btns.append(B(f"{icon} {name}", callback_data=f"chs:c:{ch['id']}"))
    rows += [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([B(back, callback_data="currencies_done")])
    return text, KB(rows)

# ───────────────  handlers  ────────────────
async def show(update: Update, ctx: ContextTypes.DEFAULT_TYPE, language: str):
    """Button '◈ My channel'."""
    q = update.callback_query
    cid = q.message.chat.id
    ctx.user_data.pop("awaiting", None)
    ctx.user_data.pop("chan_back", None)
    text, keyboard = channel_panel(cid, ctx, language)
    await q.answer()
    await q.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, language: str):
    q = update.callback_query
    t = _t(language)
    if q.data.startswith("chan_remove_"):
        try:
            channel_id = int(q.data[len("chan_remove_"):])
        except ValueError:
            await q.answer()
            return
        remove_channel(q.message.chat.id, channel_id)
        ctx.user_data.pop("awaiting", None)
        await q.answer("حذف شد" if language == "fa" else i18n.t(language, "Removed"))
        text, kb = channel_panel(q.message.chat.id, ctx, language)
        await q.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return
    if q.data == "chan_remove":  # legacy button: show list instead of removing all
        await show(update, ctx, language)
        return
    ctx.user_data["awaiting"] = AWAIT_CHANNEL
    from_cal = q.data == "chan_add_cal"          # started from the ▦ calendar: come back there afterwards
    if from_cal:
        ctx.user_data["chan_back"] = "cal"
    else:
        ctx.user_data.pop("chan_back", None)
    await q.answer()
    text = ("شناسه کانال رو بفرست یا یک پیام از کانال فوروارد کن." if language == "fa"
            else i18n.t(language, "Send the channel username or forward a channel post."))
    await q.message.edit_text(text,
                              reply_markup=KB([[B(t["cancel"], callback_data="cal" if from_cal else "my_channel")]]),
                              parse_mode=ParseMode.HTML)


def _channel_ref(msg) -> int | str | None:
    """@username / t.me link / -100 id typed by the user, or the channel of a forwarded post."""
    origin = getattr(msg, "forward_origin", None)
    if origin is not None and getattr(origin, "chat", None) is not None:
        return origin.chat.id
    fwd_chat = getattr(msg, "forward_from_chat", None)   # older python-telegram-bot
    if fwd_chat is not None:
        return fwd_chat.id
    text = (msg.text or "").strip()
    if not text:
        return None
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if text.lower().startswith(prefix):
            text = "@" + text[len(prefix):].split("/")[0].split("?")[0]
    if text.lstrip("-").isdigit():
        return int(text)
    if not text.startswith("@"):
        text = "@" + text
    return text if len(text) > 4 and " " not in text else None


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE, language: str,
                     subscribe_and_get_time, on_connected=None) -> None:
    """Text / forwarded post while waiting for the channel."""
    msg = update.message
    t = _t(language)
    bot = _bot_name(ctx)
    ref = _channel_ref(msg)
    if ref is None:
        await msg.reply_text("شناسه یا پیام کانال رو پیدا نکردم." if language == "fa" else i18n.t(language, "Channel not found."), parse_mode=ParseMode.HTML)
        return
    try:
        chat = await ctx.bot.get_chat(ref)
    except TelegramError:
        await msg.reply_text("کانال پیدا نشد؛ شناسه و دسترسی ربات رو بررسی کن." if language == "fa" else i18n.t(language, "Channel not found. Check its username and the bot's access."), parse_mode=ParseMode.HTML)
        return
    if chat.type != ChatType.CHANNEL:
        await msg.reply_text(t["not_channel"], parse_mode=ParseMode.HTML)
        return
    try:
        me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
    except TelegramError:
        await msg.reply_text("دسترسی «ارسال پیام» رو به ربات بده." if language == "fa" else i18n.t(language, "Give the bot permission to post messages."), parse_mode=ParseMode.HTML)
        return
    if me.status not in ADMIN_STATUSES:
        await msg.reply_text("دسترسی «ارسال پیام» رو به ربات بده." if language == "fa" else i18n.t(language, "Give the bot permission to post messages."), parse_mode=ParseMode.HTML)
        return
    if me.status == ChatMemberStatus.ADMINISTRATOR and getattr(me, "can_post_messages", True) is False:
        await msg.reply_text("دسترسی «ارسال پیام» رو به ربات بده." if language == "fa" else i18n.t(language, "Give the bot permission to post messages."), parse_mode=ParseMode.HTML)
        return
    try:
        member = await ctx.bot.get_chat_member(chat.id, update.effective_user.id)
        permitted = (member.status == ChatMemberStatus.OWNER or
                     (member.status == ChatMemberStatus.ADMINISTRATOR and getattr(member, "can_post_messages", False)))
    except TelegramError:
        permitted = False
    if not permitted:
        await msg.reply_text("برای اتصال، باید دسترسی ارسال پیام در این کانال داشته باشی." if language == "fa"
                             else i18n.t(language, "You need permission to post in this channel to connect it."), parse_mode=ParseMode.HTML)
        return
    set_channel(msg.chat.id, chat.id, chat.title or "", chat.username)
    ctx.user_data.pop("awaiting", None)
    back_to = ctx.user_data.pop("chan_back", None)
    ch = html.escape(label({"id": chat.id, "title": chat.title, "username": chat.username}), quote=False)
    if back_to == "cal" and on_connected is not None:      # added from the calendar: select it there and go back
        text, kb = on_connected(msg.chat.id, chat.id, language)
    else:
        text, kb = channel_panel(msg.chat.id, ctx, language)
    success = (f"✓ {ch} وصل شد.\n\n" if language == "fa"
               else i18n.t(language, "✓ {ch} connected.\n\n").format(ch=ch))
    await msg.reply_text(success + text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def notify_failed(ctx, chat_id: int, language: str, ch: dict):
    key = (chat_id, ch["id"])
    now = time.monotonic()
    if now - _last_failure_notice.get(key, float("-inf")) < 3600:
        return
    try:
        name = html.escape(label(ch), quote=False)
        text = (f"△ ارسال به <b>{name}</b> ناموفق بود. دسترسی ارسال پیام ربات رو بررسی کن."
                if language == "fa" else
                i18n.t(language, "△ Could not post to <b>{name}</b>. Check the bot's Post messages permission.").format(name=name))
        await ctx.bot.send_message(chat_id, text,
                                   parse_mode=ParseMode.HTML)
        _last_failure_notice[key] = now
    except TelegramError:
        pass
