"""Configuration, constants, and layout utilities."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram.constants import ChatMemberStatus

BASE_DIR = Path(__file__).resolve().parent

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


def parse_bool(value: str) -> bool:
    """Parse string representation of boolean."""
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


def parse_id_list(value: str) -> set[int]:
    """Parse comma-separated list of Telegram user IDs."""
    try:
        return {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError as error:
        raise ValueError("ADMIN_USER_IDS должен содержать ID через запятую") from error


JOIN_CALLBACK_PREFIX = "join"
JOIN_START_PREFIX = "join_"

MEMBER_STATUSES = (
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
    ChatMemberStatus.RESTRICTED,
)

QUESTION_PATTERN = re.compile(
    r"(?i)\b("
    r"кто\s+(с|на|из|тут|в|поступил|поступает|учится|выбрал|идет|шел|пойдет|туда)|"
    r"есть\s+(ли\s+)?(тут\s+)?кто|"
    r"кто[- ](нибудь|то)|"
    r"а\s+кто|"
    r"ищу\s+(кто|кого|с|на|из)|"
    r"много\s+(ли\s+)?(тут\s+)?(кто|с|на|из)|"
    r"кого\s+больше|"
    r"что\s+(лучше|выбрать|посоветуете)|"
    r"куда\s+(лучше|поступать|идти)|"
    r"стоит\s+ли\s+(идти\s+на|выбирать)|"
    r"расскажите\s+(про|о)|"
    r"подскажите\s+(про|по|о|насчет)|"
    r"как\s+(вам|там|учиться\s+на)|"
    r"чем\s+отличается|"
    r"а\s+ты\s+(с|на|из)|"
    r"вы\s+(с|на|из)"
    r")\b"
)

SELF_ID_PREFIX_PATTERN = re.compile(
    r"(?i)\b("
    r"я\s+(с|из|на|в|поступил|поступила|учусь|выбрал|выбрала|иду)|"
    r"моя\s+оп|"
    r"мо[её]\s+направление|"
    r"моя\s+программа|"
    r"поступил(а)?\s+(на|в)|"
    r"выбрал(а)?|"
    r"учусь\s+(на|в)|"
    r"направление|"
    r"программа|"
    r"специальность"
    r")\b"
)

CS_CODE_PATTERN = re.compile(
    r"(?i)^\s*(?:(?:привет[,\s]*)?(?:я\s+(?:с|из|на|в)\s+)?|моя\s+оп\s*)?(cs|кс|ыс)[.!]?\s*$"
)

AMBIGUOUS_CS_QUESTION = (
    "Вы о <b>Computer Science (IT)</b> или <b>Cybersecurity (CS)</b>?\n\n"
    "Ответьте, выбрав один из вариантов."
)

DEFAULT_OPS: dict[str, dict[str, Any]] = {
    "SE": {
        "name": "Software Engineering",
        "school": "School of Software Engineering",
        "admin": "@Alsh444 и @Anuarick",
        "aliases": [
            "SE",
            "СЕ",
            "Software Engineering",
            "сешник",
            "сешники",
            "сешница",
            "софтвер",
            "софтверщик",
            "софтварщик",
            "софтваре",
            "софтвар",
            "софтвер инжиниринг",
            "софтвар инжиниринг",
        ],
    },
    "IT": {
        "name": "Computer Science",
        "school": "School of Artificial Intelligence and Data Science",
        "admin": "@TypicallyRain",
        "aliases": [
            "IT",
            "ИТ",
            "Computer Science",
            "айти",
            "айтишник",
            "айтишники",
            "компьютер сайнс",
            "комп сайнс",
            "комп сай",
            "комп саи",
            "компьютерные науки",
            "кс",
        ],
    },
    "BDA": {
        "name": "Big Data Analysis",
        "school": "School of Artificial Intelligence and Data Science",
        "admin": "@therealarujxx",
        "aliases": [
            "BDA",
            "БДА",
            "Big Data Analysis",
            "Big Data",
            "бдашник",
            "бдашники",
            "бигдата",
            "биг дата",
            "дата анализис",
            "биг дата анализис",
        ],
    },
    "MCS": {
        "name": "Mathematical and Computational Science",
        "school": "School of Artificial Intelligence and Data Science",
        "admin": "@howflowersbloom",
        "aliases": [
            "MCS",
            "МКС",
            "Mathematical and Computational Science",
            "мксник",
            "мксники",
            "маткомп",
            "математикал",
        ],
    },
    "CS": {
        "name": "Cybersecurity",
        "school": "School of Cybersecurity",
        "admin": "@alishaisyapping",
        "aliases": [
            "CS",
            "КС",
            "Cybersecurity",
            "Cyber Security",
            "кибербез",
            "кибербезопасность",
            "киберсекьюрити",
            "кибер секьюрити",
            "сайберсекьюрити",
            "сайбер",
            "кибер",
        ],
    },
    "SST": {
        "name": "Smart Security Technologies",
        "school": "School of Cybersecurity",
        "admin": "@alishaisyapping",
        "aliases": [
            "SST",
            "ССТ",
            "Smart Security Technologies",
            "сстшник",
            "смарт секьюрити",
            "смартсекьюрити",
            "смарт секьюрити технолоджис",
        ],
    },
    "IIOT": {
        "name": "Industrial Internet of Things",
        "school": "School of Intelligent Systems",
        "admin": "@dhshrbrhr и @urkerim",
        "aliases": [
            "IIOT",
            "ИИОТ",
            "Industrial Internet of Things",
            "ииотник",
            "индастриал иот",
            "индустриал иот",
            "иот",
        ],
    },
    "EE": {
        "name": "Electronic Engineering",
        "school": "School of Intelligent Systems",
        "admin": "@dhshrbrhr и @urkerim",
        "aliases": [
            "EE",
            "ЕЕ",
            "ЭЭ",
            "Electronic Engineering",
            "электронщик",
            "электроник инжиниринг",
            "електроник инжиниринг",
            "электроника",
        ],
    },
    "ST": {
        "name": "Smart Technologies",
        "school": "School of Intelligent Systems",
        "admin": "@dhshrbrhr и @urkerim",
        "aliases": [
            "ST",
            "СТ",
            "Smart Technologies",
            "стшник",
            "смарт тех",
            "смарт технолоджис",
        ],
    },
    "DNE": {
        "name": "Digital technologies in nuclear power engineering",
        "school": "School of Intelligent Systems",
        "admin": "@dhshrbrhr и @urkerim",
        "aliases": [
            "DNE",
            "DTNPE",
            "ДНЕ",
            "ДТНПЕ",
            "Digital technologies in nuclear power engineering",
            "днешник",
            "нуклеар",
            "ядерка",
            "ядерная инженерия",
        ],
    },
    "ITM": {
        "name": "IT Management",
        "school": "School of Digital Public Administration",
        "admin": "@assiixq",
        "aliases": [
            "ITM",
            "ИТМ",
            "IT Management",
            "итмщик",
            "айти менеджмент",
            "ит менеджмент",
        ],
    },
    "ITE": {
        "name": "IT Entrepreneurship",
        "school": "School of Digital Public Administration",
        "admin": "@assiixq",
        "aliases": [
            "ITE",
            "ИТЕ",
            "IT Entrepreneurship",
            "итешник",
            "айти предпринимательство",
            "ит предпринимательство",
            "айти энтрепренершип",
        ],
    },
    "AIB": {
        "name": "AI Business",
        "school": "School of Digital Public Administration",
        "admin": "@assiixq",
        "aliases": [
            "AIB",
            "АИБ",
            "AI Business",
            "аибник",
            "ииб",
            "аи бизнес",
            "ай бизнес",
            "ии бизнес",
        ],
    },
    "MT": {
        "name": "Media Technologies",
        "school": "School of Creative Industries",
        "admin": "@Subbzerr01",
        "aliases": [
            "MT",
            "МТ",
            "Media Technologies",
            "мтшник",
            "медиа тех",
            "медия тех",
            "медиа технологии",
            "медия технологии",
            "медиатехнологии",
        ],
    },
    "DJ": {
        "name": "Digital Journalism",
        "school": "School of Creative Industries",
        "admin": "@vveetaaa",
        "aliases": [
            "DJ",
            "ДЖ",
            "Digital Journalism",
            "джник",
            "журналистика",
            "диджитал журналистика",
            "цифровая журналистика",
            "диджей",
        ],
    },
    "DPA": {
        "name": "Digital Public Administration",
        "school": "School of Digital Public Administration",
        "admin": "@assiixq",
        "aliases": [
            "DPA",
            "ДПА",
            "Digital Public Administration",
            "дпашник",
            "диджитал паблик",
            "госуправление",
        ],
    },
    "DL": {
        "name": "Digital Jurisprudence",
        "school": "School of Digital Public Administration",
        "admin": "@finrandiri",
        "aliases": [
            "DL",
            "ДЛ",
            "Digital Jurisprudence",
            "длшник",
            "юриспруденция",
            "диджитал юриспруденция",
            "цифровая юриспруденция",
            "цифровые юристы",
        ],
    },
}


@dataclass(frozen=True)
class Settings:
    token: str
    greetings_path: Path
    admins_path: Path = BASE_DIR / "admins.json"
    op_admins_path: Path = BASE_DIR / "op_admins.json"
    invites_path: Path = BASE_DIR / "invites.json"
    database_url: str = ""
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
        database_url = os.getenv("DATABASE_URL", "").strip()

        return cls(
            token=token,
            greetings_path=project_path("GREETINGS_FILE", "greetings.txt"),
            admins_path=project_path("ADMINS_FILE", "admins.json"),
            op_admins_path=project_path("OP_ADMINS_FILE", "op_admins.json"),
            invites_path=project_path("INVITES_FILE", "invites.json"),
            database_url=database_url,
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
