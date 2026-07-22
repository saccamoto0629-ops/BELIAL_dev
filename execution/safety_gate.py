"""Fail-closed pre-submit Safety Gate for the FUTURE Live Execution layer (Issue #75).

*** NOTHING HERE PLACES A REAL ORDER. No broker adapter, no live connection,
no network I/O exists in this module. The gate validates an ``ExecutionRequest``
against static policy, a caller-supplied market snapshot, and broker safety
queries BEFORE the request may cross the ``Broker.submit_order`` boundary. ***

Design (docs/architecture/safety_gate.md is the authoritative spec):

- The gate is INDEPENDENT of Strategy and of any Broker adapter: Strategy code
  holds no safety policy, and a broker adapter holds no gate logic. The gate
  depends only on the ``broker`` boundary models and ``ExecutionRequest``.
- This module holds the DECISION layer only: policy, market snapshot,
  typed connection identity, gate evaluation, decision model. The single
  sanctioned submission path lives in ``execution/live_submission.py``
  (``LiveOrderSubmissionService``) and is pinned repository-wide by the AST
  test ``execution/tests/test_submission_boundary.py`` -- no other module may
  call ``submit_order`` / ``to_order_request``.
- Typed connection identity (Issue #75 review r1): mode / target are NOT
  caller-supplied per-call strings. A :class:`BrokerConnection` binds the
  broker INSTANCE to its ``ExecutionMode`` and connection target at
  composition time, and cross-checks the broker's concrete type against the
  mode: only ``FakeBroker`` may claim ``fake``, and ``demo`` / ``live``
  connections cannot exist at all until a real adapter type is registered.
  A FakeBroker claiming ``live`` is therefore unrepresentable, not merely
  rejected at evaluation time.
- FAIL-CLOSED: every external lookup (broker positions / account, kill-switch
  state) that fails, returns garbage, or looks stale (future-dated or older
  than ``max_reference_age`` -- account state included) produces a
  machine-readable rejection instead of an implicit allow.
- ALL violations are collected (not first-fail) and then DEDUPLICATED by code
  preserving first-occurrence order, so one decision carries every rejection
  code that applied, each exactly once.
- Reference price for SL/TP validation: LIMIT orders use the request's own
  ``requested_price`` (the model already guarantees the SL/TP side relations
  at construction; the gate re-checks as defense in depth). MARKET orders use
  the expected fill side of the snapshot -- ask for BUY, bid for SELL --
  falling back to ``reference_price`` when the side price is unavailable.
- Staleness base: the gate's injected UTC clock at evaluation time. A market
  snapshot observed in the FUTURE is rejected (clock skew / corrupt data must
  not pass as "fresh").
- Position limits are COUNT-based and conservative: every broker-reported
  position counts (long and short of the same symbol both count, duplicates
  are not deduplicated), and the new order counts as +1 -- an order is
  rejected when the existing count has already reached the limit. Quantity
  aggregation / margin-based sizing is deliberately NOT modelled (non-scope,
  Issue #75). Working (partially filled) orders are not counted -- only what
  the broker reports as positions.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Protocol

from broker.broker_interface import Broker
from broker.broker_models import (
    AccountState,
    OrderSide,
    OrderType,
    PositionInfo,
)
from broker.fake_broker import FakeBroker
from execution.execution_models import ExecutionRequest


class ExecutionMode(enum.Enum):
    """Where an order submission believes it is running.

    ``FAKE`` is the test / development mode (FakeBroker only). It is
    deliberately NOT named "paper": Paper Execution (``paper-execution/``) is
    an independent virtual-execution layer that never touches the broker
    boundary and never passes through this gate.
    """

    FAKE = "fake"
    DEMO = "demo"
    LIVE = "live"


# Typed connection identity registry (Issue #75 review r1): which concrete
# broker type may claim which execution mode. DEMO / LIVE deliberately have
# NO entry -- no such adapter exists, so no such connection can be built.
# A future adapter is added HERE, never by loosening BrokerConnection.
BROKER_TYPE_BY_MODE: dict[ExecutionMode, type] = {
    ExecutionMode.FAKE: FakeBroker,
}


class SafetyRejectionCode(enum.Enum):
    """Machine-readable rejection codes. These values are the PRIMARY record
    of why an order was blocked (logs and stored details are secondary)."""

    SYMBOL_NOT_ALLOWED = "symbol_not_allowed"
    SIDE_NOT_ALLOWED = "side_not_allowed"
    QUANTITY_LIMIT_EXCEEDED = "quantity_limit_exceeded"
    SYMBOL_POSITION_LIMIT_EXCEEDED = "symbol_position_limit_exceeded"
    ACCOUNT_POSITION_LIMIT_EXCEEDED = "account_position_limit_exceeded"
    STOP_LOSS_REQUIRED = "stop_loss_required"
    STOP_LOSS_INVALID = "stop_loss_invalid"
    TAKE_PROFIT_INVALID = "take_profit_invalid"
    MARKET_SNAPSHOT_MISMATCH = "market_snapshot_mismatch"
    REFERENCE_PRICE_MISSING = "reference_price_missing"
    REFERENCE_PRICE_STALE = "reference_price_stale"
    REFERENCE_TIMESTAMP_FUTURE = "reference_timestamp_future"
    SPREAD_MISSING = "spread_missing"
    SPREAD_LIMIT_EXCEEDED = "spread_limit_exceeded"
    MARKET_CLOSED = "market_closed"
    SYMBOL_NOT_TRADABLE = "symbol_not_tradable"
    KILL_SWITCH_ENABLED = "kill_switch_enabled"
    BROKER_QUERY_FAILED = "broker_query_failed"
    STATE_QUERY_FAILED = "state_query_failed"
    DECISION_PERSIST_FAILED = "decision_persist_failed"
    EXECUTION_MODE_MISMATCH = "execution_mode_mismatch"
    GATE_INTERNAL_ERROR = "gate_internal_error"
    INTENT_NOT_REGISTERED = "intent_not_registered"
    DUPLICATE_SUBMISSION = "duplicate_submission"


def _require_utc(value, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {value!r}")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field_name} must be UTC (utcoffset 0), got {value.tzinfo}")


def _require_non_empty_str(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


@dataclass(frozen=True)
class BrokerConnection:
    """Typed connection identity: the broker INSTANCE travels together with
    the execution mode and connection target it was wired for.

    Bound once at composition time -- callers can no longer claim an
    arbitrary mode/target per call, and the claimed mode is cross-checked
    against the broker's concrete type via :data:`BROKER_TYPE_BY_MODE`:

    - ``fake`` requires a ``FakeBroker`` instance (subclasses included);
    - a ``FakeBroker`` can claim nothing but ``fake``;
    - ``demo`` / ``live`` connections cannot be constructed at all until a
      real adapter type is registered (fail closed by unrepresentability).

    The target string must be exact (non-empty, no surrounding whitespace);
    whether it is allowed for the mode stays a policy decision
    (``SafetyPolicy.allowed_connection_targets``), evaluated by the gate.
    """

    broker: Broker
    execution_mode: ExecutionMode
    connection_target: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution_mode, ExecutionMode):
            raise ValueError(f"execution_mode must be an ExecutionMode, got {self.execution_mode!r}")
        _require_non_empty_str(self.connection_target, "connection_target")
        if self.connection_target != self.connection_target.strip():
            raise ValueError(
                f"connection_target {self.connection_target!r} carries surrounding whitespace"
            )
        if not isinstance(self.broker, Broker):
            raise ValueError(f"broker must satisfy the Broker protocol, got {self.broker!r}")
        expected_type = BROKER_TYPE_BY_MODE.get(self.execution_mode)
        if expected_type is None:
            raise ValueError(
                f"no broker implementation is registered for execution mode "
                f"{self.execution_mode.value!r}; only "
                f"{sorted(mode.value for mode in BROKER_TYPE_BY_MODE)} connections can exist yet"
            )
        if not isinstance(self.broker, expected_type):
            raise ValueError(
                f"execution mode {self.execution_mode.value!r} requires a "
                f"{expected_type.__name__} instance, got {type(self.broker).__name__}"
            )
        claimed_modes = [
            mode for mode, broker_type in BROKER_TYPE_BY_MODE.items()
            if isinstance(self.broker, broker_type)
        ]
        if claimed_modes != [self.execution_mode]:
            raise ValueError(
                f"broker type {type(self.broker).__name__} is not uniquely bound to "
                f"execution mode {self.execution_mode.value!r}"
            )


@dataclass(frozen=True)
class MarketSafetySnapshot:
    """Market data the gate judges against, observed at ``observed_at_utc``.

    The CALLER supplies this snapshot (the gate performs no market-data I/O
    itself); the gate rejects it when it is stale, from the future, for the
    wrong symbol, or missing the prices a check needs.
    """

    symbol: str
    reference_price: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    observed_at_utc: datetime
    market_open: bool
    tradable: bool

    def __post_init__(self) -> None:
        _require_non_empty_str(self.symbol, "symbol")
        _require_utc(self.observed_at_utc, "observed_at_utc")
        for name in ("reference_price", "bid", "ask"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite() or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite Decimal or None, got {value!r}")
        if not isinstance(self.market_open, bool):
            raise ValueError(f"market_open must be a bool, got {self.market_open!r}")
        if not isinstance(self.tradable, bool):
            raise ValueError(f"tradable must be a bool, got {self.tradable!r}")
        if self.bid is not None and self.ask is not None and self.ask < self.bid:
            raise ValueError(f"ask {self.ask} must be >= bid {self.bid}")

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True)
class SafetyPolicy:
    """Static safety policy. ``policy_id`` identifies this configuration in
    every persisted decision, so an audit can tell WHICH policy allowed or
    rejected an order.

    Position limits are COUNT limits (see the module docstring): 0 means "no
    new positions at all". ``max_spread_by_symbol`` must cover every allowed
    symbol -- a symbol without a spread limit is a configuration error, not a
    runtime surprise. Connection targets are exact case-sensitive strings and
    one target may belong to only one mode (an endpoint that is valid for two
    modes cannot exist).
    """

    policy_id: str
    allowed_symbols: frozenset[str]
    allowed_sides: frozenset[OrderSide]
    max_order_quantity: Decimal
    max_positions_per_symbol: int
    max_positions_account: int
    max_reference_age: timedelta
    max_spread_by_symbol: dict[str, Decimal]
    allowed_connection_targets: dict[ExecutionMode, frozenset[str]]
    require_stop_loss: bool = True

    def __post_init__(self) -> None:
        _require_non_empty_str(self.policy_id, "policy_id")
        if not self.allowed_symbols:
            raise ValueError("allowed_symbols must not be empty")
        for symbol in self.allowed_symbols:
            _require_non_empty_str(symbol, "allowed_symbols entry")
            if symbol != symbol.strip():
                raise ValueError(f"allowed symbol {symbol!r} carries surrounding whitespace")
        if not self.allowed_sides:
            raise ValueError("allowed_sides must not be empty")
        for side in self.allowed_sides:
            if not isinstance(side, OrderSide):
                raise ValueError(f"allowed_sides entries must be OrderSide, got {side!r}")
        if not isinstance(self.max_order_quantity, Decimal) or not self.max_order_quantity.is_finite():
            raise ValueError(f"max_order_quantity must be a finite Decimal, got {self.max_order_quantity!r}")
        if self.max_order_quantity <= 0:
            raise ValueError("max_order_quantity must be > 0")
        for name in ("max_positions_per_symbol", "max_positions_account"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be an int >= 0, got {value!r}")
        if not isinstance(self.max_reference_age, timedelta) or self.max_reference_age <= timedelta(0):
            raise ValueError(f"max_reference_age must be a positive timedelta, got {self.max_reference_age!r}")
        if not isinstance(self.max_spread_by_symbol, dict):
            raise ValueError("max_spread_by_symbol must be a dict")
        if set(self.max_spread_by_symbol) != set(self.allowed_symbols):
            raise ValueError(
                "max_spread_by_symbol keys must exactly cover allowed_symbols; "
                f"got {sorted(self.max_spread_by_symbol)} vs {sorted(self.allowed_symbols)}"
            )
        for symbol, spread in self.max_spread_by_symbol.items():
            if not isinstance(spread, Decimal) or not spread.is_finite() or spread < 0:
                raise ValueError(f"max spread for {symbol!r} must be a non-negative finite Decimal")
        if not isinstance(self.allowed_connection_targets, dict):
            raise ValueError("allowed_connection_targets must be a dict")
        seen_targets: dict[str, ExecutionMode] = {}
        for mode, targets in self.allowed_connection_targets.items():
            if not isinstance(mode, ExecutionMode):
                raise ValueError(f"allowed_connection_targets keys must be ExecutionMode, got {mode!r}")
            if not isinstance(targets, frozenset):
                raise ValueError(f"targets for {mode.value!r} must be a frozenset")
            for target in targets:
                _require_non_empty_str(target, f"connection target for {mode.value!r}")
                if target != target.strip():
                    raise ValueError(f"connection target {target!r} carries surrounding whitespace")
                if target in seen_targets:
                    raise ValueError(
                        f"connection target {target!r} is configured for both "
                        f"{seen_targets[target].value!r} and {mode.value!r}; one endpoint "
                        "cannot be valid for two execution modes"
                    )
                seen_targets[target] = mode
        if not isinstance(self.require_stop_loss, bool):
            raise ValueError(f"require_stop_loss must be a bool, got {self.require_stop_loss!r}")


@dataclass(frozen=True)
class SafetyRejection:
    """One violation: ``code`` is the machine-readable primary record,
    ``message`` is the human-readable detail (no secrets belong here)."""

    code: SafetyRejectionCode
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, SafetyRejectionCode):
            raise ValueError(f"code must be a SafetyRejectionCode, got {self.code!r}")
        _require_non_empty_str(self.message, "message")


def dedupe_rejections(rejections: list[SafetyRejection]) -> tuple[SafetyRejection, ...]:
    """Order-preserving dedup by code: the FIRST occurrence of each code wins
    (Issue #75 review r1). ``SafetyDecision`` enforces the resulting
    uniqueness as a construction invariant."""
    deduped: list[SafetyRejection] = []
    seen: set[SafetyRejectionCode] = set()
    for rejection in rejections:
        if rejection.code not in seen:
            seen.add(rejection.code)
            deduped.append(rejection)
    return tuple(deduped)


@dataclass(frozen=True)
class SafetyDecision:
    """One gate evaluation outcome, as persisted for audit.

    ``policy_id`` records which policy configuration produced the decision;
    ``execution_mode`` / ``connection_target`` record the typed connection
    identity the evaluation ran against; ``account_id`` / ``position_count``
    record what the broker reported at evaluation time (None when the broker
    query failed). Rejection codes are unique within one decision
    (order-preserving dedup, first occurrence wins).
    """

    execution_request_id: str
    allowed: bool
    checked_at_utc: datetime
    policy_id: str
    rejections: tuple[SafetyRejection, ...] = ()
    execution_mode: str | None = None
    connection_target: str | None = None
    account_id: str | None = None
    position_count: int | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.execution_request_id, "execution_request_id")
        if not isinstance(self.allowed, bool):
            raise ValueError(f"allowed must be a bool, got {self.allowed!r}")
        _require_utc(self.checked_at_utc, "checked_at_utc")
        _require_non_empty_str(self.policy_id, "policy_id")
        if not isinstance(self.rejections, tuple):
            raise ValueError(f"rejections must be a tuple, got {type(self.rejections).__name__}")
        for entry in self.rejections:
            if not isinstance(entry, SafetyRejection):
                raise ValueError(f"every rejections entry must be a SafetyRejection, got {entry!r}")
        if self.allowed == bool(self.rejections):
            raise ValueError(
                "allowed decisions must have no rejections; rejected decisions must have at least one"
            )
        codes = [entry.code for entry in self.rejections]
        if len(codes) != len(set(codes)):
            raise ValueError(
                "rejection codes must be unique within one decision (dedupe_rejections upstream)"
            )
        for name in ("execution_mode", "connection_target", "account_id"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, name)
        if self.position_count is not None and (
            not isinstance(self.position_count, int)
            or isinstance(self.position_count, bool)
            or self.position_count < 0
        ):
            raise ValueError(f"position_count must be an int >= 0 or None, got {self.position_count!r}")

    @property
    def rejection_codes(self) -> tuple[str, ...]:
        return tuple(item.code.value for item in self.rejections)


class SafetyStateProvider(Protocol):
    """BELIAL-side safety state the gate must consult (kill switch)."""

    def is_kill_switch_enabled(self) -> bool: ...


@dataclass
class StaticSafetyState:
    """In-memory ``SafetyStateProvider`` (tests / static configuration)."""

    kill_switch_enabled: bool = False

    def is_kill_switch_enabled(self) -> bool:
        return self.kill_switch_enabled


class SafetyGate:
    """Evaluate every pre-submit condition and collect ALL rejection reasons.

    ``evaluate()`` is pure decision logic plus the broker/state safety
    queries; it performs no submission and no persistence. Caller programming
    errors (wrong argument types, a broken clock) raise ``ValueError`` --
    inside the sanctioned submission path (``LiveOrderSubmissionService``)
    they are converted to a fail-closed ``GATE_INTERNAL_ERROR`` rejection.
    """

    def __init__(
        self,
        *,
        policy: SafetyPolicy,
        state_provider: SafetyStateProvider,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(policy, SafetyPolicy):
            raise ValueError(f"policy must be a SafetyPolicy, got {policy!r}")
        if not callable(clock):
            raise ValueError(f"clock must be a callable returning a UTC datetime, got {clock!r}")
        self._policy = policy
        self._state_provider = state_provider
        self._clock = clock

    @property
    def policy_id(self) -> str:
        return self._policy.policy_id

    def evaluate(
        self,
        request: ExecutionRequest,
        *,
        connection: BrokerConnection,
        market: MarketSafetySnapshot,
    ) -> SafetyDecision:
        if not isinstance(request, ExecutionRequest):
            raise ValueError(f"request must be an ExecutionRequest, got {request!r}")
        if not isinstance(connection, BrokerConnection):
            raise ValueError(f"connection must be a BrokerConnection, got {connection!r}")
        if not isinstance(market, MarketSafetySnapshot):
            raise ValueError(f"market must be a MarketSafetySnapshot, got {market!r}")
        now = self._clock()
        _require_utc(now, "SafetyGate clock value")

        policy = self._policy
        rejections: list[SafetyRejection] = []

        def reject(code: SafetyRejectionCode, message: str) -> None:
            rejections.append(SafetyRejection(code=code, message=message))

        # --- static policy: symbol / side / quantity -------------------------
        if request.symbol not in policy.allowed_symbols:
            reject(SafetyRejectionCode.SYMBOL_NOT_ALLOWED, f"symbol {request.symbol!r} is not allowed")
        if request.side not in policy.allowed_sides:
            reject(SafetyRejectionCode.SIDE_NOT_ALLOWED, f"side {request.side.value!r} is not allowed")
        if request.quantity > policy.max_order_quantity:
            reject(
                SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED,
                f"quantity {request.quantity} exceeds limit {policy.max_order_quantity}",
            )

        # --- market snapshot: identity / freshness / open / tradable ---------
        if market.symbol != request.symbol:
            reject(
                SafetyRejectionCode.MARKET_SNAPSHOT_MISMATCH,
                f"market snapshot is for {market.symbol!r}, request is for {request.symbol!r}",
            )
        if market.reference_price is None:
            reject(SafetyRejectionCode.REFERENCE_PRICE_MISSING, "reference price is unavailable")
        if market.observed_at_utc > now:
            reject(
                SafetyRejectionCode.REFERENCE_TIMESTAMP_FUTURE,
                f"market snapshot observed_at {market.observed_at_utc.isoformat()} is after "
                f"the evaluation clock {now.isoformat()}",
            )
        elif now - market.observed_at_utc > policy.max_reference_age:
            reject(
                SafetyRejectionCode.REFERENCE_PRICE_STALE,
                f"reference price age {now - market.observed_at_utc} exceeds {policy.max_reference_age}",
            )
        if not market.market_open:
            reject(SafetyRejectionCode.MARKET_CLOSED, "market is closed")
        if not market.tradable:
            reject(SafetyRejectionCode.SYMBOL_NOT_TRADABLE, "symbol is not tradable")

        # --- spread ----------------------------------------------------------
        # Policy construction guarantees a spread limit for every ALLOWED
        # symbol, so a missing limit here only co-occurs with
        # SYMBOL_NOT_ALLOWED; a missing spread means bid/ask are unavailable.
        spread_limit = policy.max_spread_by_symbol.get(request.symbol)
        if spread_limit is None or market.spread is None:
            reject(SafetyRejectionCode.SPREAD_MISSING, "spread or spread limit is unavailable")
        elif market.spread > spread_limit:
            reject(
                SafetyRejectionCode.SPREAD_LIMIT_EXCEEDED,
                f"spread {market.spread} exceeds limit {spread_limit}",
            )

        # --- stop loss / take profit ----------------------------------------
        if policy.require_stop_loss and request.stop_loss_price is None:
            reject(SafetyRejectionCode.STOP_LOSS_REQUIRED, "stop loss is required")
        self._validate_protective_prices(request, market, reject)

        # --- execution mode vs connection target -----------------------------
        # The connection is a typed identity (broker instance bound to mode +
        # target at composition time; BrokerConnection cross-checks the
        # broker's concrete type). Re-verified here as defense in depth, then
        # the TARGET is checked against the policy allow-list for the mode --
        # exact, case-sensitive; unknown targets, case or whitespace variants
        # never match (fail closed).
        expected_type = BROKER_TYPE_BY_MODE.get(connection.execution_mode)
        if expected_type is None or not isinstance(connection.broker, expected_type):
            reject(
                SafetyRejectionCode.EXECUTION_MODE_MISMATCH,
                f"broker type {type(connection.broker).__name__} does not match execution "
                f"mode {connection.execution_mode.value!r}",
            )
        allowed_targets = policy.allowed_connection_targets.get(
            connection.execution_mode, frozenset()
        )
        if connection.connection_target not in allowed_targets:
            reject(
                SafetyRejectionCode.EXECUTION_MODE_MISMATCH,
                f"connection target {connection.connection_target!r} is not allowed for "
                f"execution mode {connection.execution_mode.value!r}",
            )

        # --- kill switch (absolute: any enabled state rejects every order) ---
        try:
            kill_switch = self._state_provider.is_kill_switch_enabled()
        except Exception as exc:  # provider may be DB / file backed; fail closed
            reject(SafetyRejectionCode.STATE_QUERY_FAILED, f"kill switch state query failed: {exc}")
        else:
            if not isinstance(kill_switch, bool):
                # A truthy/falsy non-bool is a broken provider, not an answer.
                reject(
                    SafetyRejectionCode.STATE_QUERY_FAILED,
                    f"kill switch state query returned non-boolean {kill_switch!r}",
                )
            elif kill_switch:
                reject(
                    SafetyRejectionCode.KILL_SWITCH_ENABLED,
                    "kill switch is enabled; all new orders are rejected",
                )

        # --- broker safety queries: positions / account ----------------------
        positions: tuple[PositionInfo, ...] | None = None
        account: AccountState | None = None
        try:
            positions = self._reliable_positions(connection.broker.get_positions(), now)
            account = self._reliable_account(connection.broker.get_account_state(), now)
        except Exception as exc:
            reject(SafetyRejectionCode.BROKER_QUERY_FAILED, f"broker safety query failed: {exc}")

        if positions is not None:
            # Count-based limits, new order counted as +1 (module docstring).
            symbol_count = sum(1 for position in positions if position.symbol == request.symbol)
            if symbol_count >= policy.max_positions_per_symbol:
                reject(
                    SafetyRejectionCode.SYMBOL_POSITION_LIMIT_EXCEEDED,
                    f"{symbol_count} open position(s) for {request.symbol!r} already reach the "
                    f"per-symbol limit {policy.max_positions_per_symbol}",
                )
            if len(positions) >= policy.max_positions_account:
                reject(
                    SafetyRejectionCode.ACCOUNT_POSITION_LIMIT_EXCEEDED,
                    f"{len(positions)} open position(s) already reach the account limit "
                    f"{policy.max_positions_account}",
                )

        deduped = dedupe_rejections(rejections)
        return SafetyDecision(
            execution_request_id=request.execution_request_id,
            allowed=not deduped,
            checked_at_utc=now,
            policy_id=policy.policy_id,
            rejections=deduped,
            execution_mode=connection.execution_mode.value,
            connection_target=connection.connection_target,
            account_id=account.account_id if account is not None else None,
            position_count=len(positions) if positions is not None else None,
        )

    def _reliable_positions(self, raw, now: datetime) -> tuple[PositionInfo, ...]:
        """Validate the broker's position answer before trusting it for limits.

        A malformed container, a non-PositionInfo entry, or a snapshot
        timestamp that is stale (older than ``max_reference_age``) or from the
        future makes the whole answer unreliable -> the caller converts the
        raised error into BROKER_QUERY_FAILED (fail closed).
        """
        if not isinstance(raw, tuple):
            raise TypeError(f"broker returned positions as {type(raw).__name__}, expected tuple")
        for position in raw:
            if not isinstance(position, PositionInfo):
                raise TypeError(f"broker returned non-PositionInfo entry {position!r}")
            if position.retrieved_at_utc > now:
                raise ValueError(
                    f"position snapshot for {position.symbol!r} is from the future "
                    f"({position.retrieved_at_utc.isoformat()})"
                )
            if now - position.retrieved_at_utc > self._policy.max_reference_age:
                raise ValueError(
                    f"position snapshot for {position.symbol!r} is stale "
                    f"({position.retrieved_at_utc.isoformat()})"
                )
        return raw

    def _reliable_account(self, raw, now: datetime) -> AccountState:
        """Same reliability bar for the account snapshot (Issue #75 review r1):
        wrong type, future-dated or stale ``retrieved_at_utc`` -> unreliable
        -> BROKER_QUERY_FAILED via the caller (fail closed)."""
        if not isinstance(raw, AccountState):
            raise TypeError(f"broker returned non-AccountState {raw!r}")
        if raw.retrieved_at_utc > now:
            raise ValueError(
                f"account snapshot is from the future ({raw.retrieved_at_utc.isoformat()})"
            )
        if now - raw.retrieved_at_utc > self._policy.max_reference_age:
            raise ValueError(f"account snapshot is stale ({raw.retrieved_at_utc.isoformat()})")
        return raw

    @staticmethod
    def _validate_protective_prices(
        request: ExecutionRequest,
        market: MarketSafetySnapshot,
        reject: Callable[[SafetyRejectionCode, str], None],
    ) -> None:
        """SL/TP side validation against the order-type-specific base price.

        LIMIT: base is ``requested_price`` (already guaranteed by the
        ``ExecutionRequest`` constructor; re-checked as defense in depth).
        MARKET: base is the expected fill side -- ask for BUY, bid for SELL --
        falling back to ``reference_price``. No base price -> nothing to
        check here (REFERENCE_PRICE_MISSING already rejects the order).
        """
        if request.order_type is OrderType.LIMIT:
            base = request.requested_price
        elif request.side is OrderSide.BUY:
            base = market.ask if market.ask is not None else market.reference_price
        else:
            base = market.bid if market.bid is not None else market.reference_price
        if base is None:
            return
        buying = request.side is OrderSide.BUY
        if request.stop_loss_price is not None:
            invalid_sl = request.stop_loss_price >= base if buying else request.stop_loss_price <= base
            if invalid_sl:
                reject(
                    SafetyRejectionCode.STOP_LOSS_INVALID,
                    f"stop loss {request.stop_loss_price} is not on the losing side of "
                    f"base price {base} for {request.side.value}",
                )
        if request.take_profit_price is not None:
            invalid_tp = request.take_profit_price <= base if buying else request.take_profit_price >= base
            if invalid_tp:
                reject(
                    SafetyRejectionCode.TAKE_PROFIT_INVALID,
                    f"take profit {request.take_profit_price} is not on the winning side of "
                    f"base price {base} for {request.side.value}",
                )
