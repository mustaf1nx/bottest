"""Unified Database Storage for SQLite and PostgreSQL.

Provides persistent relational storage for:
- Allowed administrators
- Educational Programs (OPs), schools, admins, and chat bindings
- Pending and issued invite links
- Onboarding audit trail and security logs
- Telemetry & funnel analytics events
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Protocol

from models import OPProgram, PendingInvite

LOGGER = logging.getLogger(__name__)


class DBConnection(Protocol):
    def cursor(self) -> Any: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


class DatabaseStorage:
    """Synchronous repository supporting SQLite and PostgreSQL dialects."""

    def __init__(self, database_url: str | Path) -> None:
        self.raw_url = str(database_url).strip()
        self.is_postgres = (
            self.raw_url.startswith("postgresql://")
            or self.raw_url.startswith("postgres://")
        )
        self._initialize_schema()

    def _get_raw_connection(self) -> Any:
        if self.is_postgres:
            import psycopg

            return psycopg.connect(self.raw_url)
        else:
            # SQLite path
            db_path = self.raw_url
            if db_path.startswith("sqlite:///"):
                db_path = db_path[len("sqlite:///") :]
            elif db_path.startswith("sqlite://"):
                db_path = db_path[len("sqlite://") :]
            if not db_path:
                db_path = ":memory:"
            elif db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            return conn

    @contextmanager
    def connect(self) -> Generator[Any, None, None]:
        """Context manager providing a transactional connection."""
        conn = self._get_raw_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize_schema(self) -> None:
        """Create tables and indexes if they do not exist."""
        with self.connect() as conn:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS allowed_admins (
                        user_id BIGINT PRIMARY KEY,
                        added_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS op_programs (
                        code TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        school TEXT NOT NULL,
                        admin TEXT NOT NULL,
                        aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
                        chat_id BIGINT,
                        chat_title TEXT NOT NULL DEFAULT '',
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_invites (
                        invite_link TEXT PRIMARY KEY,
                        target_chat_id BIGINT NOT NULL,
                        user_id BIGINT NOT NULL,
                        op_code TEXT NOT NULL,
                        expires_at DOUBLE PRECISION NOT NULL,
                        source_chat_id BIGINT,
                        source_message_id BIGINT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS onboarding_audit_log (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        chat_id BIGINT NOT NULL,
                        action TEXT NOT NULL,
                        details TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS analytics_events (
                        id BIGSERIAL PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        user_id BIGINT,
                        chat_id BIGINT,
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
            else:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS allowed_admins (
                        user_id INTEGER PRIMARY KEY,
                        added_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS op_programs (
                        code TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        school TEXT NOT NULL,
                        admin TEXT NOT NULL,
                        aliases TEXT NOT NULL DEFAULT '[]',
                        chat_id INTEGER,
                        chat_title TEXT NOT NULL DEFAULT '',
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_invites (
                        invite_link TEXT PRIMARY KEY,
                        target_chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        op_code TEXT NOT NULL,
                        expires_at REAL NOT NULL,
                        source_chat_id INTEGER,
                        source_message_id INTEGER,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS onboarding_audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL,
                        action TEXT NOT NULL,
                        details TEXT NOT NULL DEFAULT '',
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS analytics_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        user_id INTEGER,
                        chat_id INTEGER,
                        payload TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
            cur.close()

    # ------------------------------------------------------------------ #
    # Admins
    # ------------------------------------------------------------------ #
    def load_admin_ids(self) -> set[int]:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM allowed_admins")
            return {int(row[0]) for row in cur.fetchall()}

    def add_admin(self, user_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute(
                    "INSERT INTO allowed_admins (user_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING user_id",
                    (user_id,),
                )
                res = cur.fetchone()
                return res is not None
            else:
                cur.execute(
                    "INSERT OR IGNORE INTO allowed_admins (user_id) VALUES (?)",
                    (user_id,),
                )
                return cur.rowcount > 0

    def add_admins(self, user_ids: Iterable[int]) -> None:
        values = [(int(uid),) for uid in user_ids]
        if not values:
            return
        with self.connect() as conn:
            cur = conn.cursor()
            if self.is_postgres:
                cur.executemany(
                    "INSERT INTO allowed_admins (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    values,
                )
            else:
                cur.executemany(
                    "INSERT OR IGNORE INTO allowed_admins (user_id) VALUES (?)",
                    values,
                )

    # ------------------------------------------------------------------ #
    # Educational Programs (OPs)
    # ------------------------------------------------------------------ #
    def seed_ops(self, programs: Mapping[str, Mapping[str, Any]]) -> None:
        if not programs:
            return
        with self.connect() as conn:
            cur = conn.cursor()
            for code, info in programs.items():
                code_upper = code.upper()
                name = str(info.get("name", code_upper))
                school = str(info.get("school", ""))
                admin = str(info.get("admin", "@admin"))
                aliases = list(info.get("aliases", []))
                chat_id = info.get("chat_id")
                chat_title = str(info.get("chat_title", ""))

                if self.is_postgres:
                    cur.execute(
                        """
                        INSERT INTO op_programs (code, name, school, admin, aliases, chat_id, chat_title)
                        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                        ON CONFLICT (code) DO NOTHING
                        """,
                        (
                            code_upper,
                            name,
                            school,
                            admin,
                            json.dumps(aliases, ensure_ascii=False),
                            chat_id,
                            chat_title,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO op_programs (code, name, school, admin, aliases, chat_id, chat_title)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            code_upper,
                            name,
                            school,
                            admin,
                            json.dumps(aliases, ensure_ascii=False),
                            chat_id,
                            chat_title,
                        ),
                    )

    def load_ops(self) -> dict[str, OPProgram]:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT code, name, school, admin, aliases, chat_id, chat_title FROM op_programs ORDER BY code"
            )
            rows = cur.fetchall()
            ops: dict[str, OPProgram] = {}
            for code, name, school, admin, aliases_raw, chat_id, chat_title in rows:
                if isinstance(aliases_raw, str):
                    try:
                        aliases = json.loads(aliases_raw)
                    except Exception:
                        aliases = []
                elif isinstance(aliases_raw, list):
                    aliases = aliases_raw
                else:
                    aliases = []

                code_upper = str(code).upper()
                ops[code_upper] = OPProgram(
                    code=code_upper,
                    name=str(name),
                    school=str(school),
                    admin=str(admin),
                    aliases=tuple(str(a) for a in aliases),
                    chat_id=int(chat_id) if chat_id else None,
                    chat_title=str(chat_title or ""),
                )
            return ops

    def save_op(self, op: OPProgram) -> None:
        with self.connect() as conn:
            cur = conn.cursor()
            aliases_json = json.dumps(list(op.aliases), ensure_ascii=False)
            now = datetime.now(timezone.utc).isoformat()
            if self.is_postgres:
                cur.execute(
                    """
                    INSERT INTO op_programs (code, name, school, admin, aliases, chat_id, chat_title, updated_at)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, NOW())
                    ON CONFLICT (code) DO UPDATE SET
                        name = EXCLUDED.name,
                        school = EXCLUDED.school,
                        admin = EXCLUDED.admin,
                        aliases = EXCLUDED.aliases,
                        chat_id = EXCLUDED.chat_id,
                        chat_title = EXCLUDED.chat_title,
                        updated_at = NOW()
                    """,
                    (
                        op.code.upper(),
                        op.name,
                        op.school,
                        op.admin,
                        aliases_json,
                        op.chat_id,
                        op.chat_title,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO op_programs (code, name, school, admin, aliases, chat_id, chat_title, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(code) DO UPDATE SET
                        name = excluded.name,
                        school = excluded.school,
                        admin = excluded.admin,
                        aliases = excluded.aliases,
                        chat_id = excluded.chat_id,
                        chat_title = excluded.chat_title,
                        updated_at = excluded.updated_at
                    """,
                    (
                        op.code.upper(),
                        op.name,
                        op.school,
                        op.admin,
                        aliases_json,
                        op.chat_id,
                        op.chat_title,
                        now,
                    ),
                )

    def set_op_admin(self, code: str, admin: str) -> bool:
        with self.connect() as conn:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute(
                    "UPDATE op_programs SET admin = %s, updated_at = NOW() WHERE code = %s RETURNING code",
                    (admin, code.upper()),
                )
                return cur.fetchone() is not None
            else:
                now = datetime.now(timezone.utc).isoformat()
                cur.execute(
                    "UPDATE op_programs SET admin = ?, updated_at = ? WHERE code = ?",
                    (admin, now, code.upper()),
                )
                return cur.rowcount > 0

    def set_op_chat(self, code: str, chat_id: int | None, chat_title: str = "") -> bool:
        with self.connect() as conn:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute(
                    "UPDATE op_programs SET chat_id = %s, chat_title = %s, updated_at = NOW() WHERE code = %s RETURNING code",
                    (chat_id, chat_title, code.upper()),
                )
                return cur.fetchone() is not None
            else:
                now = datetime.now(timezone.utc).isoformat()
                cur.execute(
                    "UPDATE op_programs SET chat_id = ?, chat_title = ?, updated_at = ? WHERE code = ?",
                    (chat_id, chat_title, now, code.upper()),
                )
                return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    # Pending Invites
    # ------------------------------------------------------------------ #
    def load_pending_invites(self) -> dict[str, PendingInvite]:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT invite_link, target_chat_id, user_id, op_code, expires_at, source_chat_id, source_message_id
                FROM pending_invites
                """
            )
            rows = cur.fetchall()
            invites: dict[str, PendingInvite] = {}
            for link, target_chat, uid, op, exp, src_chat, src_msg in rows:
                invites[str(link)] = PendingInvite(
                    invite_link=str(link),
                    target_chat_id=int(target_chat),
                    user_id=int(uid),
                    op_code=str(op),
                    expires_at=float(exp),
                    source_chat_id=int(src_chat) if src_chat is not None else None,
                    source_message_id=int(src_msg) if src_msg is not None else None,
                )
            return invites

    def save_pending_invite(self, invite: PendingInvite) -> None:
        with self.connect() as conn:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute(
                    """
                    INSERT INTO pending_invites (invite_link, target_chat_id, user_id, op_code, expires_at, source_chat_id, source_message_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (invite_link) DO UPDATE SET
                        source_chat_id = EXCLUDED.source_chat_id,
                        source_message_id = EXCLUDED.source_message_id
                    """,
                    (
                        invite.invite_link,
                        invite.target_chat_id,
                        invite.user_id,
                        invite.op_code,
                        invite.expires_at,
                        invite.source_chat_id,
                        invite.source_message_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO pending_invites (invite_link, target_chat_id, user_id, op_code, expires_at, source_chat_id, source_message_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(invite_link) DO UPDATE SET
                        source_chat_id = excluded.source_chat_id,
                        source_message_id = excluded.source_message_id
                    """,
                    (
                        invite.invite_link,
                        invite.target_chat_id,
                        invite.user_id,
                        invite.op_code,
                        invite.expires_at,
                        invite.source_chat_id,
                        invite.source_message_id,
                    ),
                )

    def delete_pending_invite(self, invite_link: str) -> None:
        with self.connect() as conn:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute("DELETE FROM pending_invites WHERE invite_link = %s", (invite_link,))
            else:
                cur.execute("DELETE FROM pending_invites WHERE invite_link = ?", (invite_link,))

    def delete_expired_invites(self, now: float | None = None) -> int:
        current_time = now if now is not None else time.time()
        with self.connect() as conn:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute("DELETE FROM pending_invites WHERE expires_at < %s", (current_time,))
                return cur.rowcount
            else:
                cur.execute("DELETE FROM pending_invites WHERE expires_at < ?", (current_time,))
                return cur.rowcount

    # ------------------------------------------------------------------ #
    # Audit Logging & Analytics
    # ------------------------------------------------------------------ #
    def log_onboarding_action(
        self, user_id: int, chat_id: int, action: str, details: str = ""
    ) -> None:
        try:
            with self.connect() as conn:
                cur = conn.cursor()
                if self.is_postgres:
                    cur.execute(
                        """
                        INSERT INTO onboarding_audit_log (user_id, chat_id, action, details)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (user_id, chat_id, action, details),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO onboarding_audit_log (user_id, chat_id, action, details)
                        VALUES (?, ?, ?, ?)
                        """,
                        (user_id, chat_id, action, details),
                    )
        except Exception as error:
            LOGGER.warning("Не удалось записать аудит онбординга: %s", error)

    def record_analytics_event(
        self, event_type: str, user_id: int | None, chat_id: int | None, payload: dict[str, Any] | None = None
    ) -> None:
        try:
            with self.connect() as conn:
                cur = conn.cursor()
                payload_json = json.dumps(payload or {}, ensure_ascii=False)
                if self.is_postgres:
                    cur.execute(
                        """
                        INSERT INTO analytics_events (event_type, user_id, chat_id, payload)
                        VALUES (%s, %s, %s, %s::jsonb)
                        """,
                        (event_type, user_id, chat_id, payload_json),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO analytics_events (event_type, user_id, chat_id, payload)
                        VALUES (?, ?, ?, ?)
                        """,
                        (event_type, user_id, chat_id, payload_json),
                    )
        except Exception as error:
            LOGGER.warning("Не удалось записать событие аналитики: %s", error)


# Backward-compatibility alias
PostgresStorage = DatabaseStorage
