"""
❖ Channel look: how the Daily High-Impact News is posted in EACH of a user's channels.

  • mode   "text" (default) or "image" (calendar picture + caption with only the important news)
  • style  one of 6 numbered designs (Timeline, Bento, Editorial, HUD, Ticket, Spotlight), shown together in ONE picture
  • logo   optional, per channel: an uploaded picture, or a generated monogram (1-3 letters on a gradient badge).
           The 6 designs are rebuilt from its colour.

Settings live in channel_style_multi (chat_id, channel_id). A channel without its own row inherits the old
per-user row (channel_style), so nothing changes for existing users until they touch a channel.
Logos are BLOBs in the SQLite DB, so the normal backup / restore includes them.
The channel name is never drawn on the picture.

Callback data (all start with "chs:"):
  chs:c:<ch>            look panel of one channel
  chs:m:<ch>:<text|image>
  chs:t:<ch>            style sheet (shows a progress bar while it is built)
  chs:p:<ch>:<key>      pick a style
  chs:l:<ch>            logo menu          chs:u:<ch>  upload a logo
  chs:ld:<ch>           remove the logo    chs:mg:<ch> monogram (type letters)
  chs:ma:<ch>           monogram, automatic letters      chs:mc:<ch>:<n>  monogram colour n
Old "chs" / "chs_*" buttons in old messages just open the channel list.

Two separate looks, never mixed:
  • ▤ news    one look PER connected channel (channel_id = the channel)
  • ▦ calendar ONE look for the user's daily date picture (channel_id = CAL_ID = 0), used for every place the date is sent
    (callbacks: chs:c:0 opens its style sheet directly)
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import io
import logging
import re
import sqlite3
import time
from contextlib import closing

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as KB, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

import bot_settings as cfg
from bot import calendar_image as ci
from bot import channels

log = logging.getLogger(__name__)

AWAIT_LOGO = "chs_await_logo"
AWAIT_MONO = "chs_await_mono"
CH_KEY = "chs_channel"          # user_data: channel being edited while we wait for a logo / letters
MONO_TXT = "chs_mono_text"      # user_data: letters chosen for the monogram
SHEET_MSG = "chs_sheet_msg"
MAX_UPLOAD = 10 * 1024 * 1024
ROWS_PER_IMAGE = 12
MAX_IMAGES = 4
N_DESIGNS = len(ci.STYLE_KEYS)
CAL_ID = 0                      # pseudo channel id = the calendar picture look (separate from every news channel)

MONO_COLORS = [(("آبی", "Blue"), (37, 99, 235)), (("بنفش", "Violet"), (124, 58, 237)), (("سبز", "Green"), (5, 150, 105)),
               (("نارنجی", "Orange"), (234, 120, 8)), (("قرمز", "Red"), (225, 29, 72)), (("خاکستری", "Slate"), (51, 65, 85))]


# ───────────────  DB  ────────────────
def ensure_table():
    with closing(sqlite3.connect(cfg.DB_PATH, timeout=10)) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS channel_style (
            chat_id INTEGER PRIMARY KEY,
            mode    TEXT NOT NULL DEFAULT 'text',
            theme   TEXT NOT NULL DEFAULT 'aurora',
            logo    BLOB)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS channel_style_multi (
            chat_id    INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            mode       TEXT NOT NULL DEFAULT 'text',
            theme      TEXT NOT NULL DEFAULT 'timeline',
            logo       BLOB,
            PRIMARY KEY (chat_id, channel_id))""")


def get_style(chat_id: int, channel_id: int | None = None, blobs: bool = False) -> dict:
    """Style of one channel (falls back to the user's old shared style, then to the defaults)."""
    cols = "mode, theme, logo" if blobs else "mode, theme, logo IS NOT NULL"
    row = None
    with closing(sqlite3.connect(cfg.DB_PATH, timeout=10)) as conn:
        if channel_id is not None:
            row = conn.execute(f"SELECT {cols} FROM channel_style_multi WHERE chat_id = ? AND channel_id = ?",
                               (chat_id, channel_id)).fetchone()
        if row is None and channel_id not in (chat_id, CAL_ID):   # (own chat / calendar look: no old shared style)
            row = conn.execute(f"SELECT {cols} FROM channel_style WHERE chat_id = ?", (chat_id,)).fetchone()
    if not row:
        return {"mode": "image" if channel_id == CAL_ID else "text", "theme": ci.STYLE_KEYS[0],
                "logo": None if blobs else False}
    return {"mode": "image" if channel_id == CAL_ID else (row[0] if row[0] in ("text", "image") else "text"),
            "theme": row[1] if row[1] in ci.STYLE_KEYS else ci.STYLE_KEYS[0],   # old theme names -> style 1
            "logo": row[2] if blobs else bool(row[2])}


