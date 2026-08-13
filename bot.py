"""Telegram bot that welcomes new members using Markov-chain text."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from markov import MarkovChain


BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    token: str
    greetings_path: Path
    admins_path: Path = BASE_DIR / "admins.json"
    op_admins_path: Path = BASE_DIR / "op_admins.json"
    initial_admin_ids: frozenset[int] = frozenset()
    markov_order: int = 2
    max_words: int = 28

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(BASE_DIR / ".env")
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN не задан. Скопируйте .env.example в .env "
                "и добавьте токен от @BotFather."
            )

        def project_path(variable: str, default: str) -> Path:
            value = Path(os.getenv(variable, default))
            return value if value.is_absolute() else BASE_DIR / value

        admin_ids = parse_id_list(os.getenv("ADMIN_USER_IDS", ""))

        return cls(
            token=token,
            greetings_path=project_path("GREETINGS_FILE", "greetings.txt"),
            admins_path=project_path("ADMINS_FILE", "admins.json"),
            op_admins_path=project_path("OP_ADMINS_FILE", "op_admins.json"),
            initial_admin_ids=frozenset(admin_ids),
            markov_order=int(os.getenv("MARKOV_ORDER", "2")),
            max_words=int(os.getenv("MAX_GREETING_WORDS", "28")),
        )


def parse_id_list(value: str) -> set[int]:
    try:
        return {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError as error:
        raise ValueError("ADMIN_USER_IDS должен содержать ID через запятую") from error


class AdminRegistry:
    """Persistent allow-list for people who may use the bot in private."""

    def __init__(self, path: Path, initial_ids: frozenset[int] = frozenset()) -> None:
        self.path = path
        self._ids = set(initial_ids)
        if path.exists():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                self._ids.update(int(user_id) for user_id in stored)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(f"Не удалось прочитать список админов {path}") from error

    def contains(self, user_id: int) -> bool:
        return user_id in self._ids

    def add(self, user_id: int) -> bool:
        if user_id in self._ids:
            return False
        self._ids.add(user_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(sorted(self._ids), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
        return True


EN_LAYOUT = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./~QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?"
RU_LAYOUT = "ёйцукенгшщзхъфывапролджэячсмитьбю.ЁЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,"

TRANS_EN_TO_RU = str.maketrans(EN_LAYOUT, RU_LAYOUT)
TRANS_RU_TO_EN = str.maketrans(RU_LAYOUT, EN_LAYOUT)


def swap_keyboard_layout(text: str) -> str:
    """Convert text between English QWERTY and Russian JCUKEN layouts."""
    res = []
    for char in text:
        if char in EN_LAYOUT:
            res.append(char.translate(TRANS_EN_TO_RU))
        elif char in RU_LAYOUT:
            res.append(char.translate(TRANS_RU_TO_EN))
        else:
            res.append(char)
    return "".join(res)


DEFAULT_OPS: dict[str, dict[str, Any]] = {
    "SE": {
        "name": "Software Engineering",
        "school": "School of Software Engineering",
        "admin": "@Alsh444",
        "aliases": ["SE", "СЕ", "Software Engineering", "сешник", "сешники", "сешница", "софтвер", "софтверщик", "софтварщик", "софтваре", "софтвар", "софтвер инжиниринг", "софтвар инжиниринг"],
    },
    "IT": {
        "name": "Computer Science",
        "school": "School of Artificial Intelligence and Data Science",
        "admin": "@TypicallyRain",
        "aliases": ["IT", "ИТ", "Computer Science", "айти", "айтишник", "айтишники", "компьютер сайнс", "комп сайнс", "комп сай", "комп саи", "компьютерные науки", "кс"],
    },
    "BDA": {
        "name": "Big Data Analysis",
        "school": "School of Artificial Intelligence and Data Science",
        "admin": "@therealarujxx",
        "aliases": ["BDA", "БДА", "Big Data Analysis", "Big Data", "бдашник", "бдашники", "бигдата", "биг дата", "дата анализис", "биг дата анализис"],
    },
    "MCS": {
        "name": "Mathematical and Computational Science",
        "school": "School of Artificial Intelligence and Data Science",
        "admin": "@howflowersbloom",
        "aliases": ["MCS", "МКС", "Mathematical and Computational Science", "мксник", "мксники", "маткомп", "математикал"],
    },
    "CS": {
        "name": "Cybersecurity",
        "school": "School of Cybersecurity",
        "admin": "@alishaisyapping",
        "aliases": ["CS", "КС", "Cybersecurity", "Cyber Security", "кибербез", "кибербезопасность", "киберсекьюрити", "кибер секьюрити", "сайберсекьюрити", "сайбер", "кибер"],
    },
    "SST": {
        "name": "Smart Security Technologies",
        "school": "School of Cybersecurity",
        "admin": "@alishaisyapping",
        "aliases": ["SST", "ССТ", "Smart Security Technologies", "сстшник", "смарт секьюрити", "смартсекьюрити", "смарт секьюрити технолоджис"],
    },
    "IIOT": {
        "name": "Industrial Internet of Things",
        "school": "School of Intelligent Systems",
        "admin": "@dhshrbrhr",
        "aliases": ["IIOT", "ИИОТ", "Industrial Internet of Things", "ииотник", "индастриал иот", "индустриал иот", "иот"],
    },
    "EE": {
        "name": "Electronic Engineering",
        "school": "School of Intelligent Systems",
        "admin": "@dhshrbrhr",
        "aliases": ["EE", "ЕЕ", "ЭЭ", "Electronic Engineering", "электронщик", "электроник инжиниринг", "електроник инжиниринг", "электроника"],
    },
    "ST": {
        "name": "Smart Technologies",
        "school": "School of Intelligent Systems",
        "admin": "@dhshrbrhr",
        "aliases": ["ST", "СТ", "Smart Technologies", "стшник", "смарт тех", "смарт технолоджис"],
    },
    "DNE": {
        "name": "Digital technologies in nuclear power engineering",
        "school": "School of Intelligent Systems",
        "admin": "@dhshrbrhr",
        "aliases": ["DNE", "DTNPE", "ДНЕ", "ДТНПЕ", "Digital technologies in nuclear power engineering", "днешник", "нуклеар", "ядерка", "ядерная инженерия"],
    },
    "ITM": {
        "name": "IT Management",
        "school": "School of Creative Industries",
        "admin": "@assiixq",
        "aliases": ["ITM", "ИТМ", "IT Management", "итмщик", "айти менеджмент", "ит менеджмент"],
    },
    "ITE": {
        "name": "IT Entrepreneurship",
        "school": "School of Creative Industries",
        "admin": "@assiixq",
        "aliases": ["ITE", "ИТЕ", "IT Entrepreneurship", "итешник", "айти предпринимательство", "ит предпринимательство", "айти энтрепренершип"],
    },
    "AIB": {
        "name": "AI Business",
        "school": "School of Creative Industries",
        "admin": "@assiixq",
        "aliases": ["AIB", "АИБ", "AI Business", "аибник", "ииб", "аи бизнес", "ай бизнес", "ии бизнес"],
    },
    "MT": {
        "name": "Media Technologies",
        "school": "School of Creative Industries",
        "admin": "@Subbzerr01",
        "aliases": ["MT", "МТ", "Media Technologies", "мтшник", "медиа тех", "медия тех", "медиа технологии", "медия технологии", "медиятехнологии", "медиатехнологии"],
    },
    "DJ": {
        "name": "Digital Journalism",
        "school": "School of Creative Industries",
        "admin": "@vveetaaa",
        "aliases": ["DJ", "ДЖ", "Digital Journalism", "джник", "журналистика", "диджитал журналистика", "цифровая журналистика", "диджей"],
    },
    "DPA": {
        "name": "Digital Public Administration",
        "school": "School of Digital Public Administration",
        "admin": "@assiixq",
        "aliases": ["DPA", "ДПА", "Digital Public Administration", "дпашник", "диджитал паблик", "госуправление"],
    },
    "DL": {
        "name": "Digital Jurisprudence",
        "school": "School of Digital Public Administration",
        "admin": "@finrandiri",
        "aliases": ["DL", "ДЛ", "Digital Jurisprudence", "длшник", "юриспруденция", "диджитал юриспруденция", "цифровая юриспруденция", "цифровые юристы"],
    },
}


@dataclass(frozen=True)
class PendingJoin:
    """Tracks who a bot-generated join-request invite link was issued to, so
    the ChatJoinRequestHandler can tell a legitimate auto-approval apart from
    an unrelated join request to the same group."""

    user_id: int
    group_id: int
    op_code: str


class PendingJoinRegistry:
    """In-memory map of invite_link -> PendingJoin. Not persisted to disk on
    purpose: these are single-use, short-lived tokens, so losing them on a
    restart just means the (rare) in-flight request falls back to manual
    admin approval instead of being silently auto-approved."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingJoin] = {}

    def add(self, invite_link: str, entry: PendingJoin) -> None:
        self._pending[invite_link] = entry

    def pop_matching(self, invite_link: str | None, user_id: int, group_id: int) -> PendingJoin | None:
        """Return and remove the entry only if it exists AND matches both the
        requesting user and the group. Never approve on a partial match."""
        if invite_link is None:
            return None
        entry = self._pending.get(invite_link)
        if entry is None or entry.user_id != user_id or entry.group_id != group_id:
            return None
        del self._pending[invite_link]
        return entry


