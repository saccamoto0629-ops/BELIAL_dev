import pathlib
import sys
from dataclasses import dataclass

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import backtest_models as models  # noqa: E402


@dataclass(frozen=True)
class FakeExitPolicyResult:
    """Stands in for exit_policy.ExitPolicyResult without importing paper-signal."""

    status: str
    close_price: float | None
    close_time: str | None
    close_reason: str
    exit_policy: str
    mfe: float | None = None
    mae: float | None = None


def make_signal_record(**overrides):
    kwargs = {
        "run_id": "run-1",
        "signal_time": "2026-01-05T09:00:00Z",
        "symbol": "USD_JPY",
        "timeframe": "1h",
        "direction": "long",
        "entry_price": 150.0,
        "invalidation_price": 149.0,
        "take_profit_price": 152.0,
    }
    run_id = kwargs.pop("run_id")
    kwargs.update(overrides)
    return models.create_backtest_signal_record(run_id, **kwargs)


def test_apply_exit_policy_result_maps_dataclass_fields_onto_signal_record():
    signal_record = make_signal_record()
    result = FakeExitPolicyResult(
        status="win",
        close_price=152.0,
        close_time="2026-01-05T15:00:00Z",
        close_reason="take_profit",
        exit_policy="fixed_rr",
        mfe=2.5,
        mae=-0.2,
    )

    updated = models.apply_exit_policy_result(signal_record, result)

    assert updated["result_status"] == "win"
    assert updated["close_price"] == 152.0
    assert updated["closed_at"] == "2026-01-05T15:00:00Z"
    assert updated["close_reason"] == "take_profit"
    assert updated["exit_policy"] == "fixed_rr"
    assert updated["mfe"] == 2.5
    assert updated["mae"] == -0.2


def test_apply_exit_policy_result_accepts_a_plain_dict_too():
    signal_record = make_signal_record()
    result = {
        "status": "loss",
        "close_price": 149.0,
        "close_time": "2026-01-05T11:00:00Z",
        "close_reason": "stop_loss",
        "exit_policy": "fixed_rr",
        "mfe": 0.4,
        "mae": -1.0,
    }

    updated = models.apply_exit_policy_result(signal_record, result)

    assert updated["result_status"] == "loss"
    assert updated["close_price"] == 149.0
    assert updated["closed_at"] == "2026-01-05T11:00:00Z"
    assert updated["close_reason"] == "stop_loss"
    assert updated["exit_policy"] == "fixed_rr"


def test_apply_exit_policy_result_does_not_mutate_the_original_record():
    signal_record = make_signal_record()
    original = dict(signal_record)
    result = FakeExitPolicyResult(
        status="win", close_price=152.0, close_time="2026-01-05T15:00:00Z",
        close_reason="take_profit", exit_policy="fixed_rr",
    )

    models.apply_exit_policy_result(signal_record, result)

    assert signal_record == original


def test_apply_exit_policy_result_preserves_unrelated_fields():
    signal_record = make_signal_record(bias="Bull", confidence=0.72, signal_type="ma_touch")
    result = FakeExitPolicyResult(
        status="open", close_price=None, close_time=None, close_reason="no_close", exit_policy="fixed_rr",
    )

    updated = models.apply_exit_policy_result(signal_record, result)

    assert updated["id"] == signal_record["id"]
    assert updated["run_id"] == "run-1"
    assert updated["bias"] == "Bull"
    assert updated["confidence"] == 0.72
    assert updated["signal_type"] == "ma_touch"
    assert updated["entry_price"] == 150.0
    assert updated["result_status"] == "open"
    assert updated["close_price"] is None
    assert updated["closed_at"] is None


def test_apply_exit_policy_result_computes_pnl_for_a_long_win():
    # Regression (Issue #23): backtest/summary.py's win/loss/pnl metrics are
    # only meaningful if pnl actually gets populated here -- it previously
    # never did, so every signal read back with pnl=None regardless of
    # result_status.
    signal_record = make_signal_record(direction="long", entry_price=150.0)
    result = FakeExitPolicyResult(
        status="win", close_price=152.0, close_time="2026-01-05T15:00:00Z",
        close_reason="take_profit", exit_policy="fixed_rr",
    )

    updated = models.apply_exit_policy_result(signal_record, result)

    assert updated["pnl"] == 2.0  # close_price - entry_price


def test_apply_exit_policy_result_computes_pnl_for_a_short_win():
    signal_record = make_signal_record(direction="short", entry_price=150.0)
    result = FakeExitPolicyResult(
        status="win", close_price=148.0, close_time="2026-01-05T15:00:00Z",
        close_reason="take_profit", exit_policy="fixed_rr",
    )

    updated = models.apply_exit_policy_result(signal_record, result)

    assert updated["pnl"] == 2.0  # entry_price - close_price


def test_apply_exit_policy_result_computes_negative_pnl_for_a_loss():
    signal_record = make_signal_record(direction="long", entry_price=150.0)
    result = FakeExitPolicyResult(
        status="loss", close_price=149.0, close_time="2026-01-05T11:00:00Z",
        close_reason="stop_loss", exit_policy="fixed_rr",
    )

    updated = models.apply_exit_policy_result(signal_record, result)

    assert updated["pnl"] == -1.0


def test_apply_exit_policy_result_leaves_pnl_none_when_still_open():
    signal_record = make_signal_record(direction="long", entry_price=150.0)
    result = FakeExitPolicyResult(
        status="open", close_price=None, close_time=None, close_reason="no_close", exit_policy="fixed_rr",
    )

    updated = models.apply_exit_policy_result(signal_record, result)

    assert updated["pnl"] is None  # never fabricated for an unresolved signal


def test_create_backtest_signal_record_accepts_exit_policy():
    record = make_signal_record(exit_policy="time_based_close")

    assert record["exit_policy"] == "time_based_close"


def test_create_backtest_signal_record_defaults_exit_policy_to_none():
    record = make_signal_record()

    assert record["exit_policy"] is None
