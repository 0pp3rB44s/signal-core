from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from app.logger import SensitiveDataFilter
from requests.exceptions import RequestException

from app.config import Settings
from clients.bitget_auth import build_headers
from clients.interprocess_rate_limiter import InterprocessRateLimiter


class BitgetAPIError(RuntimeError):
    pass


class BitgetRetryableError(BitgetAPIError):
    pass


class PrivateExchangeCallBlocked(BitgetAPIError):
    """Raised before transport when strict forward-paper mode sees a private call."""


# --- Order-submission outcome classification -----------------------------
# Order creation is NOT idempotent at the transport layer: a blind retry after a
# lost response can create a second real position. Requests made with
# allow_blind_retry=False are therefore never retried automatically; instead the
# outcome is classified so the caller can reconcile by clientOid.


class BitgetOrderSubmissionError(BitgetAPIError):
    """Base class for classified order-submission outcomes."""

    classification = "UNCLASSIFIED"

    def __init__(self, message: str, *, client_oid: str = "", status_code: int | None = None) -> None:
        super().__init__(message)
        self.client_oid = client_oid
        self.status_code = status_code


class BitgetOrderSubmissionAmbiguous(BitgetOrderSubmissionError):
    """The exchange may or may not have accepted the order. Reconcile, never retry blindly."""

    classification = "AMBIGUOUS"


class BitgetOrderNotSent(BitgetOrderSubmissionError):
    """The request provably never reached the exchange (no connection established)."""

    classification = "NOT_SENT"


class BitgetOrderRejected(BitgetOrderSubmissionError):
    """The exchange processed the request and refused it. No order was created."""

    classification = "REJECTED"


