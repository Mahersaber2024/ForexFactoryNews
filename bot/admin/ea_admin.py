"""
/admin → ▤ Downloads

Manage everything in the website's downloads folder (assets/downloads): EA builds, PDFs, zips, set files...
Upload new files or delete any of them. The download page (/download) reads that folder, so nothing else to do.
Module name kept as ea_admin (and callbacks as "ea:") so old buttons and imports keep working.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as KB, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from bot.ui_edit import edit_or_resend

import bot_settings as cfg

EA_EXT = (".ex5", ".ex4")
# Never put files the web server would run/render as part of the site in a public folder (XSS etc.)
BLOCKED_EXT = (".html", ".htm", ".xhtml", ".svg", ".js", ".mjs", ".php", ".phtml", ".cgi", ".py", ".sh", ".htaccess")
MAX_BYTES = 20 * 1024 * 1024        # Telegram bots can download up to 20 MB
AWAIT_UPLOAD = "ea_upload"
RULE = "──────────────"
# Same patterns downloadpage.html uses to recognise EA builds
PAGE_PATTERNS = [
    re.compile(r"^HeySolo\[ATM\](?:_[A-Za-z0-9-]+)?v\d+(?:\.\d+)*\.(ex5|ex4)$", re.I),
    re.compile(r"^Heysolo-SecTM(?:_[A-Za-z0-9-]+)?v\d+(?:\.\d+)*\.(ex5|ex4)$", re.I),
    re.compile(r"^Heysolo-ForwardTester_(?:[A-Za-z0-9-]+)?v\d+(?:\.\d+)*\.(ex5|ex4)$", re.I),
]
SAFE_NAME = re.compile(r"^[A-Za-z0-9._\-\[\]() ]+$")


def available() -> bool:
    """Only where this server hosts the site (Flask + nginx run here)."""
    return cfg.RUN_FLASK and cfg.DOWNLOADS_DIR.parent.is_dir()


def _size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024:.0f} KB" if n >= 1024 else f"{n} B"


def list_files() -> list[Path]:
    """Every file in the downloads folder (hidden files and half-finished uploads excluded)."""
    d = cfg.DOWNLOADS_DIR
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir() if p.is_file() and not p.name.startswith(".")]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


list_builds = list_files   # old name


def summary() -> str:
    files = list_files()
    total = sum(p.stat().st_size for p in files)
    return f"{len(files)} file{'' if len(files) == 1 else 's'} · {_size(total)}" if files else "empty"


def _is_ea(p: Path | str) -> bool:
    return str(p).lower().endswith(EA_EXT)


def _hidden_on_page(name: str) -> bool:
    """EA build whose name the download page won't pick up."""
    return _is_ea(name) and not any(r.match(name) for r in PAGE_PATTERNS)


def _download_page() -> str:
    return f"{cfg.PUBLIC_BASE_URL}/download" if cfg.PUBLIC_BASE_URL else ""


