"""Issue #49/#50: state model + terminal_reason closed set + factories."""

import pathlib
import sys

import pytest

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = BASE_DIR.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(REPO_ROOT / "paper-signal"))

import position_models as models  # noqa: E402
import execution_profile as profiles  # noqa: E402
import exit_policy  # noqa: E402  (paper-signal: shared vocabulary source)

PROFILE = profiles.ExecutionProfile()


def test_state_sets():
    assert models.KNOWN_STATES == {"pending", "open", "closed", "expired", "cancelled"}
    assert models.TERMINAL_STATES == {"closed", "expired", "cancelled"}
    assert models.NON_TERMINAL_STATES == {"pending", "open"}


def test_state_terminal_reason_closed_set():
    assert models.STATE_TERMINAL_REASONS["closed"] == {"take_profit", "stop_loss", "max_holding_time"}
    assert models.STATE_TERMINAL_REASONS["expired"] == {"entry_timeout"}
    assert models.STATE_TERMINAL_REASONS["cancelled"] == {"invalid_input", "manual", "position_limit"}


@pytest.mark.parametrize(
    "state,reason",
    [
        ("pending", None),
        ("open", None),
        ("closed", "take_profit"),
        ("closed", "stop_loss"),
        ("closed", "max_holding_time"),
        ("expired", "entry_timeout"),
        ("cancelled", "invalid_input"),
        ("cancelled", "manual"),
    ],
)
def test_validate_state_reason_accepts_closed_set(state, reason):
    models.validate_state_reason(state, reason)  # no raise


@pytest.mark.parametrize(
    "state,reason",
    [
        ("pending", "stop_loss"),      # non-terminal must have None
        ("open", "take_profit"),
        ("closed", None),              # terminal must have a reason
        ("closed", "entry_timeout"),   # wrong reason for state
        ("expired", "stop_loss"),
        ("cancelled", "take_profit"),
        ("bogus", None),               # unknown state
    ],
)
def test_validate_state_reason_rejects_invalid(state, reason):
    with pytest.raises(ValueError):
        models.validate_state_reason(state, reason)


def test_create_pending_position():
    pos = models.create_pending_position(
        signal_id="sig-1", symbol="USDJPY", timeframe="1h", side="long",
        strategy_profile="p", profile=PROFILE,
        signal_candle_utc="2026-07-06T12:00:00Z", entry_deadline_utc="2026-07-06T16:00:00Z",
    )
    assert pos["state"] == "pending"
    assert pos["terminal_reason"] is None
    for f in ("entry_price", "entry_time_utc", "stop_loss_price", "take_profit_price",
              "max_hold_deadline_utc", "close_price", "close_time_utc", "pnl", "last_evaluated_candle_utc"):
        assert pos[f] is None
    # execution-profile snapshot is captured up front
    assert pos["same_candle_priority"] == "stop_loss"
    assert pos["max_hold_bars"] == PROFILE.max_hold_bars


def test_create_pending_rejects_bad_side():
    with pytest.raises(ValueError):
        models.create_pending_position(
            signal_id="s", symbol="USDJPY", timeframe="1h", side="Neutral",
            strategy_profile="p", profile=PROFILE,
            signal_candle_utc="2026-07-06T12:00:00Z", entry_deadline_utc="2026-07-06T16:00:00Z",
        )


def test_create_cancelled_position():
    pos = models.create_cancelled_position(
        signal_id="sig-bad", symbol="", timeframe="", side="",
        strategy_profile="", profile=PROFILE, signal_candle_utc="", entry_deadline_utc="",
    )
    assert pos["state"] == "cancelled"
    assert pos["terminal_reason"] == "invalid_input"


def test_vocabulary_parity_with_backtest():
    # stop_loss / take_profit strings shared with the Backtest exit vocabulary.
    assert models.REASON_STOP_LOSS == exit_policy.REASON_STOP_LOSS
    assert models.REASON_TAKE_PROFIT == exit_policy.REASON_TAKE_PROFIT
    assert profiles.SAME_CANDLE_PRIORITY_STOP_LOSS == exit_policy.SAME_CANDLE_PRIORITY_STOP_LOSS
    assert profiles.SAME_CANDLE_PRIORITY_TAKE_PROFIT == exit_policy.SAME_CANDLE_PRIORITY_TAKE_PROFIT
    assert profiles.DEFAULT_SAME_CANDLE_PRIORITY == exit_policy.DEFAULT_SAME_CANDLE_PRIORITY
