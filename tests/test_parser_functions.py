"""Offline tests for safe model-response parsing and typed getters."""

import unittest
from unittest.mock import patch

from backend import parser_functions as parser


class ParserTests(unittest.TestCase):
    """Exercise parsing boundaries using synthetic data only."""

    def test_json_and_fences(self) -> None:
        """Accept ordinary JSON and surrounding plain or JSON code fences."""
        for text in ('{"skill":"Python"}', '```json\n{"skill":"Python"}\n```',
                     '```\n{"skill":"Python"}\n```', ' ```JSON\n{"skill":"Python"}\n``` '):
            with self.subTest(text=text):
                self.assertEqual(parser.try_parse_json(text), {
                    "ok": True, "value": {"skill": "Python"}, "error": None,
                })

    def test_json_values(self) -> None:
        """Preserve valid JSON scalar, list, empty object and null values."""
        for text, expected in (('[]', []), ('{}', {}), ('null', None), ('0', 0),
                               ('false', False), ('"text"', 'text')):
            with self.subTest(text=text):
                result = parser.try_parse_json(text)
                self.assertIs(result.get("ok"), True)
                self.assertEqual(result.get("value"), expected)

    def test_invalid_json_is_private(self) -> None:
        """Reject empty, malformed and fenced non-JSON input without copying it."""
        for text in (None, 12, b'{}', '', ' ', '```json\n```', '```',
                     'SYNTHETIC_PRIVATE_PAYLOAD', '```json\n{}', '{} trailing'):
            result = parser.try_parse_json(text)
            self.assertIs(result.get("ok"), False)
            self.assertIsNone(result.get("value"))
            self.assertTrue(result.get("error"))
            self.assertNotIn("SYNTHETIC_PRIVATE_PAYLOAD", str(result))

    def test_getters_preserve_valid_empty_values(self) -> None:
        """Distinguish valid empty or zero fields from missing or wrong types."""
        for getter, value in ((parser.try_get_str, ''), (parser.try_get_list, []),
                              (parser.try_get_int, 0), (parser.try_get_dict, {})):
            self.assertEqual(getter({"field": value}, "field"), {
                "ok": True, "value": value, "error": None,
            })

    def test_getters_return_explicit_defaults(self) -> None:
        """Report invalid fields while retaining the caller's typed default."""
        for getter, default in ((parser.try_get_str, "unknown"), (parser.try_get_list, ["unknown"]),
                                (parser.try_get_int, -1), (parser.try_get_dict, {"unknown": True})):
            for obj in (None, [], {}, {"field": None}):
                result = getter(obj, "field", default)
                self.assertIs(result.get("ok"), False)
                self.assertEqual(result.get("value"), default)
                self.assertTrue(result.get("error"))

    def test_getters_reject_wrong_types(self) -> None:
        """Reject coercion, boolean integers and invalid default types."""
        for value in (True, False, 1.0, "1", [], {}):
            self.assertIs(parser.try_get_int({"field": value}, "field").get("ok"), False)
        for getter, value in ((parser.try_get_str, 1), (parser.try_get_list, {}),
                              (parser.try_get_dict, [])):
            self.assertIs(getter({"field": value}, "field").get("ok"), False)
        for getter in (parser.try_get_str, parser.try_get_list, parser.try_get_int, parser.try_get_dict):
            self.assertIs(getter({}, "field", 1.5).get("ok"), False)
            self.assertIs(getter({}, []).get("ok"), False)

    def test_collection_defaults_are_fresh(self) -> None:
        """Avoid sharing mutable default lists or dictionaries across calls."""
        for getter in (parser.try_get_list, parser.try_get_dict):
            self.assertIsNot(getter({}, "field").get("value"), getter({}, "field").get("value"))

    def test_groq_content(self) -> None:
        """Read only the first choice and strip surrounding content whitespace."""
        self.assertEqual(parser.safe_groq_content({"choices": [
            {"message": {"content": '  {"skills": []}\n'}},
            {"message": {"content": "unused"}},
        ]}), {"ok": True, "value": '{"skills": []}', "error": None})

    def test_invalid_groq_envelopes(self) -> None:
        """Fail cleanly for missing, empty or wrongly typed envelope members."""
        for envelope in (None, [], {}, {"choices": None}, {"choices": {}},
                         {"choices": []}, {"choices": [None]}, {"choices": [{}]},
                         {"choices": [{"message": []}]},
                         {"choices": [{"message": {"content": None}}]},
                         {"choices": [{"message": {"content": 12}}]},
                         {"choices": [{"message": {"content": " "}}]},
                         {"choices": [{}, {"message": {"content": "ignore"}}]}):
            result = parser.safe_groq_content(envelope)
            self.assertIs(result.get("ok"), False)
            self.assertIsNone(result.get("value"))
            self.assertTrue(result.get("error"))

    def test_unexpected_parser_failure_is_private(self) -> None:
        """Convert parser exceptions into failure results without their messages."""
        with patch.object(parser.json, "loads", side_effect=RuntimeError("SYNTHETIC_PRIVATE_PAYLOAD")):
            result = parser.try_parse_json('{}')
        self.assertIs(result.get("ok"), False)
        self.assertNotIn("SYNTHETIC_PRIVATE_PAYLOAD", str(result))
