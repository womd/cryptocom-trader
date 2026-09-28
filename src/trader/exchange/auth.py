"""Request signing for Crypto.com Exchange API v1 private methods.

sig = HMAC_SHA256(secret, method + id + api_key + param_string + nonce), hex-encoded,
where param_string concatenates keys in sorted order followed by their values
(lists/dicts flattened recursively, None rendered as "null").
"""

from __future__ import annotations

import hashlib
import hmac

MAX_LEVEL = 3


def params_to_str(obj, level: int = 0) -> str:
    if level >= MAX_LEVEL:
        return str(obj)
    if not isinstance(obj, dict):
        return str(obj)
    out = []
    for key in sorted(obj):
        out.append(key)
        value = obj[key]
        if value is None:
            out.append("null")
        elif isinstance(value, list):
            for item in value:
                out.append(params_to_str(item, level + 1) if isinstance(item, dict) else str(item))
        elif isinstance(value, dict):
            out.append(params_to_str(value, level + 1))
        else:
            out.append(str(value))
    return "".join(out)


def sign_request(request: dict, api_key: str, api_secret: str) -> dict:
    """Return a copy of `request` (with id, method, nonce, params) plus api_key and sig."""
    req = dict(request)
    req["api_key"] = api_key
    param_str = params_to_str(req.get("params") or {})
    payload = f"{req['method']}{req['id']}{api_key}{param_str}{req['nonce']}"
    req["sig"] = hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return req
