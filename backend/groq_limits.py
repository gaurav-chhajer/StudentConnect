"""Allowlisted Groq response metadata; no credentials or response bodies."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
import re
import time

import requests

COUNTS=('limit_requests','remaining_requests','limit_tokens','remaining_tokens')
RESETS=('reset_requests','reset_tokens')
REJECTION_COUNTS = ('rejected_limit', 'rejected_used', 'rejected_requested')
TIMES = RESETS + ('retry_at', 'rejected_at')
ENUMS = {
    'limited_dimension': frozenset(prefix + kind + '_per_' + period
        for prefix, kind in (('', 'tokens'), ('input_', 'tokens'), ('output_', 'tokens'), ('', 'requests'))
        for period in ('minute', 'day')),
    'request_fit': frozenset(('oversized', 'wait_for_reset', 'unknown')),
    'rejection_detail': frozenset(('parsed', 'unavailable')),
}
REJECTION_FIELDS = REJECTION_COUNTS + ('limited_dimension', 'request_fit', 'rejection_detail',
                                       'rejected_at', 'retry_at')
MAX_DELAY_SECONDS = 366 * 86400
MAX_ERROR_BYTES = 32768
KEY_SLOT_COUNT = 7
logger = logging.getLogger(__name__)


def validate_metrics(metrics):
    if (not isinstance(metrics, dict) or not metrics or
            set(metrics) - set(COUNTS + REJECTION_COUNTS + TIMES + tuple(ENUMS))):
        raise ValueError('Invalid limit metrics')
    for field, value in metrics.items():
        if field in COUNTS:
            if type(value) is not int or not 0 <= value < 10**12:
                raise ValueError('Invalid limit count')
        elif field in REJECTION_COUNTS:
            if type(value) not in (int, float) or not 0 <= value < 10**12 or not math.isfinite(value):
                raise ValueError('Invalid rejection count')
        elif field in ENUMS:
            if not isinstance(value, str) or value not in ENUMS[field]:
                raise ValueError('Invalid limit classification')
        else:
            if not isinstance(value, str) or len(value) > 40:
                raise ValueError('Invalid limit time')
            if datetime.fromisoformat(value).tzinfo is None:
                raise ValueError('Limit time requires timezone')


def _duration(value):
    if not isinstance(value, str) or len(value) > 80:
        return None
    parts = re.findall(r'([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)', value)
    if not parts or ''.join(number + unit for number, unit in parts) != value:
        return None
    seconds = sum(float(number) * {'ms': .001, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit]
                  for number, unit in parts)
    return seconds if 0 <= seconds <= MAX_DELAY_SECONDS else None

def capture(headers):
    now=datetime.now(timezone.utc)
    result={}
    for field in COUNTS:
        value=headers.get('x-ratelimit-'+field.replace('_','-'))
        if isinstance(value,str) and re.fullmatch(r'[0-9]{1,12}',value):
            result[field]=int(value)
    for field in RESETS:
        value=headers.get('x-ratelimit-'+field.replace('_','-'))
        seconds = _duration(value)
        if seconds is not None:
            result[field]=(now+timedelta(seconds=seconds)).isoformat()
    retry = headers.get('retry-after')
    if isinstance(retry, str) and len(retry) <= 80:
        if re.fullmatch(r'[0-9]{1,8}(?:\.[0-9]{1,6})?', retry):
            seconds = float(retry)
            if seconds <= MAX_DELAY_SECONDS:
                result['retry_at'] = (now + timedelta(seconds=seconds)).isoformat()
        else:
            try:
                target = parsedate_to_datetime(retry)
            except (ValueError, TypeError, OverflowError):
                target = None  # Malformed optional provider metadata is not a retry instruction.
            if target is not None and target.tzinfo is not None and 0 <= (target - now).total_seconds() <= MAX_DELAY_SECONDS:
                result['retry_at'] = target.astimezone(timezone.utc).isoformat()
    return result


def rejection(response):
    """Extract bounded numeric/enum diagnostics; never persist the provider message."""
    now = datetime.now(timezone.utc)
    metrics = dict(capture(response.headers), rejected_at=now.isoformat(),
                   request_fit='unknown', rejection_detail='unavailable')
    try:
        raw = response.content
    except (requests.RequestException, RuntimeError) as error:
        logger.warning('Groq rejection body unavailable: error=%s', type(error).__name__)
        return metrics
    if not isinstance(raw, bytes) or len(raw) > MAX_ERROR_BYTES:
        return metrics
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        return metrics
    detail = payload.get('error') if isinstance(payload, dict) else None
    message = detail.get('message') if isinstance(detail, dict) else None
    if not isinstance(message, str):
        return metrics
    metrics['rejection_detail'] = 'parsed'
    dimension = re.search(r'\b(?:(input|output)\s+)?(tokens|requests) per (minute|day)\b', message, re.I)
    if dimension:
        field = '_'.join(part.casefold() for part in dimension.groups() if part)
        field = field.rsplit('_', 1)[0] + '_per_' + dimension[3].casefold()
        if field in ENUMS['limited_dimension']:
            metrics['limited_dimension'] = field
    for name in ('limit', 'used', 'requested'):
        found = re.search(r'\b' + name + r'\s*:?\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)', message, re.I)
        if found:
            text = found[1].replace(',', '')
            if len(text) > 24:
                continue
            number = float(text) if '.' in text else int(text)
            if math.isfinite(number) and 0 <= number < 10**12:
                metrics['rejected_' + name] = number
    if 'rejected_requested' in metrics and 'rejected_limit' in metrics:
        metrics['request_fit'] = ('oversized' if metrics['rejected_requested'] > metrics['rejected_limit']
                                  else 'wait_for_reset')
    if 'retry_at' not in metrics:
        hint = re.search(r'try again in\s+((?:[0-9]+(?:\.[0-9]+)?(?:ms|s|m|h|d))+)', message, re.I)
        seconds = _duration(hint[1].casefold()) if hint else None
        if seconds is not None:
            metrics['retry_at'] = (now + timedelta(seconds=seconds)).isoformat()
    return metrics


class CooldownPending(RuntimeError):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__('Configured Groq keys are cooling down')


class QuotaState:
    """Consumer-owned monotonic deadlines, shared across batches/reconnections."""
    def __init__(self):
        self.until = [0.0] * KEY_SLOT_COUNT

    def record(self, slot, metrics):
        validate_metrics(metrics)
        if type(slot) is not int or not 0 <= slot < KEY_SLOT_COUNT:
            raise ValueError('Invalid quota slot')
        if 'retry_at' in metrics:
            delay = (datetime.fromisoformat(metrics['retry_at']) - datetime.now(timezone.utc)).total_seconds()
            if not -MAX_DELAY_SECONDS <= delay <= MAX_DELAY_SECONDS:
                raise ValueError('Provider cooldown exceeds supported duration')
            self.until[slot] = max(self.until[slot], time.monotonic() + max(0, delay))

    def delay(self, slot):
        return max(0.0, self.until[slot] - time.monotonic())
