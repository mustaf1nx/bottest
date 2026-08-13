"""ОПЦИОНАЛЬНО: добавление в чат по юзернейму через аккаунт-помощник (MTProto).

Зачем это нужно
---------------
Bot API физически не умеет добавлять людей в чат: метода вроде ``addChatMember``
в нём нет. Добавить участника может только обычный аккаунт. Поэтому, если вы
хотите, чтобы «при наличии юзернейма человек добавлялся сам», нужен отдельный
пользовательский аккаунт (Telethon), который является админом чатов ОП.

Ограничения, о которых стоит знать заранее
------------------------------------------
* У большинства студентов в настройках приватности стоит «Кто может добавлять
  меня в группы: мои контакты» — тогда Telegram вернёт ``USER_PRIVACY_RESTRICTED``
  и добавить не получится. Это не баг, это защита на стороне пользователя.
* Массовые добавления с одного аккаунта Telegram считает спамом: возможны
  FLOOD_WAIT и ограничения на аккаунт. Используйте отдельный аккаунт, не свой
  основной, и не выкручивайте темп.
* Автоматизация пользовательских аккаунтов — серая зона правил Telegram.

Если модуль не настроен, бот просто работает по схеме с персональными
ссылками-заявками (см. ``invites.py``) — она безопасна и не требует аккаунта.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

LOGGER = logging.getLogger(__name__)


class AddOutcome(str, Enum):
    ADDED = "added"
    ALREADY_MEMBER = "already_member"
    PRIVACY_RESTRICTED = "privacy_restricted"
    USER_NOT_FOUND = "user_not_found"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"


@dataclass(frozen=True)
class AddResult:
    outcome: AddOutcome
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (AddOutcome.ADDED, AddOutcome.ALREADY_MEMBER)


class UserbotAdder:
    """Тонкая обёртка над Telethon. Создаётся только если заданы креды."""

    def __init__(self, api_id: int, api_hash: str, session: str) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session
        self._client = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_environment(cls, api_id: str, api_hash: str, session: str):
        """Вернуть экземпляр или ``None``, если модуль не настроен."""
        if not (api_id and api_hash and session):
            return None
        try:
            import telethon  # noqa: F401
        except ImportError:
            LOGGER.warning(
                "TELETHON_* заданы, но пакет telethon не установлен. "
                "Добавление по юзернейму отключено."
            )
            return None
        try:
            return cls(int(api_id), api_hash, session)
        except ValueError:
            LOGGER.warning("TELETHON_API_ID должен быть числом.")
            return None

    async def start(self) -> None:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        self._client = TelegramClient(
            StringSession(self._session), self._api_id, self._api_hash
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            LOGGER.error("Сессия Telethon недействительна, добавление отключено.")
            await self.stop()

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    @property
    def ready(self) -> bool:
        return self._client is not None

    async def add_by_username(self, username: str, chat_id: int) -> AddResult:
        """Добавить пользователя по юзернейму в чат ОП."""
        if self._client is None:
            return AddResult(AddOutcome.FAILED, "клиент не запущен")

        from telethon import errors, functions

        handle = username.lstrip("@")
        async with self._lock:  # последовательно: так меньше риск флуда
            try:
                user = await self._client.get_entity(handle)
                channel = await self._client.get_entity(chat_id)
                await self._client(
                    functions.channels.InviteToChannelRequest(
                        channel=channel, users=[user]
                    )
                )
                await asyncio.sleep(2)  # мягкий темп, чтобы не ловить флуд
                return AddResult(AddOutcome.ADDED)
            except errors.UserAlreadyParticipantError:
                return AddResult(AddOutcome.ALREADY_MEMBER)
            except (
                errors.UserPrivacyRestrictedError,
                errors.UserNotMutualContactError,
                errors.UserChannelsTooMuchError,
            ) as error:
                return AddResult(AddOutcome.PRIVACY_RESTRICTED, type(error).__name__)
            except (
                errors.UsernameNotOccupiedError,
                errors.UsernameInvalidError,
                ValueError,
            ) as error:
                return AddResult(AddOutcome.USER_NOT_FOUND, str(error))
            except errors.FloodWaitError as error:
                LOGGER.warning("FLOOD_WAIT %s c при добавлении", error.seconds)
                return AddResult(AddOutcome.RATE_LIMITED, f"{error.seconds} c")
            except Exception as error:  # noqa: BLE001 - падать из-за этого нельзя
                LOGGER.warning("Не удалось добавить @%s: %s", handle, error)
                return AddResult(AddOutcome.FAILED, str(error))
