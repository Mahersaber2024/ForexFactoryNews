"""
Activity log group for the Forex news bot.

The admin sets the log group from the bot's admin panel (Admin → Log group): send the group ID and the
bot creates one topic per category by itself and posts the events there: new users, user activity,
admin actions, user management (ban / delete), backups and errors.
The group must be a Telegram *forum* group (Topics on) where the bot is an admin with "Manage topics".
Logging never raises and never slows the bot down.
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

LOG_GROUP_ID: Optional[int] = None


class Topics:
    """Logical topic ids (thread id 1 is the group's default "General" topic, so we start at 2)."""
    USER_JOIN = 2
    USER_ACTIVITY = 3
    ADMIN_ACTION = 4
    USER_MANAGEMENT = 5
    BACKUP = 6
    SYSTEM_ERROR = 7


TOPIC_NAMES = {
    Topics.USER_JOIN: "📢 New Users",
    Topics.USER_ACTIVITY: "🔄 User Activity",
    Topics.ADMIN_ACTION: "🔧 Admin Actions",
    Topics.USER_MANAGEMENT: "👥 User Management",
    Topics.BACKUP: "🗄 Backups",
    Topics.SYSTEM_ERROR: "💥 System Errors",
}

_TOPIC_THREAD_MAP: dict[int, int] = {}
_load_state: Callable[[], dict] | None = None
_save_state: Callable[[dict], None] | None = None


def init_logger(group_id: int, load_state: Callable[[], dict], save_state: Callable[[dict], None]) -> None:
    """group_id: the log group; load_state/save_state persist the created topic ids (the bot keeps them in its DB)."""
    global LOG_GROUP_ID, _load_state, _save_state, _TOPIC_THREAD_MAP
    LOG_GROUP_ID = int(group_id) if group_id else None
    _load_state, _save_state = load_state, save_state
    _TOPIC_THREAD_MAP = {}
    logger.info(f"Log group: {LOG_GROUP_ID}")


def enabled() -> bool:
    return bool(LOG_GROUP_ID)


def fire(coro) -> None:
    """Run a log coroutine in the background (never blocks or breaks the handler that called it)."""
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()


# ───────────────  topics  ───────────────
def _read_state() -> dict[int, int]:
    """Topic ids saved for THIS group (a different group id means start fresh)."""
    try:
        state = _load_state() if _load_state else {}
    except Exception as e:
        logger.error(f"Could not load topic state: {e}")
        return {}
    if not isinstance(state, dict) or state.get("group") != LOG_GROUP_ID:
        return {}
    return {int(k): int(v) for k, v in (state.get("topics") or {}).items()}


def _write_state() -> None:
    try:
        if _save_state:
            _save_state({"group": LOG_GROUP_ID, "topics": {str(k): v for k, v in _TOPIC_THREAD_MAP.items()}})
    except Exception as e:
        logger.error(f"Could not save topic state: {e}")


async def _create_topic(bot, topic_id: int) -> Optional[int]:
    try:
        res = await bot.create_forum_topic(chat_id=LOG_GROUP_ID, name=TOPIC_NAMES[topic_id], icon_color=0x6FB9F0)
    except Exception as e:
        logger.error(f"Could not create log topic {topic_id}: {e}")
        return None
    _TOPIC_THREAD_MAP[topic_id] = res.message_thread_id
    _write_state()
    return res.message_thread_id


async def create_all_topics(bot) -> bool:
    """Creates the topics that don't exist yet (Telegram can't list them, so ids are kept in the bot's DB)."""
    global _TOPIC_THREAD_MAP
    if not LOG_GROUP_ID:
        return False
    _TOPIC_THREAD_MAP = _read_state()
    for topic_id in TOPIC_NAMES:
        if topic_id in _TOPIC_THREAD_MAP:
            continue
        await _create_topic(bot, topic_id)
        await asyncio.sleep(0.5)          # stay under Telegram's rate limit
    return True


async def check_group(bot, group_id: int) -> dict:
    """Looks at a group before it is used: reachable? a forum? is the bot admin with Manage topics?"""
    info = {"ok": False, "title": "", "forum": False, "admin": False, "manage_topics": False, "error": ""}
    try:
        chat = await bot.get_chat(group_id)
        info["title"] = chat.title or ""
        info["forum"] = bool(getattr(chat, "is_forum", False))
        member = await bot.get_chat_member(group_id, bot.id)
        info["admin"] = member.status in ("administrator", "creator")
        info["manage_topics"] = member.status == "creator" or bool(getattr(member, "can_manage_topics", False))
        info["ok"] = True
    except Exception as e:
        info["error"] = str(e)
    return info


async def activate_group(bot, group_id: int, load_state: Callable[[], dict], save_state: Callable[[dict], None]) -> dict:
    """Switches logging to this group: creates the topics and posts a first message. Returns check_group()'s info."""
    info = await check_group(bot, group_id)
    if not info["ok"]:
        return info
    init_logger(group_id, load_state, save_state)
    await create_all_topics(bot)
    await log_bot_started(bot, "Log group connected")
    return info


def deactivate_group(load_state: Callable[[], dict], save_state: Callable[[dict], None]) -> None:
    init_logger(0, load_state, save_state)


# ───────────────  sending  ───────────────
async def send_log_message(topic_id: int, text: str, bot) -> None:
    if not LOG_GROUP_ID or bot is None:
        return
    if len(text) > 4000:
        text = text[:3990] + "…"
    for attempt in (1, 2):
        thread_id = _TOPIC_THREAD_MAP.get(topic_id)      # None → the group isn't a forum: plain message
        try:
            await bot.send_message(chat_id=LOG_GROUP_ID, text=text, parse_mode="HTML",
                                   message_thread_id=thread_id, disable_web_page_preview=True)
            return
        except Exception as e:
            if attempt == 1 and thread_id and "thread not found" in str(e).lower():
                _TOPIC_THREAD_MAP.pop(topic_id, None)    # someone deleted the topic: make a new one and retry
                await _create_topic(bot, topic_id)
                continue
            logger.error(f"Log message failed (topic {topic_id}): {e}")
            return


# ───────────────  formatting  ───────────────
def _e(v) -> str:
    return html.escape(str(v), quote=False)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def user_block(user_id: int, username: str | None = None, first_name: str | None = None,
               last_name: str | None = None) -> str:
    name = " ".join(p for p in (first_name, last_name) if p) or "Unknown"
    uname = f"@{username}" if username else "None"
    return (f"📛 Name: {_e(name)}\n"
            f"🔰 Username: {_e(uname)}\n"
            f"🆔 ID: <code>{user_id}</code>")


# ───────────────  events  ───────────────
async def log_bot_started(bot, details: str = "") -> None:
    text = f"✅ <b>Bot started</b>\n⏰ {_now()}"
    if details:
        text += f"\n{_e(details)}"
    await send_log_message(Topics.SYSTEM_ERROR, text, bot)


async def log_user_join(bot, user_id: int, username=None, first_name=None, last_name=None,
                        language_code: str | None = None) -> None:
    text = ("📢 <b>New user</b>\n\n" + user_block(user_id, username, first_name, last_name)
            + (f"\n🌐 Telegram language: {_e(language_code)}" if language_code else "")
            + f"\n⏰ {_now()}")
    await send_log_message(Topics.USER_JOIN, text, bot)


async def log_user_activity(bot, user_id: int, action: str, details: str = "", username=None,
                            first_name=None, last_name=None) -> None:
    text = (f"🔄 <b>{_e(action)}</b>\n\n" + user_block(user_id, username, first_name, last_name)
            + (f"\n{_e(details)}" if details else "") + f"\n⏰ {_now()}")
    await send_log_message(Topics.USER_ACTIVITY, text, bot)


async def log_admin_action(bot, admin_id: int, action: str, details: str = "", username=None,
                           first_name=None, last_name=None) -> None:
    text = (f"🔧 <b>{_e(action)}</b>\n\n👮 Admin:\n" + user_block(admin_id, username, first_name, last_name)
            + (f"\n\n{_e(details)}" if details else "") + f"\n⏰ {_now()}")
    await send_log_message(Topics.ADMIN_ACTION, text, bot)


async def log_user_management(bot, admin_id: int, action: str, target_id: int, target_label: str = "",
                              details: str = "") -> None:
    text = (f"👥 <b>{_e(action)}</b>\n\n"
            f"🎯 User: <code>{target_id}</code>" + (f" · {_e(target_label)}" if target_label else "")
            + f"\n👮 By admin: <code>{admin_id}</code>"
            + (f"\n{_e(details)}" if details else "") + f"\n⏰ {_now()}")
    await send_log_message(Topics.USER_MANAGEMENT, text, bot)


async def log_backup(bot, action: str, details: str = "") -> None:
    text = f"🗄 <b>{_e(action)}</b>" + (f"\n\n{_e(details)}" if details else "") + f"\n⏰ {_now()}"
    await send_log_message(Topics.BACKUP, text, bot)


async def log_system_error(bot, error: str, context: str = "") -> None:
    text = (f"💥 <b>Error</b>" + (f" · {_e(context)}" if context else "")
            + f"\n<pre>{_e(error[-1500:])}</pre>\n⏰ {_now()}")
    await send_log_message(Topics.SYSTEM_ERROR, text, bot)
