"""Production input providers for Shadow Mode (Issue #78 / PR #100 review).

The PRODUCTION shadow path reads a STORED signal and resolves production
configuration through these provider boundaries -- it never accepts inline
fixture data. Contract (no permissive defaults, fail closed):

- a signal that cannot be found is an explicit :class:`ShadowInputError`;
- a missing / unreadable / schema-incomplete signal store is an explicit
  error, never an empty answer;
- market and account snapshots come from providers too; a missing snapshot
  is an explicit error (staleness is then judged by the Safety Gate with its
  normal fail-closed rules).

``StoredSignalProvider`` is a Protocol so the execution layer stays decoupled
from the Paper Signal component: :class:`PaperSignalDbSignalProvider` is the
production adapter reading the Paper Signal SQLite store (owned and written
by ``paper-signal/`` alone; this adapter opens it STRICTLY read-only via
``execution.sqlite_ro`` -- the same cross-component read-only pattern Paper
Execution uses for the Chart DB). The ``paper-signal`` directory is not an
importable package name, so the adapter speaks to the DB file, not the
module; the column set it needs is verified explicitly and any mismatch is an
explicit error.

Snapshot FILE providers load operator-maintained JSON snapshots (the
"injected context" Issue #78 prescribes while no live broker/market feed
exists): every field is REQUIRED -- absent prices, flags or timestamps are
input errors, never silently defaulted. Static in-memory providers exist for
composition from already-built snapshots (the scenario suite uses them).
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from broker.broker_models import AccountState, PositionInfo, PositionSide
from execution.safety_gate import AccountSafetySnapshot, MarketSafetySnapshot
from execution.sqlite_ro import ReadOnlyDbMissingError, connect_read_only
from execution.strict_json import load_strict_json_file


class ShadowInputError(RuntimeError):
    """A production shadow input could not be resolved (missing signal,
    missing snapshot, unreadable store, unresolved configuration). Always
    explicit -- the production path never substitutes defaults."""


SIGNAL_SIDE_LONG = "long"
SIGNAL_SIDE_SHORT = "short"
KNOWN_SIGNAL_SIDES = frozenset({SIGNAL_SIDE_LONG, SIGNAL_SIDE_SHORT})

# The CLOSED set of signal statuses the production shadow path may execute
# (PR #100 re-review Major 3). Today's Paper Signals are hypotheses stored as
# ``paper_only`` -- nothing else is runnable. Comparison is EXACT: no case
# folding, no trimming, no implicit normalization; NULL / empty / unknown /
# ``archived`` / case variants all fail closed with the signal id and the
# offending status in the error. A future runnable status is added HERE,
# deliberately.
EXECUTABLE_SIGNAL_STATUSES: frozenset[str] = frozenset({"paper_only"})


def require_executable_signal_status(signal_id: str, status) -> None:
    """Fail closed unless ``status`` is EXACTLY one of
    :data:`EXECUTABLE_SIGNAL_STATUSES` (see the constant's comment)."""
    if not isinstance(status, str) or status not in EXECUTABLE_SIGNAL_STATUSES:
        raise ShadowInputError(
            f"stored signal {signal_id!r} carries status {status!r}, which is not an "
            f"executable status ({sorted(EXECUTABLE_SIGNAL_STATUSES)}); refusing to "
            "build an execution request from it"
        )


def _require_non_empty_str(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


@dataclass(frozen=True)
class StoredSignal:
    """One stored signal as the execution layer consumes it (a projection of
    the Paper Signal row; signal-side vocabulary ``long`` / ``short``)."""

    signal_id: str
    created_at_utc: datetime
    symbol: str
    timeframe: str
    side: str
    strategy_id: str
    status: str

    def __post_init__(self) -> None:
        for name in ("signal_id", "symbol", "timeframe", "strategy_id", "status"):
            _require_non_empty_str(getattr(self, name), name)
        if self.side not in KNOWN_SIGNAL_SIDES:
            raise ValueError(
                f"side must be one of {sorted(KNOWN_SIGNAL_SIDES)}, got {self.side!r}"
            )
        if (
            not isinstance(self.created_at_utc, datetime)
            or self.created_at_utc.tzinfo is None
            or self.created_at_utc.utcoffset() is None
            or self.created_at_utc.utcoffset().total_seconds() != 0
        ):
            raise ValueError(
                f"created_at_utc must be a timezone-aware UTC datetime, got {self.created_at_utc!r}"
            )


class StoredSignalProvider(Protocol):
    """Where stored signals come from. ``source_id`` identifies the concrete
    store in the shadow record's provenance."""

    @property
    def source_id(self) -> str: ...

    def get_signal(self, signal_id: str) -> StoredSignal: ...


class MarketSnapshotProvider(Protocol):
    @property
    def source_id(self) -> str: ...

    def get_market_snapshot(self, symbol: str) -> MarketSafetySnapshot: ...


class AccountSnapshotProvider(Protocol):
    @property
    def source_id(self) -> str: ...

    def get_account_snapshot(self) -> AccountSafetySnapshot: ...


# --- stored signals: Paper Signal DB adapter ----------------------------------

_PAPER_SIGNALS_TABLE = "paper_signals"
_REQUIRED_SIGNAL_COLUMNS = (
    "signal_id",
    "created_at_utc",
    "symbol",
    "timeframe",
    "side",
    "strategy_profile",
    "status",
)


class PaperSignalDbSignalProvider:
    """Read-only adapter over the Paper Signal SQLite store (see the module
    docstring). Never creates, migrates or writes the DB."""

    def __init__(self, db_path: str | Path):
        if not isinstance(db_path, (str, Path)):
            raise ValueError(f"db_path must be a filesystem path, got {type(db_path).__name__}")
        self._db_path = Path(db_path)

    @property
    def source_id(self) -> str:
        return f"paper_signal_db:{self._db_path}"

    def get_signal(self, signal_id: str) -> StoredSignal:
        if not isinstance(signal_id, str) or not signal_id.strip():
            raise ShadowInputError(f"signal_id must be a non-empty string, got {signal_id!r}")
        try:
            connection = connect_read_only(self._db_path)
        except ReadOnlyDbMissingError as exc:
            raise ShadowInputError(
                f"paper signal DB {self._db_path} does not exist; cannot resolve stored "
                f"signal {signal_id!r}"
            ) from exc
        with closing(connection):
            try:
                columns = {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({_PAPER_SIGNALS_TABLE})")
                }
                missing = [name for name in _REQUIRED_SIGNAL_COLUMNS if name not in columns]
                if missing:
                    raise ShadowInputError(
                        f"paper signal DB {self._db_path} does not provide the required "
                        f"columns {missing} on {_PAPER_SIGNALS_TABLE!r}; refusing to guess"
                    )
                row = connection.execute(
                    f"SELECT {', '.join(_REQUIRED_SIGNAL_COLUMNS)} FROM {_PAPER_SIGNALS_TABLE} "
                    "WHERE signal_id = ?",
                    (signal_id,),
                ).fetchone()
            except ShadowInputError:
                raise
            except Exception as exc:
                raise ShadowInputError(
                    f"paper signal DB {self._db_path} could not be read: {exc}"
                ) from exc
        if row is None:
            raise ShadowInputError(
                f"stored signal {signal_id!r} does not exist in {self._db_path}"
            )
        # Status gate BEFORE any model construction (PR #100 re-review
        # Major 3): a non-executable signal never becomes a StoredSignal, so
        # no execution request, claim, decision or shadow record can follow.
        require_executable_signal_status(signal_id, row["status"])
        try:
            created_at = datetime.fromisoformat(row["created_at_utc"])
            if created_at.tzinfo is None:
                raise ValueError(f"stored timestamp {row['created_at_utc']!r} is naive")
            return StoredSignal(
                signal_id=row["signal_id"],
                created_at_utc=created_at,
                symbol=row["symbol"],
                timeframe=row["timeframe"],
                side=row["side"],
                strategy_id=row["strategy_profile"],
                status=row["status"],
            )
        except (ValueError, TypeError) as exc:
            raise ShadowInputError(
                f"stored signal {signal_id!r} in {self._db_path} failed validation: {exc}"
            ) from exc


# --- market / account snapshots -----------------------------------------------


@dataclass(frozen=True)
class StaticMarketSnapshotProvider:
    """An already-captured market snapshot (composition/tests). Requesting a
    different symbol than the snapshot covers is an input error."""

    snapshot: MarketSafetySnapshot
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, MarketSafetySnapshot):
            raise ValueError(f"snapshot must be a MarketSafetySnapshot, got {self.snapshot!r}")
        _require_non_empty_str(self.source, "source")

    @property
    def source_id(self) -> str:
        return self.source

    def get_market_snapshot(self, symbol: str) -> MarketSafetySnapshot:
        if symbol != self.snapshot.symbol:
            raise ShadowInputError(
                f"market snapshot source {self.source!r} covers {self.snapshot.symbol!r}, "
                f"not {symbol!r}"
            )
        return self.snapshot


@dataclass(frozen=True)
class StaticAccountSnapshotProvider:
    snapshot: AccountSafetySnapshot
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, AccountSafetySnapshot):
            raise ValueError(f"snapshot must be an AccountSafetySnapshot, got {self.snapshot!r}")
        _require_non_empty_str(self.source, "source")

    @property
    def source_id(self) -> str:
        return self.source

    def get_account_snapshot(self) -> AccountSafetySnapshot:
        return self.snapshot


