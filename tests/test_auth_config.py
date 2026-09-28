import hashlib
import hmac

import pytest

from trader.config import Settings, apply_set
from trader.exchange.auth import params_to_str, sign_request


def test_params_to_str_sorts_and_flattens():
    params = {"b": 2, "a": [1, {"y": "q", "x": None}], "c": {"k": "v"}}
    assert params_to_str(params) == "a1xnullyqb2ckv"


def test_sign_request_matches_manual_hmac():
    req = {"id": 7, "method": "private/user-balance", "params": {}, "nonce": 1000}
    signed = sign_request(req, "KEY", "SECRET")
    payload = "private/user-balance7KEY1000"
    expected = hmac.new(b"SECRET", payload.encode(), hashlib.sha256).hexdigest()
    assert signed["sig"] == expected
    assert signed["api_key"] == "KEY"
    assert "sig" not in req  # original untouched


def test_apply_set_nested_and_json_values():
    data: dict = {}
    apply_set(data, "strategy.entry_zone=0.1")
    apply_set(data, "strategy.trend_filter=false")
    apply_set(data, "environment=uat")
    assert data == {"strategy": {"entry_zone": 0.1, "trend_filter": False}, "environment": "uat"}
    s = Settings(_env_file=None, **data)
    assert s.strategy.entry_zone == 0.1 and not s.strategy.trend_filter
    assert s.rest_url.startswith("https://uat-api")


def test_settings_reject_mixed_quote_currencies():
    with pytest.raises(ValueError, match="quoted in USD"):
        Settings(_env_file=None, symbols=["BTC_USD", "ETH_BTC"])
