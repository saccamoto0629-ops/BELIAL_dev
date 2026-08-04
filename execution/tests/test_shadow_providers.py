"""Issue #78 / PR #100: the production shadow input path -- the stored-signal
provider (paper-signal DB adapter, read-only), the strict execution
configuration, the snapshot file providers (no permissive defaults), the
Signal -> ExecutionRequest builder and the composition root."""

import json
import sqlite3
from decimal import Decimal

import pytest

from broker.broker_models import OrderSide, OrderType
from execution import shadow_store
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.shadow_composition import (
    CLIENT_ORDER_ID_PREFIX,
    EXECUTION_REQUEST_ID_PREFIX,
    build_execution_request_from_signal,
    run_production_shadow_cycle,
)
from execution.shadow_config import (
    ShadowConfigError,
    load_shadow_execution_config,
    parse_shadow_execution_config,
)
from execution.shadow_execution import ShadowCycleStatus
from execution.shadow_providers import (
    AccountSnapshotFileProvider,
    EXECUTABLE_SIGNAL_STATUSES,
    MarketSnapshotFileProvider,
    PaperSignalDbSignalProvider,
    ShadowInputError,
    StaticAccountSnapshotProvider,
    StaticMarketSnapshotProvider,
    StoredSignal,
    require_executable_signal_status,
)
from shadow_helpers import NOW, make_account, make_market


