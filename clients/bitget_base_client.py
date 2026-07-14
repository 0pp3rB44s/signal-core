from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from app.logger import SensitiveDataFilter
from requests.exceptions import RequestException

from app.config import Settings
from clients.bitget_auth import build_headers


class BitgetAPIError(RuntimeError):
    pass


class BitgetRetryableError(BitgetAPIError):
    pass


class BitgetBaseClient:
    """Base Bitget REST layer: auth, request, retry, rate-limit and validation."""

    _MAX_ERROR_MESSAGE_LENGTH = 300

    @classmethod
    def _safe_response_error(cls, response: requests.Response, *, private: bool) -> tuple[str, str]:
        """Return only a bounded, redacted exchange code/message pair."""
        code = "unknown"
        message = "upstream error response"
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None

        if isinstance(payload, dict):
            code = str(payload.get("code") or code)
            message = str(payload.get("msg") or payload.get("message") or message)
        elif not private:
            # Public endpoints may return useful plain-text errors. Private
            # response bodies are deliberately never copied into logs/errors.
            message = str(response.text or message)

        message = SensitiveDataFilter.redact(message).replace("\r", " ").replace("\n", " ")
        return code[:64], message[: cls._MAX_ERROR_MESSAGE_LENGTH]

    _global_rate_limit_lock = threading.Lock()
    _global_last_request_ts = 0.0

    def __init__(self, settings: Settings, timeout: int = 15) -> None:
        self.settings = settings
        self.timeout = timeout
        self.base_url = settings.bitget_base_url.rstrip("/")
        self.log = logging.getLogger(self.__class__.__name__)
        self.max_request_retries = int(getattr(settings, "bitget_max_request_retries", 3) or 3)
        self.retry_backoff_seconds = float(getattr(settings, "bitget_retry_backoff_seconds", 1.25) or 1.25)
        self.rate_limit_min_interval_seconds = float(getattr(settings, "bitget_rate_limit_min_interval_ms", 120) or 120) / 1000.0
        self.rate_limit_429_cooldown_seconds = float(getattr(settings, "bitget_rate_limit_429_cooldown_sec", 5.0) or 5.0)

    @property
    def has_credentials(self) -> bool:
        return all(
            [
                self.settings.bitget_api_key,
                self.settings.bitget_api_secret,
                self.settings.bitget_api_passphrase,
            ]
        )

    def _rate_limit_wait(self) -> None:
        if self.rate_limit_min_interval_seconds <= 0:
            return

        with self._global_rate_limit_lock:
            now = time.perf_counter()
            elapsed = now - type(self)._global_last_request_ts
            sleep_seconds = self.rate_limit_min_interval_seconds - elapsed

            if sleep_seconds > 0:
                self.log.info(
                    "BITGET_RATE_LIMIT_WAIT | sleep=%ss | min_interval=%ss",
                    round(sleep_seconds, 4),
                    self.rate_limit_min_interval_seconds,
                )
                time.sleep(sleep_seconds)

            type(self)._global_last_request_ts = time.perf_counter()

    @staticmethod
    def _validate_futures_order_flags(body: dict[str, Any]) -> None:
        reduce_only = body.get("reduceOnly")
        if reduce_only is not None and str(reduce_only).lower() not in {"yes", "no", "true", "false"}:
            raise BitgetAPIError(f"Invalid reduceOnly value: {reduce_only}")

        trade_side = body.get("tradeSide")
        if trade_side is not None and str(trade_side).lower() not in {"open", "close"}:
            raise BitgetAPIError(f"Invalid tradeSide value: {trade_side}")

        side = body.get("side")
        if side is not None and str(side).lower() not in {"buy", "sell"}:
            raise BitgetAPIError(f"Invalid side value: {side}")

        hold_side = body.get("holdSide")
        if hold_side is not None and str(hold_side).lower() not in {"long", "short"}:
            raise BitgetAPIError(f"Invalid holdSide value: {hold_side}")

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        private: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        body_for_signing = None
        if body is not None:
            body_for_signing = json.dumps(body, separators=(",", ":"))

        headers: dict[str, str] = {}
        if private:
            if not self.has_credentials:
                raise BitgetAPIError("Missing Bitget API credentials for private request.")
            headers = build_headers(
                api_key=self.settings.bitget_api_key,
                api_secret=self.settings.bitget_api_secret,
                passphrase=self.settings.bitget_api_passphrase,
                method=method,
                request_path=path,
                params=params,
                body=body_for_signing,
                locale=self.settings.bitget_locale,
            )

        request_kwargs: dict[str, Any] = {
            "method": method.upper(),
            "url": url,
            "params": params,
            "headers": headers,
            "timeout": self.timeout,
        }
        if body_for_signing is not None:
            request_kwargs["data"] = body_for_signing
            headers["Content-Type"] = "application/json"

        self._rate_limit_wait()
        last_exception: Exception | None = None

        for attempt in range(1, self.max_request_retries + 1):
            started_at = time.perf_counter()

            try:
                response = requests.request(**request_kwargs)
                latency_ms = round((time.perf_counter() - started_at) * 1000, 2)

                self.log.info(
                    "BITGET_API_LATENCY | method=%s | path=%s | status=%s | latency_ms=%s",
                    method.upper(),
                    path,
                    response.status_code,
                    latency_ms,
                )

                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    status_code = response.status_code
                    error_code, safe_message = self._safe_response_error(response, private=private)
                    retryable_status = status_code in {408, 429, 500, 502, 503, 504}
                    log_method = self.log.warning if retryable_status else self.log.error
                    log_method(
                        "BITGET_HTTP_ERROR | method=%s | path=%s | status=%s | code=%s | retryable=%s | attempt=%s | msg=%s",
                        method.upper(),
                        path,
                        status_code,
                        error_code,
                        retryable_status,
                        attempt,
                        safe_message,
                    )

                    if retryable_status and attempt < self.max_request_retries:
                        sleep_seconds = self.rate_limit_429_cooldown_seconds if status_code == 429 else self.retry_backoff_seconds * attempt
                        self.log.warning(
                            "BITGET_RETRY_BACKOFF | method=%s | path=%s | sleep=%ss | attempt=%s",
                            method.upper(),
                            path,
                            sleep_seconds,
                            attempt,
                        )
                        time.sleep(sleep_seconds)
                        continue

                    raise BitgetAPIError(
                        f"Bitget HTTP error: status={status_code} code={error_code} msg={safe_message}"
                    ) from exc

                payload = response.json()
                code = str(payload.get("code", ""))

                if code not in {"00000", "0", "success"}:
                    retryable_code = code in {"429", "40015", "40010", "40725", "45001"}
                    log_method = self.log.warning if retryable_code else self.log.error
                    log_method(
                        "BITGET_API_ERROR | method=%s | path=%s | code=%s | retryable=%s | attempt=%s | msg=%s",
                        method.upper(),
                        path,
                        code,
                        retryable_code,
                        attempt,
                        payload.get("msg"),
                    )

                    if retryable_code and attempt < self.max_request_retries:
                        sleep_seconds = self.rate_limit_429_cooldown_seconds if code == "429" else self.retry_backoff_seconds * attempt
                        self.log.warning(
                            "BITGET_API_RETRY | method=%s | path=%s | code=%s | sleep=%ss | attempt=%s",
                            method.upper(),
                            path,
                            code,
                            sleep_seconds,
                            attempt,
                        )
                        time.sleep(sleep_seconds)
                        continue

                    raise BitgetAPIError(
                        "Bitget error: "
                        f"code={code} msg={SensitiveDataFilter.redact(str(payload.get('msg') or 'upstream error'))[:self._MAX_ERROR_MESSAGE_LENGTH]}"
                    )

                return payload

            except (RequestsTimeout, RequestsConnectionError, RequestException) as exc:
                last_exception = exc
                error_text = str(exc).lower()
                network_resolution_error = (
                    "failed to resolve" in error_text
                    or "nameresolutionerror" in error_text
                    or "temporary failure in name resolution" in error_text
                    or "nodename nor servname provided" in error_text
                )
                if network_resolution_error:
                    self.log.error(
                        "BITGET_DNS_RESOLUTION_FAILURE | method=%s | path=%s | attempt=%s/%s | error=%s",
                        method.upper(),
                        path,
                        attempt,
                        self.max_request_retries,
                        exc,
                    )
                retryable = attempt < self.max_request_retries
                self.log.warning(
                    "BITGET_REQUEST_EXCEPTION | method=%s | path=%s | attempt=%s | retryable=%s | error=%s",
                    method.upper(),
                    path,
                    attempt,
                    retryable,
                    exc,
                )

                if retryable:
                    sleep_seconds = self.retry_backoff_seconds * attempt
                    self.log.warning(
                        "BITGET_NETWORK_RETRY | method=%s | path=%s | sleep=%ss | attempt=%s",
                        method.upper(),
                        path,
                        sleep_seconds,
                        attempt,
                    )
                    time.sleep(sleep_seconds)
                    continue

                raise BitgetRetryableError(
                    f"Bitget request failed after retries: {exc}"
                ) from exc

        if last_exception:
            raise BitgetRetryableError(str(last_exception)) from last_exception

        raise BitgetAPIError("Bitget request failed with unknown state")
