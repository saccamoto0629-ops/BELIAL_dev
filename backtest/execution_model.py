"""Issue #26: a minimal virtual-fill (execution) model for the Backtest.

*** PAPER-ONLY PRICE ADJUSTMENT. NOT AN EXECUTION LAYER. ***

This module answers a single, deliberately narrow question: "if a paper
hypothesis entered/exited at this *raw* candle price, what price would it
realistically have been filled at once a fixed spread and slippage are
charged against it?" It never places an order, calls a broker/Execution
API, models bid/ask history, reads tick data, widens spread around news,
or converts to pips -- it only shifts a single price by two constants.

Direction of the adjustment is always *unfavorable* to the position (you
buy a little higher and sell a little lower than the raw price), which is
the conservative choice for evaluating a strategy:

  Entry  long:  raw_price + spread + slippage
  Entry  short: raw_price - spread - slippage
  Close  long:  raw_price - spread - slippage
  Close  short: raw_price + spread + slippage

``spread`` and ``slippage`` are treated as absolute price differences in
the same units as the raw price (no pips conversion, no per-symbol digit
handling). Both must be >= 0; a negative value is a programming/config
error and raises ``ValueError`` here (the CLI layer additionally rejects
negatives as an argparse usage error before ever reaching this module).
"""

from __future__ import annotations

from dataclasses import dataclass

# The only execution-model "type" this module implements. Persisted onto the
# backtest_runs.config_json and echoed in the run summary so a reader can tell
# which fill model produced a run's prices.
EXECUTION_MODEL_TYPE = "fixed_spread_slippage"

LONG = "long"
SHORT = "short"
VALID_DIRECTIONS = (LONG, SHORT)


def _validate_cost(name: str, value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be >= 0, got {parsed!r}")
    return parsed


def _validate_direction(direction: str) -> str:
    if direction not in VALID_DIRECTIONS:
        raise ValueError(
            f"direction must be one of {VALID_DIRECTIONS!r}, got {direction!r}"
        )
    return direction


@dataclass(frozen=True)
class ExecutionModel:
    """Fixed spread + slippage virtual-fill model.

    Immutable: ``spread`` / ``slippage`` are validated (>= 0) once at
    construction, so every later ``adjust_*`` call is guaranteed sane.
    ``spread == slippage == 0.0`` (the default) is a no-op model that
    returns raw prices unchanged, which is how the Backtest preserves its
    pre-Issue-#26 behaviour when neither ``--spread`` nor ``--slippage`` is
    given.
    """

    spread: float = 0.0
    slippage: float = 0.0

    def __post_init__(self) -> None:
        # frozen dataclass: assign through object.__setattr__ so the
        # coerced float (e.g. an int 0 -> 0.0) sticks.
        object.__setattr__(self, "spread", _validate_cost("spread", self.spread))
        object.__setattr__(self, "slippage", _validate_cost("slippage", self.slippage))

    @property
    def total_cost(self) -> float:
        """Combined per-side adjustment magnitude (spread + slippage)."""
        return self.spread + self.slippage

    def adjust_entry_price(self, raw_price: float, direction: str) -> float:
        """Shift an entry fill against the position: long pays up, short sells down."""
        _validate_direction(direction)
        price = float(raw_price)
        if direction == LONG:
            return price + self.total_cost
        return price - self.total_cost

    def adjust_close_price(self, raw_price: float | None, direction: str) -> float | None:
        """Shift a close fill against the position: long sells down, short buys up.

        ``raw_price is None`` (an open / no_close signal that never resolved
        to a close) is passed straight through as ``None`` -- there is no
        fill to charge costs against, so it must stay unresolved.
        """
        _validate_direction(direction)
        if raw_price is None:
            return None
        price = float(raw_price)
        if direction == LONG:
            return price - self.total_cost
        return price + self.total_cost

    def to_config(self) -> dict:
        """Serialize to the config/summary shape (a fresh dict each call)."""
        return {
            "type": EXECUTION_MODEL_TYPE,
            "spread": self.spread,
            "slippage": self.slippage,
        }

    @classmethod
    def from_config(cls, config: dict) -> "ExecutionModel":
        """Rebuild a model from a ``to_config()``-shaped dict without mutating it.

        Only ``spread`` / ``slippage`` are read; ``type`` is ignored beyond
        being the marker of which model this is. The passed dict is never
        modified.
        """
        return cls(
            spread=config.get("spread", 0.0),
            slippage=config.get("slippage", 0.0),
        )
