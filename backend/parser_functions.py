"""Safe parsing boundaries for structured model responses without logging input."""

import json
from typing import Generic, TypeVar, TypedDict


Value = TypeVar("Value")


class ParseResult(TypedDict, Generic[Value]):
    """Explicit success or failure with a typed value and input-free error."""

    ok: bool
    value: Value | None
    error: str | None


def try_parse_json(text: object) -> ParseResult[object]:
    """Parse JSON with optional surrounding code fences without exposing input."""
    try:
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "value": None, "error": "JSON text must be a nonempty string"}
        cleaned = text.strip()
        if cleaned.startswith("```") and cleaned.endswith("```"):
            cleaned = cleaned.removeprefix("```").removesuffix("```").strip()
            if cleaned.casefold().startswith("json"):
                cleaned = cleaned.removeprefix(cleaned[:4]).strip()
        if not cleaned:
            return {"ok": False, "value": None, "error": "JSON text is empty"}
        return {"ok": True, "value": json.loads(cleaned), "error": None}
    except Exception:
        return {"ok": False, "value": None, "error": "JSON text could not be parsed"}


def try_get_str(obj: object, key: str, default: str = "") -> ParseResult[str]:
    """Read a string or return the supplied default with an explicit failure."""
    try:
        if not isinstance(default, str):
            return {"ok": False, "value": None, "error": "String default is invalid"}
        value = obj.get(key) if isinstance(obj, dict) else None
        if not isinstance(value, str):
            return {"ok": False, "value": default, "error": "String field is missing or invalid"}
        return {"ok": True, "value": value, "error": None}
    except Exception:
        return {"ok": False, "value": None, "error": "String field could not be read"}


def try_get_list(obj: object, key: str, default: list[object] | None = None) -> ParseResult[list[object]]:
    """Read a list or return a fresh empty or supplied default with a failure."""
    try:
        fallback = [] if default is None else default
        if not isinstance(fallback, list):
            return {"ok": False, "value": None, "error": "List default is invalid"}
        value = obj.get(key) if isinstance(obj, dict) else None
        if not isinstance(value, list):
            return {"ok": False, "value": fallback, "error": "List field is missing or invalid"}
        return {"ok": True, "value": value, "error": None}
    except Exception:
        return {"ok": False, "value": None, "error": "List field could not be read"}


def try_get_int(obj: object, key: str, default: int = 0) -> ParseResult[int]:
    """Read an integer without coercion or accepting booleans as integers."""
    try:
        if type(default) is not int:
            return {"ok": False, "value": None, "error": "Integer default is invalid"}
        value = obj.get(key) if isinstance(obj, dict) else None
        if type(value) is not int:
            return {"ok": False, "value": default, "error": "Integer field is missing or invalid"}
        return {"ok": True, "value": value, "error": None}
    except Exception:
        return {"ok": False, "value": None, "error": "Integer field could not be read"}


def try_get_dict(obj: object, key: str, default: dict[str, object] | None = None) -> ParseResult[dict[str, object]]:
    """Read a dictionary or return a fresh empty or supplied default with failure."""
    try:
        fallback = {} if default is None else default
        if not isinstance(fallback, dict):
            return {"ok": False, "value": None, "error": "Dictionary default is invalid"}
        value = obj.get(key) if isinstance(obj, dict) else None
        if not isinstance(value, dict):
            return {"ok": False, "value": fallback, "error": "Dictionary field is missing or invalid"}
        return {"ok": True, "value": value, "error": None}
    except Exception:
        return {"ok": False, "value": None, "error": "Dictionary field could not be read"}


def safe_groq_content(response_json: object) -> ParseResult[str]:
    """Read nonempty first-choice message content from a Groq response envelope."""
    try:
        choices = try_get_list(response_json, "choices").get("value")
        if not isinstance(choices, list) or not choices:
            return {"ok": False, "value": None, "error": "Groq choices are missing or invalid"}
        first_choice = next(iter(choices), None)
        message = try_get_dict(first_choice, "message").get("value")
        content = try_get_str(message, "content").get("value")
        if not isinstance(content, str) or not content.strip():
            return {"ok": False, "value": None, "error": "Groq message content is missing or invalid"}
        return {"ok": True, "value": content.strip(), "error": None}
    except Exception:
        return {"ok": False, "value": None, "error": "Groq message content could not be read"}
