"""Execution profile: the fixed *execution rules* a virtual position is judged by.

Issue #49 separates two profiles that must never be conflated:

- ``strategy_profile`` -- owned by Paper Signal, RECORD-ONLY. It identifies which
  signal-generation strategy produced a signal and is *never* used to resolve
  execution rules (SL/TP, deadlines, priority, costs).
- ``execution_profile`` (this module) -- owned by Paper Execution. It defines the
  execution rules: SL/TP sizing, Entry deadline, max holding time, the ambiguous
  same-candle priority, and the spread/slippage/commission costs.

Resolution (Issue #49): a profile is chosen from Paper Execution's OWN config by
``(symbol, timeframe)`` with a fallback to the default profile. The signal's
classification fields (including ``strategy_profile``) are never resolution keys.

Every parameter a position is judged by is snapshotted onto the position row at
``pending``/``open`` time (see ``position_models``/``position_store``), so a later
change to CLI flags or config can never alter an existing position's outcome --
the position is always re-evaluated from its own stored snapshot, not from the
live profile.

``max_active_positions_per_side`` (Issue #85) is the one deliberate exception
to the snapshot rule: it is an INTAKE-time gate ("may this signal become a new
position, given the other positions currently active in its
symbol/timeframe/side scope?"), not a rule any individual position is judged
by. It is therefore evaluated against the live profile at signal intake and
never snapshotted onto position rows -- an existing position's evaluation is
unaffected by it.
"""

from __future__ import annotations

from dataclasses import dataclass

# Same-candle priority vocabulary -- identical strings to
# paper-signal/exit_policy.py so Paper Execution and Backtest agree on the
# conservative default and can be cross-checked.
SAME_CANDLE_PRIORITY_STOP_LOSS = "stop_loss"
SAME_CANDLE_PRIORITY_TAKE_PROFIT = "take_profit"
KNOWN_SAME_CANDLE_PRIORITIES = frozenset(
    {SAME_CANDLE_PRIORITY_STOP_LOSS, SAME_CANDLE_PRIORITY_TAKE_PROFIT}
)
DEFAULT_SAME_CANDLE_PRIORITY = SAME_CANDLE_PRIORITY_STOP_LOSS

DEFAULT_PROFILE_NAME = "default"


@dataclass(frozen=True)
class ExecutionProfile:
    """Immutable set of execution rules for one virtual position.

    Defaults mirror the Backtest vocabulary: ``fallback_stop_pct`` matches
    exit_policy's default, ``risk_reward`` and ``max_hold_bars`` match the
    squeeze-breakout strategy, and ``same_candle_priority`` defaults to the
    conservative ``stop_loss``. ``max_entry_delay_bars`` defaults to 3 bars
    (Issue #49: "timeframe × 3本分").

    ``max_active_positions_per_side`` (Issue #85): concurrent-position cap per
    ``(symbol, timeframe, side)`` scope, counting non-terminal (pending / open)
    positions. Defaults to 1 -- one active position per direction per scope; a
    signal refused by the cap is recorded as ``cancelled`` /
    ``position_limit``. ``None`` disables the cap (the pre-#85 unlimited,
    per-signal-independent behaviour). Intake-time gate only -- never
    snapshotted onto position rows (see the module docstring).
    """

    name: str = DEFAULT_PROFILE_NAME
    max_entry_delay_bars: int = 3
    max_hold_bars: int = 24
    same_candle_priority: str = DEFAULT_SAME_CANDLE_PRIORITY
    fallback_stop_pct: float = 0.003
    risk_reward: float = 2.0
    size: float = 1.0
    spread: float = 0.0
    slippage: float = 0.0
    commission: float = 0.0
    max_active_positions_per_side: int | None = 1

    def __post_init__(self) -> None:
        if self.same_candle_priority not in KNOWN_SAME_CANDLE_PRIORITIES:
            raise ValueError(
                f"same_candle_priority must be one of {sorted(KNOWN_SAME_CANDLE_PRIORITIES)}, "
                f"got {self.same_candle_priority!r}"
            )
        for field_name in ("max_entry_delay_bars", "max_hold_bars"):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be > 0, got {getattr(self, field_name)!r}")
        if self.size <= 0:
            raise ValueError(f"size must be > 0, got {self.size!r}")
        for field_name in ("fallback_stop_pct", "risk_reward"):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be > 0, got {getattr(self, field_name)!r}")
        for field_name in ("spread", "slippage", "commission"):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be >= 0, got {getattr(self, field_name)!r}")
        if self.max_active_positions_per_side is not None and self.max_active_positions_per_side < 1:
            raise ValueError(
                "max_active_positions_per_side must be >= 1 or None (unlimited), "
                f"got {self.max_active_positions_per_side!r}"
            )


DEFAULT_PROFILE = ExecutionProfile()


def resolve_execution_profile(
    symbol: str,
    timeframe: str,
    *,
    overrides: dict[tuple[str, str], ExecutionProfile] | None = None,
    default: ExecutionProfile = DEFAULT_PROFILE,
) -> ExecutionProfile:
    """Resolve the execution profile for ``(symbol, timeframe)``.

    Issue #49: keyed ONLY by ``(symbol, timeframe)`` from Paper Execution's own
    config, falling back to ``default``. Signal classification fields are never
    consulted here.
    """
    if overrides:
        profile = overrides.get((symbol, timeframe))
        if profile is not None:
            return profile
    return default
