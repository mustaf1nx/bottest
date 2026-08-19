"""Unit tests for config, layout conversion, and helpers."""

import unittest
from config import parse_bool, parse_id_list, swap_keyboard_layout
from helpers import (
    build_op_chat_welcome_text,
    build_start_deep_link,
    format_admin_tag,
    is_inquiry_or_question,
    is_likely_op_declaration,
)
from models import OPProgram


class ConfigAndHelpersTests(unittest.TestCase):
    def test_parse_bool(self) -> None:
        self.assertTrue(parse_bool("true"))
        self.assertTrue(parse_bool("1"))
        self.assertTrue(parse_bool("yes"))
        self.assertTrue(parse_bool("ДА"))
        self.assertFalse(parse_bool("false"))
        self.assertFalse(parse_bool("0"))
        self.assertFalse(parse_bool("no"))

    def test_parse_id_list(self) -> None:
        self.assertEqual(parse_id_list("100, 200, 300"), {100, 200, 300})
        self.assertEqual(parse_id_list(""), set())
        with self.assertRaises(ValueError):
            parse_id_list("100, invalid_id")

    def test_swap_keyboard_layout(self) -> None:
        self.assertEqual(swap_keyboard_layout("ghbdtn"), "привет")
        self.assertEqual(swap_keyboard_layout("руддщ"), "hello")

    def test_format_admin_tag(self) -> None:
        self.assertEqual(format_admin_tag("alex"), "@alex")
        self.assertEqual(format_admin_tag("@alex"), "@alex")
        self.assertEqual(
            format_admin_tag("123456789"),
            '<a href="tg://user?id=123456789">Администратор</a>',
        )

    def test_build_start_deep_link(self) -> None:
        self.assertEqual(
            build_start_deep_link("my_cool_bot", "SE"),
            "https://t.me/my_cool_bot?start=join_SE",
        )

    def test_is_inquiry_or_question(self) -> None:
        self.assertTrue(is_inquiry_or_question("Кто тут на SE?"))
        self.assertTrue(is_inquiry_or_question("А есть кто с CS?"))
        self.assertTrue(is_inquiry_or_question("Что лучше: IT или BDA?"))
        self.assertFalse(is_inquiry_or_question("Я поступил на SE"))

    def test_is_likely_op_declaration(self) -> None:
        op_se = OPProgram(
            code="SE",
            name="Software Engineering",
            school="School of SE",
            admin="@alex",
            aliases=("SE", "сешник"),
        )
        self.assertTrue(is_likely_op_declaration("SE", [op_se]))
        self.assertTrue(is_likely_op_declaration("Я поступил на SE!", [op_se]))
        self.assertTrue(is_likely_op_declaration("сешник", [op_se]))


if __name__ == "__main__":
    unittest.main()
