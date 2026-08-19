"""Unit tests for WelcomeTracker and onboarding state handling."""

import time
import unittest

from tracker import WelcomeTracker


class WelcomeTrackerTests(unittest.TestCase):
    def test_debounce_behavior(self) -> None:
        tracker = WelcomeTracker()
        chat_id = 100
        user_id = 42

        # First welcome allowed
        self.assertTrue(tracker.should_welcome(chat_id, user_id, debounce_seconds=5.0))
        # Immediate duplicate blocked
        self.assertFalse(tracker.should_welcome(chat_id, user_id, debounce_seconds=5.0))
        # Different user allowed
        self.assertTrue(tracker.should_welcome(chat_id, 999, debounce_seconds=5.0))

    def test_anchored_reply_and_message_tracking(self) -> None:
        tracker = WelcomeTracker(max_messages=3, ttl_seconds=60.0)
        chat_id = 200
        user_id = 55
        welcome_msg_id = 1001

        tracker.add_welcome_message(chat_id, welcome_msg_id, user_id)
        self.assertTrue(tracker.is_anchored_reply(chat_id, user_id, welcome_msg_id))
        self.assertFalse(tracker.is_anchored_reply(chat_id, 999, welcome_msg_id))
        self.assertFalse(tracker.is_anchored_reply(chat_id, user_id, 9999))

        # Thread messages
        tracker.track_thread_message(chat_id, user_id, 1002)
        tracker.track_thread_message(chat_id, user_id, 1003)

        popped = tracker.pop_thread_messages(chat_id, user_id)
        self.assertEqual(popped, [welcome_msg_id, 1002, 1003])
        self.assertEqual(tracker.pop_thread_messages(chat_id, user_id), [])

    def test_message_budget_decrements(self) -> None:
        tracker = WelcomeTracker(max_messages=2)
        chat_id = 300
        user_id = 77

        tracker.add_user(chat_id, user_id)
        self.assertTrue(tracker.is_active_newcomer(chat_id, user_id))

        tracker.record_message(chat_id, user_id)
        self.assertTrue(tracker.is_active_newcomer(chat_id, user_id))

        tracker.record_message(chat_id, user_id)
        self.assertFalse(tracker.is_active_newcomer(chat_id, user_id))

    def test_clarification_state(self) -> None:
        tracker = WelcomeTracker()
        chat_id = 400
        user_id = 88

        self.assertFalse(tracker.is_pending_clarification(chat_id, user_id))
        tracker.add_pending_clarification(chat_id, user_id)
        self.assertTrue(tracker.is_pending_clarification(chat_id, user_id))
        tracker.clear_pending_clarification(chat_id, user_id)
        self.assertFalse(tracker.is_pending_clarification(chat_id, user_id))


if __name__ == "__main__":
    unittest.main()
