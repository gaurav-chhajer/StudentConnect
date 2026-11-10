"""Signed, account-bound unsubscribe links for email digests."""

import hashlib
import hmac
import os
import re
from base64 import urlsafe_b64encode

from flask import current_app

from backend.hosting import site_origin


def _secret_key():
    try:
        key = current_app.secret_key
    except RuntimeError:
        key = os.environ.get("STUDENTCONNECT_SECRET_KEY")
    if isinstance(key, str):
        return key.encode("utf-8")
    return key if isinstance(key, bytes) and key else None


def _email_hash(email):
    if not isinstance(email, str) or not email:
        raise ValueError("A valid account email is required")
    return hashlib.sha256(email.strip().casefold().encode("utf-8")).hexdigest()


def token_for(user_id, email):
    if type(user_id) is not int or not 1 <= user_id <= 2147483647:
        raise ValueError("Invalid account identifier")
    key = _secret_key()
    if key is None:
        return None
    digest = _email_hash(email)
    payload = f"v1:{user_id}:{digest}"
    signature = urlsafe_b64encode(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest()).rstrip(b"=").decode("ascii")
    return f"{user_id}.{digest}.{signature}"


def parse_token(token):
    if not isinstance(token, str) or len(token) > 160:
        return None
    match = re.fullmatch(r"([1-9][0-9]{0,9})\.([0-9a-f]{64})\.([A-Za-z0-9_-]{43})", token)
    key = _secret_key()
    if match is None or key is None:
        return None
    user_id = int(match.group(1))
    if user_id > 2147483647:
        return None
    digest, supplied = match.group(2), match.group(3)
    payload = f"v1:{user_id}:{digest}"
    expected = urlsafe_b64encode(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest()).rstrip(b"=").decode("ascii")
    return (user_id, digest) if hmac.compare_digest(supplied, expected) else None


def matches_account(token, user_id, email):
    parsed = parse_token(token)
    return bool(parsed and parsed[0] == user_id and hmac.compare_digest(parsed[1], _email_hash(email)))


def unsubscribe_url(user):
    token = token_for(user["id"], user["email"])
    if token is None:
        return site_origin() + "/settings#email"
    return site_origin() + "/unsubscribe?token=" + token
