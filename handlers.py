"""Telegram event and command handlers."""

from __future__ import annotations

import asyncio
import html
import logging
from typing import TYPE_CHECKING

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from config import JOIN_CALLBACK_PREFIX, JOIN_START_PREFIX
from helpers import (
    build_join_keyboard,
    build_op_chat_welcome_text,
    build_op_response,
    build_start_deep_link,
    build_welcome_text,
    find_existing_op_membership,
    format_admin_tag,
    get_user_mention,
    is_chat_member,
    is_connection_error,
    is_inquiry_or_question,
    is_likely_op_declaration,
    reply_with_connect_retry,
    resolve_cs_choice,
)
from markov import MarkovChain
from models import OPProgram, PendingInvite

if TYPE_CHECKING:
    from config import Settings
    from database import DatabaseStorage
    from invites import InviteError, InviteManager
    from registry import AdminRegistry, OPRegistry
    from tracker import WelcomeTracker
    from userbot import UserbotAdder

LOGGER = logging.getLogger(__name__)


async def is_authorized_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Check if the user invoking the command has administrative rights."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    registry: AdminRegistry = context.application.bot_data["admins"]
    if registry.contains(user.id):
        return True
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        try:
            membership = await context.bot.get_chat_member(chat.id, user.id)
        except TelegramError:
            return False
        return membership.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    if message.chat.type == ChatType.PRIVATE and context.args:
        payload = context.args[0]
        if payload.startswith(JOIN_START_PREFIX):
            op_code = payload[len(JOIN_START_PREFIX) :].upper()
            await deliver_invite_via_deep_link(update, context, op_code)
            return

    registry: AdminRegistry = context.application.bot_data["admins"]
    if message.chat.type == ChatType.PRIVATE and not registry.contains(user.id):
        await reply_with_connect_retry(
            message,
            "Привет! 👋 Теперь я смогу присылать тебе ссылку на чат твоей ОП "
            "сюда, в личные сообщения, а не в общий чат.\n\n"
            "Вернись в чат первого курса, ответь на моё приветствие кодом "
            "своей ОП (например SE) и нажми кнопку доступа.",
        )
        return
    await reply_with_connect_retry(
        message,
        "Я подключён и приветствую новых участников фразами, созданными "
        "марковской цепью. Команда проверки: /preview",
    )