def style_key(chat_id: int, channel_id: int) -> tuple:
    """Channels with the same key get the same pictures, so the delivery renders them once."""
    st = get_style(chat_id, channel_id, blobs=True)
    return st["mode"], st["theme"], hashlib.sha1(st["logo"]).hexdigest() if st["logo"] else None


def _set(chat_id: int, channel_id: int, **fields):
    assert set(fields) <= {"mode", "theme", "logo"}
    with closing(sqlite3.connect(cfg.DB_PATH, timeout=10)) as conn, conn:
        # first edit of a channel: start from what it used so far (the old shared style)
        if channel_id != CAL_ID:
            conn.execute("""INSERT OR IGNORE INTO channel_style_multi (chat_id, channel_id, mode, theme, logo)
                            SELECT ?, ?, mode, theme, logo FROM channel_style WHERE chat_id = ?""",
                         (chat_id, channel_id, chat_id))
        conn.execute("INSERT OR IGNORE INTO channel_style_multi (chat_id, channel_id) VALUES (?, ?)",
                     (chat_id, channel_id))
        for col, val in fields.items():
            conn.execute(f"UPDATE channel_style_multi SET {col} = ? WHERE chat_id = ? AND channel_id = ?",
                         (val, chat_id, channel_id))


def forget(chat_id: int, channel_id: int):
    """Called when a channel is disconnected."""
    try:
        with closing(sqlite3.connect(cfg.DB_PATH, timeout=10)) as conn, conn:
            conn.execute("DELETE FROM channel_style_multi WHERE chat_id = ? AND channel_id = ?",
                         (chat_id, channel_id))
    except sqlite3.OperationalError:
        pass