_MARKET_FILE_KEYS = frozenset(
    {"symbol", "reference_price", "bid", "ask", "observed_at_utc", "market_open", "tradable"}
)
_ACCOUNT_FILE_KEYS = frozenset(
    {"account_id", "currency", "balance", "equity", "margin_available", "retrieved_at_utc",
     "positions"}
)
_POSITION_FILE_KEYS = frozenset(
    {"symbol", "side", "quantity", "average_entry_price", "retrieved_at_utc"}
)


def _file_decimal(mapping: dict, key: str, name: str, *, optional: bool = False) -> Decimal | None:
    if key not in mapping:
        raise ShadowInputError(f"{name}.{key} is required (no defaults on the production path)")
    value = mapping[key]
    if value is None:
        if optional:
            return None
        raise ShadowInputError(f"{name}.{key} must not be null")
    if not isinstance(value, str):
        raise ShadowInputError(f"{name}.{key} must be a decimal string, got {value!r}")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ShadowInputError(f"{name}.{key} is not a valid decimal string: {value!r}") from None


def _file_utc(mapping: dict, key: str, name: str) -> datetime:
    if key not in mapping:
        raise ShadowInputError(f"{name}.{key} is required (no defaults on the production path)")
    value = mapping[key]
    if not isinstance(value, str):
        raise ShadowInputError(f"{name}.{key} must be an ISO-8601 UTC string, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ShadowInputError(f"{name}.{key} is not a valid timestamp: {exc}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ShadowInputError(f"{name}.{key} must be timezone-aware UTC, got {value!r}")
    return parsed


def _file_bool(mapping: dict, key: str, name: str) -> bool:
    if key not in mapping:
        raise ShadowInputError(f"{name}.{key} is required (no defaults on the production path)")
    if not isinstance(mapping[key], bool):
        raise ShadowInputError(f"{name}.{key} must be a boolean, got {mapping[key]!r}")
    return mapping[key]


def _file_str(mapping: dict, key: str, name: str) -> str:
    if key not in mapping:
        raise ShadowInputError(f"{name}.{key} is required (no defaults on the production path)")
    value = mapping[key]
    if not isinstance(value, str) or not value.strip():
        raise ShadowInputError(f"{name}.{key} must be a non-empty string, got {value!r}")
    return value


class MarketSnapshotFileProvider:
    """Operator-maintained market snapshot JSON (every field required; bid /
    ask / reference_price may be explicit null when genuinely unavailable --
    the gate then rejects fail-closed, which is the honest outcome)."""

    def __init__(self, path: str | Path):
        self._path = Path(path)

    @property
    def source_id(self) -> str:
        return f"market_snapshot_file:{self._path}"

    def get_market_snapshot(self, symbol: str) -> MarketSafetySnapshot:
        raw = load_strict_json_file(
            self._path, description="market snapshot file", error_cls=ShadowInputError
        )
        if not isinstance(raw, dict) or set(raw) != _MARKET_FILE_KEYS:
            raise ShadowInputError(
                f"market snapshot file {self._path} must hold exactly the keys "
                f"{sorted(_MARKET_FILE_KEYS)}"
            )
        try:
            snapshot = MarketSafetySnapshot(
                symbol=_file_str(raw, "symbol", "market snapshot"),
                reference_price=_file_decimal(raw, "reference_price", "market snapshot", optional=True),
                bid=_file_decimal(raw, "bid", "market snapshot", optional=True),
                ask=_file_decimal(raw, "ask", "market snapshot", optional=True),
                observed_at_utc=_file_utc(raw, "observed_at_utc", "market snapshot"),
                market_open=_file_bool(raw, "market_open", "market snapshot"),
                tradable=_file_bool(raw, "tradable", "market snapshot"),
            )
        except ShadowInputError:
            raise
        except ValueError as exc:
            raise ShadowInputError(f"market snapshot file {self._path} is invalid: {exc}") from exc
        if snapshot.symbol != symbol:
            raise ShadowInputError(
                f"market snapshot file {self._path} covers {snapshot.symbol!r}, not {symbol!r}"
            )
        return snapshot


class AccountSnapshotFileProvider:
    """Operator-maintained account/position snapshot JSON (every field
    required; no fabricated balances, no defaults)."""

    def __init__(self, path: str | Path):
        self._path = Path(path)

    @property
    def source_id(self) -> str:
        return f"account_snapshot_file:{self._path}"

    def get_account_snapshot(self) -> AccountSafetySnapshot:
        raw = load_strict_json_file(
            self._path, description="account snapshot file", error_cls=ShadowInputError
        )
        if not isinstance(raw, dict) or set(raw) != _ACCOUNT_FILE_KEYS:
            raise ShadowInputError(
                f"account snapshot file {self._path} must hold exactly the keys "
                f"{sorted(_ACCOUNT_FILE_KEYS)}"
            )
        positions_raw = raw["positions"]
        if not isinstance(positions_raw, list):
            raise ShadowInputError(f"account snapshot file {self._path}: positions must be a list")
        positions = []
        for index, entry in enumerate(positions_raw):
            name = f"account snapshot positions[{index}]"
            if not isinstance(entry, dict) or set(entry) != _POSITION_FILE_KEYS:
                raise ShadowInputError(
                    f"{name} must hold exactly the keys {sorted(_POSITION_FILE_KEYS)}"
                )
            side_raw = entry.get("side")
            try:
                side = PositionSide(side_raw)
            except ValueError:
                raise ShadowInputError(
                    f"{name}.side must be one of "
                    f"{sorted(item.value for item in PositionSide)}, got {side_raw!r}"
                ) from None
            try:
                positions.append(
                    PositionInfo(
                        symbol=_file_str(entry, "symbol", name),
                        side=side,
                        quantity=_file_decimal(entry, "quantity", name),
                        average_entry_price=_file_decimal(entry, "average_entry_price", name),
                        retrieved_at_utc=_file_utc(entry, "retrieved_at_utc", name),
                    )
                )
            except ShadowInputError:
                raise
            except Exception as exc:
                raise ShadowInputError(f"{name} is invalid: {exc}") from exc
        try:
            account = AccountState(
                account_id=_file_str(raw, "account_id", "account snapshot"),
                currency=_file_str(raw, "currency", "account snapshot"),
                balance=_file_decimal(raw, "balance", "account snapshot"),
                equity=_file_decimal(raw, "equity", "account snapshot"),
                margin_available=_file_decimal(raw, "margin_available", "account snapshot"),
                retrieved_at_utc=_file_utc(raw, "retrieved_at_utc", "account snapshot"),
            )
        except ShadowInputError:
            raise
        except Exception as exc:
            raise ShadowInputError(f"account snapshot file {self._path} is invalid: {exc}") from exc
        return AccountSafetySnapshot(positions=tuple(positions), account=account)
