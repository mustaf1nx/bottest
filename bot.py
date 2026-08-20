"""Telegram bot that welcomes new members using Markov-chain text and handles OP routing."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# Re-exports for backward compatibility and clean public API
from config import (
    AMBIGUOUS_CS_QUESTION,
    BASE_DIR,
    CS_CODE_PATTERN,
    DEFAULT_OPS,
    EN_LAYOUT,
    JOIN_CALLBACK_PREFIX,
    JOIN_START_PREFIX,
    MEMBER_STATUSES,
    QUESTION_PATTERN,
    RU_LAYOUT,
    SELF_ID_PREFIX_PATTERN,
    Settings,
    parse_bool,
    parse_id_list,
    swap_keyboard_layout,
)
from database import DatabaseStorage, PostgresStorage
from handlers import (
    allow_private_messages,
    ask_cs_clarification,
    cleanup_onboarding_thread,
    deliver_invite,
    deliver_invite_via_deep_link,
    describe_chat_readiness,
    handle_chat_join_request,
    handle_join_button,
    handle_op_message,
    is_authorized_admin,
    log_error,
    preview,
    set_op_admin,
    set_op_chat,
    show_ids,
    show_op_chats,
    show_ops,
    start,
    unknown_command,
    welcome_chat_member_update,
    welcome_new_members,
)
from helpers import (
    build_join_keyboard,
    build_op_chat_welcome_text,
    build_op_response,
    build_start_deep_link,
    build_welcome_text,
    find_existing_op_membership,
    format_admin_tag,
    is_chat_member,
    is_connect_timeout,
    is_connection_error,
    is_inquiry_or_question,
    is_likely_op_declaration,
    reply_with_connect_retry,
    resolve_cs_choice,
)
from invites import InviteError, InviteManager
from markov import MarkovChain
from models import AddOutcome, AddResult, IssueResult, OPProgram, PendingInvite
from registry import AdminRegistry, OPRegistry
from tracker import WelcomeTracker
from userbot import UserbotAdder

LOGGER = logging.getLogger(__name__)

__all__ = [
    "BASE_DIR",
    "Settings",
    "parse_bool",
    "parse_id_list",
    "swap_keyboard_layout",
    "EN_LAYOUT",
    "RU_LAYOUT",
    "DEFAULT_OPS",
    "OPProgram",
    "PendingInvite",
    "IssueResult",
    "AddOutcome",
    "AddResult",
    "DatabaseStorage",
    "PostgresStorage",
    "AdminRegistry",
    "OPRegistry",
    "WelcomeTracker",
    "InviteManager",
    "InviteError",
    "MarkovChain",
    "UserbotAdder",
    "format_admin_tag",
    "build_welcome_text",
    "build_op_chat_welcome_text",
    "build_op_response",
    "build_start_deep_link",
    "build_join_keyboard",
    "is_connection_error",
    "is_connect_timeout",
    "reply_with_connect_retry",
    "is_inquiry_or_question",
    "is_likely_op_declaration",
    "resolve_cs_choice",
    "is_chat_member",
    "find_existing_op_membership",
    "is_authorized_admin",
    "describe_chat_readiness",
    "cleanup_onboarding_thread",
    "deliver_invite",
    "deliver_invite_via_deep_link",
    "handle_join_button",
    "handle_chat_join_request",
    "ask_cs_clarification",
    "handle_op_message",
    "show_ops",
    "set_op_admin",
    "set_op_chat",
    "show_op_chats",
    "welcome_new_members",
    "welcome_chat_member_update",
    "unknown_command",
    "log_error",
    "start",
    "preview",
    "allow_private_messages",
    "show_ids",
    "create_application",
    "main",
]


def create_application(settings: Settings) -> Application:
    """Build and configure the Telegram application."""
    greeting_chain = MarkovChain.from_file(
        settings.greetings_path,
        order=settings.markov_order,
    )

    db: DatabaseStorage | None = None
    if settings.database_url:
        try:
            db = DatabaseStorage(settings.database_url)
            LOGGER.info("База данных подключена: %s", settings.database_url)
        except Exception as error:
            LOGGER.error("Не удалось инициализировать БД (%s), используется файл", error)

    admins = AdminRegistry(
        path=settings.admins_path,
        initial_ids=settings.initial_admin_ids,
        db=db,
    )
    op_registry = OPRegistry(
        path=settings.op_admins_path,
        db=db,
    )
    welcome_tracker = WelcomeTracker(max_messages=5)
    invite_manager = InviteManager(
        path=settings.invites_path,
        ttl_seconds=settings.invite_ttl_seconds,
        hourly_limit=settings.invite_hourly_limit,
        db=db,
    )
    userbot = UserbotAdder.from_environment(
        settings.telethon_api_id,
        settings.telethon_api_hash,
        settings.telethon_session,
    )

    request = HTTPXRequest(
        connect_timeout=20,
        read_timeout=20,
        write_timeout=20,
        pool_timeout=5,
    )
    updates_request = HTTPXRequest(
        connection_pool_size=1,
        connect_timeout=20,
        read_timeout=30,
        write_timeout=20,
        pool_timeout=5,
    )

    async def on_startup(app: Application) -> None:
        # Записываем момент запуска бота: события вступления (join), которые
        # произошли раньше этого момента (например, пока бот был выключен
        # между деплоями), не должны приводить к приветствию — Telegram
        # доставит их одним пакетом сразу при подключении бота.
        app.bot_data["startup_time"] = datetime.now(timezone.utc)
        if userbot is not None:
            await userbot.start()
        await invite_manager.sweep(app.bot)

    async def on_shutdown(app: Application) -> None:
        if userbot is not None:
            await userbot.stop()

    application = (
        Application.builder()
        .token(settings.token)
        .request(request)
        .get_updates_request(updates_request)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    application.bot_data.update(
        {
            "greeting_chain": greeting_chain,
            "admins": admins,
            "op_registry": op_registry,
            "welcome_tracker": welcome_tracker,
            "invites": invite_manager,
            "userbot": userbot,
            "settings": settings,
            "db": db,
            "startup_time": None,
        }
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("preview", preview))
    application.add_handler(CommandHandler("allowpm", allow_private_messages))
    application.add_handler(CommandHandler("id", show_ids))
    application.add_handler(CommandHandler("ops", show_ops))
    application.add_handler(CommandHandler("setopadmin", set_op_admin))
    application.add_handler(
        ChatMemberHandler(welcome_chat_member_update, ChatMemberHandler.CHAT_MEMBER)
    )
    application.add_handler(CommandHandler("setopchat", set_op_chat))
    application.add_handler(CommandHandler("setopgroup", set_op_chat))
    application.add_handler(CommandHandler("opchats", show_op_chats))
    application.add_handler(
        CallbackQueryHandler(handle_join_button, pattern=rf"^{JOIN_CALLBACK_PREFIX}:")
    )
    application.add_handler(ChatJoinRequestHandler(handle_chat_join_request))
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_op_message)
    )
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(log_error)

    return application


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_environment()
    LOGGER.info("Корпус приветствий: %s", settings.greetings_path)
    create_application(settings).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