async def deliver_invite_via_deep_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE, op_code: str
) -> None:
    """/start join_<КОД_ОП> — delivery of previously issued join links via deep link."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    op = op_registry.get(op_code)
    if op is None or not op.chat_id:
        await reply_with_connect_retry(
            message,
            f"❌ ОП '{html.escape(op_code)}' не найдена или чат ещё не подключён. "
            "Список: /ops",
        )
        return

    if await is_chat_member(context.bot, op.chat_id, user.id):
        await reply_with_connect_retry(message, "Вы уже состоите в чате этой ОП 🙂")
        return

    manager: InviteManager = context.application.bot_data["invites"]
    pending = manager.find_active(user.id, op.chat_id)
    if pending is None:
        await reply_with_connect_retry(
            message,
            "У вас нет активной заявки на этот чат. Сначала ответьте в общем "
            f"чате первого курса своей ОП (<b>{html.escape(op.code)}</b>) и "
            "нажмите кнопку доступа там — я сразу пришлю ссылку сюда.",
            parse_mode=ParseMode.HTML,
        )
        return

    user_mention = user.mention_html()
    try:
        notice = await deliver_invite(context, pending, op, user_mention, None)
    except Exception:
        LOGGER.exception(
            "Не удалось доставить ссылку через /start пользователю %s (ОП %s)",
            user.id,
            op.code,
        )
        await manager.retire(context.bot, pending)
        await reply_with_connect_retry(
            message,
            "Не получилось отправить ссылку — сбой соединения с Telegram. "
            "Нажмите кнопку в чате ещё раз через минуту.",
        )
        return
    await reply_with_connect_retry(message, notice)


async def preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show an example welcome text without waiting for a new member."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    registry: AdminRegistry = context.application.bot_data["admins"]
    if message.chat.type == ChatType.PRIVATE and not registry.contains(user.id):
        await reply_with_connect_retry(
            message,
            "Доступ закрыт. Если вы администратор, выполните /allowpm в группе.",
        )
        return

    chain: MarkovChain = context.application.bot_data["greeting_chain"]
    settings: Settings = context.application.bot_data["settings"]
    text = build_welcome_text(chain, get_user_mention(user), settings.max_words)
    await reply_with_connect_retry(message, text, parse_mode=ParseMode.HTML)


async def allow_private_messages(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Authorize a group administrator for private messaging with the bot."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await reply_with_connect_retry(message, "Эту команду нужно выполнить в группе.")
        return

    try:
        membership = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        await reply_with_connect_retry(
            message,
            "Не удалось проверить статус. Назначьте бота администратором группы "
            "и повторите /allowpm.",
        )
        return
    if membership.status not in (
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ):
        await reply_with_connect_retry(
            message, "Команда доступна только администраторам группы."
        )
        return

    registry: AdminRegistry = context.application.bot_data["admins"]
    added = registry.add(user.id)
    status = "Доступ к личным сообщениям открыт." if added else "Доступ уже был открыт."
    await reply_with_connect_retry(
        message, f"{status} Теперь напишите мне в личный чат."
    )


async def show_ids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message and user and chat:
        await reply_with_connect_retry(
            message, f"Ваш ID: {user.id}\nID этого чата: {chat.id}"
        )


async def cleanup_onboarding_thread(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int | None, user_id: int
) -> None:
    """Clean up the entire onboarding Q&A thread in the main chat once member joined."""
    if chat_id is None:
        return
    tracker: WelcomeTracker = context.application.bot_data["welcome_tracker"]
    message_ids = tracker.pop_thread_messages(chat_id, user_id)
    for message_id in message_ids:
        try:
            await context.bot.delete_message(chat_id, message_id)
        except TelegramError as error:
            LOGGER.debug(
                "Не удалось удалить сообщение %s в чате %s: %s",
                message_id,
                chat_id,
                error,
            )


async def deliver_invite(
    context: ContextTypes.DEFAULT_TYPE,
    invite: PendingInvite,
    op: OPProgram,
    user_mention: str,
    source_message: Message | None,
) -> str:
    """Deliver personal invite link via PM, or fallback to deep link / group card."""
    manager: InviteManager = context.application.bot_data["invites"]
    settings: Settings = context.application.bot_data["settings"]
    minutes = max(1, invite.seconds_left // 60)

    private_text = (
        f"🔐 Персональная ссылка в чат <b>{html.escape(op.code)}</b> "
        f"({html.escape(op.name)}):\n\n{html.escape(invite.invite_link)}\n\n"
        f"⏳ Действует {minutes} мин. и только для вашего аккаунта: "
        "заявки от других людей по этой ссылке отклоняются автоматически."
    )

    try:
        await context.bot.send_message(
            chat_id=invite.user_id,
            text=private_text,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except (Forbidden, TelegramError):
        LOGGER.info("Личные сообщения пользователю %s недоступны", invite.user_id)
    else:
        asyncio.create_task(manager.expire_later(context.bot, invite))
        return "Ссылка отправлена вам в личные сообщения ✅"

    if not settings.invite_group_fallback or source_message is None:
        return (
            "Напишите мне в личные сообщения /start и нажмите кнопку ещё раз — "
            "ссылку я отправлю туда."
        )

    bot_username = context.bot.username
    if bot_username:
        deep_link = build_start_deep_link(bot_username, op.code)
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("▶️ Открыть бота и получить ссылку", url=deep_link)]]
        )
        addition = (
            "\n\n▶️ Чтобы получить ссылку, откройте диалог со мной и нажмите "
            "Start — пришлю её сразу в личные сообщения."
        )
        original_html = getattr(source_message, "text_html", None)
        if original_html:
            try:
                await source_message.edit_text(
                    original_html + addition,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                return (
                    "Нажмите кнопку в этом сообщении, чтобы открыть бота и "
                    "получить ссылку в личные сообщения."
                )
            except TelegramError as error:
                LOGGER.debug("Не удалось отредактировать карточку ОП: %s", error)
        prompt_text = (
            f"{user_mention}, чтобы получить персональную ссылку в чат "
            f"<b>{html.escape(op.code)}</b>, откройте диалог со мной и нажмите "
            "Start — ссылку пришлю сразу в личные сообщения."
        )
        await reply_with_connect_retry(
            source_message,
            prompt_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return "Нажмите кнопку в чате, чтобы открыть бота и получить ссылку в личные сообщения."

    group_text = (
        f"{user_mention}, ваша персональная ссылка в чат "
        f"<b>{html.escape(op.code)}</b>:\n\n{html.escape(invite.invite_link)}\n\n"
        f"⏳ Действует {minutes} мин. Ссылка привязана к вашему аккаунту: "
        "если по ней постучится кто-то другой, бот отклонит заявку. "
        "Сообщение удалится автоматически."
    )
    sent = await reply_with_connect_retry(
        source_message,
        group_text,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        protect_content=True,
    )
    if sent is not None and hasattr(sent, "message_id"):
        manager.attach_message(invite, sent.chat_id, sent.message_id)
    asyncio.create_task(manager.expire_later(context.bot, invite))
    return f"Ссылка отправлена в чат. Она только для вас, {minutes} мин."


async def handle_join_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Callback query handler for '🔐 Вступить в чат' buttons."""
    from invites import InviteError

    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != JOIN_CALLBACK_PREFIX:
        await query.answer()
        return

    _, op_code, raw_target_id = parts
    try:
        target_id = int(raw_target_id)
    except ValueError:
        await query.answer()
        return

    if query.from_user.id != target_id:
        await query.answer(
            "Эта кнопка предназначена другому участнику. "
            "Дождитесь своего приветствия и ответьте на него своей ОП.",
            show_alert=True,
        )
        LOGGER.info(
            "Пользователь %s нажал чужую кнопку доступа (владелец %s)",
            query.from_user.id,
            target_id,
        )
        return

    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    op = op_registry.get(op_code)
    if op is None or not op.chat_id:
        await query.answer(
            "Чат этой ОП пока не подключён. Напишите ответственному за ОП.",
            show_alert=True,
        )
        return

    source_message = query.message if isinstance(query.message, Message) else None
    source_chat_id = query.message.chat_id if query.message else None

    if source_chat_id is not None and not await is_chat_member(
        context.bot, source_chat_id, target_id
    ):
        await query.answer("Сначала нужно вступить в общий чат.", show_alert=True)
        return

    if await is_chat_member(context.bot, op.chat_id, target_id):
        await query.answer("Вы уже состоите в чате этой ОП 🙂", show_alert=True)
        return

    adder: UserbotAdder | None = context.application.bot_data.get("userbot")
    username = query.from_user.username
    if adder is not None and adder.ready and username:
        try:
            result = await asyncio.wait_for(
                adder.add_by_username(username, op.chat_id), timeout=8.0
            )
        except asyncio.TimeoutError:
            LOGGER.info(
                "Добавление @%s через userbot не уложилось в таймаут, переходим к ссылке",
                username,
            )
        else:
            if result.ok:
                await query.answer(f"Готово, вы добавлены в чат {op.code} ✅", show_alert=True)
                await cleanup_onboarding_thread(context, source_chat_id, target_id)
                return
            LOGGER.info(
                "Добавление @%s не удалось (%s), переходим к ссылке",
                username,
                result.outcome.value,
            )

    manager: InviteManager = context.application.bot_data["invites"]
    try:
        issued = await manager.issue(context.bot, op.code, op.chat_id, target_id)
    except InviteError as error:
        await query.answer(str(error), show_alert=True)
        return

    if source_chat_id is not None:
        manager.set_source_chat(issued.invite, source_chat_id)

    if issued.reused:
        minutes = max(1, issued.invite.seconds_left // 60)
        await query.answer(
            "Ссылка уже отправлена — проверьте сообщения выше или личный чат. "
            f"Она действует ещё около {minutes} мин.",
            show_alert=True,
        )
        return

    user_mention = query.from_user.mention_html()
    try:
        notice = await deliver_invite(context, issued.invite, op, user_mention, source_message)
    except Exception:
        LOGGER.exception(
            "Не удалось доставить ссылку пользователю %s (ОП %s)",
            target_id,
            op.code,
        )
        await manager.retire(context.bot, issued.invite)
        await query.answer(
            "Не получилось отправить ссылку — сбой соединения с Telegram. "
            "Нажмите кнопку ещё раз через минуту.",
            show_alert=True,
        )
        return
    await query.answer(notice, show_alert=True)


async def handle_chat_join_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Approve chat join requests only for the designated owner of the personal invite link."""
    request = update.chat_join_request
    if not request:
        return

    manager: InviteManager = context.application.bot_data["invites"]
    invite_link = request.invite_link.invite_link if request.invite_link else None
    outcome, invite = await manager.handle_join_request(
        context.bot, request.chat.id, request.from_user.id, invite_link
    )

    if outcome == "approved":
        op_registry: OPRegistry = context.application.bot_data["op_registry"]
        op = op_registry.find_by_chat_id(request.chat.id)
        label = f" {op.code}" if op else ""
        try:
            await context.bot.send_message(
                request.from_user.id,
                f"Добро пожаловать в чат{label}! 🎉",
            )
        except TelegramError:
            pass
        if invite is not None:
            await cleanup_onboarding_thread(
                context, invite.source_chat_id, invite.user_id
            )


async def ask_cs_clarification(
    message: Message,
    user_mention: str,
    tracker: WelcomeTracker,
    chat_id: int,
    user_id: int,
) -> None:
    """Prompt student to clarify between CS (Cybersecurity) and IT (Computer Science)."""
    from config import AMBIGUOUS_CS_QUESTION

    sent_msg = await reply_with_connect_retry(
        message,
        AMBIGUOUS_CS_QUESTION,
        parse_mode=ParseMode.HTML,
        reply_to_message_id=message.message_id,
    )
    if sent_msg and hasattr(sent_msg, "message_id"):
        tracker.add_welcome_message(chat_id, sent_msg.message_id, user_id)
    tracker.add_pending_clarification(chat_id, user_id)


async def handle_op_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Process incoming text messages in groups to detect OP declarations and provide join cards."""
    from config import CS_CODE_PATTERN

    message = update.effective_message
    if not message or not message.text or not message.chat:
        return

    if message.from_user and message.from_user.is_bot:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    tracker: WelcomeTracker = context.application.bot_data["welcome_tracker"]

    reply_to = message.reply_to_message
    reply_to_id = reply_to.message_id if reply_to else None
    is_reply = tracker.is_anchored_reply(chat_id, user_id, reply_to_id)
    is_newcomer = tracker.is_active_newcomer(chat_id, user_id)

    if tracker.has_answered(chat_id, user_id):
        return

    if not is_reply and not is_newcomer:
        return

    tracker.track_thread_message(chat_id, user_id, message.message_id)

    text = message.text.strip()
    LOGGER.info(
        "handle_op_message: user_id=%s, text=%r, is_reply=%s, is_newcomer=%s",
        user_id,
        text,
        is_reply,
        is_newcomer,
    )

    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    user_mention = (
        message.from_user.mention_html() if message.from_user else "Студент"
    )

    if tracker.is_pending_clarification(chat_id, user_id):
        op = resolve_cs_choice(op_registry, text)
        if op is None:
            if is_reply:
                await ask_cs_clarification(
                    message, user_mention, tracker, chat_id, user_id
                )
            else:
                tracker.record_message(chat_id, user_id)
            return
        tracker.clear_pending_clarification(chat_id, user_id)
        tracker.remove_user(chat_id, user_id)
        tracker.mark_answered(chat_id, user_id)
        sent_msg = await reply_with_connect_retry(
            message,
            build_op_response(user_mention, op),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.message_id,
            reply_markup=build_join_keyboard(op, user_id),
        )
        if sent_msg and hasattr(sent_msg, "message_id"):
            tracker.track_thread_message(chat_id, user_id, sent_msg.message_id)
        return

    if is_inquiry_or_question(text):
        tracker.record_message(chat_id, user_id)
        return

    if CS_CODE_PATTERN.search(text):
        await ask_cs_clarification(
            message, user_mention, tracker, chat_id, user_id
        )
        return

    matched_ops = op_registry.find_matching_ops(text)

    if not matched_ops:
        tracker.record_message(chat_id, user_id)
        if is_reply:
            help_text = (
                f"Не удалось распознать ОП в вашем сообщении, {user_mention}. 🤔\n\n"
                "Пожалуйста, укажите код или название вашей ОП (например: <b>SE</b>, <b>CS</b>, <b>IT</b>, <b>BDA</b>, <b>MCS</b>).\n"
                "Полный список доступных ОП можно посмотреть с помощью команды /ops."
            )
            sent_msg = await reply_with_connect_retry(
                message,
                help_text,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=message.message_id,
            )
            if sent_msg and hasattr(sent_msg, "message_id"):
                tracker.add_welcome_message(chat_id, sent_msg.message_id, user_id)
        return

    if not is_reply and not is_likely_op_declaration(text, matched_ops):
        tracker.record_message(chat_id, user_id)
        return

    tracker.remove_user(chat_id, user_id)
    tracker.mark_answered(chat_id, user_id)

    keyboard = None
    if len(matched_ops) == 1:
        reply_text = build_op_response(user_mention, matched_ops[0])
        keyboard = build_join_keyboard(matched_ops[0], user_id)
    else:
        items = []
        buttons = []
        for op in matched_ops:
            admin_tag = format_admin_tag(op.admin)
            items.append(
                f"• <b>{html.escape(op.code)}</b> ({html.escape(op.name)})\n"
                f"  🏫 <i>{html.escape(op.school)}</i> — {admin_tag}"
            )
            op_keyboard = build_join_keyboard(op, user_id)
            if op_keyboard:
                buttons.extend(op_keyboard.inline_keyboard)
        if buttons:
            keyboard = InlineKeyboardMarkup(buttons)
        formatted_items = "\n\n".join(items)
        reply_text = (
            f"Привет, {user_mention}! 👋\n\n"
            f"📍 <b>Найдены направления (ОП):</b>\n\n{formatted_items}\n\n"
            "Выберите вариант кнопкой выше — один человек может вступить "
            "только в одну ОП."
        )

    db: DatabaseStorage | None = context.application.bot_data.get("db")
    if db is not None:
        db.log_onboarding_action(
            user_id,
            chat_id,
            "op_matched",
            f"ops={','.join(o.code for o in matched_ops)};text={text}",
        )
        db.record_analytics_event(
            "op_identified",
            user_id,
            chat_id,
            {"matched_ops": [o.code for o in matched_ops], "text": text},
        )

    sent_msg = await reply_with_connect_retry(
        message,
        reply_text,
        parse_mode=ParseMode.HTML,
        reply_to_message_id=message.message_id,
        reply_markup=keyboard,
    )
    if sent_msg and hasattr(sent_msg, "message_id"):
        tracker.track_thread_message(chat_id, user_id, sent_msg.message_id)


async def show_ops(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ops command — show list of all educational programs and admins."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return

    if not await is_authorized_admin(update, context):
        await reply_with_connect_retry(
            message, "Команда доступна только администраторам."
        )
        return

    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    all_ops = op_registry.get_all()

    by_school: dict[str, list[OPProgram]] = {}
    for op in all_ops.values():
        by_school.setdefault(op.school, []).append(op)

    sections = []
    for school, ops in by_school.items():
        op_lines = []
        for op in ops:
            admin_tag = format_admin_tag(op.admin)
            op_lines.append(
                f"• <b>{html.escape(op.code)}</b> — {html.escape(op.name)} ({admin_tag})"
            )
        lines_str = "\n".join(op_lines)
        sections.append(f"🏫 <b>{html.escape(school)}</b>\n{lines_str}")

    response = "📚 <b>Список образовательных программ (ОП):</b>\n\n" + "\n\n".join(
        sections
    )
    await reply_with_connect_retry(message, response, parse_mode=ParseMode.HTML)


async def set_op_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setopadmin <КОД_ОП> <@username_или_id> — assign admin to an OP."""
    message = update.effective_message
    if not message:
        return

    if not await is_authorized_admin(update, context):
        await reply_with_connect_retry(
            message, "Команда доступна только администраторам."
        )
        return

    if not context.args or len(context.args) < 2:
        await reply_with_connect_retry(
            message,
            "Использование: /setopadmin <КОД_ОП> <@username_или_id>\n"
            "Пример: /setopadmin SE @alex_admin",
        )
        return

    code = context.args[0].upper()
    admin = context.args[1]

    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    if op_registry.set_admin(code, admin):
        await reply_with_connect_retry(
            message,
            f"✅ Для ОП <b>{html.escape(code)}</b> успешно назначен администратор {format_admin_tag(admin)}.",
            parse_mode=ParseMode.HTML,
        )
    else:
        all_codes = ", ".join(op_registry.get_all().keys())
        await reply_with_connect_retry(
            message,
            f"❌ ОП '{html.escape(code)}' не найдена. Доступные ОП: {all_codes}",
        )


async def describe_chat_readiness(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> str | None:
    """Validate if the bot has required administrator permissions in target OP chat."""
    try:
        membership = await context.bot.get_chat_member(chat_id, context.bot.id)
    except TelegramError as error:
        return (
            "❌ Не вижу этот чат. Добавьте бота в чат ОП и сделайте администратором.\n"
            f"<i>{html.escape(str(error))}</i>"
        )
    if membership.status != ChatMemberStatus.ADMINISTRATOR:
        return "❌ Бот должен быть администратором чата ОП."
    if not getattr(membership, "can_invite_users", False):
        return "❌ У бота нет права «Приглашать пользователей» в этом чате."
    return None


async def set_op_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setopchat <КОД_ОП> [chat_id] — bind target chat to an OP."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not await is_authorized_admin(update, context):
        await reply_with_connect_retry(
            message, "Команда доступна только администраторам."
        )
        return
    if not context.args:
        await reply_with_connect_retry(
            message,
            "Использование: /setopchat <КОД_ОП> [chat_id]\n"
            "Проще всего выполнить команду прямо в чате нужной ОП: "
            "/setopchat SE\n"
            "Чтобы отвязать чат: /setopchat SE off",
        )
        return

    code = context.args[0].upper()
    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    if op_registry.get(code) is None:
        await reply_with_connect_retry(
            message, f"❌ ОП '{html.escape(code)}' не найдена. Список: /ops"
        )
        return

    if len(context.args) > 1 and context.args[1].lower() in {"off", "none", "-"}:
        op_registry.set_chat(code, None, "")
        await reply_with_connect_retry(message, f"🔓 Чат для ОП {code} отвязан.")
        return

    if len(context.args) > 1:
        try:
            target_chat_id = int(context.args[1])
        except ValueError:
            await reply_with_connect_retry(
                message, "chat_id должен быть числом, например -1001234567890."
            )
            return
        title = ""
    else:
        if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            await reply_with_connect_retry(
                message,
                "Выполните команду внутри чата ОП или укажите chat_id вторым аргументом.",
            )
            return
        target_chat_id = chat.id
        title = chat.title or ""

    problem = await describe_chat_readiness(context, target_chat_id)
    if problem:
        await reply_with_connect_retry(message, problem)
        return

    op_registry.set_chat(code, target_chat_id, title)
    await reply_with_connect_retry(
        message,
        f"✅ Чат <code>{target_chat_id}</code> привязан к ОП <b>{html.escape(code)}</b>. "
        "Новые студенты будут получать в него персональные ссылки.",
        parse_mode=ParseMode.HTML,
    )


async def show_op_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/opchats — check chat bindings and bot readiness."""
    message = update.effective_message
    if not message:
        return
    if not await is_authorized_admin(update, context):
        await reply_with_connect_retry(
            message, "Команда доступна только администраторам."
        )
        return

    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    lines = []
    for op in op_registry.get_all().values():
        if not op.chat_id:
            lines.append(f"• <b>{html.escape(op.code)}</b> — чат не привязан")
            continue
        problem = await describe_chat_readiness(context, op.chat_id)
        mark = "⚠️" if problem else "✅"
        title = html.escape(op.chat_title) if op.chat_title else str(op.chat_id)
        lines.append(f"• <b>{html.escape(op.code)}</b> — {mark} {title}")

    await reply_with_connect_retry(
        message,
        "🔗 <b>Чаты ОП:</b>\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def welcome_new_members(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle new chat members service update."""
    message = update.effective_message
    if not message or not message.new_chat_members or not message.chat:
        return

    chain: MarkovChain = context.application.bot_data["greeting_chain"]
    settings: Settings = context.application.bot_data["settings"]
    tracker: WelcomeTracker = context.application.bot_data["welcome_tracker"]
    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    bot_id = context.bot.id

    op = op_registry.find_by_chat_id(message.chat.id)

    for member in message.new_chat_members:
        try:
            if member.id == bot_id:
                await reply_with_connect_retry(
                    message,
                    "✅ Бот подключён. Я буду приветствовать новых участников. "
                    "Администратор может выполнить /allowpm, чтобы открыть личные "
                    "команды /start и /preview.",
                )
                continue

            if not tracker.should_welcome(message.chat.id, member.id):
                continue

            mention = get_user_mention(member)
            if op is not None:
                text = build_op_chat_welcome_text(mention, op)
                await reply_with_connect_retry(message, text, parse_mode=ParseMode.HTML)
                continue

            existing_op = await find_existing_op_membership(
                context.bot, op_registry, member.id
            )
            if existing_op is not None:
                # Уже состоит в чате своей ОП (например, перезашёл в общий
                # чат первого курса) — не спамим повторным "какая у вас ОП?".
                LOGGER.info(
                    "Пропускаем приветствие: user_id=%s уже в чате ОП %s",
                    member.id,
                    existing_op.code,
                )
                continue

            text = build_welcome_text(chain, mention, settings.max_words)
            sent_msg = await reply_with_connect_retry(
                message, text, parse_mode=ParseMode.HTML
            )
            if sent_msg and hasattr(sent_msg, "message_id"):
                tracker.add_welcome_message(
                    message.chat.id, sent_msg.message_id, member.id
                )
            else:
                tracker.add_user(message.chat.id, member.id)
            db: DatabaseStorage | None = context.application.bot_data.get("db")
            if db is not None:
                db.log_onboarding_action(member.id, message.chat.id, "new_member_welcomed")
                db.record_analytics_event(
                    "welcome_sent",
                    member.id,
                    message.chat.id,
                    {"username": member.username, "first_name": member.first_name},
                )
            LOGGER.info(
                "Новый участник добавлен в трекер: chat_id=%s, user_id=%s",
                message.chat.id,
                member.id,
            )
        except TelegramError as error:
            LOGGER.error(
                "Не удалось поприветствовать %s в чате %s: %s",
                member.id,
                message.chat.id,
                error,
            )


async def welcome_chat_member_update(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle chat_member update for newly joined participants."""
    result = update.chat_member
    if not result or not result.chat:
        return
    was_member = result.old_chat_member.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    )
    is_member = result.new_chat_member.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
    )
    if not was_member and is_member:
        user = result.new_chat_member.user
        if user.is_bot:
            return
        tracker: WelcomeTracker = context.application.bot_data["welcome_tracker"]
        if not tracker.should_welcome(result.chat.id, user.id):
            return

        op_registry: OPRegistry = context.application.bot_data["op_registry"]
        op = op_registry.find_by_chat_id(result.chat.id)
        mention = get_user_mention(user)

        if op is not None:
            # Чат конкретной ОП — приветствуем без вопроса про ОП
            text = build_op_chat_welcome_text(mention, op)
            await context.bot.send_message(
                chat_id=result.chat.id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            return

        existing_op = await find_existing_op_membership(
            context.bot, op_registry, user.id
        )
        if existing_op is not None:
            # Уже состоит в чате своей ОП — не спамим повторным вопросом.
            LOGGER.info(
                "Пропускаем приветствие (chat_member): user_id=%s уже в чате ОП %s",
                user.id,
                existing_op.code,
            )
            return

        chain: MarkovChain = context.application.bot_data["greeting_chain"]
        settings: Settings = context.application.bot_data["settings"]
        text = build_welcome_text(chain, mention, settings.max_words)
        sent_msg = await context.bot.send_message(
            chat_id=result.chat.id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        if sent_msg and hasattr(sent_msg, "message_id"):
            tracker.add_welcome_message(result.chat.id, sent_msg.message_id, user.id)
        else:
            tracker.add_user(result.chat.id, user.id)

        db: DatabaseStorage | None = context.application.bot_data.get("db")
        if db is not None:
            db.log_onboarding_action(user.id, result.chat.id, "chat_member_welcomed")
            db.record_analytics_event(
                "welcome_sent",
                user.id,
                result.chat.id,
                {"username": user.username, "first_name": user.first_name},
            )
        LOGGER.info(
            "Новый участник (chat_member) добавлен в трекер: chat_id=%s, user_id=%s",
            result.chat.id,
            user.id,
        )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Feedback on unrecognised command."""
    message = update.effective_message
    if not message or not message.text:
        return
    attempted = message.text.split()[0]
    await reply_with_connect_retry(
        message,
        f"❓ Команда {html.escape(attempted)} не найдена.\n"
        "Список команд: /start",
    )


async def log_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error logging handler with distinct network warning categorization."""
    try:
        import bot

        logger = getattr(bot, "LOGGER", LOGGER)
    except ImportError:
        logger = LOGGER

    if context.error and is_connection_error(context.error):
        logger.warning("Сетевая ошибка при взаимодействии с Telegram: %s", context.error)
    else:
        logger.error("Ошибка при обработке события Telegram", exc_info=context.error)

