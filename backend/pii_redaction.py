"""Redact direct resume identifiers before sending text to external providers."""

import re


_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
_ADDRESS_LABEL = re.compile(r"(?i)\b(?:address|street address|postal address)\s*[:\-]")
_ADDRESS_DETAIL = re.compile(
    r"(?i)\b(?:street|st\.?|road|rd\.?|avenue|ave\.?|lane|ln\.?|"
    r"boulevard|blvd\.?|apartment|apt\.?|flat|house number|pincode|pin code|postal code|zip code)\b"
)
_NUMBERED_STREET = re.compile(
    r"(?i)^\s*\d{1,5}\s+[\w .'-]{1,50}\s+"
    r"(?:street|st\.?|road|rd\.?|avenue|ave\.?|lane|ln\.?|boulevard|blvd\.?)\b"
)


def redact_resume(text, name=None):
    """Redact a known account name and common direct identifiers, preserving lines."""
    if not isinstance(text, str):
        raise TypeError("Resume text must be a string")
    if name is not None and (not isinstance(name, str) or len(name) > 200):
        raise ValueError("Account name must be bounded text or null")

    redacted = []
    for line in text.splitlines():
        if _ADDRESS_LABEL.search(line) or _NUMBERED_STREET.search(line) or _ADDRESS_DETAIL.search(line):
            redacted.append("[ADDRESS REDACTED]")
            continue
        line = _EMAIL.sub("[EMAIL REDACTED]", line)
        line = _PHONE.sub(lambda match: "[PHONE REDACTED]"
                          if sum(character.isdigit() for character in match.group()) >= 10
                          else match.group(), line)
        if name and len(name.strip()) >= 2:
            name_pattern = re.compile(r"(?<!\w)" + re.escape(name.strip()) + r"(?!\w)", re.IGNORECASE)
            line = name_pattern.sub("[NAME REDACTED]", line)
        redacted.append(line)
    return "\n".join(redacted)
