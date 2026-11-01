"""
Concurrency for the Telegram bot.

By default python-telegram-bot handles updates ONE AT A TIME: while one user's slow request (fetching news,
drawing a picture) runs, every other user waits, and the bot looks frozen. PerChatUpdateProcessor handles
many chats at the same time but keeps each chat's own updates in order (so a user's clicks and messages can't
race each other). A flood from one chat is capped so it can't starve the others.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from telegram.ext import BaseUpdateProcessor

logger = logging.getLogger(__name__)

MAX_QUEUED_PER_CHAT = 8       # updates of ONE chat waiting at the same time; more are dropped (flood protection)


class PerChatUpdateProcessor(BaseUpdateProcessor):
    def __init__(self, max_concurrent_updates: int = 256):
        super().__init__(max_concurrent_updates)
        self._chats: dict = {}            # chat key -> [asyncio.Lock, number of updates inside / waiting]

    @staticmethod
    def _key(update):
        chat = getattr(update, "effective_chat", None)
        if chat is not None:
            return chat.id
        user = getattr(update, "effective_user", None)
        return user.id if user is not None else None

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def do_process_update(self, update, coroutine) -> None:
        key = self._key(update)
        if key is None:
            await coroutine
            return
        entry = self._chats.get(key)
        if entry is None:
            entry = self._chats[key] = [asyncio.Lock(), 0]
        if entry[1] >= MAX_QUEUED_PER_CHAT:
            logger.debug("Chat %s sent too many updates at once: dropping one", key)
            close = getattr(coroutine, "close", None)
            if close:
                close()                    # avoids a 'coroutine was never awaited' warning
            return
        entry[1] += 1
        try:
            async with entry[0]:
                await coroutine
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._chats.get(key) is entry:
                del self._chats[key]


def install_default_executor(loop: asyncio.AbstractEventLoop, workers: int) -> None:
    """asyncio.to_thread() uses a small default pool (about 5-36 threads). Give the bot a bigger one for the
    blocking parts (database, HTTP, picture drawing)."""
    loop.set_default_executor(ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bot-io"))
