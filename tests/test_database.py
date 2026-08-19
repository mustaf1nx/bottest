"""Unit and integration tests for DatabaseStorage."""

import tempfile
import time
import unittest
from pathlib import Path

from database import DatabaseStorage
from models import OPProgram, PendingInvite


class DatabaseStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.storage = DatabaseStorage(f"sqlite:///{self.db_path}")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_admin_storage_operations(self) -> None:
        self.assertEqual(self.storage.load_admin_ids(), set())

        # Add single admin
        self.assertTrue(self.storage.add_admin(111))
        self.assertFalse(self.storage.add_admin(111))  # Duplicate ignored
        self.assertEqual(self.storage.load_admin_ids(), {111})

        # Add multiple admins
        self.storage.add_admins([222, 333, 111])
        self.assertEqual(self.storage.load_admin_ids(), {111, 222, 333})

    def test_op_programs_storage_and_seeding(self) -> None:
        initial_ops = {
            "SE": {
                "name": "Software Engineering",
                "school": "School of SE",
                "admin": "@alex",
                "aliases": ["SE", "сешник"],
            },
            "IT": {
                "name": "Computer Science",
                "school": "School of AI",
                "admin": "@kate",
                "aliases": ["IT", "айти"],
            },
        }
        self.storage.seed_ops(initial_ops)
        loaded = self.storage.load_ops()
        self.assertEqual(len(loaded), 2)
        self.assertIn("SE", loaded)
        self.assertEqual(loaded["SE"].name, "Software Engineering")
        self.assertEqual(loaded["SE"].aliases, ("SE", "сешник"))

        # Update admin
        self.assertTrue(self.storage.set_op_admin("SE", "@new_alex"))
        self.assertEqual(self.storage.load_ops()["SE"].admin, "@new_alex")

        # Update chat
        self.assertTrue(self.storage.set_op_chat("SE", -100123456789, "SE Chat"))
        se_op = self.storage.load_ops()["SE"]
        self.assertEqual(se_op.chat_id, -100123456789)
        self.assertEqual(se_op.chat_title, "SE Chat")

        # Non-existent OP
        self.assertFalse(self.storage.set_op_admin("UNKNOWN", "@nobody"))

    def test_pending_invites_storage(self) -> None:
        now = time.time()
        invite1 = PendingInvite(
            invite_link="https://t.me/joinchat/test1",
            target_chat_id=-100111,
            user_id=12345,
            op_code="SE",
            expires_at=now + 600,
            source_chat_id=-100999,
            source_message_id=555,
        )
        invite2 = PendingInvite(
            invite_link="https://t.me/joinchat/test2",
            target_chat_id=-100222,
            user_id=67890,
            op_code="IT",
            expires_at=now - 50,  # Expired
        )

        self.storage.save_pending_invite(invite1)
        self.storage.save_pending_invite(invite2)

        loaded = self.storage.load_pending_invites()
        self.assertEqual(len(loaded), 2)
        self.assertIn("https://t.me/joinchat/test1", loaded)
        self.assertEqual(loaded["https://t.me/joinchat/test1"].user_id, 12345)

        # Delete single invite
        self.storage.delete_pending_invite("https://t.me/joinchat/test1")
        self.assertEqual(len(self.storage.load_pending_invites()), 1)

        # Sweep expired
        deleted_count = self.storage.delete_expired_invites(now)
        self.assertEqual(deleted_count, 1)
        self.assertEqual(len(self.storage.load_pending_invites()), 0)

    def test_audit_and_analytics_logging(self) -> None:
        self.storage.log_onboarding_action(
            user_id=123,
            chat_id=-100,
            action="issued_invite",
            details="op=SE",
        )
        self.storage.record_analytics_event(
            event_type="join_button_clicked",
            user_id=123,
            chat_id=-100,
            payload={"op": "SE", "speed_ms": 140},
        )
        # Verify no exceptions raised and database integrity maintained
        with self.storage.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM onboarding_audit_log")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT COUNT(*) FROM analytics_events")
            self.assertEqual(cur.fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
