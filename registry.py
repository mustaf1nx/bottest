"""Registries for Allowed Administrators and Educational Programs (OPs)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from config import DEFAULT_OPS, swap_keyboard_layout
from database import DatabaseStorage
from models import OPProgram

LOGGER = logging.getLogger(__name__)


class AdminRegistry:
    """Persistent allow-list for people who may use the bot in private."""

    def __init__(
        self,
        path: Path | None = None,
        initial_ids: frozenset[int] = frozenset(),
        db: DatabaseStorage | None = None,
    ) -> None:
        self.path = path
        self.db = db
        self._ids: set[int] = set(initial_ids)

        if self.db is not None:
            db_ids = self.db.load_admin_ids()
            self._ids.update(db_ids)
            if self._ids:
                self.db.add_admins(self._ids)

        if self.path is not None and self.path.exists():
            try:
                stored = json.loads(self.path.read_text(encoding="utf-8"))
                self._ids.update(int(user_id) for user_id in stored)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(f"Не удалось прочитать список админов {self.path}") from error

        if self.path is not None and not self.path.exists() and self._ids:
            self._save_file()

    def _save_file(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(sorted(self._ids), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def contains(self, user_id: int) -> bool:
        return user_id in self._ids

    def add(self, user_id: int) -> bool:
        if user_id in self._ids:
            return False
        self._ids.add(user_id)
        if self.db is not None:
            self.db.add_admin(user_id)
        self._save_file()
        return True


class OPRegistry:
    """Registry for Educational Programs (OPs) and their assigned administrators."""

    def __init__(
        self,
        path: Path | None = None,
        db: DatabaseStorage | None = None,
    ) -> None:
        self.path = path
        self.db = db
        self._ops: dict[str, OPProgram] = {}
        self._load()

    def _load(self) -> None:
        if self.db is not None:
            loaded_db = self.db.load_ops()
            if loaded_db:
                self._ops = loaded_db
                return
            # Seed DB with default ops if empty
            self.db.seed_ops(DEFAULT_OPS)
            self._ops = self.db.load_ops()
            return

        if self.path is not None and self.path.exists():
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
                LOGGER.warning(
                    "Не удалось прочитать %s: %s. Используем значения по умолчанию.",
                    self.path,
                    error,
                )

        for code, info in DEFAULT_OPS.items():
            self._ops[code] = OPProgram(
                code=code,
                name=info["name"],
                school=info["school"],
                admin=info["admin"],
                aliases=tuple(info.get("aliases", [])),
                chat_id=info.get("chat_id"),
                chat_title=info.get("chat_title", ""),
            )
        self.save()

    def save(self) -> None:
        if self.db is not None:
            for op in self._ops.values():
                self.db.save_op(op)

        if self.path is not None:
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
        code_upper = code.upper()
        if code_upper not in self._ops:
            return False
        self._ops[code_upper] = replace(self._ops[code_upper], admin=admin)
        if self.db is not None:
            self.db.set_op_admin(code_upper, admin)
        self.save()
        return True

    def set_chat(
        self, code: str, chat_id: int | None, chat_title: str = ""
    ) -> bool:
        """Привязать (или отвязать при chat_id=None) чат ОП."""
        code_upper = code.upper()
        if code_upper not in self._ops:
            return False
        self._ops[code_upper] = replace(
            self._ops[code_upper], chat_id=chat_id, chat_title=chat_title
        )
        if self.db is not None:
            self.db.set_op_chat(code_upper, chat_id, chat_title)
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
