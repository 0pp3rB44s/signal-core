import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode


def timestamp_ms() -> str:
    return str(int(time.time() * 1000))


def compact_json(payload: dict[str, Any] | str | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if not payload:
        return ""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def build_query_string(params: dict[str, Any] | None) -> str:
    if not params:
        return ""
    clean = {k: v for k, v in params.items() if v is not None}
    return urlencode(clean)


def sign_message(secret: str, message: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_headers(
    api_key: str,
    api_secret: str,
    passphrase: str,
    method: str,
    request_path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | str | None = None,
    locale: str = "en-US",
) -> dict[str, str]:
    ts = timestamp_ms()
    query = build_query_string(params)
    body_str = compact_json(body)
    prehash = f"{ts}{method.upper()}{request_path}"
    if query:
        prehash += f"?{query}"
    prehash += body_str
    signature = sign_message(api_secret, prehash)

    return {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "locale": locale,
    }