async def _edit(msg, text: str, kb: KB):
    try:
        await edit_or_resend(msg, text, kb, disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ───────────────  views  ────────────────
def _list_view(ctx) -> tuple[str, KB]:
    files = list_files()
    ctx.user_data["ea_files"] = [p.name for p in files]
    lines = ["<b>DOWNLOADS</b>", RULE, f"{summary()}"]
    if _download_page():
        lines.append(_download_page())
    lines.append("")
    if files:
        lines.append("Tap a file to manage it.")
        if any(_hidden_on_page(p.name) for p in files):
            lines.append("<i>△ = EA name not recognised, it won't show on the page</i>")
    else:
        lines.append("Nothing here yet.")
    rows = []
    for i, p in enumerate(files):
        st = p.stat()
        mark = "△ " if _hidden_on_page(p.name) else ""
        rows.append([B(f"{mark}{p.name}  ·  {_size(st.st_size)}  ·  {datetime.fromtimestamp(st.st_mtime):%b %d}",
                       callback_data=f"ea:open:{i}")])
    rows.append([B("＋ Upload", callback_data="ea:up"), B("← Back", callback_data="admin_menu")])
    return "\n".join(lines), KB(rows)


def _file_view(path: Path, idx: int) -> tuple[str, KB]:
    st = path.stat()
    lines = ["<b>FILE</b>", RULE, f"<code>{path.name}</code>", "",
             f"Size      {_size(st.st_size)}",
             f"Updated   {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}"]
    if _is_ea(path):
        lines.append("Page      " + ("△ hidden (name not recognised)" if _hidden_on_page(path.name) else "● listed"))
    if cfg.PUBLIC_BASE_URL:
        lines.append(f"\n{cfg.PUBLIC_BASE_URL}/assets/downloads/{path.name}")
    kb = KB([[B("✕ Delete", callback_data=f"ea:del:{idx}"), B("← Back", callback_data="ea:list")]])
    return "\n".join(lines), kb


# ───────────────  callbacks  ────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    act, arg = parts[1], (parts[2] if len(parts) > 2 else "")

    if act == "list":
        ctx.user_data.pop("awaiting", None)
        await q.answer()
        await _edit(q.message, *_list_view(ctx))
        return

    if act == "up":
        ctx.user_data["awaiting"] = AWAIT_UPLOAD
        await q.answer()
        await _edit(q.message,
                    "<b>UPLOAD</b>\n" + RULE + "\n"
                    "Send the file(s) here as <b>File</b> (max 20 MB).\n"
                    "Any type: .ex5, .ex4, .pdf, .zip, .set …\n"
                    "Same name replaces the old one.\n\n"
                    "<i>EA names must match the page, e.g.</i> <code>HeySolo[ATM]v4.2.ex5</code>",
                    KB([[B("✓ Done", callback_data="ea:list")]]))
        return

    names = ctx.user_data.get("ea_files", [])
    try:
        name = names[int(arg)]
    except (ValueError, IndexError):
        await q.answer("List expired, refreshed.")
        await _edit(q.message, *_list_view(ctx))
        return
    path = cfg.DOWNLOADS_DIR / name
    if not path.is_file():
        await q.answer("That file is gone.")
        await _edit(q.message, *_list_view(ctx))
        return

    if act == "open":
        await q.answer()
        await _edit(q.message, *_file_view(path, int(arg)))
        return

    if act == "del":
        await q.answer()
        await _edit(q.message,
                    f"<b>DELETE FILE</b>\n{RULE}\n<code>{name}</code>\n\nIt disappears from the site right away.",
                    KB([[B("✕ Delete", callback_data=f"ea:delok:{arg}"), B("← Cancel", callback_data=f"ea:open:{arg}")]]))
        return

    if act == "delok":
        path.unlink(missing_ok=True)
        await q.answer(f"Deleted {name}")
        await _edit(q.message, *_list_view(ctx))
        return

    await q.answer()


# ───────────────  uploads  ────────────────
def wants_document(update: Update, ctx) -> bool:
    if not available():
        return False
    aw = ctx.user_data.get("awaiting")
    name = (update.message.document.file_name or "").lower()
    # Any file while ＋ Upload is open; outside it, an .ex5/.ex4 is still a shortcut
    return aw == AWAIT_UPLOAD or (not aw and name.endswith(EA_EXT))


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc = msg.document
    name = Path(doc.file_name or "").name
    if not name or name.startswith(".") or not SAFE_NAME.match(name):
        await msg.reply_text("✕ Use a simple file name (letters, numbers, . _ - [ ] ( ) only).")
        return
    if name.lower().endswith(BLOCKED_EXT):
        await msg.reply_text("✕ Web pages and scripts can't go in the downloads folder. Zip it first.")
        return
    if doc.file_size and doc.file_size > MAX_BYTES:
        await msg.reply_text("✕ Telegram bots can only download files up to 20 MB.")
        return

    d = cfg.DOWNLOADS_DIR
    d.mkdir(parents=True, exist_ok=True)
    dest = d / name
    existed = dest.exists()
    fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=str(d))
    os.close(fd)
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(custom_path=tmp)
        os.chmod(tmp, 0o644)           # nginx (www-data) must be able to read it
        os.replace(tmp, dest)
    except Exception as e:
        Path(tmp).unlink(missing_ok=True)
        await msg.reply_text(f"✕ Upload failed: {e}")
        return

    text = f"✓ {'Replaced' if existed else 'Uploaded'}  <code>{name}</code>  ·  {_size(dest.stat().st_size)}"
    if _hidden_on_page(name):
        text += ("\n\n△ The download page won't recognise this EA name. Use e.g.\n"
                 "<code>HeySolo[ATM]v4.2.ex5</code>\n<code>HeySolo[ATM]_PROPv4.2.ex5</code>\n"
                 "<code>Heysolo-SecTMv1.0.ex5</code>")
    if _download_page():
        text += f"\n\n{_download_page()}"
    await msg.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                         reply_markup=KB([[B("▤ Downloads", callback_data="ea:list")]]))