@dataclass(frozen=True)
class OPProgram:
    code: str
    name: str
    school: str
    admin: str
    aliases: tuple[str, ...] = ()
    group_id: int | None = None


class WelcomeTracker:
    """Tracks welcome messages and recently joined members so OP reactions only trigger for the actual new member."""

    def __init__(self, max_messages: int = 10) -> None:
        self.max_messages = max_messages
        self._active_new_members: dict[tuple[int, int], int] = {}
        self._welcome_messages: dict[tuple[int, int], int] = {}
        self._pending_clarification: set[tuple[int, int]] = set()

    def add_welcome_message(
        self, chat_id: int, welcome_message_id: int, target_user_id: int
    ) -> None:
        """Anchor a bot message to a specific member. Only replies to this exact
        message from this exact member will be treated as that member's answer."""
        self._welcome_messages[(chat_id, welcome_message_id)] = target_user_id
        self._active_new_members[(chat_id, target_user_id)] = self.max_messages

    def add_user(self, chat_id: int, user_id: int) -> None:
        self._active_new_members[(chat_id, user_id)] = self.max_messages

    def is_target_member(
        self,
        chat_id: int,
        user_id: int,
        reply_to_message_id: int | None = None,
    ) -> bool:
        # Require an explicit Reply to a bot message that was anchored to a
        # specific member (their welcome message or a follow-up prompt sent
        # to them). This is the only way to match — plain messages that
        # merely mention an OP code (e.g. someone just chatting and typing
        # "se") are never treated as an answer, since many different people
        # talk in the chat and could coincidentally type something like that.
        if reply_to_message_id is None:
            return False

        welcome_target = self._welcome_messages.get((chat_id, reply_to_message_id))
        if welcome_target is None:
            return False

        return user_id == welcome_target

    def record_message(self, chat_id: int, user_id: int) -> None:
        key = (chat_id, user_id)
        if key in self._active_new_members:
            self._active_new_members[key] -= 1
            if self._active_new_members[key] <= 0:
                del self._active_new_members[key]

    def remove_user(self, chat_id: int, user_id: int) -> None:
        self._active_new_members.pop((chat_id, user_id), None)
        self.clear_pending_clarification(chat_id, user_id)

    def is_pending_clarification(self, chat_id: int, user_id: int) -> bool:
        return (chat_id, user_id) in self._pending_clarification

    def add_pending_clarification(self, chat_id: int, user_id: int) -> None:
        self._pending_clarification.add((chat_id, user_id))

    def clear_pending_clarification(self, chat_id: int, user_id: int) -> None:
        self._pending_clarification.discard((chat_id, user_id))


