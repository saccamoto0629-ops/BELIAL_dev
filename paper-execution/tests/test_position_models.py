"""Issue #50: virtual-position data model + vocabulary."""

import pathlib
import sys

import pytest

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = BASE_DIR.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(REPO_ROOT / "paper-signal"))

import position_models as models  # noqa: E402
import exit_policy  # noqa: E402  (paper-signal: source of the shared close-reason vocabulary)


def test_create_virtual_position_starts_open_with_no_exit_fields():
    pos = models.create_virtual_position(
        paper_signal_id="sig-1",
        strategy_profile="market_bias_macd_alignment_v1",
        symbol="USDJPY",
        timeframe="1h",
        side="long",
        entry_price=150.0,
        opened_at="2026-07-05T12:00:00Z",
        stop_loss=149.5,
        take_profit=151.0,
    )
    assert pos["status"] == models.STATUS_OPEN
    assert pos["side"] == "long"
    assert pos["entry_price"] == 150.0
    assert pos["stop_loss"] == 149.5
    assert pos["take_profit"] == 151.0
    assert pos["size"] == models.DEFAULT_SIZE
    # every exit / PnL field is None until the position closes
    for field in (
        "exit_price", "closed_at", "close_reason",
        "gross_pnl", "net_pnl", "spread_cost", "slippage_cost", "fee_cost", "r_multiple",
    ):
        assert pos[field] is None
    assert pos["created_at"] == pos["updated_at"]


def test_create_virtual_position_rejects_unknown_side():
    with pytest.raises(ValueError):
        models.create_virtual_position(
            paper_signal_id="sig-1",
            strategy_profile="p",
            symbol="USDJPY",
            timeframe="1h",
            side="Neutral",
            entry_price=1.0,
            opened_at="2026-07-05T12:00:00Z",
        )


def test_close_reason_vocabulary_matches_backtest_exit_policy():
    # Kept string-equal so a Paper Execution close reads exactly like a Backtest
    # exit (the two vocabularies are duplicated, not imported, so this guards it).
    assert models.CLOSE_REASON_STOP_LOSS == exit_policy.REASON_STOP_LOSS
    assert models.CLOSE_REASON_TAKE_PROFIT == exit_policy.REASON_TAKE_PROFIT
    assert models.CLOSE_REASON_TIME_EXPIRED == exit_policy.REASON_TIME_EXPIRED


def test_status_and_side_constant_sets():
    assert models.KNOWN_STATUSES == {"open", "closed"}
    assert models.KNOWN_SIDES == {"long", "short"}
    assert models.KNOWN_EVENT_TYPES == {"opened", "closed"}
