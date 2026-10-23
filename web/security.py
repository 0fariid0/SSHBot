import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def telegram_user(data: str, bot_token: str, now=None) -> int:
    """Telegram Mini App HMAC. Reject duplicates, stale/future assertions."""
    pairs = parse_qsl(data, keep_blank_values=True, strict_parsing=True)
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError('duplicate fields')
    signature = fields.pop('hash', '')
    check = '\n'.join(f'{k}={v}' for k, v in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError('invalid signature')
    age = (time.time() if now is None else now) - int(fields['auth_date'])
    if not -30 <= age <= 300:
        raise ValueError('expired assertion')
    uid = json.loads(fields['user'])['id']
    if type(uid) is not int or uid <= 0:
        raise ValueError('invalid user')
    return uid


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()