#: HTTP statuses that leave the fate of a submitted order unknown.
AMBIGUOUS_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


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

    def __init__(self, settings: Settings, timeout: int = 15) -> None:
        self.settings = settings
        self.timeout = timeout
        self.base_url = settings.bitget_base_url.rstrip("/")
        self.log = logging.getLogger(self.__class__.__name__)
        self.max_request_retries = int(getattr(settings, "bitget_max_request_retries", 3) or 3)
        self.retry_backoff_seconds = float(getattr(settings, "bitget_retry_backoff_seconds", 1.25) or 1.25)
        self.rate_limit_min_interval_seconds = float(getattr(settings, "bitget_rate_limit_min_interval_ms", 120) or 120) / 1000.0
        self.rate_limit_429_cooldown_seconds = float(getattr(settings, "bitget_rate_limit_429_cooldown_sec", 5.0) or 5.0)
        self.rate_limiter = InterprocessRateLimiter(
            getattr(settings, "bitget_rate_limit_state_path", "state/bitget_rate_limit.json"),
            self.rate_limit_min_interval_seconds,
        )

    @property
    def has_credentials(self) -> bool:
        if self.settings.forward_paper_only:
            return False
        return all(
            [
                self.settings.bitget_api_key.get_secret_value(),
                self.settings.bitget_api_secret.get_secret_value(),
                self.settings.bitget_api_passphrase.get_secret_value(),
            ]
        )

    def _rate_limit_wait(self) -> None:
        self.rate_limiter.wait()

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

    @staticmethod
    def _connection_never_established(exc: Exception) -> bool:
        """True only when the request provably never reached the exchange.

        A read timeout or a reset mid-response is NOT in this category: the
        exchange may already have accepted the order.
        """
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return True
        error_text = str(exc).lower()
        return any(
            marker in error_text
            for marker in (
                "failed to resolve",
                "nameresolutionerror",
                "temporary failure in name resolution",
                "nodename nor servname provided",
                "failed to establish a new connection",
                "connection refused",
            )
        )

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        private: bool = False,
        allow_blind_retry: bool = True,
        client_oid: str = "",
    ) -> dict[str, Any]:
        """Perform one Bitget REST call.

        ``allow_blind_retry=False`` marks the call as a non-idempotent
        order-creating request: it is attempted exactly once and every failure is
        classified (NOT_SENT / AMBIGUOUS / REJECTED) so the caller can reconcile
        by clientOid instead of POSTing again.
        """
        if private and self.settings.forward_paper_only:
            raise PrivateExchangeCallBlocked(
                "Private exchange call blocked: FORWARD_PAPER_ONLY is active"
            )
        url = f"{self.base_url}{path}"
        body_for_signing = None
        if body is not None:
            body_for_signing = json.dumps(body, separators=(",", ":"))

        headers: dict[str, str] = {}
        if private:
            if not self.has_credentials:
                raise BitgetAPIError("Missing Bitget API credentials for private request.")
            headers = build_headers(
                api_key=self.settings.bitget_api_key.get_secret_value(),
                api_secret=self.settings.bitget_api_secret.get_secret_value(),
                passphrase=self.settings.bitget_api_passphrase.get_secret_value(),
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
                    retryable_status = (
                        status_code in AMBIGUOUS_HTTP_STATUSES and allow_blind_retry
                    )
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

                    if not allow_blind_retry:
                        raise self._classify_http_failure(
                            method=method,
                            path=path,
                            status_code=status_code,
                            error_code=error_code,
                            safe_message=safe_message,
                            client_oid=client_oid,
                        ) from exc

                    raise BitgetAPIError(
                        f"Bitget HTTP error: status={status_code} code={error_code} msg={safe_message}"
                    ) from exc

                payload = response.json()
                code = str(payload.get("code", ""))

                if code not in {"00000", "0", "success"}:
                    retryable_code = (
                        code in {"429", "40015", "40010", "40725", "45001"} and allow_blind_retry
                    )
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

                    safe_business_message = SensitiveDataFilter.redact(
                        str(payload.get("msg") or "upstream error")
                    )[: self._MAX_ERROR_MESSAGE_LENGTH]

                    if not allow_blind_retry:
                        # The exchange answered on the business layer: it saw the
                        # request and refused it, so no order was created.
                        self.log.error(
                            "ORDER_SUBMISSION_CLASSIFIED | method=%s | path=%s | classification=REJECTED | code=%s | client_oid=%s",
                            method.upper(),
                            path,
                            code,
                            client_oid or "-",
                        )
                        raise BitgetOrderRejected(
                            f"Bitget rejected order: code={code} msg={safe_business_message}",
                            client_oid=client_oid,
                        )

                    raise BitgetAPIError(f"Bitget error: code={code} msg={safe_business_message}")

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
                retryable = attempt < self.max_request_retries and allow_blind_retry
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

                if not allow_blind_retry:
                    raise self._classify_transport_failure(
                        method=method,
                        path=path,
                        exc=exc,
                        client_oid=client_oid,
                    ) from exc

                raise BitgetRetryableError(
                    f"Bitget request failed after retries: {exc}"
                ) from exc

        if last_exception:
            raise BitgetRetryableError(str(last_exception)) from last_exception

        raise BitgetAPIError("Bitget request failed with unknown state")

    def _classify_http_failure(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        error_code: str,
        safe_message: str,
        client_oid: str,
    ) -> BitgetOrderSubmissionError:
        """Classify a non-2xx response to a non-idempotent order request."""
        if status_code in AMBIGUOUS_HTTP_STATUSES:
            # 408/5xx: the exchange may already hold the order. 429 is included
            # deliberately - reconciliation is cheap, a duplicate position is not.
            error: BitgetOrderSubmissionError = BitgetOrderSubmissionAmbiguous(
                f"Ambiguous order submission: status={status_code} code={error_code} msg={safe_message}",
                client_oid=client_oid,
                status_code=status_code,
            )
        else:
            error = BitgetOrderRejected(
                f"Order rejected by exchange: status={status_code} code={error_code} msg={safe_message}",
                client_oid=client_oid,
                status_code=status_code,
            )

        self.log.error(
            "ORDER_SUBMISSION_CLASSIFIED | method=%s | path=%s | classification=%s | status=%s | code=%s | client_oid=%s",
            method.upper(),
            path,
            error.classification,
            status_code,
            error_code,
            client_oid or "-",
        )
        return error

    def _classify_transport_failure(
        self,
        *,
        method: str,
        path: str,
        exc: Exception,
        client_oid: str,
    ) -> BitgetOrderSubmissionError:
        """Classify a transport failure on a non-idempotent order request."""
        if self._connection_never_established(exc):
            error: BitgetOrderSubmissionError = BitgetOrderNotSent(
                f"Order request never reached the exchange: {exc}",
                client_oid=client_oid,
            )
        else:
            # Read timeouts and mid-response failures are ambiguous by
            # definition: silence is not evidence that the order was not filled.
            error = BitgetOrderSubmissionAmbiguous(
                f"Ambiguous order submission (no usable response): {exc}",
                client_oid=client_oid,
            )

        self.log.critical(
            "ORDER_SUBMISSION_CLASSIFIED | method=%s | path=%s | classification=%s | client_oid=%s | error=%s",
            method.upper(),
            path,
            error.classification,
            client_oid or "-",
            exc,
        )
        return error
