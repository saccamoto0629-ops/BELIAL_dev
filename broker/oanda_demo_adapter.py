r"""OANDA Demo Adapter — the DEMO-ONLY Broker implementation (Issue #79).

*** DEMO ONLY. THE LIVE ENDPOINT IS REFUSED AT CONSTRUCTION (FAIL-CLOSED). ***

This adapter translates between the broker-agnostic common models
(``broker_models``) and the OANDA v20 REST API for a PRACTICE (demo)
account. It is a pure translation layer:

    ExecutionRequest --(execution layer)--> OrderRequest
        -> OANDA v20 HTTP request
        -> OANDA v20 HTTP response
        -> common broker models (OrderResult / PositionInfo / AccountState)

Responsibility boundary (Issue #79 / Issues #73–#78):

- NO strategy logic, NO execution decisions, NO Safety Gate checks, NO
  idempotency / duplicate-suppression logic, NO shadow-mode awareness.
  Those live in the existing execution layers; this module only converts.
- OANDA-specific wire fields never leave this module: every public method
  speaks ``broker_models`` types and typed ``BrokerError`` exceptions only.
- ``client_order_id`` (bound 1:1 to the ``execution_request_id`` by the
  execution store, Issue #74) is sent to OANDA as the client order
  extension (``clientExtensions.id``) and is the key for ``get_order`` /
  ``cancel_order`` (OANDA's ``@<client id>`` order specifier), so broker-side
  records stay correlatable with the originating Execution Request.
- NO automatic resend anywhere: a timeout raises ``BrokerTimeoutError``
  after exactly one attempt and the outcome stays UNKNOWN until a later
  query resolves it (Issue #74 contract).

Secrets (Issue #79 / PR #101 review):

- API token / account id / endpoint come from environment variables only
  (``OandaDemoConfig.from_env``). The adapter performs NO logging, its
  exception messages are built from static text plus short broker-supplied
  reason strings — with the token / account id values SCRUBBED out of any
  broker-echoed text — and transport-level exceptions are re-raised
  ``from None`` so URL/header-bearing library errors never leak into
  tracebacks. ``OandaDemoConfig`` excludes token, account id AND endpoint
  from ``repr()``. Nothing secret crosses the boundary either:
  ``connection_identity().target`` is the constant label ``"oanda-demo"``
  (never the URL) and ``AccountState.account_id`` carries a MASKED account
  identifier (the response id is verified against the configured one, then
  masked before it leaves the adapter).
- Redirects are never followed (``allow_redirects=False``): a 3xx answer
  cannot pull the connection off the demo endpoint and is converted to a
  common error instead (fail-closed).

Error translation (everything crossing the boundary is a common type):

- transport timeout            -> ``BrokerTimeoutError`` (outcome UNKNOWN)
- connection failure           -> ``BrokerCommunicationError``
- HTTP 401 / 403               -> ``BrokerAuthenticationError``
- HTTP 429                     -> ``BrokerRateLimitError``
- HTTP 5xx on a submit         -> ``BrokerTimeoutError`` (the server saw the
  request, so the outcome is unknown — never "not placed")
- HTTP 5xx on a read           -> ``BrokerCommunicationError``
- OANDA order rejection        -> returned ``OrderResult`` with state
  ``REJECTED`` + ``reject_reason`` (a definitive business answer, not an
  exception — Issue #73 error style)
- malformed / unsupported call -> ``BrokerInvalidRequestError``
- unknown order specifier      -> ``BrokerOrderNotFoundError``
- refused cancellation         -> ``BrokerCancellationError``
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping

from .broker_models import (
    AccountState,
    BrokerAuthenticationError,
    BrokerCancellationError,
    BrokerCommunicationError,
    BrokerConnectionIdentity,
    BrokerError,
    BrokerInvalidRequestError,
    BrokerOrderNotFoundError,
    BrokerRateLimitError,
    BrokerTimeoutError,
    Fill,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
    PositionInfo,
    PositionSide,
    TradeInfo,
    weighted_average_price,
)

# The ONLY endpoint this adapter will ever talk to. Anything else — the live
# host most of all — is refused at construction time (fail-closed allowlist,
# Issue #79).
OANDA_DEMO_API_URL = "https://api-fxpractice.oanda.com"
_OANDA_LIVE_HOST_MARKER = "fxtrade"

# The connection-identity target label (Issue #75 contract). A constant,
# NON-SECRET label — never the endpoint URL — so nothing configured via
# environment variables crosses into the execution layer (PR #101 review).
OANDA_DEMO_CONNECTION_TARGET = "oanda-demo"

# client_order_id charset accepted by this adapter (PR #101 review): the id
# is embedded in URL paths as the "@<id>" order specifier, so it is
# restricted to unreserved URL characters up to 128 chars. The execution
# layer generates ids well inside this alphabet.
_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# OANDA's definitive duplicate-id rejection. The Broker boundary contract
# (Issue #73 / FakeBroker) models a duplicate client_order_id as
# ``BrokerInvalidRequestError``, not as an ordinary REJECTED result.
_OANDA_DUPLICATE_ID_REASON = "CLIENT_ORDER_ID_ALREADY_EXISTS"

# Environment variable names (values are read from the process environment
# only; never hard-code or log the values — docs/operations/operations.md).
ENV_API_TOKEN = "OANDA_API_TOKEN"
ENV_ACCOUNT_ID = "OANDA_ACCOUNT_ID"
ENV_API_URL = "OANDA_API_URL"
ENV_HTTP_TIMEOUT_SECONDS = "OANDA_HTTP_TIMEOUT_SECONDS"

_DEFAULT_TIMEOUT_SECONDS = 10.0


# --- transport boundary -------------------------------------------------------
#
# The HTTP transport is injectable so unit tests exercise the full translation
# without any network. A transport is a callable:
#
#     transport(method, url, headers, body_text_or_None, timeout_seconds)
#         -> (status_code, response_text)
#
# and may raise the Transport* exceptions below. Transport error messages must
# be STATIC (no URL, no headers, no request body): the URL contains the
# account id and the headers contain the token.


class TransportError(Exception):
    """The HTTP exchange failed in an unclassified way."""


class TransportTimeoutError(TransportError):
    """The HTTP exchange timed out (the request may have been processed)."""


class TransportConnectionError(TransportError):
    """The connection could not be established (request never sent)."""


def _requests_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: str | None,
    timeout_seconds: float,
) -> tuple[int, str]:
    """Default transport: one plain ``requests`` call, no retries.

    Library exceptions are converted to Transport* types with static
    messages and ``from None`` — ``requests`` errors embed the URL (which
    contains the account id) and must never surface in a traceback.
    """
    import requests

    try:
        response = requests.request(
            method,
            url,
            headers=dict(headers),
            data=body.encode("utf-8") if body is not None else None,
            timeout=timeout_seconds,
            # Fail-closed (PR #101 review): a redirect must never pull the
            # connection off the demo endpoint. 3xx answers are returned
            # as-is and converted to a common error by the adapter.
            allow_redirects=False,
        )
    except requests.exceptions.Timeout:
        raise TransportTimeoutError("HTTP request to the OANDA demo endpoint timed out") from None
    except requests.exceptions.ConnectionError:
        raise TransportConnectionError(
            "connection to the OANDA demo endpoint could not be established"
        ) from None
    except requests.exceptions.RequestException:
        raise TransportError("HTTP exchange with the OANDA demo endpoint failed") from None
    return response.status_code, response.text


# --- configuration ------------------------------------------------------------


def _require_non_empty_env(value, env_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        # Deliberately names the VARIABLE, never echoes a value.
        raise ValueError(f"environment variable {env_name} must be set and non-empty")
    return value.strip()


@dataclass(frozen=True)
class OandaDemoConfig:
    """Construction-time configuration for :class:`OandaDemoAdapter`.

    ``api_token`` and ``account_id`` are secrets: excluded from ``repr()``
    and never included in exception messages or logs. The endpoint is
    validated against the demo allowlist here, so a live endpoint can never
    reach the adapter at all (fail-closed at startup, Issue #79).
    """

    api_token: str = field(repr=False)
    account_id: str = field(repr=False)
    # The endpoint is environment-managed like the credentials (Issue #79);
    # it is excluded from repr() as well (PR #101 review).
    endpoint: str = field(default=OANDA_DEMO_API_URL, repr=False)
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.api_token, str) or not self.api_token.strip():
            raise ValueError(f"api_token must be a non-empty string (set {ENV_API_TOKEN})")
        if not isinstance(self.account_id, str) or not self.account_id.strip():
            raise ValueError(f"account_id must be a non-empty string (set {ENV_ACCOUNT_ID})")
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise ValueError(f"endpoint must be a non-empty string (set {ENV_API_URL})")
        normalized = self.endpoint.strip().rstrip("/")
        if _OANDA_LIVE_HOST_MARKER in normalized.lower():
            raise ValueError(
                "the configured OANDA endpoint is a LIVE endpoint; this adapter is "
                f"demo-only and refuses to start (allowed: {OANDA_DEMO_API_URL})"
            )
        if normalized != OANDA_DEMO_API_URL:
            raise ValueError(
                "the configured OANDA endpoint is not the demo (fxpractice) endpoint; "
                f"this adapter is demo-only and refuses to start (allowed: {OANDA_DEMO_API_URL})"
            )
        object.__setattr__(self, "endpoint", normalized)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not self.timeout_seconds > 0
        ):
            raise ValueError(
                f"timeout_seconds must be a positive finite number, got {self.timeout_seconds!r}"
            )

    def masked_account_id(self) -> str:
        """A display-safe account identifier: the real id never crosses the
        adapter boundary (PR #101 review). Long ids keep a short prefix and
        suffix for recognisability; short ids are fully masked."""
        value = self.account_id
        if len(value) <= 8:
            return "***"
        return f"{value[:3]}***{value[-3:]}"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "OandaDemoConfig":
        """Build the config from environment variables ONLY (Issue #79).

        ``OANDA_API_TOKEN`` / ``OANDA_ACCOUNT_ID`` are required;
        ``OANDA_API_URL`` defaults to the demo endpoint and anything else is
        refused; ``OANDA_HTTP_TIMEOUT_SECONDS`` optionally overrides the
        request timeout.
        """
        env = os.environ if environ is None else environ
        api_token = _require_non_empty_env(env.get(ENV_API_TOKEN), ENV_API_TOKEN)
        account_id = _require_non_empty_env(env.get(ENV_ACCOUNT_ID), ENV_ACCOUNT_ID)
        endpoint = env.get(ENV_API_URL)
        if endpoint is None or not endpoint.strip():
            endpoint = OANDA_DEMO_API_URL
        raw_timeout = env.get(ENV_HTTP_TIMEOUT_SECONDS)
        if raw_timeout is None or not raw_timeout.strip():
            timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
        else:
            try:
                timeout_seconds = float(raw_timeout)
            except ValueError:
                raise ValueError(
                    f"environment variable {ENV_HTTP_TIMEOUT_SECONDS} must be a number"
                ) from None
        return cls(
            api_token=api_token,
            account_id=account_id,
            endpoint=endpoint,
            timeout_seconds=timeout_seconds,
        )


# --- response parsing helpers ---------------------------------------------------


def _parse_utc_time(value, field_name: str) -> datetime:
    """Parse an OANDA RFC3339 timestamp (nanosecond precision, ``Z`` suffix)
    into a timezone-aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        raise BrokerCommunicationError(
            f"broker response field {field_name} is not a timestamp string"
        )
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    # datetime.fromisoformat accepts at most microseconds: truncate the
    # fractional part OANDA reports with nanosecond precision.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        offset = ""
        for index, char in enumerate(tail):
            if char.isdigit():
                digits += char
            else:
                offset = tail[index:]
                break
        text = f"{head}.{(digits[:6] or '0').ljust(6, '0')}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise BrokerCommunicationError(
            f"broker response field {field_name} is not a valid RFC3339 timestamp"
        ) from None
    if parsed.tzinfo is None:
        raise BrokerCommunicationError(
            f"broker response field {field_name} carries no timezone"
        )
    return parsed.astimezone(timezone.utc)


def _parse_decimal(value, field_name: str) -> Decimal:
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise BrokerCommunicationError(
                f"broker response field {field_name} is not a decimal string"
            ) from None
        if parsed.is_finite():
            return parsed
    raise BrokerCommunicationError(
        f"broker response field {field_name} is not a finite decimal string"
    )


def _short_reason(payload: Mapping, *keys: str) -> str | None:
    """Extract a short, human-readable broker reason (``rejectReason`` /
    ``errorMessage`` style) without ever echoing the full payload."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return None


# --- adapter --------------------------------------------------------------------


class OandaDemoAdapter:
    """Demo-only OANDA v20 implementation of ``broker_interface.Broker``.

    Structural Protocol conformance (Issue #73): the five order operations
    plus the ``connection_identity()`` contract (Issue #75) and one adapter
    extra, ``get_tradable_instruments()``. Stateless between calls — order
    lookups always go to the broker (the execution store, not the adapter,
    owns local order state; Issue #74).
    """

    def __init__(
        self,
        config: OandaDemoConfig,
        *,
        transport: Callable[[str, str, Mapping[str, str], str | None, float], tuple[int, str]]
        | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(config, OandaDemoConfig):
            raise ValueError(f"config must be an OandaDemoConfig, got {type(config).__name__}")
        if transport is not None and not callable(transport):
            raise ValueError("transport must be a callable (method, url, headers, body, timeout)")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be a callable returning a UTC datetime")
        self._config = config
        self._transport = transport or _requests_transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Immutable identity derived from the construction-time config
        # (Issue #75): the mode is ALWAYS "demo" — the config cannot hold
        # anything but the demo endpoint in the first place. The target is
        # the constant, non-secret label (PR #101 review: the URL is
        # environment-managed and must not cross into the execution layer).
        self._connection_identity = BrokerConnectionIdentity(
            mode="demo", target=OANDA_DEMO_CONNECTION_TARGET
        )

    # --- Broker interface -----------------------------------------------------

    def submit_order(self, request: OrderRequest) -> OrderResult:
        """Submit a MARKET (FOK) or LIMIT (GTC) order to the demo account.

        Exactly ONE attempt: a timeout raises ``BrokerTimeoutError`` and the
        outcome stays UNKNOWN for the caller to resolve by query — the
        adapter never resends (Issue #74 / #79).
        """
        if not isinstance(request, OrderRequest):
            raise BrokerInvalidRequestError(f"request must be an OrderRequest, got {request!r}")
        self._require_client_order_id(request.client_order_id)
        units = request.quantity if request.side is OrderSide.BUY else -request.quantity
        order: dict = {
            "type": request.order_type.value.upper(),
            "instrument": request.symbol,
            "units": str(units),
            "positionFill": "DEFAULT",
            # The client-side identifier: OANDA stores it on the order and
            # every dependent transaction, keeping broker records joinable
            # with the execution_request_id bound to this client_order_id.
            "clientExtensions": {"id": request.client_order_id},
        }
        if request.order_type is OrderType.MARKET:
            order["timeInForce"] = "FOK"
        else:
            order["timeInForce"] = "GTC"
            order["price"] = str(request.limit_price)

        status, payload = self._request(
            "POST",
            f"/v3/accounts/{self._config.account_id}/orders",
            body={"order": order},
            client_order_id=request.client_order_id,
            outcome_unknown_on_ambiguity=True,
        )

        if status in (400, 404):
            reject = payload.get("orderRejectTransaction")
            if isinstance(reject, Mapping):
                reason = (
                    _short_reason(reject, "rejectReason")
                    or _short_reason(payload, "errorMessage")
                    or "rejected by broker"
                )
                reason = self._redact(reason)
                # A duplicate client_order_id is a boundary-contract error
                # (Issue #73 / FakeBroker parity), not an ordinary broker
                # rejection (PR #101 review).
                if _OANDA_DUPLICATE_ID_REASON in reason:
                    raise BrokerInvalidRequestError(
                        f"duplicate client_order_id {request.client_order_id!r}",
                        client_order_id=request.client_order_id,
                    )
                # The reject transaction may echo the client extensions; if
                # it does, it must echo OURS (response correlation).
                self._verify_client_correlation(
                    self._client_extension_id(reject),
                    request.client_order_id,
                    required=False,
                    context="orderRejectTransaction",
                )
                # A definitive business rejection is a RESULT, not an
                # exception (Issue #73 error style).
                return self._result_from_request(
                    request, state=OrderState.REJECTED, reject_reason=reason
                )
            raise BrokerInvalidRequestError(
                self._message("order submission was refused as invalid", payload),
                client_order_id=request.client_order_id,
            )
        if status not in (200, 201):
            raise BrokerTimeoutError(
                f"unexpected HTTP status {status} after submit; outcome unknown — "
                "resolve by a later query, never by resending",
                client_order_id=request.client_order_id,
            )

        fill_tx = payload.get("orderFillTransaction")
        cancel_tx = payload.get("orderCancelTransaction")
        create_tx = payload.get("orderCreateTransaction")
        reissue_reject_tx = payload.get("orderReissueRejectTransaction")
        if isinstance(fill_tx, Mapping):
            # The fill transaction must be correlated to OUR order before it
            # may be bound to this client_order_id (PR #101 review).
            self._verify_client_correlation(
                fill_tx.get("clientOrderID"),
                request.client_order_id,
                required=True,
                context="orderFillTransaction",
            )
            broker_order_id = self._tx_order_id(fill_tx, create_tx)
            filled_quantity = abs(
                _parse_decimal(fill_tx.get("units"), "orderFillTransaction.units")
            )
            # Partial fills (PR #101 review): the response may fill less
            # than the requested quantity. If the remainder was cancelled or
            # its reissue was rejected the order is done (CANCELLED with the
            # partial fills kept); otherwise it is still working
            # (PARTIALLY_FILLED). An orderReissueTransaction alone means the
            # remainder keeps working, which is also PARTIALLY_FILLED. A
            # zero or over-requested fill quantity is a malformed response,
            # detected BEFORE any model construction.
            if filled_quantity == request.quantity:
                state = OrderState.FILLED
            elif 0 < filled_quantity < request.quantity:
                remainder_done = isinstance(cancel_tx, Mapping) or isinstance(
                    reissue_reject_tx, Mapping
                )
                state = OrderState.CANCELLED if remainder_done else OrderState.PARTIALLY_FILLED
            else:
                raise BrokerCommunicationError(
                    "broker fill quantity is inconsistent with the requested quantity",
                    client_order_id=request.client_order_id,
                )
            fill = Fill(
                fill_id=self._tx_id(fill_tx, "orderFillTransaction.id"),
                price=_parse_decimal(fill_tx.get("price"), "orderFillTransaction.price"),
                quantity=filled_quantity,
                filled_at_utc=_parse_utc_time(fill_tx.get("time"), "orderFillTransaction.time"),
            )
            return self._result_from_request(
                request,
                state=state,
                broker_order_id=broker_order_id,
                fills=(fill,),
            )
        if isinstance(cancel_tx, Mapping):
            # e.g. a FOK market order the demo book could not fill in full.
            self._verify_client_correlation(
                cancel_tx.get("clientOrderID"),
                request.client_order_id,
                required=True,
                context="orderCancelTransaction",
            )
            return self._result_from_request(
                request,
                state=OrderState.CANCELLED,
                broker_order_id=self._tx_order_id(cancel_tx, create_tx),
            )
        if isinstance(create_tx, Mapping):
            self._verify_client_correlation(
                self._client_extension_id(create_tx),
                request.client_order_id,
                required=True,
                context="orderCreateTransaction",
            )
            return self._result_from_request(
                request,
                state=OrderState.ACCEPTED,
                broker_order_id=self._tx_id(create_tx, "orderCreateTransaction.id"),
            )
        raise BrokerTimeoutError(
            "broker response after submit carried no known transaction; outcome "
            "unknown — resolve by a later query, never by resending",
            client_order_id=request.client_order_id,
        )

    def cancel_order(self, client_order_id: str) -> OrderResult:
        """Cancel the pending order registered under ``client_order_id``
        (OANDA order specifier ``@<client id>``) and return the post-cancel
        snapshot."""
        self._require_client_order_id(client_order_id)
        status, payload = self._request(
            "PUT",
            f"/v3/accounts/{self._config.account_id}/orders/"
            f"{self._order_specifier(client_order_id)}/cancel",
            client_order_id=client_order_id,
        )
        if status == 404:
            # OANDA answers 404 both for "no such order" and "no longer
            # cancellable" — disambiguate by querying the order itself,
            # mirroring FakeBroker semantics (unknown id -> NOT_FOUND,
            # terminal order -> CANCELLATION_FAILURE).
            try:
                current = self.get_order(client_order_id)
            except BrokerOrderNotFoundError:
                raise BrokerOrderNotFoundError(
                    f"no order under client_order_id {client_order_id!r}",
                    client_order_id=client_order_id,
                ) from None
            reason = _short_reason(payload, "errorMessage")
            reason = (
                self._redact(reason)
                if reason
                else f"order is not cancellable (state {current.state.value!r})"
            )
            raise BrokerCancellationError(reason, client_order_id=client_order_id)
        if status != 200:
            raise BrokerCancellationError(
                self._message(f"cancellation failed with HTTP status {status}", payload),
                client_order_id=client_order_id,
            )
        return self.get_order(client_order_id)

    def get_order(self, client_order_id: str) -> OrderResult:
        """Return the latest broker-side snapshot of the order registered
        under ``client_order_id``."""
        self._require_client_order_id(client_order_id)
        status, payload = self._request(
            "GET",
            f"/v3/accounts/{self._config.account_id}/orders/"
            f"{self._order_specifier(client_order_id)}",
            client_order_id=client_order_id,
        )
        if status == 404:
            raise BrokerOrderNotFoundError(
                f"no order under client_order_id {client_order_id!r}",
                client_order_id=client_order_id,
            )
        if status != 200:
            raise BrokerCommunicationError(
                self._message(f"order lookup failed with HTTP status {status}", payload),
                client_order_id=client_order_id,
            )
        order = payload.get("order")
        if not isinstance(order, Mapping):
            raise BrokerCommunicationError(
                "broker response carried no order object",
                client_order_id=client_order_id,
            )
        return self._result_from_order(order, client_order_id)

    def get_positions(self) -> tuple[PositionInfo, ...]:
        """Return the open positions of the demo account as common models."""
        status, payload = self._request(
            "GET", f"/v3/accounts/{self._config.account_id}/openPositions"
        )
        if status != 200:
            raise BrokerCommunicationError(
                self._message(f"open-position lookup failed with HTTP status {status}", payload)
            )
        raw_positions = payload.get("positions")
        if not isinstance(raw_positions, list):
            raise BrokerCommunicationError("broker response carried no positions list")
        retrieved_at = self._now()
        positions: list[PositionInfo] = []
        for entry in raw_positions:
            if not isinstance(entry, Mapping):
                raise BrokerCommunicationError("broker positions entry is not an object")
            instrument = entry.get("instrument")
            if not isinstance(instrument, str) or not instrument.strip():
                raise BrokerCommunicationError("broker positions entry carries no instrument")
            for side_key, side in (("long", PositionSide.LONG), ("short", PositionSide.SHORT)):
                side_entry = entry.get(side_key)
                if not isinstance(side_entry, Mapping):
                    continue
                units = _parse_decimal(
                    side_entry.get("units", "0"), f"position.{side_key}.units"
                )
                if units == 0:
                    continue
                positions.append(
                    PositionInfo(
                        symbol=instrument,
                        side=side,
                        quantity=abs(units),
                        average_entry_price=_parse_decimal(
                            side_entry.get("averagePrice"), f"position.{side_key}.averagePrice"
                        ),
                        retrieved_at_utc=retrieved_at,
                    )
                )
        return tuple(positions)

    def get_account_state(self) -> AccountState:
        """Return the demo-account snapshot as the common ``AccountState``."""
        status, payload = self._request(
            "GET", f"/v3/accounts/{self._config.account_id}/summary"
        )
        if status != 200:
            raise BrokerCommunicationError(
                self._message(f"account lookup failed with HTTP status {status}", payload)
            )
        account = payload.get("account")
        if not isinstance(account, Mapping):
            raise BrokerCommunicationError("broker response carried no account object")
        account_id = account.get("id")
        if not isinstance(account_id, str) or not account_id.strip():
            raise BrokerCommunicationError("broker account object carries no id")
        if account_id != self._config.account_id:
            # Correlation check, statically worded (neither id is echoed).
            raise BrokerCommunicationError(
                "broker response describes a different account than configured"
            )
        currency = account.get("currency")
        if not isinstance(currency, str) or not currency.strip():
            raise BrokerCommunicationError("broker account object carries no currency")
        return AccountState(
            # The REAL account id never leaves the adapter (PR #101 review):
            # AccountState reprs plainly, so it carries the masked form.
            account_id=self._config.masked_account_id(),
            currency=currency,
            balance=_parse_decimal(account.get("balance"), "account.balance"),
            equity=_parse_decimal(account.get("NAV"), "account.NAV"),
            margin_available=_parse_decimal(
                account.get("marginAvailable"), "account.marginAvailable"
            ),
            retrieved_at_utc=self._now(),
        )

    # --- connection identity / adapter extras ----------------------------------

    def connection_identity(self) -> BrokerConnectionIdentity:
        """The immutable identity derived from the construction-time config
        (Issue #75): always mode ``"demo"`` with the demo endpoint as target."""
        return self._connection_identity

    def get_open_trades(self) -> tuple[TradeInfo, ...]:
        """Return the open trades of the demo account as common models
        (Issue #79 scope: open trade query; adapter extra beyond the
        Issue #73 five-operation Protocol, like ``get_tradable_instruments``)."""
        status, payload = self._request(
            "GET", f"/v3/accounts/{self._config.account_id}/openTrades"
        )
        if status != 200:
            raise BrokerCommunicationError(
                self._message(f"open-trade lookup failed with HTTP status {status}", payload)
            )
        raw_trades = payload.get("trades")
        if not isinstance(raw_trades, list):
            raise BrokerCommunicationError("broker response carried no trades list")
        retrieved_at = self._now()
        trades: list[TradeInfo] = []
        for entry in raw_trades:
            if not isinstance(entry, Mapping):
                raise BrokerCommunicationError("broker trades entry is not an object")
            trade_id = entry.get("id")
            if not isinstance(trade_id, str) or not trade_id.strip():
                raise BrokerCommunicationError("broker trades entry carries no id")
            instrument = entry.get("instrument")
            if not isinstance(instrument, str) or not instrument.strip():
                raise BrokerCommunicationError("broker trades entry carries no instrument")
            units = _parse_decimal(entry.get("currentUnits"), "trade.currentUnits")
            if units == 0:
                raise BrokerCommunicationError("broker open trade carries zero units")
            try:
                trades.append(
                    TradeInfo(
                        trade_id=trade_id,
                        symbol=instrument,
                        side=PositionSide.LONG if units > 0 else PositionSide.SHORT,
                        quantity=abs(units),
                        open_price=_parse_decimal(entry.get("price"), "trade.price"),
                        opened_at_utc=_parse_utc_time(entry.get("openTime"), "trade.openTime"),
                        retrieved_at_utc=retrieved_at,
                    )
                )
            except ValueError as exc:
                raise BrokerCommunicationError(
                    self._redact(f"broker trade violated common-model invariants: {exc}")
                ) from None
        return tuple(trades)

    def get_tradable_instruments(self) -> tuple[str, ...]:
        """Return the instrument names tradable on the demo account (sorted).

        Adapter extra beyond the Issue #73 Protocol (Issue #79 scope:
        tradable-instrument confirmation). Names are plain strings in
        OANDA's ``BASE_QUOTE`` form (e.g. ``USD_JPY``), which is already the
        repository-wide symbol vocabulary.
        """
        status, payload = self._request(
            "GET", f"/v3/accounts/{self._config.account_id}/instruments"
        )
        if status != 200:
            raise BrokerCommunicationError(
                self._message(f"instrument lookup failed with HTTP status {status}", payload)
            )
        raw = payload.get("instruments")
        if not isinstance(raw, list):
            raise BrokerCommunicationError("broker response carried no instruments list")
        names: list[str] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                raise BrokerCommunicationError("broker instruments entry is not an object")
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                raise BrokerCommunicationError("broker instruments entry carries no name")
            names.append(name)
        return tuple(sorted(names))

    # --- HTTP plumbing ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping | None = None,
        client_order_id: str | None = None,
        outcome_unknown_on_ambiguity: bool = False,
    ) -> tuple[int, Mapping]:
        """One HTTP exchange, translated to the common error taxonomy.

        ``outcome_unknown_on_ambiguity`` is True for state-changing submits:
        any failure that cannot prove the request went unprocessed (timeout,
        5xx, malformed response, unclassified transport error) maps to
        ``BrokerTimeoutError`` so the caller treats the outcome as UNKNOWN
        instead of "not placed". Reads map the same failures to
        ``BrokerCommunicationError``.
        """
        url = self._config.endpoint + path
        headers = {
            "Authorization": f"Bearer {self._config.api_token}",
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "RFC3339",
        }
        body_text = json.dumps(body) if body is not None else None
        try:
            status, text = self._transport(
                method, url, headers, body_text, self._config.timeout_seconds
            )
        except TransportTimeoutError:
            raise BrokerTimeoutError(
                "request to the OANDA demo endpoint timed out; the outcome is unknown "
                "and is resolved by a later query, never by resending",
                client_order_id=client_order_id,
            ) from None
        except TransportConnectionError:
            raise BrokerCommunicationError(
                "connection to the OANDA demo endpoint could not be established",
                client_order_id=client_order_id,
            ) from None
        except BrokerError:
            raise
        except Exception:
            # Unclassified transport failure: for a submit the request may
            # have been processed, so fail toward UNKNOWN, never "not sent".
            if outcome_unknown_on_ambiguity:
                raise BrokerTimeoutError(
                    "HTTP exchange with the OANDA demo endpoint failed in an "
                    "unclassified way; the outcome is unknown",
                    client_order_id=client_order_id,
                ) from None
            raise BrokerCommunicationError(
                "HTTP exchange with the OANDA demo endpoint failed",
                client_order_id=client_order_id,
            ) from None

        if 300 <= status < 400:
            # Redirects are never followed (allow_redirects=False in the
            # default transport): following one could carry the Authorization
            # header off the demo endpoint (PR #101 review, fail-closed).
            if outcome_unknown_on_ambiguity:
                raise BrokerTimeoutError(
                    f"the OANDA demo endpoint answered with a redirect (HTTP {status}); "
                    "redirects are never followed (demo-only fail-closed) and the "
                    "outcome is unknown — resolve by a later query, never by resending",
                    client_order_id=client_order_id,
                )
            raise BrokerCommunicationError(
                f"the OANDA demo endpoint answered with a redirect (HTTP {status}); "
                "redirects are never followed (demo-only fail-closed)",
                client_order_id=client_order_id,
            )
        if status in (401, 403):
            raise BrokerAuthenticationError(
                f"the OANDA demo endpoint rejected the credentials (HTTP {status})",
                client_order_id=client_order_id,
            )
        if status == 429:
            raise BrokerRateLimitError(
                "the OANDA demo endpoint rate-limited the request (HTTP 429); "
                "the adapter never retries on its own",
                client_order_id=client_order_id,
            )
        if status >= 500:
            if outcome_unknown_on_ambiguity:
                raise BrokerTimeoutError(
                    f"the OANDA demo endpoint answered HTTP {status} after a submit; "
                    "the outcome is unknown — resolve by a later query, never by resending",
                    client_order_id=client_order_id,
                )
            raise BrokerCommunicationError(
                f"the OANDA demo endpoint answered HTTP {status}",
                client_order_id=client_order_id,
            )

        if not isinstance(text, str) or not text.strip():
            payload: Mapping = {}
        else:
            try:
                parsed = json.loads(text)
            except ValueError:
                if outcome_unknown_on_ambiguity:
                    raise BrokerTimeoutError(
                        "the OANDA demo endpoint answered with a malformed body after "
                        "a submit; the outcome is unknown",
                        client_order_id=client_order_id,
                    ) from None
                raise BrokerCommunicationError(
                    "the OANDA demo endpoint answered with a malformed body",
                    client_order_id=client_order_id,
                ) from None
            payload = parsed if isinstance(parsed, Mapping) else {}
        return status, payload

    # --- translation helpers ------------------------------------------------------

    def _now(self) -> datetime:
        now = self._clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
            or now.utcoffset().total_seconds() != 0
        ):
            raise BrokerCommunicationError("clock must return a timezone-aware UTC datetime")
        return now

    @staticmethod
    def _require_client_order_id(client_order_id) -> None:
        # The id is embedded in URL paths ("@<id>" specifier), so beyond
        # non-emptiness it must stay inside unreserved URL characters and a
        # bounded length (PR #101 review) — otherwise query/cancel could not
        # resolve an already-submitted order.
        if not isinstance(client_order_id, str) or not _CLIENT_ORDER_ID_RE.fullmatch(
            client_order_id
        ):
            raise BrokerInvalidRequestError(
                "client_order_id must be 1-128 characters of [A-Za-z0-9._-] "
                f"(URL-safe, starting with a letter or digit), got {client_order_id!r}"
            )

    @staticmethod
    def _order_specifier(client_order_id: str) -> str:
        # "@<id>" is OANDA's client-order-id specifier form. The id has been
        # validated against _CLIENT_ORDER_ID_RE, so it is URL-path-safe as-is.
        return f"@{client_order_id}"

    def _redact(self, text: str) -> str:
        """Scrub the secret values out of broker-echoed text before it can
        reach an exception message or a returned reject_reason (PR #101
        review: the broker may reflect request values back)."""
        for secret in (self._config.api_token, self._config.account_id):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _message(self, prefix: str, payload: Mapping) -> str:
        reason = _short_reason(payload, "errorMessage", "rejectReason")
        return f"{prefix}: {self._redact(reason)}" if reason else prefix

    @staticmethod
    def _client_extension_id(tx: Mapping):
        extensions = tx.get("clientExtensions")
        if isinstance(extensions, Mapping):
            return extensions.get("id")
        return None

    def _verify_client_correlation(
        self, echoed, client_order_id: str, *, required: bool, context: str
    ) -> None:
        """The broker's echo of the client identifier must match the request
        before a response may be bound to this ``client_order_id`` (PR #101
        review): a missing (where required) or different echo means the
        response cannot be trusted to describe OUR order."""
        if echoed is None:
            if required:
                raise BrokerCommunicationError(
                    f"broker response ({context}) did not echo the client order "
                    "identifier; refusing to bind the response to this order",
                    client_order_id=client_order_id,
                )
            return
        if echoed != client_order_id:
            raise BrokerCommunicationError(
                f"broker response ({context}) echoed a different client order "
                "identifier; refusing to bind the response to this order",
                client_order_id=client_order_id,
            )

    @staticmethod
    def _tx_id(tx: Mapping, field_name: str) -> str:
        value = tx.get("id")
        if not isinstance(value, str) or not value.strip():
            raise BrokerCommunicationError(f"broker response field {field_name} is missing")
        return value

    @staticmethod
    def _tx_order_id(tx: Mapping, create_tx) -> str:
        value = tx.get("orderID")
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(create_tx, Mapping):
            value = create_tx.get("id")
            if isinstance(value, str) and value.strip():
                return value
        raise BrokerCommunicationError("broker response carried no order id")

    def _result_from_request(
        self,
        request: OrderRequest,
        *,
        state: OrderState,
        broker_order_id: str | None = None,
        fills: tuple[Fill, ...] = (),
        reject_reason: str | None = None,
    ) -> OrderResult:
        filled = sum((f.quantity for f in fills), Decimal(0))
        try:
            return OrderResult(
                client_order_id=request.client_order_id,
                broker_order_id=broker_order_id,
                state=state,
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                requested_quantity=request.quantity,
                filled_quantity=filled,
                remaining_quantity=request.quantity - filled,
                requested_price=request.limit_price
                if request.order_type is OrderType.LIMIT
                else None,
                average_fill_price=weighted_average_price(fills),
                updated_at_utc=self._now(),
                fills=fills,
                reject_reason=reject_reason,
            )
        except ValueError as exc:
            # A broker answer inconsistent with the common-model invariants
            # must cross the boundary as a common BrokerError, never as a
            # naked ValueError (PR #101 review).
            raise BrokerCommunicationError(
                self._redact(f"broker response violated common-model invariants: {exc}"),
                client_order_id=request.client_order_id,
            ) from None

    def _result_from_order(self, order: Mapping, client_order_id: str) -> OrderResult:
        """Translate an OANDA order object (GET orders/@id) into the common
        ``OrderResult``, fetching the filling transaction for fill details.

        The order's echoed client extension id is verified against the
        queried ``client_order_id`` first: a missing or different echo is a
        broken response correlation (PR #101 review)."""
        self._verify_client_correlation(
            self._client_extension_id(order),
            client_order_id,
            required=True,
            context="order.clientExtensions",
        )
        # A broker-side answer with an unexpected shape is a RESPONSE
        # problem (communication failure), not a caller mistake (PR #101
        # review): this adapter only ever creates MARKET/LIMIT orders.
        oanda_type = order.get("type")
        if oanda_type == "MARKET":
            order_type = OrderType.MARKET
        elif oanda_type == "LIMIT":
            order_type = OrderType.LIMIT
        else:
            raise BrokerCommunicationError(
                f"broker order object carries an unsupported type {oanda_type!r} "
                "(this adapter handles MARKET/LIMIT only)",
                client_order_id=client_order_id,
            )
        instrument = order.get("instrument")
        if not isinstance(instrument, str) or not instrument.strip():
            raise BrokerCommunicationError(
                "broker order object carries no instrument",
                client_order_id=client_order_id,
            )
        units = _parse_decimal(order.get("units"), "order.units")
        side = OrderSide.BUY if units > 0 else OrderSide.SELL
        quantity = abs(units)
        broker_order_id = order.get("id")
        if not isinstance(broker_order_id, str) or not broker_order_id.strip():
            raise BrokerCommunicationError(
                "broker order object carries no id", client_order_id=client_order_id
            )
        requested_price = (
            _parse_decimal(order.get("price"), "order.price")
            if order_type is OrderType.LIMIT
            else None
        )
        oanda_state = order.get("state")
        if oanda_state in ("PENDING", "TRIGGERED"):
            state = OrderState.ACCEPTED
            fills: tuple[Fill, ...] = ()
        elif oanda_state == "CANCELLED":
            state = OrderState.CANCELLED
            fills = ()
        elif oanda_state == "FILLED":
            state = OrderState.FILLED
            fills = (self._fetch_fill(order, client_order_id),)
        else:
            raise BrokerCommunicationError(
                f"broker order object carries an unknown state {oanda_state!r}",
                client_order_id=client_order_id,
            )
        filled = sum((f.quantity for f in fills), Decimal(0))
        try:
            return OrderResult(
                client_order_id=client_order_id,
                broker_order_id=broker_order_id,
                state=state,
                symbol=instrument,
                side=side,
                order_type=order_type,
                requested_quantity=quantity,
                filled_quantity=filled,
                remaining_quantity=quantity - filled,
                requested_price=requested_price,
                average_fill_price=weighted_average_price(fills),
                updated_at_utc=self._now(),
                fills=fills,
            )
        except ValueError as exc:
            raise BrokerCommunicationError(
                self._redact(f"broker response violated common-model invariants: {exc}"),
                client_order_id=client_order_id,
            ) from None

    def _fetch_fill(self, order: Mapping, client_order_id: str) -> Fill:
        """Fetch the ORDER_FILL transaction referenced by a FILLED order to
        recover fill price / quantity / time (transaction & fill results,
        Issue #79 scope)."""
        tx_id = order.get("fillingTransactionID")
        if not isinstance(tx_id, str) or not tx_id.strip():
            raise BrokerCommunicationError(
                "filled order carries no fillingTransactionID",
                client_order_id=client_order_id,
            )
        status, payload = self._request(
            "GET",
            f"/v3/accounts/{self._config.account_id}/transactions/{tx_id}",
            client_order_id=client_order_id,
        )
        if status != 200:
            raise BrokerCommunicationError(
                self._message(f"transaction lookup failed with HTTP status {status}", payload),
                client_order_id=client_order_id,
            )
        tx = payload.get("transaction")
        if not isinstance(tx, Mapping):
            raise BrokerCommunicationError(
                "broker response carried no transaction object",
                client_order_id=client_order_id,
            )
        return Fill(
            fill_id=self._tx_id(tx, "transaction.id"),
            price=_parse_decimal(tx.get("price"), "transaction.price"),
            quantity=abs(_parse_decimal(tx.get("units"), "transaction.units")),
            filled_at_utc=_parse_utc_time(tx.get("time"), "transaction.time"),
        )
