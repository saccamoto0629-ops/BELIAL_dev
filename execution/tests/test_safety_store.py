from datetime import datetime, timezone

from execution.safety_gate import SafetyDecision, SafetyRejection, SafetyRejectionCode
from execution.safety_store import SQLiteSafetyDecisionStore


def test_rejected_decision_round_trips_with_machine_readable_codes(tmp_path):
    store = SQLiteSafetyDecisionStore(tmp_path / "safety.sqlite3")
    store.initialize()
    decision = SafetyDecision(
        execution_request_id="exec-1",
        allowed=False,
        checked_at_utc=datetime(2026, 7, 22, tzinfo=timezone.utc),
        rejections=(
            SafetyRejection(SafetyRejectionCode.KILL_SWITCH_ENABLED, "kill switch is enabled"),
            SafetyRejection(SafetyRejectionCode.MARKET_CLOSED, "market is closed"),
        ),
        account_id="fake-account",
        position_count=2,
    )

    store.record(decision)

    restored = store.list_for_request("exec-1")
    assert restored == (decision,)
    assert restored[0].rejection_codes == ("kill_switch_enabled", "market_closed")


def test_allowed_decision_round_trips(tmp_path):
    store = SQLiteSafetyDecisionStore(tmp_path / "safety.sqlite3")
    store.initialize()
    decision = SafetyDecision(
        execution_request_id="exec-ok",
        allowed=True,
        checked_at_utc=datetime(2026, 7, 22, 1, tzinfo=timezone.utc),
        account_id="fake-account",
        position_count=0,
    )
    store.record(decision)
    assert store.list_for_request("exec-ok") == (decision,)
