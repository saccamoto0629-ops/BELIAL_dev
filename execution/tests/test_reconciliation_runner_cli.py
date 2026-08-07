"""Issue #105: the manual reconciliation CLI (``python -m
execution.reconciliation_runner``) -- reconcile-only, demo-only, no network.

The real OANDA API is never touched: the ``oanda-demo`` path is exercised
through environment-driven REFUSALS (non-demo endpoint, missing
credentials) and through a monkeypatched adapter factory. FakeBroker covers
everything else.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import (
    BrokerConnectionIdentity,
    OrderRequest,
    OrderSide,
    OrderType,
)
from broker.fake_broker import FakeBroker
from execution import execution_store, reconciliation_store
from execution import reconciliation_runner as runner
from execution.execution_models import ExecutionRequest

INTERNAL_T = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
NOW_ARG = "2026-02-01T00:00:00+00:00"

FAKE_TOKEN = "cli-test-token-not-a-real-secret"
FAKE_ACCOUNT = "101-000-9999999-001"


def seed_execution_db(db_path):
    """One FILLED-side order the (empty) CLI broker cannot know about."""
    request = ExecutionRequest(
        execution_request_id="req-1",
        signal_id="sig-1",
        strategy_id="strat-1",
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("10"),
        requested_at_utc=INTERNAL_T,
    )
    execution_store.init_db(db_path=db_path)
    execution_store.create_execution_order(
        request, client_order_id="client-req-1", created_at_utc=INTERNAL_T, db_path=db_path
    )
    execution_store.mark_submitting(
        "req-1", reason="submitting", changed_at_utc=INTERNAL_T, db_path=db_path
    )
    broker = FakeBroker(default_fill_price=Decimal("2400"))
    result = broker.submit_order(
        OrderRequest(
            client_order_id="client-req-1",
            symbol="XAUUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("10"),
            created_at_utc=INTERNAL_T,
        )
    )
    execution_store.record_order_result("req-1", result, reason="filled", db_path=db_path)


def run_cli(tmp_path, *extra):
    argv = [
        "--broker",
        "fake",
        "--data-dir",
        str(tmp_path),
        "--now",
        NOW_ARG,
        "--run-id",
        "run-cli-1",
        *extra,
    ]
    return runner.main(argv)


# --- dry-run: zero side effects -----------------------------------------------


def test_dry_run_creates_no_files_at_all(tmp_path, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    exit_code = run_cli(data_dir, "--dry-run", "--json")
    assert exit_code == runner.EXIT_OK
    assert list(data_dir.iterdir()) == []  # neither DB was created
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["recorded_to"] is None
    assert payload["reconcile_only"] is True


def test_dry_run_leaves_existing_dbs_byte_identical(tmp_path):
    execution_db = tmp_path / "execution_orders.sqlite3"
    seed_execution_db(execution_db)
    reconciliation_db = tmp_path / "broker_reconciliation.sqlite3"
    reconciliation_store.init_reconciliation_db(reconciliation_db)
    before = (execution_db.read_bytes(), reconciliation_db.read_bytes())

    exit_code = run_cli(tmp_path, "--dry-run")

    # the internal order is unknown to the empty CLI broker -> discrepancies
    assert exit_code == runner.EXIT_DISCREPANCIES
    assert (execution_db.read_bytes(), reconciliation_db.read_bytes()) == before


# --- persisted runs -----------------------------------------------------------


def test_clean_run_records_an_audit_row_and_exits_zero(tmp_path, capsys):
    exit_code = run_cli(tmp_path, "--json")
    assert exit_code == runner.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    reconciliation_db = tmp_path / "broker_reconciliation.sqlite3"
    assert reconciliation_db.exists()
    stored = reconciliation_store.get_reconciliation_run(
        "run-cli-1", db_path=reconciliation_db
    )
    assert stored is not None
    assert stored.position_summary is not None
    assert payload["position_scope"]["checked_position_count"] == 0
    assert payload["has_partial_failures"] is False


def test_discrepancies_exit_one_and_are_recorded(tmp_path, capsys):
    seed_execution_db(tmp_path / "execution_orders.sqlite3")

    exit_code = run_cli(tmp_path, "--json")

    assert exit_code == runner.EXIT_DISCREPANCIES
    payload = json.loads(capsys.readouterr().out)
    types = {d["discrepancy_type"] for d in payload["order_discrepancies"]}
    assert "broker_order_missing" in types
    position_types = {
        d["discrepancy_type"] for d in payload["position_discrepancies"]
    }
    assert "position_missing_on_broker" in position_types
    stored = reconciliation_store.get_reconciliation_run(
        "run-cli-1", db_path=tmp_path / "broker_reconciliation.sqlite3"
    )
    assert stored.position_discrepancies


def test_duplicate_run_id_is_refused_without_touching_the_first_run(tmp_path):
    assert run_cli(tmp_path) == runner.EXIT_OK
    reconciliation_db = tmp_path / "broker_reconciliation.sqlite3"
    before = reconciliation_db.read_bytes()

    assert run_cli(tmp_path) == runner.EXIT_DISCREPANCIES  # runtime failure path

    assert reconciliation_db.read_bytes() == before


def test_orders_only_skips_the_position_scope(tmp_path, capsys):
    exit_code = run_cli(tmp_path, "--orders-only", "--json")
    assert exit_code == runner.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["position_scope"] is None
    assert payload["position_discrepancies"] == []


def test_json_output_is_pure_ascii(tmp_path, capsys):
    seed_execution_db(tmp_path / "execution_orders.sqlite3")
    run_cli(tmp_path, "--json")
    out = capsys.readouterr().out
    out.encode("ascii")  # raises on any non-ASCII byte
    json.loads(out)


# --- demo-only fail-closed ----------------------------------------------------


def oanda_env(monkeypatch, **overrides):
    values = {
        "OANDA_API_TOKEN": FAKE_TOKEN,
        "OANDA_ACCOUNT_ID": FAKE_ACCOUNT,
    }
    values.update(overrides)
    for key in ("OANDA_API_TOKEN", "OANDA_ACCOUNT_ID", "OANDA_API_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        if value is not None:
            monkeypatch.setenv(key, value)


def test_live_endpoint_is_refused_fail_closed_without_leaking_secrets(
    tmp_path, monkeypatch, capsys
):
    oanda_env(monkeypatch, OANDA_API_URL="https://api-fxtrade.oanda.com")

    exit_code = runner.main(
        ["--broker", "oanda-demo", "--data-dir", str(tmp_path), "--dry-run"]
    )

    assert exit_code == runner.EXIT_USAGE_ERROR
    captured = capsys.readouterr()
    assert "demo-only" in captured.err
    for secret in (FAKE_TOKEN, FAKE_ACCOUNT, "fxtrade", "oanda.com"):
        assert secret not in captured.out + captured.err
    assert list(tmp_path.iterdir()) == []  # nothing was created before the refusal


def test_unknown_endpoint_is_refused_fail_closed(tmp_path, monkeypatch, capsys):
    oanda_env(monkeypatch, OANDA_API_URL="https://broker.example.com")
    exit_code = runner.main(
        ["--broker", "oanda-demo", "--data-dir", str(tmp_path), "--dry-run"]
    )
    assert exit_code == runner.EXIT_USAGE_ERROR
    assert "refused" in capsys.readouterr().err


def test_missing_credentials_are_a_usage_error_without_echoing_values(
    tmp_path, monkeypatch, capsys
):
    oanda_env(monkeypatch, OANDA_API_TOKEN=None)
    exit_code = runner.main(
        ["--broker", "oanda-demo", "--data-dir", str(tmp_path), "--dry-run"]
    )
    assert exit_code == runner.EXIT_USAGE_ERROR
    err = capsys.readouterr().err
    assert "OANDA_API_TOKEN" in err  # names the variable ...
    assert FAKE_ACCOUNT not in err  # ... never a value


def test_oanda_demo_path_wires_the_adapter_without_any_network(
    tmp_path, monkeypatch, capsys
):
    """The oanda-demo branch is exercised end-to-end with the adapter class
    monkeypatched to an offline stand-in: proves the wiring (env config ->
    adapter -> reconcile-only facade -> run) with zero HTTP."""
    from broker import oanda_demo_adapter as oa

    oanda_env(monkeypatch)
    calls = {}

    class OfflineAdapter:
        def __init__(self, config, **kwargs):
            calls["config"] = config
            self._inner = FakeBroker()

        def connection_identity(self):
            return BrokerConnectionIdentity(mode="demo", target="oanda-demo")

        def submit_order(self, request):  # pragma: no cover - must never run
            raise AssertionError("reconcile-only")

        def cancel_order(self, client_order_id):  # pragma: no cover
            raise AssertionError("reconcile-only")

        def get_order(self, client_order_id):
            return self._inner.get_order(client_order_id)

        def get_positions(self):
            return self._inner.get_positions()

        def get_account_state(self):  # pragma: no cover - must never run
            raise AssertionError("reconciliation never reads account state")

        def get_open_trades(self):
            return self._inner.get_open_trades()

        def get_closed_trades(self):
            return self._inner.get_closed_trades()

    monkeypatch.setattr(oa, "OandaDemoAdapter", OfflineAdapter)

    exit_code = runner.main(
        [
            "--broker",
            "oanda-demo",
            "--data-dir",
            str(tmp_path),
            "--dry-run",
            "--json",
            "--run-id",
            "run-oanda-1",
        ]
    )

    # like the real adapter, the stand-in has no list_orders capability, so
    # the run honestly reports a PARTIAL result (broker-only orders were not
    # checkable) and exits 1 rather than pretending a clean reconciliation
    assert exit_code == runner.EXIT_DISCREPANCIES
    payload = json.loads(capsys.readouterr().out)
    assert payload["broker"] == "demo / oanda-demo"
    assert payload["has_partial_failures"] is True
    assert "OrderListingBroker" in payload["broker_listing_error"]
    assert payload["position_scope"] is not None
    assert calls["config"].account_id == FAKE_ACCOUNT
    # the token/account never surface in output
    text = json.dumps(payload)
    assert FAKE_TOKEN not in text
    assert FAKE_ACCOUNT not in text


# --- usage errors -------------------------------------------------------------


def test_unknown_broker_choice_is_an_argparse_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        runner.main(["--broker", "real-money", "--data-dir", str(tmp_path)])
    assert exc_info.value.code == runner.EXIT_USAGE_ERROR


def test_invalid_now_is_a_usage_error(tmp_path, capsys):
    exit_code = runner.main(
        ["--broker", "fake", "--data-dir", str(tmp_path), "--now", "not-a-time"]
    )
    assert exit_code == runner.EXIT_USAGE_ERROR
    assert "--now" in capsys.readouterr().err


def test_blank_run_id_is_a_usage_error(tmp_path):
    assert (
        runner.main(["--broker", "fake", "--data-dir", str(tmp_path), "--run-id", "  "])
        == runner.EXIT_USAGE_ERROR
    )


def test_naive_now_is_a_usage_error(tmp_path):
    assert (
        runner.main(
            ["--broker", "fake", "--data-dir", str(tmp_path), "--now", "2026-02-01T00:00:00"]
        )
        == runner.EXIT_USAGE_ERROR
    )


# --- the reconcile-only facade ------------------------------------------------


def test_reconcile_only_broker_poisons_every_mutating_operation():
    facade = runner.ReconcileOnlyBroker(FakeBroker())
    with pytest.raises(runner.ReconcileOnlyViolationError):
        facade.submit_order(object())
    with pytest.raises(runner.ReconcileOnlyViolationError):
        facade.cancel_order("c-1")
    with pytest.raises(runner.ReconcileOnlyViolationError):
        facade.get_account_state()


def test_reconcile_only_broker_mirrors_the_inner_capabilities():
    from broker.broker_interface import (
        Broker,
        ClosedTradeListingBroker,
        OrderListingBroker,
        TradeListingBroker,
    )

    full = runner.ReconcileOnlyBroker(FakeBroker())
    assert isinstance(full, Broker)
    assert isinstance(full, OrderListingBroker)
    assert isinstance(full, TradeListingBroker)
    assert isinstance(full, ClosedTradeListingBroker)

    class MinimalBroker:
        def submit_order(self, request): ...
        def cancel_order(self, client_order_id): ...
        def get_order(self, client_order_id): ...
        def get_positions(self): ...
        def get_account_state(self): ...

    minimal = runner.ReconcileOnlyBroker(MinimalBroker())
    assert isinstance(minimal, Broker)
    assert not isinstance(minimal, OrderListingBroker)
    assert not isinstance(minimal, TradeListingBroker)
    assert not isinstance(minimal, ClosedTradeListingBroker)
