"""Telegram bot that welcomes new members using Markov-chain text."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import Forbidden, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from invites import InviteError, InviteManager, PendingInvite
from markov import MarkovChain
from userbot import AddOutcome, UserbotAdder


BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    token: str
    greetings_path: Path
    admins_path: Path = BASE_DIR / "admins.json"
    op_admins_path: Path = BASE_DIR / "op_admins.json"
    invites_path: Path = BASE_DIR / "invites.json"
    initial_admin_ids: frozenset[int] = frozenset()
    markov_order: int = 2
    max_words: int = 28
    invite_ttl_seconds: int = 900
    invite_hourly_limit: int = 5
    invite_group_fallback: bool = True
    telethon_api_id: str = ""
    telethon_api_hash: str = ""
    telethon_session: str = ""

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
            invites_path=project_path("INVITES_FILE", "invites.json"),
            initial_admin_ids=frozenset(admin_ids),
            markov_order=int(os.getenv("MARKOV_ORDER", "2")),
            max_words=int(os.getenv("MAX_GREETING_WORDS", "28")),
            invite_ttl_seconds=int(os.getenv("INVITE_TTL_SECONDS", "900")),
            invite_hourly_limit=int(os.getenv("INVITE_HOURLY_LIMIT", "5")),
            invite_group_fallback=parse_bool(
                os.getenv("INVITE_GROUP_FALLBACK", "true")
            ),
            telethon_api_id=os.getenv("TELETHON_API_ID", "").strip(),
            telethon_api_hash=os.getenv("TELETHON_API_HASH", "").strip(),
            telethon_session=os.getenv("TELETHON_SESSION", "").strip(),
        )


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


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
class OPProgram:
    code: str
    name: str
    school: str
    admin: str
    aliases: tuple[str, ...] = ()
    # ID чата ОП, куда бот выдаёт доступ. None — привязка не настроена.
    chat_id: int | None = None
    chat_title: str = ""


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
                    raw_chat_id = info.get("chat_id")
                    self._ops[code_upper] = OPProgram(
                        code=code_upper,
                        name=str(info.get("name", code_upper)),
                        school=str(info.get("school", "")),
                        admin=str(info.get("admin", "@admin")),
                        aliases=tuple(str(a) for a in raw_aliases),
                        chat_id=int(raw_chat_id) if raw_chat_id else None,
                        chat_title=str(info.get("chat_title", "")),
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
                "chat_id": prog.chat_id,
                "chat_title": prog.chat_title,
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
        return self._update(code, admin=admin)

    def set_chat(
        self, code: str, chat_id: int | None, chat_title: str = ""
    ) -> bool:
        """Привязать (или отвязать при ``chat_id=None``) чат ОП."""
        return self._update(code, chat_id=chat_id, chat_title=chat_title)

    def _update(self, code: str, **changes: Any) -> bool:
        code_upper = code.upper()
        if code_upper not in self._ops:
            return False
        self._ops[code_upper] = replace(self._ops[code_upper], **changes)
        self.save()
        return True

    def find_by_chat_id(self, chat_id: int) -> OPProgram | None:
        for prog in self._ops.values():
            if prog.chat_id == chat_id:
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    registry: AdminRegistry = context.application.bot_data["admins"]
    if message.chat.type == ChatType.PRIVATE and not registry.contains(user.id):
        # Обычным студентам личный чат нужен, чтобы получать сюда ссылку на
        # чат своей ОП. Служебные команды при этом остаются закрытыми.
        await reply_with_connect_retry(
            message,
            "Привет! 👋 Теперь я смогу присылать тебе ссылку на чат твоей ОП "
            "сюда, в личные сообщения, а не в общий чат.\n\n"
            "Вернись в чат первого курса, ответь на моё приветствие кодом "
            "своей ОП (например SE) и нажми кнопку доступа."
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


def build_op_response(user_mention: str, op: OPProgram) -> str:
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


# --------------------------------------------------------------------------- #
# Выдача доступа в чаты ОП
# --------------------------------------------------------------------------- #

JOIN_CALLBACK_PREFIX = "join"
MEMBER_STATUSES = (
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
    ChatMemberStatus.RESTRICTED,
)


def build_join_keyboard(op: OPProgram, user_id: int) -> InlineKeyboardMarkup | None:
    """Кнопка доступа, жёстко привязанная к конкретному пользователю."""
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


async def is_chat_member(bot: object, chat_id: int, user_id: int) -> bool:
    try:
        membership = await bot.get_chat_member(chat_id, user_id)  # type: ignore[attr-defined]
    except TelegramError:
        return False
    if membership.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(membership, "is_member", False))
    return membership.status in MEMBER_STATUSES


async def deliver_invite(
    context: ContextTypes.DEFAULT_TYPE,
    invite: PendingInvite,
    op: OPProgram,
    user_mention: str,
    source_message: Message | None,
) -> str:
    """Отдать ссылку человеку. Сначала в личку, при неудаче — в чат.

    Возвращает короткий текст для всплывающего ответа на нажатие кнопки.
    """
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
    """Нажатие кнопки «Вступить в чат ОП»."""
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

    # Главная проверка: кнопка работает только у того, кому она адресована.
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

    # У старых сообщений query.message может быть недоступен для ответа.
    source_message = query.message if isinstance(query.message, Message) else None
    source_chat_id = query.message.chat_id if query.message else None

    # Доступ выдаём только участникам общего чата первого курса.
    if source_chat_id is not None and not await is_chat_member(
        context.bot, source_chat_id, target_id
    ):
        await query.answer("Сначала нужно вступить в общий чат.", show_alert=True)
        return

    if await is_chat_member(context.bot, op.chat_id, target_id):
        await query.answer("Вы уже состоите в чате этой ОП 🙂", show_alert=True)
        return

    # Необязательный путь: аккаунт-помощник добавляет по юзернейму сам.
    adder: UserbotAdder | None = context.application.bot_data.get("userbot")
    username = query.from_user.username
    if adder is not None and adder.ready and username:
        result = await adder.add_by_username(username, op.chat_id)
        if result.ok:
            await query.answer(f"Готово, вы добавлены в чат {op.code} ✅", show_alert=True)
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

    if issued.reused:
        minutes = max(1, issued.invite.seconds_left // 60)
        await query.answer(
            "Ссылка уже отправлена — проверьте сообщения выше или личный чат. "
            f"Она действует ещё около {minutes} мин.",
            show_alert=True,
        )
        return

    user_mention = query.from_user.mention_html()
    notice = await deliver_invite(context, issued.invite, op, user_mention, source_message)
    await query.answer(notice, show_alert=True)


async def handle_chat_join_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Одобрить заявку только у владельца ссылки, остальные — отклонить."""
    request = update.chat_join_request
    if not request:
        return

    manager: InviteManager = context.application.bot_data["invites"]
    invite_link = request.invite_link.invite_link if request.invite_link else None
    outcome = await manager.handle_join_request(
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
        await reply_with_connect_retry(
            message,
            build_op_response(user_mention, op),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.message_id,
            reply_markup=build_join_keyboard(op, user_id),
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

    keyboard = None
    if len(matched_ops) == 1:
        reply_text = build_op_response(user_mention, matched_ops[0])
        keyboard = build_join_keyboard(matched_ops[0], user_id)
    else:
        items = []
        for op in matched_ops:
            admin_tag = format_admin_tag(op.admin)
            items.append(
                f"• <b>{html.escape(op.code)}</b> ({html.escape(op.name)})\n"
                f"  🏫 <i>{html.escape(op.school)}</i> — {admin_tag}"
            )
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


async def is_authorized_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
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


async def set_op_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setopchat <КОД_ОП> [chat_id] — привязать чат ОП к коду."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not await is_authorized_admin(update, context):
        await reply_with_connect_retry(message, "Команда доступна только администраторам.")
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


async def describe_chat_readiness(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> str | None:
    """Проверить, что бот способен выдавать доступ в этот чат."""
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


async def show_op_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/opchats — диагностика привязок и прав бота."""
    message = update.effective_message
    if not message:
        return
    if not await is_authorized_admin(update, context):
        await reply_with_connect_retry(message, "Команда доступна только администраторам.")
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
    message = update.effective_message
    if not message or not message.new_chat_members or not message.chat:
        return

    chain: MarkovChain = context.application.bot_data["greeting_chain"]
    settings: Settings = context.application.bot_data["settings"]
    tracker: WelcomeTracker = context.application.bot_data["welcome_tracker"]
    bot_id = context.bot.id

    for member in message.new_chat_members:
        if member.id == bot_id:
            await reply_with_connect_retry(
                message,
                "✅ Бот подключён. Я буду приветствовать новых участников. "
                "Администратор может выполнить /allowpm, чтобы открыть личные "
                "команды /start и /preview.",
            )
            continue
        text = build_welcome_text(chain, member.mention_html(), settings.max_words)
        sent_msg = await reply_with_connect_retry(message, text, parse_mode=ParseMode.HTML)
        if sent_msg and hasattr(sent_msg, "message_id"):
            tracker.add_welcome_message(message.chat.id, sent_msg.message_id, member.id)
        else:
            tracker.add_user(message.chat.id, member.id)


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
    invite_manager = InviteManager(
        settings.invites_path,
        ttl_seconds=settings.invite_ttl_seconds,
        hourly_limit=settings.invite_hourly_limit,
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
        }
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("preview", preview))
    application.add_handler(CommandHandler("allowpm", allow_private_messages))
    application.add_handler(CommandHandler("id", show_ids))
    application.add_handler(CommandHandler("ops", show_ops))
    application.add_handler(CommandHandler("setopadmin", set_op_admin))
    application.add_handler(CommandHandler("setopchat", set_op_chat))
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
    # chat_join_request не входит в набор обновлений по умолчанию —
    # без явного allowed_updates бот не увидит заявки на вступление.
    create_application(settings).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

