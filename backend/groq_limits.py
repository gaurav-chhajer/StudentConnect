"""Allowlisted Groq response metadata; no credentials or response bodies."""
from datetime import datetime, timedelta, timezone
import re

COUNTS=('limit_requests','remaining_requests','limit_tokens','remaining_tokens')
RESETS=('reset_requests','reset_tokens')

def capture(headers):
    now=datetime.now(timezone.utc)
    result={}
    for field in COUNTS:
        value=headers.get('x-ratelimit-'+field.replace('_','-'))
        if isinstance(value,str) and re.fullmatch(r'[0-9]{1,12}',value):
            result[field]=int(value)
    for field in RESETS:
        value=headers.get('x-ratelimit-'+field.replace('_','-'))
        if not isinstance(value,str) or len(value)>80:
            continue
        parts=re.findall(r'([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)',value)
        if not parts or ''.join(n+u for n,u in parts)!=value:
            continue
        seconds=sum(float(n)*{'ms':.001,'s':1,'m':60,'h':3600,'d':86400}[u] for n,u in parts)
        if 0<=seconds<=366*86400:
            result[field]=(now+timedelta(seconds=seconds)).isoformat()
    return result
