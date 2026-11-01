"""Editing a screen in place, also when that screen is a picture.

A photo message (the style picker, a calendar card ...) has no text to edit: Telegram answers
'There is no text in the message to edit'. edit_or_resend() then deletes the picture and sends the screen as a
fresh message, so a button such as ‹ Back always lands on the screen it points to.
"""
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError


def _no_text(error: Exception) -> bool:
    return "no text in the message" in str(error).lower()


async def edit_or_resend(message, text: str, kb=None, **kwargs):
    try:
        return await message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, **kwargs)
    except BadRequest as e:
        if not _no_text(e):
            raise                      # 'not modified' etc.: the caller decides
    try:
        await message.delete()
    except TelegramError:
        pass                           # too old / already gone: the new message is still sent
    return await message.chat.send_message(text, reply_markup=kb, parse_mode=ParseMode.HTML, **kwargs)
