"""Data models and schemas for the bot."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class OPProgram:
    """Educational Program (Образовательная Программа / ОП)."""

    code: str
    name: str
    school: str
    admin: str
    aliases: tuple[str, ...] = ()
    chat_id: int | None = None
    chat_title: str = ""


@dataclass
class PendingInvite:
    """Issued personal invite link with join request approval mechanism."""

    invite_link: str
    target_chat_id: int
    user_id: int
    op_code: str
    expires_at: float
    source_chat_id: int | None = None
    source_message_id: int | None = None

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def seconds_left(self) -> int:
        return max(0, int(self.expires_at - time.time()))


@dataclass
class IssueResult:
    """Result of attempting to issue an invite link."""

    invite: PendingInvite
    reused: bool = False


class AddOutcome(str, Enum):
    """Outcome status for userbot add operations."""

    ADDED = "added"
    ALREADY_MEMBER = "already_member"
    PRIVACY_RESTRICTED = "privacy_restricted"
    USER_NOT_FOUND = "user_not_found"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"


@dataclass(frozen=True)
class AddResult:
    """Result from adding a user via Userbot."""

    outcome: AddOutcome
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (AddOutcome.ADDED, AddOutcome.ALREADY_MEMBER)
