"""WelcomeTracker: Onboarding session tracking and debounce management."""

from __future__ import annotations

import time
from typing import Any


class WelcomeTracker:
    """Tracks welcome messages and recently joined members so OP reactions only trigger for the actual new member."""

    def __init__(self, max_messages: int = 10, ttl_seconds: float = 900.0) -> None:
        self.max_messages = max_messages
        self.ttl_seconds = ttl_seconds
        self._active_new_members: dict[tuple[int, int], dict[str, Any]] = {}
        self._welcome_messages: dict[tuple[int, int], int] = {}
        self._pending_clarification: set[tuple[int, int]] = set()
        self._recent_welcomes: dict[tuple[int, int], float] = {}
        self._answered: set[tuple[int, int]] = set()
        self._thread_messages: dict[tuple[int, int], list[int]] = {}

    def should_welcome(
        self, chat_id: int, user_id: int, debounce_seconds: float = 15.0
    ) -> bool:
        """Prevent duplicate greetings within a short debounce window."""
        now = time.time()
        self._recent_welcomes = {
            k: ts for k, ts in self._recent_welcomes.items() if now - ts < debounce_seconds
        }
        key = (chat_id, user_id)
        if key in self._recent_welcomes:
            return False
        self._recent_welcomes[key] = now
        return True

    def add_welcome_message(
        self, chat_id: int, welcome_message_id: int, target_user_id: int
    ) -> None:
        """Anchor a bot message to a specific member."""
        self._welcome_messages[(chat_id, welcome_message_id)] = target_user_id
        self.add_user(chat_id, target_user_id)
        self.track_thread_message(chat_id, target_user_id, welcome_message_id)

    def add_user(self, chat_id: int, user_id: int) -> None:
        """Register a newcomer with active message budget and timestamp."""
        self._active_new_members[(chat_id, user_id)] = {
            "messages_left": self.max_messages,
            "joined_at": time.time(),
        }

    def is_active_newcomer(self, chat_id: int, user_id: int) -> bool:
        """Check if user joined recently and has remaining message budget."""
        key = (chat_id, user_id)
        info = self._active_new_members.get(key)
        if not info:
            return False
        if time.time() - info["joined_at"] > self.ttl_seconds:
            self._active_new_members.pop(key, None)
            return False
        return info["messages_left"] > 0

    def is_anchored_reply(
        self,
        chat_id: int,
        user_id: int,
        reply_to_message_id: int | None = None,
    ) -> bool:
        """Check if message is replying to the bot's welcome message specifically meant for this user."""
        if reply_to_message_id is None:
            return False
        welcome_target = self._welcome_messages.get((chat_id, reply_to_message_id))
        return welcome_target is not None and user_id == welcome_target

    def is_target_member(
        self,
        chat_id: int,
        user_id: int,
        reply_to_message_id: int | None = None,
    ) -> bool:
        return self.is_anchored_reply(
            chat_id, user_id, reply_to_message_id
        ) or self.is_active_newcomer(chat_id, user_id)

    def has_answered(self, chat_id: int, user_id: int) -> bool:
        return (chat_id, user_id) in self._answered

    def mark_answered(self, chat_id: int, user_id: int) -> None:
        self._answered.add((chat_id, user_id))

    def track_thread_message(self, chat_id: int, user_id: int, message_id: int) -> None:
        """Track onboarding message IDs for cleanup after join."""
        self._thread_messages.setdefault((chat_id, user_id), []).append(message_id)

    def pop_thread_messages(self, chat_id: int, user_id: int) -> list[int]:
        """Pop all tracked onboarding messages for this user in chat."""
        return self._thread_messages.pop((chat_id, user_id), [])

    def record_message(self, chat_id: int, user_id: int) -> None:
        """Decrement message budget for non-matching utterances."""
        key = (chat_id, user_id)
        if key in self._active_new_members:
            self._active_new_members[key]["messages_left"] -= 1
            if self._active_new_members[key]["messages_left"] <= 0:
                del self._active_new_members[key]

    def remove_user(self, chat_id: int, user_id: int) -> None:
        """Remove user from active newcomers list."""
        self._active_new_members.pop((chat_id, user_id), None)
        self.clear_pending_clarification(chat_id, user_id)

    def is_pending_clarification(self, chat_id: int, user_id: int) -> bool:
        return (chat_id, user_id) in self._pending_clarification

    def add_pending_clarification(self, chat_id: int, user_id: int) -> None:
        self._pending_clarification.add((chat_id, user_id))

    def clear_pending_clarification(self, chat_id: int, user_id: int) -> None:
        self._pending_clarification.discard((chat_id, user_id))
