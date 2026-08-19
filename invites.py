"""Безопасная выдача персональных пригласительных ссылок в чаты ОП.

Модель безопасности
-------------------
Ссылки создаются с ``creates_join_request=True``. Такая ссылка НЕ добавляет
человека в чат: она создаёт заявку на вступление, которую бот рассматривает
сам. Бот одобряет заявку, только если ``user_id`` заявителя совпадает с тем,
кому ссылка была выдана. Поэтому даже утёкшая (пересланная, заскриншоченная)
ссылка бесполезна для посторонних — их заявки автоматически отклоняются.

Дополнительные ограничения:
* короткий срок жизни (``ttl_seconds``, по умолчанию 15 минут);
* ссылка отзывается сразу после успешного вступления, по истечении срока
  или при первой же чужой попытке;
* лимит на количество выдач одному человеку в час;
* сообщение со ссылкой в общем чате удаляется автоматически.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Bot
from telegram.error import TelegramError

from models import IssueResult, PendingInvite

if TYPE_CHECKING:
    from database import DatabaseStorage

LOGGER = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 15 * 60
DEFAULT_HOURLY_LIMIT = 5


class InviteError(Exception):
    """Не удалось выдать ссылку по понятной пользователю причине."""


class InviteManager:
    """Хранит и обслуживает персональные ссылки-приглашения."""

    def __init__(
        self,
        path: Path | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        hourly_limit: int = DEFAULT_HOURLY_LIMIT,
        db: DatabaseStorage | None = None,
    ) -> None:
        self.path = path
        self.db = db
        self.ttl_seconds = ttl_seconds
        self.hourly_limit = hourly_limit
        self._pending: dict[str, PendingInvite] = {}
        self._issue_log: dict[int, list[float]] = {}
        self._lock = asyncio.Lock()
        self._load()

    # ------------------------------------------------------------------ #
    # Хранилище
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if self.db is not None:
            self._pending = self.db.load_pending_invites()
            return

        if self.path is not None and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as error:
                LOGGER.warning("Не удалось прочитать %s: %s", self.path, error)
                return
            for item in raw.get("pending", []):
                try:
                    invite = PendingInvite(**item)
                except TypeError:
                    continue
                self._pending[invite.invite_link] = invite

    def _save(self) -> None:
        if self.db is not None:
            for invite in self._pending.values():
                self.db.save_pending_invite(invite)

        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"pending": [asdict(item) for item in self._pending.values()]}
            temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.path)

    # ------------------------------------------------------------------ #
    # Поиск
    # ------------------------------------------------------------------ #
    def find_active(self, user_id: int, target_chat_id: int) -> PendingInvite | None:
        for invite in self._pending.values():
            if (
                invite.user_id == user_id
                and invite.target_chat_id == target_chat_id
                and not invite.is_expired
            ):
                return invite
        return None

    def lookup(self, invite_link: str) -> PendingInvite | None:
        return self._pending.get(invite_link)

    def _rate_limited(self, user_id: int) -> bool:
        now = time.time()
        history = [ts for ts in self._issue_log.get(user_id, []) if now - ts < 3600]
        self._issue_log[user_id] = history
        return len(history) >= self.hourly_limit

    # ------------------------------------------------------------------ #
    # Выдача
    # ------------------------------------------------------------------ #
    async def issue(
        self,
        bot: Bot,
        op_code: str,
        target_chat_id: int,
        user_id: int,
    ) -> IssueResult:
        """Создать (или переиспользовать) персональную ссылку."""
        async with self._lock:
            existing = self.find_active(user_id, target_chat_id)
            if existing is not None:
                return IssueResult(existing, reused=True)

            if self._rate_limited(user_id):
                raise InviteError(
                    "Слишком много попыток получить ссылку. "
                    "Попробуйте через час или напишите ответственному за ОП."
                )

            expire_at = datetime.now(timezone.utc) + timedelta(
                seconds=self.ttl_seconds + 60
            )
            try:
                link = await bot.create_chat_invite_link(
                    chat_id=target_chat_id,
                    name=f"{op_code}-{user_id}"[:32],
                    expire_date=expire_at,
                    creates_join_request=True,
                )
            except TelegramError as error:
                LOGGER.error(
                    "Не удалось создать ссылку для чата %s: %s", target_chat_id, error
                )
                raise InviteError(
                    "Не получилось создать ссылку. Убедитесь, что бот — "
                    "администратор чата ОП с правом «Приглашать пользователей»."
                ) from error

            invite = PendingInvite(
                invite_link=link.invite_link,
                target_chat_id=target_chat_id,
                user_id=user_id,
                op_code=op_code,
                expires_at=time.time() + self.ttl_seconds,
            )
            self._pending[invite.invite_link] = invite
            self._issue_log.setdefault(user_id, []).append(time.time())
            if self.db is not None:
                self.db.save_pending_invite(invite)
                self.db.log_onboarding_action(
                    user_id, target_chat_id, "issued_invite", f"op={op_code}"
                )
            self._save()
            LOGGER.info(
                "Выдана ссылка в чат %s для пользователя %s (ОП %s)",
                target_chat_id,
                user_id,
                op_code,
            )
            return IssueResult(invite, reused=False)

    def attach_message(
        self, invite: PendingInvite, chat_id: int, message_id: int
    ) -> None:
        """Запомнить сообщение со ссылкой, чтобы удалить его позже."""
        invite.source_chat_id = chat_id
        invite.source_message_id = message_id
        if self.db is not None:
            self.db.save_pending_invite(invite)
        self._save()

    def set_source_chat(self, invite: PendingInvite, chat_id: int) -> None:
        """Запомнить исходный чат первого курса."""
        if invite.source_chat_id is None:
            invite.source_chat_id = chat_id
            if self.db is not None:
                self.db.save_pending_invite(invite)
            self._save()

    # ------------------------------------------------------------------ #
    # Заявки на вступление
    # ------------------------------------------------------------------ #
    async def handle_join_request(
        self, bot: Bot, chat_id: int, user_id: int, invite_link: str | None
    ) -> tuple[str, PendingInvite | None]:
        """Решить судьбу заявки на вступление."""
        if not invite_link:
            return "ignored", None

        async with self._lock:
            invite = self._pending.get(invite_link)
            if invite is None or invite.target_chat_id != chat_id:
                return "ignored", None

            if invite.user_id != user_id or invite.is_expired:
                reason = "истёкшая" if invite.is_expired else "чужая"
                LOGGER.warning(
                    "Отклонена заявка от %s в чат %s (%s ссылка, выдана для %s)",
                    user_id,
                    chat_id,
                    reason,
                    invite.user_id,
                )
                try:
                    await bot.decline_chat_join_request(chat_id, user_id)
                except TelegramError as error:
                    LOGGER.warning("Не удалось отклонить заявку: %s", error)
                if self.db is not None:
                    self.db.log_onboarding_action(
                        user_id,
                        chat_id,
                        "declined_join",
                        f"reason={reason},owner={invite.user_id}",
                    )
                # Ссылка скомпрометирована — сжигаем её целиком.
                await self._retire(bot, invite)
                return "declined", invite

            try:
                await bot.approve_chat_join_request(chat_id, user_id)
            except TelegramError as error:
                LOGGER.warning("Не удалось одобрить заявку: %s", error)
                return "ignored", None

            if self.db is not None:
                self.db.log_onboarding_action(
                    user_id, chat_id, "approved_join", f"op={invite.op_code}"
                )
            await self._retire(bot, invite)
            LOGGER.info("Пользователь %s принят в чат %s", user_id, chat_id)
            return "approved", invite

    # ------------------------------------------------------------------ #
    # Уборка
    # ------------------------------------------------------------------ #
    async def _retire(self, bot: Bot, invite: PendingInvite) -> None:
        """Отозвать ссылку, удалить сообщение с ней и забыть запись."""
        self._pending.pop(invite.invite_link, None)
        if self.db is not None:
            self.db.delete_pending_invite(invite.invite_link)
        self._save()
        try:
            await bot.revoke_chat_invite_link(
                invite.target_chat_id, invite.invite_link
            )
        except TelegramError as error:
            LOGGER.debug("Ссылку не удалось отозвать: %s", error)
        if invite.source_chat_id and invite.source_message_id:
            try:
                await bot.delete_message(
                    invite.source_chat_id, invite.source_message_id
                )
            except TelegramError as error:
                LOGGER.debug("Сообщение со ссылкой не удалено: %s", error)

    async def retire(self, bot: Bot, invite: PendingInvite) -> None:
        async with self._lock:
            await self._retire(bot, invite)

    async def expire_later(self, bot: Bot, invite: PendingInvite) -> None:
        """Через TTL отозвать ссылку, если ею так и не воспользовались."""
        await asyncio.sleep(max(1, invite.seconds_left))
        async with self._lock:
            if self._pending.get(invite.invite_link) is invite:
                await self._retire(bot, invite)

    async def sweep(self, bot: Bot) -> int:
        """Подчистить просроченные ссылки."""
        async with self._lock:
            expired = [i for i in self._pending.values() if i.is_expired]
            for invite in expired:
                await self._retire(bot, invite)
        if expired:
            LOGGER.info("Отозвано просроченных ссылок: %s", len(expired))
        return len(expired)
