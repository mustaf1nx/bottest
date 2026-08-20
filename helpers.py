"""Formatting, network retry, text parsing, and UI helper functions."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import timedelta
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from config import (
    AMBIGUOUS_CS_QUESTION,
    CS_CODE_PATTERN,
    JOIN_CALLBACK_PREFIX,
    JOIN_START_PREFIX,
    MEMBER_STATUSES,
    QUESTION_PATTERN,
    SELF_ID_PREFIX_PATTERN,
    swap_keyboard_layout,
)
from markov import MarkovChain
from models import OPProgram
from registry import OPRegistry

LOGGER = logging.getLogger(__name__)


def format_admin_tag(admin: str) -> str:
    """Format admin string to a valid Telegram HTML tag or handle."""
    admin_str = admin.strip()
    if not admin_str:
        return "@admin"
    if admin_str.isdigit():
        return f'<a href="tg://user?id={admin_str}">Администратор</a>'
    if not admin_str.startswith("@") and not admin_str.startswith("<a "):
        admin_str = f"@{admin_str}"
    return html.escape(admin_str) if not admin_str.startswith("<a ") else admin_str


def get_user_mention(user: object | None) -> str:
    """Safely build user HTML mention with fallback for missing, empty, or single-character names."""
    if user is None:
        return "Студент"

    full_name = getattr(user, "full_name", "") or ""
    full_name = full_name.strip()
    username = getattr(user, "username", None)
    user_id = getattr(user, "id", None)

    # If full name is empty, whitespace, or just punctuation (like "." or "-")
    if not full_name or not any(c.isalnum() for c in full_name):
        if username:
            return f"@{html.escape(username)}"
        if user_id:
            return f'<a href="tg://user?id={user_id}">Студент</a>'
        return "Студент"

    mention_func = getattr(user, "mention_html", None)
    if callable(mention_func):
        res = mention_func()
        if res:
            return res

    if user_id:
        return f'<a href="tg://user?id={user_id}">{html.escape(full_name)}</a>'
    return html.escape(full_name)



def build_welcome_text(chain: MarkovChain, mention: str, max_words: int) -> str:
    """Build dynamic Markov welcome message with mention and onboarding reply hint."""
    generated = html.escape(chain.generate(max_words=max_words))
    return (
        f"{generated}\n\n"
        f"Рады видеть тебя, {mention}! 👋\n\n"
        "💡 <b>Ответь на это сообщение (Reply)</b>, указав свою ОП (например: <code>SE</code>, <code>CS</code>, <code>IT</code>), чтобы узнать своего ответственного админа!"
    )


def build_op_chat_welcome_text(mention: str, op: OPProgram) -> str:
    """Build welcome text for dedicated OP chat."""
    return (
        f"Привет, {mention}! 👋 Это чат ОП <b>{html.escape(op.code)}</b> "
        f"({html.escape(op.name)}).\n\n"
        "Пожалуйста, ознакомься с правилами в описании группы, а также с гайдом в закрепленном сообщении."
    )


def build_op_response(user_mention: str, op: OPProgram) -> str:
    """Build reply message for detected educational program."""
    admin_tag = format_admin_tag(op.admin)
    text = (
        f"Привет, {user_mention}! 👋\n\n"
        f"📍 <b>ОП: {html.escape(op.code)} ({html.escape(op.name)})</b>\n"
        f"🏫 <i>{html.escape(op.school)}</i>\n"
        f"👤 Ответственный администратор: {admin_tag}"
    )
    if op.chat_id:
        text += (
            "\n\n🔐 Нажмите кнопку ниже, чтобы получить доступ к чату своей ОП. "
            "Кнопка сработает только у вас."
        )
    return text


def build_start_deep_link(bot_username: str, op_code: str) -> str:
    """Generate deep link URL (e.g. t.me/bot?start=join_SE)."""
    return f"https://t.me/{bot_username}?start={JOIN_START_PREFIX}{op_code}"


def build_join_keyboard(op: OPProgram, user_id: int) -> InlineKeyboardMarkup | None:
    """Build inline keyboard button tied to specific user ID."""
    if not op.chat_id:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🔐 Вступить в чат {op.code}",
                    callback_data=f"{JOIN_CALLBACK_PREFIX}:{op.code}:{user_id}",
                )
            ]
        ]
    )


def is_connection_error(error: BaseException | None) -> bool:
    """Return True if exception chain contains network or connection issues."""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, (NetworkError, OSError)):
            return True
        name = type(current).__name__
        if "Connect" in name or "Timeout" in name or "Network" in name or "Protocol" in name:
            return True
        current = current.__cause__ or current.__context__
    return False


def is_connect_timeout(error: BaseException) -> bool:
    """Return True when exception chain indicates connection error or timeout."""
    return is_connection_error(error)


async def reply_with_connect_retry(message: object, text: str, **kwargs: object) -> Any:
    """Send reply message with exponential backoff on connection errors and flood wait handling."""
    connection_retry_delays = (1.0, 2.0)
    max_flood_retries = 3
    connection_attempt = 0
    flood_attempt = 0
    while True:
        try:
            return await message.reply_text(text, **kwargs)  # type: ignore[attr-defined]
        except RetryAfter as error:
            flood_attempt += 1
            if flood_attempt > max_flood_retries:
                raise
            raw_delay = error.retry_after
            seconds = (
                raw_delay.total_seconds()
                if isinstance(raw_delay, timedelta)
                else float(raw_delay)
            )
            delay = max(seconds, 1.0)
            LOGGER.warning(
                "Ограничение Telegram (flood control): жду %.0f с перед повтором отправки",
                delay,
            )
            await asyncio.sleep(delay)
        except NetworkError as error:
            if (
                isinstance(error, BadRequest)
                or not is_connection_error(error)
                or connection_attempt >= len(connection_retry_delays)
            ):
                raise
            delay = connection_retry_delays[connection_attempt]
            connection_attempt += 1
            LOGGER.warning(
                "Telegram недоступен при подключении; повтор отправки через %.0f с",
                delay,
            )
            await asyncio.sleep(delay)


def is_inquiry_or_question(text: str) -> bool:
    """Check if message is a general question rather than an OP declaration."""
    if not text:
        return False
    if QUESTION_PATTERN.search(text):
        return True
    if "?" in text and not SELF_ID_PREFIX_PATTERN.search(text):
        return True
    return False


def is_likely_op_declaration(text: str, matched_ops: list[OPProgram]) -> bool:
    """Check if text looks like a student self-identifying their OP."""
    if not text or not matched_ops:
        return False

    clean_text = text.strip()
    words = clean_text.split()
    word_count = len(words)

    if word_count <= 4:
        return True

    if SELF_ID_PREFIX_PATTERN.search(clean_text):
        return True

    text_lower = clean_text.lower()
    text_swapped = swap_keyboard_layout(clean_text).lower()
    for op in matched_ops:
        for alias in op.aliases:
            alias_clean = alias.strip().lower()
            if len(alias_clean) >= 4 and (alias_clean in text_lower or alias_clean in text_swapped):
                return True
        if len(op.name) >= 4 and (op.name.lower() in text_lower or op.name.lower() in text_swapped):
            return True

    return False


def resolve_cs_choice(op_registry: Any, text: str) -> OPProgram | None:
    """Resolve CS ambiguous prompt (Cybersecurity vs Computer Science/IT)."""
    if not text:
        return None
    clean = text.strip().lower()
    if clean in ("1", "it", "ит", "bda"):
        op = op_registry.get("IT")
        if op:
            return op
    if clean in ("2", "cs", "кс", "кибербез"):
        op = op_registry.get("CS")
        if op:
            return op
    if CS_CODE_PATTERN.search(text):
        return op_registry.get("CS")
    matched = [op for op in op_registry.find_matching_ops(text) if op.code in ("IT", "CS")]
    if len(matched) == 1:
        return matched[0]
    return None


async def is_chat_member(bot: object, chat_id: int, user_id: int) -> bool:
    """Check if user is a member/admin in specified chat."""
    try:
        membership = await bot.get_chat_member(chat_id, user_id)  # type: ignore[attr-defined]
    except TelegramError:
        return False
    if membership.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(membership, "is_member", False))
    return membership.status in MEMBER_STATUSES


async def find_existing_op_membership(
    bot: object, op_registry: OPRegistry, user_id: int
) -> OPProgram | None:
    """Check whether the user already sits in one of the OP chats.

    Used to stop spamming the "какая у вас ОП?" welcome in the main
    first-year chat for people who were already placed into their OP chat
    earlier (e.g. re-joined the main chat, or the join event fired twice).
    Checks every linked OP chat concurrently so a large number of OPs
    doesn't slow down every single new-member greeting.
    """
    ops_with_chat = [op for op in op_registry.get_all().values() if op.chat_id]
    if not ops_with_chat:
        return None

    results = await asyncio.gather(
        *(is_chat_member(bot, op.chat_id, user_id) for op in ops_with_chat)
    )
    for op, is_member in zip(ops_with_chat, results):
        if is_member:
            return op
    return None