# ───────────────  logo helpers  ────────────────
def _prep_logo(data: bytes) -> bytes:
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGBA")
    flat = Image.new("RGBA", im.size, (255, 255, 255, 255))
    flat.alpha_composite(im)
    out = io.BytesIO()
    ImageOps.fit(flat.convert("RGB"), (256, 256), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()


def _shade(c, t):
    """t > 0 mixes with white, t < 0 with black."""
    tgt, k = ((255, 255, 255), t) if t > 0 else ((0, 0, 0), -t)
    return tuple(int(a + (b - a) * k) for a, b in zip(c, tgt))


def make_monogram(text: str, color: tuple) -> bytes:
    """Gradient badge with 1-3 letters, a soft light in the corner and a thin inner ring."""
    S = 1024
    g = Image.linear_gradient("L").resize((S, S))
    mask = ImageChops.add(g, g.rotate(90), scale=2)                       # diagonal 0..255
    base = Image.composite(Image.new("RGB", (S, S), _shade(color, -0.38)),
                           Image.new("RGB", (S, S), _shade(color, 0.22)), mask).convert("RGBA")
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((-S * .25, -S * .35, S * .75, S * .45), fill=(255, 255, 255, 70))
    base.alpha_composite(glow.filter(ImageFilter.GaussianBlur(S * .06)))
    ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(ring).rounded_rectangle((S * .07, S * .07, S * .93, S * .93), radius=S * .2,
                                           outline=(255, 255, 255, 95), width=int(S * .014))
    base.alpha_composite(ring)
    size = {1: .66, 2: .5, 3: .38}.get(len(text), .38) * S
    d = ImageDraw.Draw(base)
    cx, cy = S / 2, S / 2 + S * .02
    d.text((cx + S * .008, cy + S * .012), text, font=ci._font(size, True), fill=(0, 0, 0, 70), anchor="mm")  # soft shadow
    d.text((cx, cy), text, font=ci._font(size, True), fill=(255, 255, 255, 255), anchor="mm")
    out = io.BytesIO()
    base.convert("RGB").resize((256, 256), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()


def _auto_letters(ch: dict) -> str:
    """Initials from the channel title (Latin letters only: the fonts have no Persian shaping), else from @username."""
    if ch.get("id") == CAL_ID:
        return ""
    words = [w for w in re.split(r"[\s_\-]+", ch.get("title") or "") if w[:1].isascii() and w[:1].isalnum()]
    letters = "".join(w[0] for w in words[:2]) if len(words) > 1 else (words[0][:2] if words else "")
    if not letters:
        letters = re.sub(r"[^A-Za-z0-9]", "", ch.get("username") or "")[:2]
    return letters.upper()


# ───────────────  rendering  ────────────────
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_IMPACT_FA = {"High": "بالا", "Medium": "متوسط", "Low": "کم"}


def render_png(chat_id: int, events: list[dict], impacts: list[str], channel_id: int | None = None,
               tz: str = "America/New_York", language: str = "en") -> list[bytes]:
    """One or more PNGs (12 events each, max 4 images) in the channel's style. Blocking: use to_thread.
    `events` must already be in the user's time zone (tz.localize_events); `tz` is that zone's name (the captions say
    which clock it is); `language` 'fa' = Persian labels (only when the server can draw Persian, else English)."""
    st = get_style(chat_id, channel_id, blobs=True)
    fa = language == "fa" and ci.RTL_OK
    only_high = list(impacts) == ["High"]
    if fa:
        title = "اخبار مهم روزانه" if only_high else "اخبار اقتصادی روزانه"
        label = "اهمیت بالا" if only_high else "اهمیت " + " · ".join(_IMPACT_FA[i] for i in ("High", "Medium", "Low") if i in impacts)
    else:
        title = "Daily High-Impact News" if only_high else "Daily Economic News"
        label = "High impact" if only_high else " · ".join(i for i in ("High", "Medium", "Low") if i in impacts) + " impact"
    chunks = [events[i:i + ROWS_PER_IMAGE] for i in range(0, len(events), ROWS_PER_IMAGE)][:MAX_IMAGES] or [[]]
    pngs = []
    for n, chunk in enumerate(chunks, 1):
        if len(chunks) == 1:
            t = title
        else:
            part = f"{n}/{len(chunks)}"
            t = f"{title}  {part.translate(_FA_DIGITS) if fa else part}"
        pngs.append(ci.render_calendar(chunk, st["theme"], t, st["logo"] or ci.DEFAULT_LOGO,
                                       impact_label=label, max_rows=ROWS_PER_IMAGE, tz=tz,
                                       language="fa" if fa else "en"))
    return pngs


def render_date(chat_id: int, channel_id: int, model: dict) -> bytes:
    """▦ today's calendar as a picture in the CALENDAR look (never a news channel's). Blocking: use to_thread."""
    st = get_style(chat_id, CAL_ID, blobs=True)
    return ci.render_date_card(model, st["theme"], st["logo"] or ci.DEFAULT_LOGO)


_SHEET_CACHE: dict = {}
date_model_provider = None      # set by telegram_bot: (language, chat_id) -> model of today's date card (the calendar sheet's sample)


def _zone_of(chat_id: int, language: str) -> str:
    try:
        from bot import tz as tzmod
        return tzmod.zone_name(language or "en", chat_id)
    except Exception:
        return "America/New_York"


def _sheet_key(chat_id: int, channel_id: int, language: str = "") -> str:
    logo = get_style(chat_id, channel_id, blobs=True)["logo"]
    base = hashlib.sha1(logo).hexdigest() if logo else "default"
    zone = _zone_of(chat_id, language)
    # calendar and news sheets never share a cache; language and time zone are part of the picture
    return f"cal:{language}:{zone}:{base}" if channel_id == CAL_ID else f"news:{language}:{zone}:{base}"


def sheet_cached(chat_id: int, channel_id: int, language: str = "") -> bool:
    return _sheet_key(chat_id, channel_id, language) in _SHEET_CACHE


def render_sheet(chat_id: int, channel_id: int, progress=None, language: str = "en") -> bytes:
    """All 6 designs in one numbered picture, built from the channel's logo (or the bot's). Cached: it takes ~7 s.
    NEWS channel -> the 6 news designs.  CAL_ID -> the 6 designs of the DATE card (today's date).
    progress(done, total) is called from this (worker) thread after every step."""
    logo = get_style(chat_id, channel_id, blobs=True)["logo"]
    key = _sheet_key(chat_id, channel_id, language)
    if key not in _SHEET_CACHE:
        if len(_SHEET_CACHE) >= 30:
            _SHEET_CACHE.pop(next(iter(_SHEET_CACHE)))
        if channel_id == CAL_ID:
            _SHEET_CACHE[key] = ci.render_date_sheet(date_model_provider(language, chat_id), logo or ci.DEFAULT_LOGO, progress=progress)
        else:
            _SHEET_CACHE[key] = ci.render_sheet(logo or ci.DEFAULT_LOGO, progress=progress,
                                                language=language, tz=_zone_of(chat_id, language))
    return _SHEET_CACHE[key]


# ───────────────  UI helpers  ────────────────
def _style_label(st: dict) -> str:
    n = ci.STYLE_KEYS.index(st["theme"]) + 1
    return f"{n} · {ci.STYLE_NAMES[st['theme']]}"


def _name(ch: dict) -> str:
    return html.escape(ch.get("title") or channels.label(ch), quote=False)


def _channel(cid: int, channel_id: int, language: str = "en") -> dict | None:
    """A connected channel, or (channel_id == chat id) the user's own chat as a destination."""
    if channel_id == CAL_ID:
        return {"id": CAL_ID, "title": "تقویم" if language == "fa" else "Calendar", "username": None}
    if channel_id == cid:
        return {"id": cid, "title": "▢ این چت" if language == "fa" else "▢ This chat", "username": None}
    return next((c for c in channels.get_channels(cid) if c["id"] == channel_id), None)


def _bar(pct: int, width: int = 12) -> str:
    full = round(pct / 100 * width)
    return "▰" * full + "▱" * (width - full)


def _progress_text(fa: bool, pct: int, done: int = 0, total: int = N_DESIGNS + 1, note: str = "") -> str:
    if done < N_DESIGNS:
        step = (f"طرح {done + 1} از {N_DESIGNS}…" if fa else f"Design {done + 1} of {N_DESIGNS}…")
    else:
        step = "چیدن نهایی…" if fa else "Final touches…"
    if fa:
        body = (f"❖ <b>دارم طرح‌ها رو با لوگوی خودت می‌سازم</b>\n\n<code>{_bar(pct)}</code>  <b>{pct}%</b>\n{step}")
    else:
        body = (f"❖ <b>Building the designs with your logo</b>\n\n<code>{_bar(pct)}</code>  <b>{pct}%</b>\n{step}")
    return (note + "\n\n" + body) if note else body


async def _run_with_progress(target, fa: bool, note: str, work):
    """Run work(progress_cb) in a thread while editing `target` with a live percentage."""
    st = {"done": 0, "total": N_DESIGNS + 1, "t": time.monotonic()}
    took: list[float] = []

    def cb(done, total):                      # called from the worker thread
        now = time.monotonic()
        if done > st["done"]:
            took.append(now - st["t"])
        st.update(done=done, total=total, t=now)

    task = asyncio.ensure_future(asyncio.to_thread(work, cb))
    last = -1
    while True:
        done, total = st["done"], st["total"]
        per_step = 100 / total
        avg = sum(took) / len(took) if took else 1.2
        creep = min(0.9, (time.monotonic() - st["t"]) / avg) * per_step      # keeps the bar moving between steps
        pct = min(99, int(done * per_step + creep))
        if pct != last:
            last = pct
            try:
                await target.edit_text(_progress_text(fa, pct, done, total, note), parse_mode=ParseMode.HTML)
            except TelegramError:
                pass                          # "not modified" / flood limit: the next tick tries again
        if task.done():
            break
        await asyncio.wait({task}, timeout=1.0)
    return task.result()


async def _edit(msg, text: str, kb: KB):
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def _delete(msg):
    try:
        await msg.delete()
    except Exception:
        pass


async def _replace(q, ctx, text: str, kb: KB):
    """Show a text screen in place of whatever the callback came from (a picture can't turn into text)."""
    if getattr(q.message, "photo", None):
        await _delete(q.message)
        ctx.user_data.pop(SHEET_MSG, None)
        await ctx.bot.send_message(q.message.chat.id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await _edit(q.message, text, kb)


# ───────────────  screens  ────────────────
def panel(cid: int, ch: dict, language: str) -> tuple[str, KB]:
    """Look settings of ONE channel."""
    st, fa = get_style(cid, ch["id"]), language == "fa"
    n = ch["id"]
    if fa:
        text = (f"❖ <b>ظاهر ارسال · {_name(ch)}</b>\n\n"
                f"ارسال به صورت: <b>{'▣ تصویر' if st['mode'] == 'image' else '✎ متن'}</b>\n"
                f"استایل: <b>{_style_label(st)}</b>\n"
                f"لوگو: <b>{'◆ لوگوی شخصی' if st['logo'] else 'پیش‌فرض ربات'}</b>")
        b_text, b_img, b_style, b_rm, b_back = "✎ متن", "▣ تصویر", "❖ استایل و لوگو", "✕ قطع این کانال", "‹ کانال‌ها"
    else:
        text = (f"❖ <b>Look · {_name(ch)}</b>\n\n"
                f"Post as: <b>{'▣ Picture' if st['mode'] == 'image' else '✎ Text'}</b>\n"
                f"Style: <b>{_style_label(st)}</b>\n"
                f"Logo: <b>{'◆ Your own' if st['logo'] else 'Bot default'}</b>")
        b_text, b_img, b_style, b_rm, b_back = "✎ Text", "▣ Picture", "❖ Style & logo", "✕ Disconnect channel", "‹ Channels"
    mark = lambda on, label: ("✓ " if on else "") + label  # noqa: E731
    own = n == cid                                   # the user's own chat: nothing to disconnect, back = calendar
    rows = [[B(mark(st["mode"] == "text", b_text), callback_data=f"chs:m:{n}:text"),
             B(mark(st["mode"] == "image", b_img), callback_data=f"chs:m:{n}:image")],
            [B(b_style, callback_data=f"chs:t:{n}")]]
    if not own:
        rows.append([B(b_rm, callback_data=f"chan_remove_{n}")])
    rows.append([B(("‹ تقویم" if fa else "‹ Calendar") if own else b_back, callback_data="cal" if own else "my_channel")])
    return text, KB(rows)


def _sheet_kb(cid: int, channel_id: int, language: str) -> KB:
    st, fa = get_style(cid, channel_id), language == "fa"
    nums = [B(("✓ " if st["theme"] == key and st["mode"] == "image" else "") + str(i),
              callback_data=f"chs:p:{channel_id}:{key}") for i, key in enumerate(ci.STYLE_KEYS, 1)]
    rows = [nums[i:i + 3] for i in range(0, len(nums), 3)]
    rows.append([B(("◆ تغییر لوگو" if st["logo"] else "◆ لوگو / مونوگرام") if fa else
                   ("◆ Change logo" if st["logo"] else "◆ Logo / monogram"), callback_data=f"chs:l:{channel_id}")])
    rows.append([B("‹ بازگشت" if fa else "‹ Back", callback_data="cal" if channel_id == CAL_ID else f"chs:c:{channel_id}")])
    return KB(rows)


def _logo_menu(cid: int, ch: dict, language: str) -> tuple[str, KB]:
    fa, n = language == "fa", ch["id"]
    has = get_style(cid, n)["logo"]
    cal = n == CAL_ID                      # the calendar look has no monogram: logo upload only
    if fa:
        text = (f"◆ <b>لوگوی {_name(ch)}</b>\n\n" +
                ("لوگوی خودت رو آپلود کن." if cal else "لوگوی خودت رو آپلود کن، یا با چند حرف یه مونوگرام جذاب بساز."))
        rows = [[B("⇧ آپلود لوگو", callback_data=f"chs:u:{n}")] +
                ([] if cal else [B("Aa ساخت مونوگرام", callback_data=f"chs:mg:{n}")])]
        if has:
            rows.append([B("⌫ حذف لوگو (پیش‌فرض ربات)", callback_data=f"chs:ld:{n}")])
        rows.append([B("‹ بازگشت", callback_data=f"chs:t:{n}")])
    else:
        text = (f"◆ <b>Logo for {_name(ch)}</b>\n\n" +
                ("Upload your own logo." if cal else "Upload your own logo, or build a sharp monogram from a few letters."))
        rows = [[B("⇧ Upload logo", callback_data=f"chs:u:{n}")] +
                ([] if cal else [B("Aa Make a monogram", callback_data=f"chs:mg:{n}")])]
        if has:
            rows.append([B("⌫ Remove logo (bot default)", callback_data=f"chs:ld:{n}")])
        rows.append([B("‹ Back", callback_data=f"chs:t:{n}")])
    return text, KB(rows)


def _color_kb(channel_id: int, language: str) -> KB:
    row = [B(names[0 if language == "fa" else 1], callback_data=f"chs:mc:{channel_id}:{i}") for i, (names, _) in enumerate(MONO_COLORS)]
    return KB([row[:3], row[3:], [B("‹ بازگشت" if language == "fa" else "‹ Back", callback_data=f"chs:l:{channel_id}")]])


async def _show_sheet(ctx, cid: int, channel_id: int, language: str, note: str = "", target=None):
    """Send the numbered picture of all styles. First time it takes ~7 s, so a live % bar is shown meanwhile."""
    fa = language == "fa"
    old = ctx.user_data.pop(SHEET_MSG, None)
    if old:
        try:
            await ctx.bot.delete_message(cid, old)
        except Exception:
            pass
    try:
        if sheet_cached(cid, channel_id, language):
            if target is not None:
                await _delete(target)
            png = await asyncio.to_thread(render_sheet, cid, channel_id, None, language)
        else:
            if target is None:
                target = await ctx.bot.send_message(cid, _progress_text(fa, 0, note=note), parse_mode=ParseMode.HTML)
            else:
                try:
                    await target.edit_text(_progress_text(fa, 0, note=note), parse_mode=ParseMode.HTML)
                except TelegramError:
                    pass
            png = await _run_with_progress(target, fa, note, lambda cb: render_sheet(cid, channel_id, cb, language))
            await _delete(target)
    except Exception:
        log.exception("Style sheet for %s/%s failed", cid, channel_id)
        await ctx.bot.send_message(cid, "✕ ساخت طرح‌ها ناموفق بود، دوباره امتحان کن." if fa
                                   else "✕ Couldn't build the designs, please try again.",
                                   reply_markup=KB([[B("‹", callback_data=f"chs:c:{channel_id}")]]))
        return
    cap = "❖ <b>یکی از شماره‌ها رو انتخاب کن</b>" if fa else "❖ <b>Pick a number</b>"
    if get_style(cid, channel_id)["logo"]:
        cap += "\nطرح‌ها از روی لوگوی خودت ساخته شدن." if fa else "\nThe designs are built from your logo."
    else:
        cap += ("\nبرای ساخت با رنگ خودت، لوگو یا مونوگرام اضافه کن." if fa else
                "\nAdd a logo or monogram to rebuild them in your own colours.")
    if note and sheet_cached(cid, channel_id, language):
        cap = note + "\n\n" + cap
    sent = await ctx.bot.send_photo(cid, io.BytesIO(png), caption=cap, parse_mode=ParseMode.HTML,
                                    reply_markup=_sheet_kb(cid, channel_id, language))
    ctx.user_data[SHEET_MSG] = sent.message_id


# ───────────────  handlers  ────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, language: str):
    q = update.callback_query
    cid, data, fa = q.message.chat.id, q.data, language == "fa"
    parts = data.split(":")

    if len(parts) < 3 or parts[0] != "chs":       # old "chs" / "chs_*" buttons: back to the channel list
        ctx.user_data.pop("awaiting", None)
        await q.answer()
        await _replace(q, ctx, *channels.channel_panel(cid, ctx, language))
        return
    act = parts[1]
    try:
        channel_id = int(parts[2])
    except ValueError:
        await q.answer()
        return
    if channel_id == cid:                         # old calendar buttons in chat history -> the calendar look
        channel_id = CAL_ID
    ch = _channel(cid, channel_id, language)
    if ch is None:                                # disconnected meanwhile
        await q.answer("✕")
        await _replace(q, ctx, *channels.channel_panel(cid, ctx, language))
        return
    arg = parts[3] if len(parts) > 3 else ""

    if act == "c" and channel_id == CAL_ID:           # calendar look: straight to the 6 designs
        act = "t"
    if act == "c":
        ctx.user_data.pop("awaiting", None)
        await q.answer()
        await _replace(q, ctx, *panel(cid, ch, language))
    elif act == "m" and arg in ("text", "image"):
        _set(cid, channel_id, mode=arg)
        await q.answer(("✓ ارسال به صورت " + ("تصویر" if arg == "image" else "متن")) if fa else
                       ("✓ Posting as " + ("picture" if arg == "image" else "text")))
        await _edit(q.message, *panel(cid, ch, language))
    elif act == "t":
        ctx.user_data.pop("awaiting", None)
        await q.answer("◴")
        if getattr(q.message, "photo", None):
            await _delete(q.message)
            ctx.user_data.pop(SHEET_MSG, None)
            await _show_sheet(ctx, cid, channel_id, language)
        else:
            await _show_sheet(ctx, cid, channel_id, language, target=q.message)    # the panel itself becomes the progress bar
    elif act == "p" and arg in ci.STYLE_KEYS:
        _set(cid, channel_id, theme=arg, mode="image")
        n = ci.STYLE_KEYS.index(arg) + 1
        await q.answer(f"✓ استایل {n} برای {ch.get('title') or channels.label(ch)}" if fa
                       else f"✓ Style {n} for {ch.get('title') or channels.label(ch)}")
        try:
            await q.message.edit_reply_markup(reply_markup=_sheet_kb(cid, channel_id, language))
        except BadRequest:
            pass
    elif act == "l":
        ctx.user_data.pop("awaiting", None)
        await q.answer()
        await _replace(q, ctx, *_logo_menu(cid, ch, language))
    elif act == "ld":
        _set(cid, channel_id, logo=None)
        await q.answer("✓")
        await _replace(q, ctx, *_logo_menu(cid, ch, language))
    elif act == "u":
        ctx.user_data["awaiting"], ctx.user_data[CH_KEY] = AWAIT_LOGO, channel_id
        await q.answer()
        await _replace(q, ctx, "◆ لوگوت رو به صورت عکس بفرست." if fa else "◆ Send your logo as a picture.",
                       KB([[B("‹ بازگشت" if fa else "‹ Back", callback_data=f"chs:l:{channel_id}")]]))
    elif act in ("mg", "ma", "mc") and channel_id == CAL_ID:      # no monogram for the calendar: back to its logo menu
        await q.answer()
        await _replace(q, ctx, *_logo_menu(cid, ch, language))
    elif act in ("mg", "ma", "mc"):
        await _monogram_step(update, ctx, language, ch, act, arg)
    else:
        await q.answer()


async def _monogram_step(update, ctx, language, ch, act, arg):
    q, fa = update.callback_query, language == "fa"
    cid, n = q.message.chat.id, ch["id"]
    if act == "mg":                                # ask for letters
        ctx.user_data["awaiting"], ctx.user_data[CH_KEY] = AWAIT_MONO, n
        auto = _auto_letters(ch)
        rows = ([[B(("✦ خودکار: " if fa else "✦ Auto: ") + auto, callback_data=f"chs:ma:{n}")]] if auto else [])
        rows.append([B("‹ بازگشت" if fa else "‹ Back", callback_data=f"chs:l:{n}")])
        await q.answer()
        await _replace(q, ctx, ("Aa <b>مونوگرام</b>\n\n۱ تا ۳ حرف یا عدد انگلیسی بفرست، مثلاً <code>FX</code>"
                                if fa else
                                "Aa <b>Monogram</b>\n\nSend 1–3 letters or digits, for example <code>FX</code>"),
                       KB(rows))
    elif act == "ma":
        ctx.user_data[MONO_TXT], ctx.user_data[CH_KEY] = _auto_letters(ch), n
        ctx.user_data.pop("awaiting", None)
        await q.answer()
        await _replace(q, ctx, ("❖ یه رنگ برای مونوگرام انتخاب کن" if fa else "❖ Pick a colour for the monogram"),
                       _color_kb(n, language))
    else:                                          # "mc": colour picked -> build it
        text = ctx.user_data.get(MONO_TXT)
        try:
            color = MONO_COLORS[int(arg)][1]
        except (ValueError, IndexError):
            await q.answer()
            return
        if not text:
            await q.answer("Aa")
            ctx.user_data["awaiting"], ctx.user_data[CH_KEY] = AWAIT_MONO, n
            await _replace(q, ctx, "Aa اول حرف‌ها رو بفرست." if fa else "Aa Send the letters first.",
                           KB([[B("‹", callback_data=f"chs:l:{n}")]]))
            return
        await q.answer("◴")
        logo = await asyncio.to_thread(make_monogram, text, color)
        _set(cid, n, logo=logo)
        ctx.user_data.pop(MONO_TXT, None)
        if getattr(q.message, "photo", None):
            await _delete(q.message)
            ctx.user_data.pop(SHEET_MSG, None)
            await _show_sheet(ctx, cid, n, language, note="✓ مونوگرام ساخته شد." if fa else "✓ Monogram created.")
        else:
            await _show_sheet(ctx, cid, n, language, note="✓ مونوگرام ساخته شد." if fa else "✓ Monogram created.",
                              target=q.message)


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE, language: str):
    """A picture (or image file) sent while waiting for the logo."""
    msg, cid, fa = update.message, update.effective_chat.id, language == "fa"
    if ctx.user_data.get("awaiting") != AWAIT_LOGO:
        return
    channel_id = ctx.user_data.get(CH_KEY)
    if channel_id is None or _channel(cid, channel_id, language) is None:
        ctx.user_data.pop("awaiting", None)
        await msg.reply_text("✕ اول کانال رو انتخاب کن (◈ کانال من)." if fa else "✕ Pick a channel first (◈ My channel).")
        return
    if msg.photo:
        file_id, size = msg.photo[-1].file_id, msg.photo[-1].file_size
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id, size = msg.document.file_id, msg.document.file_size
    else:
        await msg.reply_text("✕ فقط عکس بفرست." if fa else "✕ Please send an image.")
        return
    if size and size > MAX_UPLOAD:
        await msg.reply_text("✕ حجم عکس باید کمتر از ۱۰ مگابایت باشه." if fa else "✕ The image must be under 10 MB.")
        return
    try:
        tg_file = await ctx.bot.get_file(file_id)
        data = bytes(await tg_file.download_as_bytearray())
        prepared = await asyncio.to_thread(_prep_logo, data)
    except Exception:
        await msg.reply_text("✕ این فایل یه عکس معتبر نیست." if fa else "✕ That isn't a valid image.")
        return
    ctx.user_data.pop("awaiting", None)
    _set(cid, channel_id, logo=prepared)
    await _show_sheet(ctx, cid, channel_id, language, note="✓ لوگو ذخیره شد." if fa else "✓ Logo saved.")


async def on_wrong_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE, language: str):
    """Text sent while in a chs_ state: letters for the monogram, otherwise 'send an image'."""
    fa = language == "fa"
    if ctx.user_data.get("awaiting") == AWAIT_MONO:
        cid, channel_id = update.effective_chat.id, ctx.user_data.get(CH_KEY)
        letters = (update.message.text or "").strip().upper()
        if channel_id is None or _channel(cid, channel_id, language) is None:
            ctx.user_data.pop("awaiting", None)
            return
        if not re.fullmatch(r"[A-Z0-9]{1,3}", letters):
            await update.message.reply_text("Aa فقط ۱ تا ۳ حرف یا عدد انگلیسی (مثل FX)." if fa
                                            else "Aa Only 1–3 letters or digits (A–Z, 0–9), like FX.")
            return
        ctx.user_data[MONO_TXT] = letters
        ctx.user_data.pop("awaiting", None)
        await update.message.reply_text(("❖ یه رنگ برای <b>%s</b> انتخاب کن" if fa else "❖ Pick a colour for <b>%s</b>") % letters,
                                        reply_markup=_color_kb(channel_id, language), parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text("▣ یه عکس بفرست (نه متن)." if fa else "▣ Please send an image, not text.")