def seed_signal_db(tmp_path, rows=None):
    """Paper-signal fixture DB with the real column set the adapter reads."""
    db = tmp_path / "paper_signals.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE paper_signals ("
        "signal_id TEXT PRIMARY KEY, created_at_utc TEXT NOT NULL, symbol TEXT NOT NULL, "
        "timeframe TEXT NOT NULL, side TEXT NOT NULL, market_bias TEXT, confidence REAL, "
        "strategy_profile TEXT NOT NULL, status TEXT NOT NULL)"
    )
    for row in rows if rows is not None else [default_signal_row()]:
        connection.execute(
            "INSERT INTO paper_signals (signal_id, created_at_utc, symbol, timeframe, side, "
            "strategy_profile, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    connection.commit()
    connection.close()
    return db


def default_signal_row(signal_id="sig-1", side="long"):
    return (signal_id, NOW.isoformat(), "USD_JPY", "H1", side, "strategy-prod-1", "paper_only")


CONFIG_RAW = {
    "config_id": "prod-config-1",
    "policy": {
        "policy_id": "prod-policy-1",
        "allowed_symbols": ["USD_JPY"],
        "allowed_sides": ["buy", "sell"],
        "max_order_quantity": "10",
        "max_positions_per_symbol": 2,
        "max_positions_account": 5,
        "max_reference_age_seconds": 30,
        "max_spread_by_symbol": {"USD_JPY": "0.05"},
        "allowed_shadow_targets": ["shadow"],
        "require_stop_loss": False,
    },
    "order_quantities": {"USD_JPY": "2"},
    "kill_switch": False,
    "connection_target": "shadow",
}


def make_config(**overrides):
    raw = json.loads(json.dumps(CONFIG_RAW))
    raw.update(overrides)
    return parse_shadow_execution_config(raw, source="test-config")


# --- stored signal provider -----------------------------------------------------


def test_signal_provider_reads_a_stored_signal(tmp_path):
    provider = PaperSignalDbSignalProvider(seed_signal_db(tmp_path))
    signal = provider.get_signal("sig-1")
    assert signal == StoredSignal(
        signal_id="sig-1", created_at_utc=NOW, symbol="USD_JPY", timeframe="H1",
        side="long", strategy_id="strategy-prod-1", status="paper_only",
    )
    assert provider.source_id.startswith("paper_signal_db:")


def test_signal_provider_missing_signal_is_an_explicit_error(tmp_path):
    provider = PaperSignalDbSignalProvider(seed_signal_db(tmp_path))
    with pytest.raises(ShadowInputError, match="does not exist"):
        provider.get_signal("nope")


def test_signal_provider_missing_db_is_an_explicit_error_and_creates_nothing(tmp_path):
    db = tmp_path / "missing.sqlite3"
    provider = PaperSignalDbSignalProvider(db)
    with pytest.raises(ShadowInputError, match="does not exist"):
        provider.get_signal("sig-1")
    assert not db.exists()


def test_signal_provider_refuses_a_schema_without_the_required_columns(tmp_path):
    db = tmp_path / "odd.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE paper_signals (signal_id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    with pytest.raises(ShadowInputError, match="required"):
        PaperSignalDbSignalProvider(db).get_signal("sig-1")


def test_signal_provider_is_read_only(tmp_path):
    db = seed_signal_db(tmp_path)
    before = db.read_bytes()
    PaperSignalDbSignalProvider(db).get_signal("sig-1")
    assert db.read_bytes() == before


# --- executable-status closed set (PR #100 re-review Major 3) --------------------


def test_paper_only_is_the_entire_executable_status_set():
    assert EXECUTABLE_SIGNAL_STATUSES == frozenset({"paper_only"})
    require_executable_signal_status("sig-1", "paper_only")  # exact match passes


@pytest.mark.parametrize(
    "status",
    [
        "archived",
        "invalid",
        "executed",
        "some-future-status",
        "",
        "   ",
        None,
        "PAPER_ONLY",
        "Paper_Only",
        " paper_only",
        "paper_only ",
        "paper_only\n",
    ],
)
def test_non_executable_statuses_fail_closed_with_the_signal_id(status):
    with pytest.raises(ShadowInputError) as excinfo:
        require_executable_signal_status("sig-9", status)
    message = str(excinfo.value)
    assert "sig-9" in message and repr(status) in message


def test_signal_provider_refuses_a_non_executable_status_row(tmp_path):
    rows = [
        default_signal_row(),
        ("sig-arch", NOW.isoformat(), "USD_JPY", "H1", "long", "strategy-prod-1", "archived"),
    ]
    provider = PaperSignalDbSignalProvider(seed_signal_db(tmp_path, rows))
    with pytest.raises(ShadowInputError, match="'archived'"):
        provider.get_signal("sig-arch")
    assert provider.get_signal("sig-1").status == "paper_only"  # closed set still passes


def test_builder_refuses_a_smuggled_status_from_a_custom_provider():
    # Even if a custom StoredSignalProvider skipped the provider-side gate,
    # the production request builder itself refuses the status.
    signal = StoredSignal(
        signal_id="sig-x", created_at_utc=NOW, symbol="USD_JPY", timeframe="H1",
        side="long", strategy_id="s", status="archived",
    )
    with pytest.raises(ShadowInputError, match="archived"):
        build_execution_request_from_signal(signal, quantity=Decimal("1"))


def test_production_cycle_rejects_a_bad_status_and_creates_nothing(tmp_path):
    rows = [
        ("sig-bad", NOW.isoformat(), "USD_JPY", "H1", "long", "strategy-prod-1", "archived"),
    ]
    with pytest.raises(ShadowInputError, match="sig-bad"):
        run_production(tmp_path, "sig-bad", rows=rows)
    # rejection happens BEFORE any store is touched: no execution request, no
    # claim, no safety decision, no shadow result
    assert not (tmp_path / "shadow.sqlite3").exists()
    assert not (tmp_path / "safety.sqlite3").exists()


# --- configuration --------------------------------------------------------------


def test_config_parses_and_exposes_quantities():
    config = make_config()
    assert config.policy.policy_id == "prod-policy-1"
    assert config.order_quantity_for("USD_JPY") == Decimal("2")


def test_config_refuses_missing_fields_unknown_keys_and_unconfigured_symbols():
    incomplete = json.loads(json.dumps(CONFIG_RAW))
    del incomplete["policy"]["max_order_quantity"]
    with pytest.raises(ShadowConfigError, match="max_order_quantity"):
        parse_shadow_execution_config(incomplete, source="test")
    unknown = json.loads(json.dumps(CONFIG_RAW))
    unknown["surprise"] = 1
    with pytest.raises(ShadowConfigError, match="unknown keys"):
        parse_shadow_execution_config(unknown, source="test")
    with pytest.raises(ShadowConfigError, match="refusing to fabricate"):
        make_config().order_quantity_for("EUR_USD")


def test_config_file_loader_uses_the_strict_loader(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"config_id": "a", "config_id": "b"}', encoding="utf-8")
    with pytest.raises(ShadowConfigError, match="duplicate"):
        load_shadow_execution_config(path)


# --- snapshot file providers ----------------------------------------------------


def write_market_file(tmp_path, **overrides):
    payload = {
        "symbol": "USD_JPY", "reference_price": "150.00", "bid": "149.99", "ask": "150.01",
        "observed_at_utc": NOW.isoformat(), "market_open": True, "tradable": True,
    }
    payload.update(overrides)
    for key in [key for key, value in overrides.items() if value is ...]:
        del payload[key]
    path = tmp_path / "market.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_market_file_provider_requires_every_field(tmp_path):
    provider = MarketSnapshotFileProvider(write_market_file(tmp_path, market_open=...))
    with pytest.raises(ShadowInputError, match="keys"):
        provider.get_market_snapshot("USD_JPY")


def test_market_file_provider_rejects_the_wrong_symbol(tmp_path):
    provider = MarketSnapshotFileProvider(write_market_file(tmp_path))
    with pytest.raises(ShadowInputError, match="covers"):
        provider.get_market_snapshot("EUR_USD")
    snapshot = provider.get_market_snapshot("USD_JPY")
    assert snapshot.spread == Decimal("0.02")


def test_account_file_provider_requires_every_field(tmp_path):
    path = tmp_path / "account.json"
    path.write_text(json.dumps({"account_id": "a"}), encoding="utf-8")
    with pytest.raises(ShadowInputError, match="keys"):
        AccountSnapshotFileProvider(path).get_account_snapshot()


def test_static_market_provider_guards_the_symbol():
    provider = StaticMarketSnapshotProvider(make_market(), "test")
    with pytest.raises(ShadowInputError, match="covers"):
        provider.get_market_snapshot("EUR_USD")


# --- Signal -> ExecutionRequest builder ------------------------------------------


def test_builder_maps_long_and_short_deterministically(tmp_path):
    provider = PaperSignalDbSignalProvider(
        seed_signal_db(tmp_path, [default_signal_row(), default_signal_row("sig-2", "short")])
    )
    request, client_order_id = build_execution_request_from_signal(
        provider.get_signal("sig-1"), quantity=Decimal("2")
    )
    assert request.execution_request_id == f"{EXECUTION_REQUEST_ID_PREFIX}sig-1"
    assert client_order_id == f"{CLIENT_ORDER_ID_PREFIX}sig-1"
    assert request.side is OrderSide.BUY and request.order_type is OrderType.MARKET
    assert request.quantity == Decimal("2")
    assert request.requested_at_utc == NOW  # the SIGNAL's timestamp
    assert request.stop_loss_price is None and request.take_profit_price is None
    short_request, _ = build_execution_request_from_signal(
        provider.get_signal("sig-2"), quantity=Decimal("1")
    )
    assert short_request.side is OrderSide.SELL


def test_builder_refuses_an_unmappable_side():
    with pytest.raises(ValueError):
        StoredSignal(
            signal_id="sig-x", created_at_utc=NOW, symbol="USD_JPY", timeframe="H1",
            side="sideways", strategy_id="s", status="paper_only",
        )


# --- composition root -----------------------------------------------------------


def run_production(tmp_path, signal_id="sig-1", *, config=None, dry_run=False, owner_id=None,
                   rows=None):
    return run_production_shadow_cycle(
        signal_id,
        signal_provider=PaperSignalDbSignalProvider(
            seed_signal_db(tmp_path, rows) if not (tmp_path / "paper_signals.sqlite3").exists()
            else tmp_path / "paper_signals.sqlite3"
        ),
        config=config if config is not None else make_config(),
        market_provider=StaticMarketSnapshotProvider(make_market(), "test-market"),
        account_provider=StaticAccountSnapshotProvider(make_account(), "test-account"),
        shadow_db_path=tmp_path / "shadow.sqlite3",
        decision_db_path=tmp_path / "safety.sqlite3",
        clock=lambda: NOW,
        owner_id=owner_id,
        dry_run=dry_run,
    )


def test_production_cycle_records_with_production_provenance(tmp_path):
    outcome = run_production(tmp_path)
    assert outcome.status is ShadowCycleStatus.RECORDED
    result = outcome.result
    assert result.provenance.input_origin == "production_signal"
    assert result.provenance.signal_source.startswith("paper_signal_db:")
    assert result.provenance.config_source == "test-config"
    assert result.provenance.config_version == "prod-config-1"
    assert result.provenance.signal_observed_at_utc == NOW
    assert result.timeframe == "H1"
    assert result.request.quantity == Decimal("2")
    assert result.allowed
    stored = shadow_store.get_shadow_result(
        f"{EXECUTION_REQUEST_ID_PREFIX}sig-1", db_path=tmp_path / "shadow.sqlite3"
    )
    assert stored == result
    assert len(SQLiteSafetyDecisionStore(tmp_path / "safety.sqlite3").list_for_request(
        stored.request.execution_request_id
    )) == 1


def test_production_retry_is_a_duplicate_not_a_conflict(tmp_path):
    first = run_production(tmp_path)
    again = run_production(tmp_path, owner_id="another-owner")
    assert again.status is ShadowCycleStatus.DUPLICATE
    assert again.result == first.result


def test_production_dry_run_creates_nothing(tmp_path):
    outcome = run_production(tmp_path, dry_run=True)
    assert outcome.status is ShadowCycleStatus.DRY_RUN
    assert not (tmp_path / "shadow.sqlite3").exists()
    assert not (tmp_path / "safety.sqlite3").exists()


def test_production_missing_signal_and_unconfigured_symbol_fail_closed(tmp_path):
    with pytest.raises(ShadowInputError, match="does not exist"):
        run_production(tmp_path, "missing-signal")
    rows = [(
        "sig-eur", NOW.isoformat(), "EUR_USD", "H1", "long", "strategy-prod-1", "paper_only",
    )]
    other = tmp_path / "eur"
    other.mkdir()
    with pytest.raises(ShadowConfigError, match="refusing to fabricate"):
        run_production(other, "sig-eur", rows=rows)