class OPRegistry:
    """Registry for Educational Programs (OPs) and their assigned administrators."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._ops: dict[str, OPProgram] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for code, info in data.items():
                    code_upper = code.upper()
                    raw_aliases = info.get("aliases", [])
                    raw_group_id = info.get("group_id")
                    self._ops[code_upper] = OPProgram(
                        code=code_upper,
                        name=str(info.get("name", code_upper)),
                        school=str(info.get("school", "")),
                        admin=str(info.get("admin", "@admin")),
                        aliases=tuple(str(a) for a in raw_aliases),
                        group_id=int(raw_group_id) if raw_group_id is not None else None,
                    )
                return
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                LOGGER.warning("Не удалось прочитать %s: %s. Используем значения по умолчанию.", self.path, error)

        for code, info in DEFAULT_OPS.items():
            self._ops[code] = OPProgram(
                code=code,
                name=info["name"],
                school=info["school"],
                admin=info["admin"],
                aliases=tuple(info.get("aliases", [])),
            )
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            code: {
                "name": prog.name,
                "school": prog.school,
                "admin": prog.admin,
                "aliases": list(prog.aliases),
                "group_id": prog.group_id,
            }
            for code, prog in self._ops.items()
        }
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def get_all(self) -> dict[str, OPProgram]:
        return dict(self._ops)

    def get(self, code: str) -> OPProgram | None:
        return self._ops.get(code.upper())

    def set_admin(self, code: str, admin: str) -> bool:
        code_upper = code.upper()
        if code_upper not in self._ops:
            return False
        current = self._ops[code_upper]
        self._ops[code_upper] = OPProgram(
            code=current.code,
            name=current.name,
            school=current.school,
            admin=admin,
            aliases=current.aliases,
            group_id=current.group_id,
        )
        self.save()
        return True

    def set_group_id(self, code: str, group_id: int) -> bool:
        code_upper = code.upper()
        if code_upper not in self._ops:
            return False
        current = self._ops[code_upper]
        self._ops[code_upper] = OPProgram(
            code=current.code,
            name=current.name,
            school=current.school,
            admin=current.admin,
            aliases=current.aliases,
            group_id=group_id,
        )
        self.save()
        return True

    def find_by_group_id(self, group_id: int) -> OPProgram | None:
        for prog in self._ops.values():
            if prog.group_id == group_id:
                return prog
        return None

    def find_matching_ops(self, text: str) -> list[OPProgram]:
        if not text:
            return []

        candidates = {text}
        swapped = swap_keyboard_layout(text)
        if swapped != text:
            candidates.add(swapped)

        matched: list[OPProgram] = []
        for code, prog in self._ops.items():
            terms = [code, prog.name] + list(prog.aliases)
            found = False
            for term in terms:
                if not term:
                    continue
                pattern = rf"(?i)(?<![a-zA-Z0-9_а-яА-ЯёЁ]){re.escape(term)}(?![a-zA-Z0-9_а-яА-ЯёЁ])"
                if any(re.search(pattern, cand) for cand in candidates):
                    found = True
                    break
            if found:
                matched.append(prog)

        return matched



def format_admin_tag(admin: str) -> str:
    admin_str = admin.strip()
    if not admin_str:
        return "@admin"
    if admin_str.isdigit():
        return f'<a href="tg://user?id={admin_str}">Администратор</a>'
    if not admin_str.startswith("@"):
        admin_str = f"@{admin_str}"
    return html.escape(admin_str)



def build_welcome_text(chain: MarkovChain, mention: str, max_words: int) -> str:
    generated = html.escape(chain.generate(max_words=max_words))
    return (
        f"{generated}\n\n"
        f"Рады видеть тебя, {mention}! 👋\n\n"
        "Пожалуйста, ознакомься с правилами в описании группы, а также с гайдом.\n\n"
        "💡 <b>Ответь на это сообщение (Reply)</b>, указав свою ОП (например: <code>SE</code>, <code>CS</code>, <code>IT</code>), чтобы узнать своего ответственного админа!"
    )


def is_connection_error(error: BaseException | None) -> bool:
    """Return True if the exception chain contains network or connection errors."""
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
    """Return True when the exception chain contains connection error or timeout."""
    return is_connection_error(error)


async def reply_with_connect_retry(message: object, text: str, **kwargs: object) -> None:
    """Retry failures that happened due to temporary connection or network issues."""
    retry_delays = (1.0, 2.0)
    for attempt in range(len(retry_delays) + 1):
        try:
            return await message.reply_text(text, **kwargs)  # type: ignore[attr-defined,no-any-return]
        except NetworkError as error:
            if not is_connection_error(error) or attempt == len(retry_delays):
                raise
            delay = retry_delays[attempt]
            LOGGER.warning(
                "Telegram недоступен при подключении; повтор отправки через %.0f с",
                delay,
            )
            await asyncio.sleep(delay)


async def send_op_invite_dm(
    message: Message, context: ContextTypes.DEFAULT_TYPE, code: str
) -> None:
    """Handle the `join_<CODE>` deep-link payload: create a single-use invite
    link to that OP's group and send it here, in private chat with the bot.
    This is the only place invite links are ever generated now — they never
    get posted into the (crowded) main/OP group chats."""
    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    op = op_registry.get(code)
    if op is None or op.group_id is None:
        await reply_with_connect_retry(
            message,
            "⚠️ Не удалось найти группу для этой ОП. Обратитесь к администратору.",
        )
        return

    user = message.from_user
    if user is None:
        return

    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=op.group_id,
            name=f"onboard-{user.id}",
            creates_join_request=True,
        )
    except TelegramError as error:
        LOGGER.warning(
            "Не удалось создать ссылку-приглашение для ОП %s (group_id=%s): %s",
            op.code,
            op.group_id,
            error,
        )
        await reply_with_connect_retry(
            message,
            "⚠️ Не удалось автоматически создать ссылку для вступления "
            "(возможно, бот потерял права администратора в группе этой ОП). "
            f"Обратитесь к администратору: {format_admin_tag(op.admin)}",
            parse_mode=ParseMode.HTML,
        )
        return

    pending: PendingJoinRegistry = context.application.bot_data["pending_joins"]
    pending.add(invite.invite_link, PendingJoin(user_id=user.id, group_id=op.group_id, op_code=op.code))

    await reply_with_connect_retry(
        message,
        f"🔗 Вот ваша персональная ссылка для вступления в группу "
        f"<b>{html.escape(op.code)} ({html.escape(op.name)})</b>:\n{invite.invite_link}\n\n"
        "Перейдите по ней и нажмите «Подать заявку» — бот сразу же автоматически "
        "примет именно вашу заявку, вручную ничего подтверждать не нужно. "
        "Ссылка привязана только к вам: если её кто-то перешлёт, чужая заявка "
        "автоматически одобрена не будет и уйдёт на ручную проверку администраторам.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    # Deep link from the group's "Вступить в группу" button: /start join_SE
    if message.chat.type == ChatType.PRIVATE and context.args:
        payload = context.args[0]
        if payload.startswith("join_"):
            code = payload[len("join_") :].upper()
            await send_op_invite_dm(message, context, code)
            return

    registry: AdminRegistry = context.application.bot_data["admins"]
    if message.chat.type == ChatType.PRIVATE and not registry.contains(user.id):
        await reply_with_connect_retry(
            message,
            "Личные сообщения доступны администраторам группы. Выполните "
            "/allowpm в группе, где вы администратор."
        )
        return
    await reply_with_connect_retry(
        message,
        "Я подключён и приветствую новых участников фразами, созданными "
        "марковской цепью. Команда проверки: /preview"
    )


async def preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show an example without waiting for a member to join."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    registry: AdminRegistry = context.application.bot_data["admins"]
    if message.chat.type == ChatType.PRIVATE and not registry.contains(user.id):
        await reply_with_connect_retry(
            message,
            "Доступ закрыт. Если вы администратор, выполните /allowpm в группе."
        )
        return

    chain: MarkovChain = context.application.bot_data["greeting_chain"]
    settings: Settings = context.application.bot_data["settings"]
    text = build_welcome_text(chain, user.mention_html(), settings.max_words)
    await reply_with_connect_retry(message, text, parse_mode=ParseMode.HTML)


async def allow_private_messages(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Authorize a real group administrator for private chat with the bot."""
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
            "и повторите /allowpm."
        )
        return
    if membership.status not in (
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ):
        await reply_with_connect_retry(message, "Команда доступна только администраторам группы.")
        return

    registry: AdminRegistry = context.application.bot_data["admins"]
    added = registry.add(user.id)
    status = "Доступ к личным сообщениям открыт." if added else "Доступ уже был открыт."
    await reply_with_connect_retry(message, f"{status} Теперь напишите мне в личный чат.")


