"""Shadow Mode cycle runner CLI (Issue #78; production-path rework PR #100):
run ONE execution cycle through the production Shadow service boundary.

*** NOTHING HERE SUBMITS AN ORDER OR TOUCHES A BROKER, and nothing here
re-implements a production decision: the runner only resolves inputs and
calls the production composition root / service. ***

Two clearly separated input paths (recorded as ``input_origin`` provenance):

- PRODUCTION SIGNAL PATH (``--signal-id``): reads a STORED signal from the
  Paper Signal store (``--signal-db``), the production execution
  configuration (``--execution-config``) and provider-maintained market /
  account snapshot files (``--market-snapshot`` / ``--account-snapshot``),
  then runs ``execution.shadow_composition.run_production_shadow_cycle``.
  NO permissive defaults exist here: a missing signal, an unconfigured
  symbol, an absent snapshot field or an incomplete configuration is an
  explicit failure. Records carry ``input_origin="production_signal"``.
- CLI FIXTURE PATH (``--cycle``): a hand-authored cycle-input JSON (request /
  market / account / policy inline). Fixture-only conveniences (default
  policy values, placeholder account) are allowed and the record is labelled
  ``input_origin="cli_fixture"`` -- it never counts as the real signal path.

Mode dispatch (validated by ``resolve_execution_mode`` -- unknown modes are
refused at startup): ``shadow`` runs and persists; ``dry-run`` evaluates with
ZERO side effects (no DB creation / write; existing DBs probed via TRUE
read-only connections); ``fake`` / ``demo`` / ``live`` are refused explicitly.

All JSON inputs go through the strict loader (``execution.strict_json``):
duplicate object keys (nested included), invalid UTF-8 / BOM, files over
256 KiB, nesting deeper than 32, oversized strings/arrays/objects and
``NaN`` / ``Infinity`` are stable usage errors -- never tracebacks. Decimal
strings additionally reject non-finite values, out-of-range exponents, and
booleans-as-numbers (prices/quantities must be strings).

Exit codes: 0 = cycle completed (RECORDED, an explicit DUPLICATE, or a
dry-run evaluation -- a Safety Gate REJECTION is a recorded outcome, not a
failure); 1 = cycle failure (identity CONFLICT with reason code
``shadow_payload_mismatch``, unresolved production inputs, store/schema
errors); 2 = usage / configuration errors (invalid mode, invalid cycle
input, invalid production configuration). ``--json`` prints a pure-ASCII
machine-readable outcome on stdout with no log lines mixed in.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from broker.broker_models import AccountState, OrderSide, OrderType, PositionInfo, PositionSide
from execution import shadow_store
from execution.cli_streams import encoding_safe_streams
from execution.execution_models import ExecutionRequest
from execution.safety_gate import (
    AccountSafetySnapshot,
    ExecutionMode,
    LOCKED_BROKER_MODES,
    MarketSafetySnapshot,
    SafetyGate,
    SafetyPolicy,
    StaticSafetyState,
    UNIMPLEMENTED_BROKER_MODES,
    resolve_execution_mode,
)
from execution.safety_store import safety_db_path
from execution.shadow_composition import run_production_shadow_cycle
from execution.shadow_config import ShadowConfigError, load_shadow_execution_config
from execution.shadow_execution import ShadowExecutionService
from execution.shadow_models import INPUT_ORIGIN_CLI_FIXTURE, ShadowProvenance
from execution.shadow_providers import (
    AccountSnapshotFileProvider,
    MarketSnapshotFileProvider,
    PaperSignalDbSignalProvider,
    ShadowInputError,
)
from execution.shadow_report import claim_payload, result_payload
from execution.strict_json import load_strict_json_file

EXIT_OK = 0
EXIT_CYCLE_FAILURE = 1
EXIT_USAGE_ERROR = 2

# Bound on decimal exponents in fixture inputs (PR #100 review: giant
# exponent spellings like 1E+999999 are refused even though Decimal could
# represent them).
MAX_DECIMAL_EXPONENT = 28

# Placeholder identity used when the FIXTURE cycle input carries no account
# block: clearly non-broker values (the gate only consumes the id and the
# snapshot freshness; balances are neither checked nor persisted as truth).
_DEFAULT_ACCOUNT_ID = "shadow-unverified-account"
_DEFAULT_ACCOUNT_CURRENCY = "XXX"

_REQUEST_REQUIRED_KEYS = frozenset(
    {"execution_request_id", "signal_id", "strategy_id", "symbol", "side", "order_type", "quantity"}
)
_REQUEST_OPTIONAL_KEYS = frozenset(
    {"requested_price", "stop_loss_price", "take_profit_price", "requested_at_utc"}
)
_MARKET_KEYS = frozenset(
    {"symbol", "reference_price", "bid", "ask", "observed_at_utc", "market_open", "tradable"}
)
_ACCOUNT_KEYS = frozenset(
    {"account_id", "currency", "balance", "equity", "margin_available", "retrieved_at_utc", "positions"}
)
_POSITION_KEYS = frozenset({"symbol", "side", "quantity", "average_entry_price", "retrieved_at_utc"})
_POLICY_KEYS = frozenset(
    {
        "policy_id",
        "allowed_symbols",
        "allowed_sides",
        "max_order_quantity",
        "max_positions_per_symbol",
        "max_positions_account",
        "max_reference_age_seconds",
        "max_spread_by_symbol",
        "allowed_shadow_targets",
        "require_stop_loss",
    }
)
_CYCLE_KEYS = frozenset(
    {"request", "client_order_id", "timeframe", "market", "account", "policy", "kill_switch"}
)


class CycleInputError(ValueError):
    """The cycle input file violates the closed-world input schema."""


def _require_mapping(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise CycleInputError(f"{name} must be an object, got {type(value).__name__}")
    return value


def _require_known_keys(mapping: dict, allowed: frozenset[str], name: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise CycleInputError(
            f"{name} has unknown keys {sorted(unknown)}; allowed: {sorted(allowed)}"
        )


def _require_str(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CycleInputError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _decimal(value, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str):
        raise CycleInputError(f"{name} must be a decimal string (numbers/booleans are refused), got {value!r}")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise CycleInputError(f"{name} is not a valid decimal string: {value!r}") from None
    if not parsed.is_finite():
        raise CycleInputError(f"{name} must be finite, got {value!r}")
    if parsed != 0 and abs(parsed.adjusted()) > MAX_DECIMAL_EXPONENT:
        raise CycleInputError(
            f"{name} exponent is out of the accepted range (+/-{MAX_DECIMAL_EXPONENT}), got {value!r}"
        )
    return parsed


def _optional_decimal(mapping: dict, key: str, name: str) -> Decimal | None:
    if key not in mapping or mapping[key] is None:
        return None
    return _decimal(mapping[key], name)


def _utc(value, name: str) -> datetime:
    if not isinstance(value, str):
        raise CycleInputError(f"{name} must be an ISO-8601 UTC string, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CycleInputError(f"{name} is not a valid ISO-8601 timestamp: {exc}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise CycleInputError(f"{name} must be timezone-aware UTC (+00:00), got {value!r}")
    return parsed


def _utc_or_now(mapping: dict, key: str, name: str, now: datetime) -> datetime:
    if key not in mapping or mapping[key] is None:
        return now
    return _utc(mapping[key], name)


def _bool(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise CycleInputError(f"{name} must be a boolean, got {value!r}")
    return value


def _build_request(spec, now: datetime) -> ExecutionRequest:
    request = _require_mapping(spec, "request")
    _require_known_keys(request, _REQUEST_REQUIRED_KEYS | _REQUEST_OPTIONAL_KEYS, "request")
    missing = _REQUEST_REQUIRED_KEYS - set(request)
    if missing:
        raise CycleInputError(f"request is missing required keys {sorted(missing)}")
    try:
        side = OrderSide(request["side"])
    except ValueError:
        raise CycleInputError(
            f"request.side must be one of {sorted(item.value for item in OrderSide)}, "
            f"got {request['side']!r}"
        ) from None
    try:
        order_type = OrderType(request["order_type"])
    except ValueError:
        raise CycleInputError(
            f"request.order_type must be one of {sorted(item.value for item in OrderType)}, "
            f"got {request['order_type']!r}"
        ) from None
    try:
        return ExecutionRequest(
            execution_request_id=_require_str(request["execution_request_id"], "request.execution_request_id"),
            signal_id=_require_str(request["signal_id"], "request.signal_id"),
            strategy_id=_require_str(request["strategy_id"], "request.strategy_id"),
            symbol=_require_str(request["symbol"], "request.symbol"),
            side=side,
            order_type=order_type,
            quantity=_decimal(request["quantity"], "request.quantity"),
            requested_at_utc=_utc_or_now(request, "requested_at_utc", "request.requested_at_utc", now),
            requested_price=_optional_decimal(request, "requested_price", "request.requested_price"),
            stop_loss_price=_optional_decimal(request, "stop_loss_price", "request.stop_loss_price"),
            take_profit_price=_optional_decimal(
                request, "take_profit_price", "request.take_profit_price"
            ),
        )
    except ValueError as exc:
        if isinstance(exc, CycleInputError):
            raise
        raise CycleInputError(f"request is invalid: {exc}") from exc


def _build_market(spec, request: ExecutionRequest, now: datetime) -> MarketSafetySnapshot:
    market = _require_mapping(spec, "market")
    _require_known_keys(market, _MARKET_KEYS, "market")
    try:
        return MarketSafetySnapshot(
            symbol=_require_str(market.get("symbol", request.symbol), "market.symbol"),
            reference_price=_optional_decimal(market, "reference_price", "market.reference_price"),
            bid=_optional_decimal(market, "bid", "market.bid"),
            ask=_optional_decimal(market, "ask", "market.ask"),
            observed_at_utc=_utc_or_now(market, "observed_at_utc", "market.observed_at_utc", now),
            market_open=_bool(market.get("market_open", True), "market.market_open"),
            tradable=_bool(market.get("tradable", True), "market.tradable"),
        )
    except ValueError as exc:
        if isinstance(exc, CycleInputError):
            raise
        raise CycleInputError(f"market is invalid: {exc}") from exc


def _build_account(spec, now: datetime) -> AccountSafetySnapshot:
    account = _require_mapping(spec, "account") if spec is not None else {}
    _require_known_keys(account, _ACCOUNT_KEYS, "account")
    positions = []
    for index, entry in enumerate(account.get("positions", [])):
        entry = _require_mapping(entry, f"account.positions[{index}]")
        _require_known_keys(entry, _POSITION_KEYS, f"account.positions[{index}]")
        try:
            position_side = PositionSide(entry.get("side"))
        except ValueError:
            raise CycleInputError(
                f"account.positions[{index}].side must be one of "
                f"{sorted(item.value for item in PositionSide)}, got {entry.get('side')!r}"
            ) from None
        try:
            positions.append(
                PositionInfo(
                    symbol=_require_str(entry.get("symbol"), f"account.positions[{index}].symbol"),
                    side=position_side,
                    quantity=_decimal(entry.get("quantity"), f"account.positions[{index}].quantity"),
                    average_entry_price=_decimal(
                        entry.get("average_entry_price"),
                        f"account.positions[{index}].average_entry_price",
                    ),
                    retrieved_at_utc=_utc_or_now(
                        entry, "retrieved_at_utc", f"account.positions[{index}].retrieved_at_utc", now
                    ),
                )
            )
        except Exception as exc:
            if isinstance(exc, CycleInputError):
                raise
            raise CycleInputError(f"account.positions[{index}] is invalid: {exc}") from exc
    try:
        state = AccountState(
            account_id=_require_str(
                account.get("account_id", _DEFAULT_ACCOUNT_ID), "account.account_id"
            ),
            currency=_require_str(
                account.get("currency", _DEFAULT_ACCOUNT_CURRENCY), "account.currency"
            ),
            balance=_decimal(account.get("balance", "0"), "account.balance"),
            equity=_decimal(account.get("equity", "0"), "account.equity"),
            margin_available=_decimal(account.get("margin_available", "0"), "account.margin_available"),
            retrieved_at_utc=_utc_or_now(account, "retrieved_at_utc", "account.retrieved_at_utc", now),
        )
    except Exception as exc:
        if isinstance(exc, CycleInputError):
            raise
        raise CycleInputError(f"account is invalid: {exc}") from exc
    return AccountSafetySnapshot(positions=tuple(positions), account=state)


def _build_policy(spec, request: ExecutionRequest) -> SafetyPolicy:
    policy = _require_mapping(spec, "policy") if spec is not None else {}
    _require_known_keys(policy, _POLICY_KEYS, "policy")
    allowed_symbols = policy.get("allowed_symbols", [request.symbol])
    if not isinstance(allowed_symbols, list) or not allowed_symbols:
        raise CycleInputError("policy.allowed_symbols must be a non-empty list of strings")
    symbols = frozenset(
        _require_str(symbol, "policy.allowed_symbols entry") for symbol in allowed_symbols
    )
    sides_spec = policy.get("allowed_sides", [side.value for side in OrderSide])
    if not isinstance(sides_spec, list) or not sides_spec:
        raise CycleInputError("policy.allowed_sides must be a non-empty list")
    try:
        sides = frozenset(OrderSide(side) for side in sides_spec)
    except ValueError:
        raise CycleInputError(
            f"policy.allowed_sides entries must be in {sorted(item.value for item in OrderSide)}"
        ) from None
    default_spread = {symbol: Decimal("0.05") for symbol in symbols}
    spread_spec = policy.get("max_spread_by_symbol", {})
    spread_spec = _require_mapping(spread_spec, "policy.max_spread_by_symbol")
    spread_overrides = {
        _require_str(symbol, "policy.max_spread_by_symbol key"): _decimal(
            value, f"policy.max_spread_by_symbol[{symbol}]"
        )
        for symbol, value in spread_spec.items()
    }
    targets_spec = policy.get("allowed_shadow_targets", ["shadow"])
    if not isinstance(targets_spec, list) or not targets_spec:
        raise CycleInputError("policy.allowed_shadow_targets must be a non-empty list of strings")
    targets = frozenset(
        _require_str(target, "policy.allowed_shadow_targets entry") for target in targets_spec
    )
    for key in ("max_positions_per_symbol", "max_positions_account", "max_reference_age_seconds"):
        if key in policy and (
            not isinstance(policy[key], int) or isinstance(policy[key], bool) or policy[key] < 0
        ):
            raise CycleInputError(f"policy.{key} must be an integer >= 0, got {policy[key]!r}")
    try:
        return SafetyPolicy(
            policy_id=_require_str(policy.get("policy_id", "shadow-cli-fixture-policy"), "policy.policy_id"),
            allowed_symbols=symbols,
            allowed_sides=sides,
            max_order_quantity=_decimal(
                policy.get("max_order_quantity", "10"), "policy.max_order_quantity"
            ),
            max_positions_per_symbol=policy.get("max_positions_per_symbol", 2),
            max_positions_account=policy.get("max_positions_account", 5),
            max_reference_age=timedelta(seconds=policy.get("max_reference_age_seconds", 30)),
            max_spread_by_symbol={**default_spread, **spread_overrides},
            allowed_connection_targets={ExecutionMode.SHADOW: targets},
            require_stop_loss=_bool(policy.get("require_stop_loss", True), "policy.require_stop_loss"),
        )
    except ValueError as exc:
        if isinstance(exc, CycleInputError):
            raise
        raise CycleInputError(f"policy is invalid: {exc}") from exc


def load_cycle_input(path: Path, now: datetime) -> dict:
    """Load and validate ONE FIXTURE cycle-input file into production
    objects (strict JSON loader: duplicate keys, UTF-8, size/depth limits)."""
    raw = load_strict_json_file(path, description="cycle input", error_cls=CycleInputError)
    cycle = _require_mapping(raw, "cycle input")
    _require_known_keys(cycle, _CYCLE_KEYS, "cycle input")
    for key in ("request", "client_order_id", "market"):
        if key not in cycle:
            raise CycleInputError(f"cycle input is missing the required key {key!r}")
    request = _build_request(cycle["request"], now)
    timeframe = cycle.get("timeframe")
    if timeframe is not None:
        timeframe = _require_str(timeframe, "timeframe")
    return {
        "request": request,
        "client_order_id": _require_str(cycle["client_order_id"], "client_order_id"),
        "timeframe": timeframe,
        "market": _build_market(cycle["market"], request, now),
        "account": _build_account(cycle.get("account"), now),
        "policy": _build_policy(cycle.get("policy"), request),
        "kill_switch": _bool(cycle.get("kill_switch", False), "kill_switch"),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m execution.shadow_runner",
        description=(
            "Run ONE execution cycle in Shadow Mode (Issue #78): the real "
            "decision path with zero broker calls. --signal-id runs the "
            "PRODUCTION stored-signal path; --cycle runs a labelled CLI fixture."
        ),
    )
    parser.add_argument(
        "--mode",
        default=ExecutionMode.SHADOW.value,
        help="execution mode (shadow / dry-run; fake, demo and live are refused here)",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--signal-id",
        default=None,
        help="PRODUCTION path: run the stored signal with this id (requires --signal-db, "
        "--execution-config, --market-snapshot, --account-snapshot; no defaults)",
    )
    source.add_argument(
        "--cycle",
        type=Path,
        default=None,
        help="FIXTURE path: hand-authored cycle input JSON (recorded as input_origin=cli_fixture)",
    )
    parser.add_argument("--signal-db", type=Path, default=None, help="paper signal DB path (production path)")
    parser.add_argument(
        "--execution-config", type=Path, default=None,
        help="production execution configuration JSON (production path)",
    )
    parser.add_argument(
        "--market-snapshot", type=Path, default=None,
        help="market snapshot JSON file (production path)",
    )
    parser.add_argument(
        "--account-snapshot", type=Path, default=None,
        help="account/position snapshot JSON file (production path)",
    )
    parser.add_argument(
        "--shadow-db",
        type=Path,
        default=None,
        help="shadow result DB path (default: SHADOW_RESULT_DB_PATH or the packaged default)",
    )
    parser.add_argument(
        "--decision-db",
        type=Path,
        default=None,
        help="safety decision DB path (default: SAFETY_GATE_DECISION_DB_PATH or the packaged default)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="directory holding both DBs (shadow_results.sqlite3 / safety_gate_decisions.sqlite3); "
        "overridden by --shadow-db / --decision-db",
    )
    parser.add_argument(
        "--owner-id",
        default=None,
        help="claim owner identity (default: a per-process value; fixed values make "
        "crash-retry recovery deterministic for operations)",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="fixed ISO-8601 UTC evaluation clock for deterministic verification runs "
        "(default: the current UTC time)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print a machine-readable JSON outcome to stdout"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    with encoding_safe_streams():
        return _run_cli(argv)


def _resolve_db_paths(args) -> tuple[Path, Path]:
    shadow_db = args.shadow_db
    decision_db = args.decision_db
    if args.data_dir is not None:
        shadow_db = shadow_db if shadow_db is not None else args.data_dir / "shadow_results.sqlite3"
        decision_db = (
            decision_db if decision_db is not None else args.data_dir / "safety_gate_decisions.sqlite3"
        )
    if shadow_db is None:
        shadow_db = shadow_store.shadow_db_path()
    if decision_db is None:
        decision_db = safety_db_path()
    return Path(shadow_db), Path(decision_db)


def _run_cli(argv: list[str] | None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        mode = resolve_execution_mode(args.mode)
    except ValueError as exc:
        print(f"invalid execution mode: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR
    if mode in UNIMPLEMENTED_BROKER_MODES:
        print(
            f"execution mode {mode.value!r} is defined but NOT implemented (no broker "
            "adapter exists); refusing to run",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR
    if mode in LOCKED_BROKER_MODES:
        # Issue #82: a live connection exists only behind the live activation
        # lock and is composed only by python -m execution.live_startup_cli.
        # The shadow runner never submits, so it refuses the mode outright
        # rather than silently running something else under a live label.
        print(
            f"execution mode {mode.value!r} is locked behind the live activation lock "
            "(python -m execution.live_startup_cli); the shadow runner never submits",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR
    if mode is ExecutionMode.FAKE:
        print(
            "execution mode 'fake' submits via LiveOrderSubmissionService (exercised by "
            "python -m execution.scenario_runner); the shadow runner never submits",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR
    if mode is ExecutionMode.DEMO:
        # Issue #132: ``demo`` became a real submitting mode. It is refused
        # here for the same reason ``fake`` is -- the shadow runner never
        # submits -- and refusing it EXPLICITLY matters more now that it no
        # longer falls into UNIMPLEMENTED_BROKER_MODES: a shadow cycle must
        # never run under a label that implies a demo account was contacted.
        print(
            "execution mode 'demo' submits to the OANDA practice account via "
            "LiveOrderSubmissionService (composed by execution/demo_lock.py); the shadow "
            "runner never submits",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR

    if args.now is not None:
        try:
            fixed_now = _utc(args.now, "--now")
        except CycleInputError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE_ERROR
        clock = lambda: fixed_now  # noqa: E731
    else:
        clock = lambda: datetime.now(timezone.utc)  # noqa: E731

    shadow_db, decision_db = _resolve_db_paths(args)
    dry_run = mode is ExecutionMode.DRY_RUN

    try:
        if args.signal_id is not None:
            outcome = _run_production(args, shadow_db, decision_db, clock, dry_run)
        else:
            outcome = _run_fixture(args, shadow_db, decision_db, clock, dry_run)
    except CycleInputError as exc:
        print(f"cycle input error: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR
    except ShadowConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR
    except shadow_store.ShadowIdentityConflictError as exc:
        # Machine-readable conflict record on stderr; a changed payload must
        # never silently return the stored record (PR #100 review).
        print(
            f"shadow identity conflict [{exc.reason_code}]: {exc} "
            f"(differing fields: {', '.join(exc.differing_fields)})",
            file=sys.stderr,
        )
        return EXIT_CYCLE_FAILURE
    except ShadowInputError as exc:
        print(f"production input error: {exc}", file=sys.stderr)
        return EXIT_CYCLE_FAILURE
    except Exception as exc:
        # Store/schema failures and unexpected errors are cycle failures --
        # never reported as a successful (or silently skipped) cycle.
        print(f"shadow cycle failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_CYCLE_FAILURE

    return _render_outcome(args, mode, outcome, shadow_db, decision_db)


def _run_production(args, shadow_db: Path, decision_db: Path, clock, dry_run: bool):
    missing = [
        flag
        for flag, value in (
            ("--signal-db", args.signal_db),
            ("--execution-config", args.execution_config),
            ("--market-snapshot", args.market_snapshot),
            ("--account-snapshot", args.account_snapshot),
        )
        if value is None
    ]
    if missing:
        raise ShadowConfigError(
            f"the production signal path requires {', '.join(missing)} (no defaults)"
        )
    config = load_shadow_execution_config(args.execution_config)
    return run_production_shadow_cycle(
        args.signal_id,
        signal_provider=PaperSignalDbSignalProvider(args.signal_db),
        config=config,
        market_provider=MarketSnapshotFileProvider(args.market_snapshot),
        account_provider=AccountSnapshotFileProvider(args.account_snapshot),
        shadow_db_path=shadow_db,
        decision_db_path=decision_db,
        clock=clock,
        owner_id=args.owner_id,
        dry_run=dry_run,
    )


def _run_fixture(args, shadow_db: Path, decision_db: Path, clock, dry_run: bool):
    now = clock()
    cycle = load_cycle_input(args.cycle, now)
    fixture_source = f"{INPUT_ORIGIN_CLI_FIXTURE}:{args.cycle}"
    provenance = ShadowProvenance(
        input_origin=INPUT_ORIGIN_CLI_FIXTURE,
        signal_source=fixture_source,
        config_source=fixture_source,
        config_version=cycle["policy"].policy_id,
        market_source=fixture_source,
        account_source=fixture_source,
        signal_observed_at_utc=cycle["request"].requested_at_utc,
    )
    gate = SafetyGate(
        policy=cycle["policy"],
        state_provider=StaticSafetyState(kill_switch_enabled=cycle["kill_switch"]),
        clock=clock,
    )
    service = ShadowExecutionService(
        gate=gate,
        decision_db_path=decision_db,
        shadow_db_path=shadow_db,
        clock=clock,
        owner_id=args.owner_id,
    )
    kwargs = dict(
        market=cycle["market"],
        account=cycle["account"],
        client_order_id=cycle["client_order_id"],
        provenance=provenance,
        timeframe=cycle["timeframe"],
    )
    if dry_run:
        return service.dry_run_cycle(cycle["request"], **kwargs)
    service.initialize_stores()
    return service.run_cycle(cycle["request"], **kwargs)


def _render_outcome(args, mode, outcome, shadow_db: Path, decision_db: Path) -> int:
    result = outcome.result
    if args.json:
        payload = {
            "mode": mode.value,
            "status": outcome.status.value,
            "shadow_db": str(shadow_db),
            "decision_db": str(decision_db),
            "result": None if result is None else result_payload(result),
            "claim": None if outcome.claim is None else claim_payload(outcome.claim),
        }
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return EXIT_OK

    if result is None:
        claim = outcome.claim
        print(
            f"{outcome.status.value.upper()} {claim.execution_request_id} mode={mode.value}"
        )
        print(
            f"  duplicate: a payload-equal cycle is in flight (claim held by "
            f"{claim.claim_owner_id} until {claim.lease_expires_at_utc.isoformat()}); "
            "nothing was evaluated or persisted by this call"
        )
        return EXIT_OK

    verdict = "ALLOWED" if result.allowed else "REJECTED"
    print(
        f"{outcome.status.value.upper()} {result.request.execution_request_id} "
        f"gate={verdict} mode={mode.value} origin={result.provenance.input_origin}"
    )
    print(
        f"  signal={result.request.signal_id} {result.request.symbol} "
        f"{result.request.side.value}/{result.request.order_type.value} "
        f"qty={result.request.quantity} timeframe={result.timeframe}"
    )
    provenance = result.provenance
    print(
        f"  provenance: signal={provenance.signal_source} config={provenance.config_source} "
        f"(version {provenance.config_version}) market={provenance.market_source} "
        f"account={provenance.account_source}"
    )
    intended = result.intended_order
    print(
        f"  intended order (NOT submitted): client_order_id={intended.client_order_id} "
        f"limit_price={intended.limit_price}"
    )
    for entry in result.decision.rejections:
        print(f"  - {entry.code.value}: {entry.message}")
    if outcome.status.value == "recorded":
        print(
            f"  shadow_result_id={result.shadow_result_id} "
            f"safety_decision_id={result.safety_decision_id} policy_id={result.decision.policy_id}"
        )
    elif outcome.status.value == "duplicate":
        print(
            f"  duplicate: existing shadow_result_id={result.shadow_result_id} was returned; "
            "no second record was created and no decision was written"
        )
    else:
        print("  dry-run: nothing was persisted, no DB file was created")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
