"""Explicit hosted configuration; local commands keep using process variables."""

import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


def load_hosted_environment(path):
    """Load only the specified private file, preserving existing environment values."""
    path = Path(path)
    if not path.is_file():
        raise ValueError("Create the private hosted .env file before starting WSGI")
    if path.stat().st_size > 65536:
        raise ValueError("Hosted configuration exceeds 64 KB")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ValueError("Hosted .env must have private permissions: chmod 600 .env")
    if not load_dotenv(dotenv_path=path, override=False, interpolate=False):
        raise ValueError("Hosted configuration is empty or dotenv loading is disabled")


def site_origin():
    value = os.environ.get("STUDENTCONNECT_BASE_URL", "")
    if not value:
        return "http://127.0.0.1:5000"
    parsed = urlsplit(value)
    if (len(value) > 300 or value != value.strip() or any(ord(character) < 33 for character in value) or parsed.scheme != "https" or
            not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", parsed.netloc) or
            "." not in parsed.netloc or ".." in parsed.netloc or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("STUDENTCONNECT_BASE_URL must be an HTTPS origin without a path or port")
    return value.rstrip("/")


def application_settings():
    origin = site_origin()
    hosted = origin.startswith("https://")
    key = os.environ.get("STUDENTCONNECT_SECRET_KEY", "")
    if key and not re.fullmatch(r"[a-fA-F0-9]{64}", key):
        raise ValueError("STUDENTCONNECT_SECRET_KEY must contain 64 hexadecimal characters")
    if hosted and not key:
        raise ValueError("Hosted mode requires a persistent STUDENTCONNECT_SECRET_KEY")
    return origin, hosted, key or secrets.token_bytes(32)