async def show_ids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message and user and chat:
        await reply_with_connect_retry(message, f"Ваш ID: {user.id}\nID этого чата: {chat.id}")


QUESTION_PATTERN = re.compile(
    r"(?i)\b("
    r"кто\s+(с|на|из|тут|в|поступил|поступает|учится|выбрал|идет|шел)|"
    r"есть\s+(ли\s+)?кто|"
    r"кто[- ]нибудь|"
    r"а\s+кто|"
    r"ищу\s+(кто|кого|с|на)|"
    r"много\s+(ли\s+)?(тут\s+)?(кто|с|на|из)|"
    r"кого\s+больше"
    r")\b"
)


CS_CODE_PATTERN = re.compile(r"^\s*(cs|кс)\s*$", re.IGNORECASE)

AMBIGUOUS_CS_QUESTION = (
    "Вы о <b>Computer Science (IT)</b> или <b>Cybersecurity (CS)</b>?\n\n"
    "Ответьте, выбрав один из вариантов."
)


def build_join_keyboard(bot_username: str, code: str) -> InlineKeyboardMarkup | None:
    """Button that deep-links into a private chat with the bot, where the
    single-use invite link will be delivered. We never post the invite link
    straight into the crowded main group — Telegram bots can't silently add
    a user to a group by username (there is no such Bot API method), and
    posting a public link in a group full of people is exactly what we're
    trying to avoid."""
    if not bot_username:
        return None
    url = f"https://t.me/{bot_username}?start=join_{code}"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"🔗 Вступить в группу {code}", url=url)]]
    )


async def build_invite_section(
    context: ContextTypes.DEFAULT_TYPE, op: OPProgram, user_id: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Return (text_note, keyboard) for inviting the user into the OP group.

    We deliberately do NOT create/post the invite link here anymore: it used
    to be posted right into the main group chat, visible to everyone. Now we
    only attach a button that sends the student into a private chat with the
    bot; the actual single-use link is generated and sent there (see
    `start()` handling the `join_<CODE>` deep-link payload)."""
    if op.group_id is None:
        return (
            "\n\n⚠️ Группа для этой ОП ещё не привязана к боту "
            "(администратор ОП может выполнить /setopgroup внутри своей группы).",
            None,
        )

    bot_username = context.bot.username or ""
    keyboard = build_join_keyboard(bot_username, op.code)
    if keyboard is None:
        return (
            "\n\n⚠️ Не удалось сформировать ссылку для перехода в личные "
            "сообщения. Обратитесь к администратору выше.",
            None,
        )
    return (
        "\n\n👉 Нажмите кнопку ниже — я пришлю одноразовую ссылку для "
        "вступления в группу в личные сообщения.",
        keyboard,
    )


def build_op_response(user_mention: str, op: OPProgram) -> str:
    admin_tag = format_admin_tag(op.admin)
    return (
        f"Привет, {user_mention}! 👋\n\n"
        f"📍 <b>ОП: {html.escape(op.code)} ({html.escape(op.name)})</b>\n"
        f"🏫 <i>{html.escape(op.school)}</i>\n"
        f"👤 Ответственный администратор: {admin_tag}"
    )


async def ask_cs_clarification(
    message: Message,
    user_mention: str,
    tracker: WelcomeTracker,
    chat_id: int,
    user_id: int,
) -> None:
    """Ask whether CS means Computer Science (IT) or Cybersecurity, then wait
    for the member's reply anchored to this prompt."""
    sent_msg = await reply_with_connect_retry(
        message,
        AMBIGUOUS_CS_QUESTION,
        parse_mode=ParseMode.HTML,
        reply_to_message_id=message.message_id,
    )
    if sent_msg and hasattr(sent_msg, "message_id"):
        tracker.add_welcome_message(chat_id, sent_msg.message_id, user_id)
    tracker.add_pending_clarification(chat_id, user_id)


def resolve_cs_choice(op_registry: OPRegistry, text: str) -> OPProgram | None:
    """Resolve the answer to the CS clarification. The question already labels
    the options as Computer Science (IT) and Cybersecurity (CS), so a bare
    "CS"/"кс" answer means Cybersecurity, "IT" means Computer Science.
    Full names and aliases are matched through the registry."""
    if not text:
        return None
    if CS_CODE_PATTERN.search(text):
        return op_registry.get("CS")
    matched = [op for op in op_registry.find_matching_ops(text) if op.code in ("IT", "CS")]
    if len(matched) == 1:
        return matched[0]
    return None


async def handle_op_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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

    if not tracker.is_target_member(chat_id, user_id, reply_to_id):
        return

    if QUESTION_PATTERN.search(message.text):
        return

    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    user_mention = (
        message.from_user.mention_html() if message.from_user else "Студент"
    )

    if tracker.is_pending_clarification(chat_id, user_id):
        op = resolve_cs_choice(op_registry, message.text)
        if op is None:
            await ask_cs_clarification(
                message, user_mention, tracker, chat_id, user_id
            )
            return
        tracker.clear_pending_clarification(chat_id, user_id)
        tracker.remove_user(chat_id, user_id)
        reply_text = build_op_response(user_mention, op)
        note, keyboard = await build_invite_section(context, op, user_id)
        reply_text += note
        await reply_with_connect_retry(
            message,
            reply_text,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.message_id,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
        return

    if CS_CODE_PATTERN.search(message.text):
        await ask_cs_clarification(
            message, user_mention, tracker, chat_id, user_id
        )
        return

    matched_ops = op_registry.find_matching_ops(message.text)

    if not matched_ops:
        tracker.record_message(chat_id, user_id)
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
        # Anchor this follow-up prompt to the same member, so if they reply
        # to *it* instead of the original welcome message, it still counts —
        # but nobody else's reply to it will.
        if sent_msg and hasattr(sent_msg, "message_id"):
            tracker.add_welcome_message(chat_id, sent_msg.message_id, user_id)
        return

    tracker.remove_user(chat_id, user_id)

    keyboard: InlineKeyboardMarkup | None = None
    if len(matched_ops) == 1:
        op = matched_ops[0]
        reply_text = build_op_response(user_mention, op)
        note, keyboard = await build_invite_section(context, op, user_id)
        reply_text += note
    else:
        items = []
        buttons = []
        for op in matched_ops:
            admin_tag = format_admin_tag(op.admin)
            note, op_keyboard = await build_invite_section(context, op, user_id)
            items.append(
                f"• <b>{html.escape(op.code)}</b> ({html.escape(op.name)})\n"
                f"  🏫 <i>{html.escape(op.school)}</i> — {admin_tag}"
                f"{note}"
            )
            if op_keyboard:
                buttons.extend(op_keyboard.inline_keyboard)
        if buttons:
            keyboard = InlineKeyboardMarkup(buttons)
        formatted_items = "\n\n".join(items)
        reply_text = (
            f"Привет, {user_mention}! 👋\n\n"
            f"📍 <b>Найдены направления (ОП):</b>\n\n{formatted_items}"
        )

    await reply_with_connect_retry(
        message,
        reply_text,
        parse_mode=ParseMode.HTML,
        reply_to_message_id=message.message_id,
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


async def show_ops(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return

    registry: AdminRegistry = context.application.bot_data["admins"]
    is_authorized = registry.contains(user.id)

    if not is_authorized and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        try:
            membership = await context.bot.get_chat_member(chat.id, user.id)
            if membership.status in (
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
            ):
                is_authorized = True
        except TelegramError:
            pass

    if not is_authorized:
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
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return

    registry: AdminRegistry = context.application.bot_data["admins"]
    is_authorized = registry.contains(user.id)

    if not is_authorized and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        try:
            membership = await context.bot.get_chat_member(chat.id, user.id)
            if membership.status in (
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
            ):
                is_authorized = True
        except TelegramError:
            pass

    if not is_authorized:
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


async def set_op_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Link an OP code to the current chat's group id.

    Must be run *inside the target OP's group* by an administrator (of that
    group, or a globally registered bot admin), with the bot itself already
    added as admin there. Example: /setopgroup SE
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await reply_with_connect_retry(
            message,
            "Эту команду нужно выполнить внутри группы образовательной программы, "
            "которую вы хотите привязать (не в личных сообщениях).",
        )
        return

    registry: AdminRegistry = context.application.bot_data["admins"]
    is_authorized = registry.contains(user.id)

    if not is_authorized:
        try:
            membership = await context.bot.get_chat_member(chat.id, user.id)
            if membership.status in (
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
            ):
                is_authorized = True
        except TelegramError:
            pass

    if not is_authorized:
        await reply_with_connect_retry(
            message, "Команда доступна только администраторам этой группы."
        )
        return

    if not context.args or len(context.args) != 1:
        await reply_with_connect_retry(
            message,
            "Использование: /setopgroup <КОД_ОП>\n"
            "Выполните эту команду прямо в группе нужной ОП. Пример: /setopgroup SE",
        )
        return

    code = context.args[0].upper()

    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if bot_member.status != ChatMemberStatus.ADMINISTRATOR:
            await reply_with_connect_retry(
                message,
                "⚠️ Бот пока не администратор этой группы. Сделайте бота админом "
                "с правом приглашать пользователей по ссылке, затем повторите команду.",
            )
            return
    except TelegramError as error:
        LOGGER.warning("Не удалось проверить права бота в чате %s: %s", chat.id, error)

    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    if op_registry.set_group_id(code, chat.id):
        await reply_with_connect_retry(
            message,
            f"🔒 Эта группа «{html.escape(chat.title or str(chat.id))}» закреплена "
            f"как группа ОП <b>{html.escape(code)}</b>. Теперь студентам, "
            "указавшим эту ОП, будут автоматически выдаваться персональные "
            "одноразовые ссылки для вступления в личные сообщения.",
            parse_mode=ParseMode.HTML,
        )
    else:
        all_codes = ", ".join(op_registry.get_all().keys())
        await reply_with_connect_retry(
            message,
            f"❌ ОП '{html.escape(code)}' не найдена. Доступные ОП: {all_codes}",
        )


async def welcome_new_members(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    if not message or not message.new_chat_members or not message.chat:
        return

    chain: MarkovChain = context.application.bot_data["greeting_chain"]
    settings: Settings = context.application.bot_data["settings"]
    tracker: WelcomeTracker = context.application.bot_data["welcome_tracker"]
    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    bot_id = context.bot.id

    # If this chat is itself a registered OP group (linked via /setopgroup),
    # people land here *after* they've already told the bot their OP in the
    # main first-year group and clicked the invite button. Don't ask them
    # again — just a plain welcome, no OP question, nothing tracked.
    linked_op = op_registry.find_by_group_id(message.chat.id)

    for member in message.new_chat_members:
        if member.id == bot_id:
            await reply_with_connect_retry(
                message,
                "✅ Бот подключён. Я буду приветствовать новых участников. "
                "Администратор может выполнить /allowpm, чтобы открыть личные "
                "команды /start и /preview.",
            )
            continue

        if linked_op is not None:
            await reply_with_connect_retry(
                message,
                f"Добро пожаловать в группу <b>{html.escape(linked_op.code)}</b>, "
                f"{member.mention_html()}! 👋",
                parse_mode=ParseMode.HTML,
            )
            continue

        text = build_welcome_text(chain, member.mention_html(), settings.max_words)
        sent_msg = await reply_with_connect_retry(message, text, parse_mode=ParseMode.HTML)
        if sent_msg and hasattr(sent_msg, "message_id"):
            tracker.add_welcome_message(message.chat.id, sent_msg.message_id, member.id)
        else:
            tracker.add_user(message.chat.id, member.id)


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-approve a join request only if it exactly matches a pending
    invite link we generated for that specific user and group in
    `send_op_invite_dm`. Anything else (someone using a stale/forwarded link,
    or joining the group through some other route) is left untouched for a
    human admin to review in the group's normal 'Requests to join' list —
    we never approve a request we didn't explicitly issue."""
    request = update.chat_join_request
    if request is None:
        return

    invite_link = request.invite_link.invite_link if request.invite_link else None
    pending: PendingJoinRegistry = context.application.bot_data["pending_joins"]
    entry = pending.pop_matching(invite_link, request.from_user.id, request.chat.id)
    if entry is None:
        LOGGER.info(
            "Заявка на вступление в чат %s от пользователя %s не распознана "
            "автоматически — оставлена на ручное рассмотрение.",
            request.chat.id,
            request.from_user.id,
        )
        return

    try:
        await context.bot.approve_chat_join_request(request.chat.id, request.from_user.id)
    except TelegramError as error:
        LOGGER.warning("Не удалось одобрить заявку на вступление: %s", error)
        return

    # Revoke the link so it can't be reused even if someone else has it.
    if invite_link:
        try:
            await context.bot.revoke_chat_invite_link(request.chat.id, invite_link)
        except TelegramError:
            pass

    try:
        await context.bot.send_message(
            request.from_user.id,
            f"✅ Заявка одобрена — добро пожаловать в группу "
            f"<b>{html.escape(entry.op_code)}</b>!",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        # The user may not have started a chat with the bot yet in some
        # edge case, or has blocked it — not fatal, they're already in the
        # group either way.
        pass


async def log_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.error and is_connection_error(context.error):
        LOGGER.warning("Сетевая ошибка при взаимодействии с Telegram: %s", context.error)
    else:
        LOGGER.error("Ошибка при обработке события Telegram", exc_info=context.error)


def create_application(settings: Settings) -> Application:
    greeting_chain = MarkovChain.from_file(
        settings.greetings_path,
        order=settings.markov_order,
    )
    admins = AdminRegistry(settings.admins_path, settings.initial_admin_ids)
    op_registry = OPRegistry(settings.op_admins_path)
    welcome_tracker = WelcomeTracker(max_messages=5)
    pending_joins = PendingJoinRegistry()
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
    application = (
        Application.builder()
        .token(settings.token)
        .request(request)
        .get_updates_request(updates_request)
        .build()
    )
    application.bot_data.update(
        {
            "greeting_chain": greeting_chain,
            "admins": admins,
            "op_registry": op_registry,
            "welcome_tracker": welcome_tracker,
            "pending_joins": pending_joins,
            "settings": settings,
        }
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("preview", preview))
    application.add_handler(CommandHandler("allowpm", allow_private_messages))
    application.add_handler(CommandHandler("id", show_ids))
    application.add_handler(CommandHandler("ops", show_ops))
    application.add_handler(CommandHandler("setopadmin", set_op_admin))
    application.add_handler(CommandHandler("setopgroup", set_op_group))
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_op_message)
    )
    application.add_handler(ChatJoinRequestHandler(handle_join_request))
    application.add_error_handler(log_error)
    return application


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    # httpx logs full Telegram request URLs, which include the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_environment()
    LOGGER.info("Корпус приветствий: %s", settings.greetings_path)
    create_application(settings).run_polling()


if __name__ == "__main__":
    main()

