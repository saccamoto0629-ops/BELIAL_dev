"""Production execution configuration for Shadow Mode (Issue #78 / PR #100).

The production shadow path resolves its Safety Gate policy, per-symbol order
quantities, kill-switch state and shadow connection target from ONE explicit
configuration document -- typically a JSON file loaded through
:func:`load_shadow_execution_config`. Contract:

- EVERY field is required. There are NO permissive defaults on the production
  path: an absent policy field, an absent quantity for the traded symbol, or
  an unknown key is an explicit :class:`ShadowConfigError` at load time.
- ``config_id`` is the configuration VERSION identifier recorded in every
  shadow record's provenance (next to the full policy snapshot), so an audit
  can tell exactly which configuration produced a decision.
- The CLI FIXTURE path (``shadow_runner --cycle``) deliberately has a
  different, more forgiving contract (fixture defaults are allowed there and
  the record is labelled ``cli_fixture``); this module is the production
  contract only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from broker.broker_models import OrderSide
from execution.safety_gate import ExecutionMode, SafetyPolicy
from execution.strict_json import load_strict_json_file


class ShadowConfigError(ValueError):
    """The production shadow configuration is missing, incomplete or invalid."""


_CONFIG_KEYS = frozenset(
    {"config_id", "policy", "order_quantities", "kill_switch", "connection_target"}
)
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


@dataclass(frozen=True)
class ShadowExecutionConfig:
    """One parsed production configuration (deep-immutable)."""

    config_id: str
    source: str
    policy: SafetyPolicy
    order_quantities: Mapping[str, Decimal]
    kill_switch: bool
    connection_target: str

    def __post_init__(self) -> None:
        for name in ("config_id", "source", "connection_target"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ShadowConfigError(f"{name} must be a non-empty string, got {value!r}")
        if not isinstance(self.policy, SafetyPolicy):
            raise ShadowConfigError(f"policy must be a SafetyPolicy, got {self.policy!r}")
        if not isinstance(self.order_quantities, Mapping) or not self.order_quantities:
            raise ShadowConfigError("order_quantities must be a non-empty mapping")
        for symbol, quantity in self.order_quantities.items():
            if not isinstance(symbol, str) or not symbol.strip():
                raise ShadowConfigError(f"order_quantities key {symbol!r} must be a symbol string")
            if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
                raise ShadowConfigError(
                    f"order_quantities[{symbol!r}] must be a positive finite Decimal, "
                    f"got {quantity!r}"
                )
        if not isinstance(self.kill_switch, bool):
            raise ShadowConfigError(f"kill_switch must be a bool, got {self.kill_switch!r}")
        object.__setattr__(self, "order_quantities", MappingProxyType(dict(self.order_quantities)))

    def order_quantity_for(self, symbol: str) -> Decimal:
        """The configured quantity for ``symbol``; an unconfigured symbol is
        an explicit error (never a fabricated size)."""
        quantity = self.order_quantities.get(symbol)
        if quantity is None:
            raise ShadowConfigError(
                f"configuration {self.config_id!r} declares no order quantity for "
                f"{symbol!r}; refusing to fabricate one"
            )
        return quantity


def _require(mapping: dict, key: str, name: str):
    if key not in mapping:
        raise ShadowConfigError(f"{name}.{key} is required (production configuration has no defaults)")
    return mapping[key]


def _config_str(mapping: dict, key: str, name: str) -> str:
    value = _require(mapping, key, name)
    if not isinstance(value, str) or not value.strip():
        raise ShadowConfigError(f"{name}.{key} must be a non-empty string, got {value!r}")
    return value


def _config_bool(mapping: dict, key: str, name: str) -> bool:
    value = _require(mapping, key, name)
    if not isinstance(value, bool):
        raise ShadowConfigError(f"{name}.{key} must be a boolean, got {value!r}")
    return value


def _config_int(mapping: dict, key: str, name: str, *, minimum: int) -> int:
    value = _require(mapping, key, name)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ShadowConfigError(f"{name}.{key} must be an integer >= {minimum}, got {value!r}")
    return value


def _config_decimal(value, name: str) -> Decimal:
    if not isinstance(value, str):
        raise ShadowConfigError(f"{name} must be a decimal string, got {value!r}")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ShadowConfigError(f"{name} is not a valid decimal string: {value!r}") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ShadowConfigError(f"{name} must be a positive finite decimal, got {value!r}")
    return parsed


def _config_str_list(mapping: dict, key: str, name: str) -> list[str]:
    value = _require(mapping, key, name)
    if not isinstance(value, list) or not value:
        raise ShadowConfigError(f"{name}.{key} must be a non-empty list of strings")
    out = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ShadowConfigError(f"{name}.{key} entries must be non-empty strings, got {entry!r}")
        out.append(entry)
    return out


def parse_shadow_execution_config(raw, *, source: str) -> ShadowExecutionConfig:
    """Parse one raw configuration mapping (closed world, everything
    required). ``source`` identifies where it came from (file path, scenario
    id) and is recorded in provenance."""
    if not isinstance(raw, dict):
        raise ShadowConfigError(
            f"shadow execution configuration must be an object, got {type(raw).__name__}"
        )
    unknown = set(raw) - _CONFIG_KEYS
    if unknown:
        raise ShadowConfigError(
            f"configuration has unknown keys {sorted(unknown)}; allowed: {sorted(_CONFIG_KEYS)}"
        )
    config_id = _config_str(raw, "config_id", "configuration")

    policy_raw = _require(raw, "policy", "configuration")
    if not isinstance(policy_raw, dict):
        raise ShadowConfigError("configuration.policy must be an object")
    unknown = set(policy_raw) - _POLICY_KEYS
    if unknown:
        raise ShadowConfigError(
            f"configuration.policy has unknown keys {sorted(unknown)}; "
            f"allowed: {sorted(_POLICY_KEYS)}"
        )
    sides = []
    for entry in _config_str_list(policy_raw, "allowed_sides", "configuration.policy"):
        try:
            sides.append(OrderSide(entry))
        except ValueError:
            raise ShadowConfigError(
                f"configuration.policy.allowed_sides entry {entry!r} must be one of "
                f"{sorted(item.value for item in OrderSide)}"
            ) from None
    spreads_raw = _require(policy_raw, "max_spread_by_symbol", "configuration.policy")
    if not isinstance(spreads_raw, dict) or not spreads_raw:
        raise ShadowConfigError("configuration.policy.max_spread_by_symbol must be a non-empty object")
    spreads = {
        symbol: _config_decimal(spread, f"configuration.policy.max_spread_by_symbol[{symbol}]")
        for symbol, spread in spreads_raw.items()
    }
    try:
        policy = SafetyPolicy(
            policy_id=_config_str(policy_raw, "policy_id", "configuration.policy"),
            allowed_symbols=frozenset(
                _config_str_list(policy_raw, "allowed_symbols", "configuration.policy")
            ),
            allowed_sides=frozenset(sides),
            max_order_quantity=_config_decimal(
                _require(policy_raw, "max_order_quantity", "configuration.policy"),
                "configuration.policy.max_order_quantity",
            ),
            max_positions_per_symbol=_config_int(
                policy_raw, "max_positions_per_symbol", "configuration.policy", minimum=0
            ),
            max_positions_account=_config_int(
                policy_raw, "max_positions_account", "configuration.policy", minimum=0
            ),
            max_reference_age=timedelta(
                seconds=_config_int(
                    policy_raw, "max_reference_age_seconds", "configuration.policy", minimum=1
                )
            ),
            max_spread_by_symbol=spreads,
            allowed_connection_targets={
                ExecutionMode.SHADOW: frozenset(
                    _config_str_list(policy_raw, "allowed_shadow_targets", "configuration.policy")
                )
            },
            require_stop_loss=_config_bool(policy_raw, "require_stop_loss", "configuration.policy"),
        )
    except ShadowConfigError:
        raise
    except ValueError as exc:
        raise ShadowConfigError(f"configuration.policy is invalid: {exc}") from exc

    quantities_raw = _require(raw, "order_quantities", "configuration")
    if not isinstance(quantities_raw, dict) or not quantities_raw:
        raise ShadowConfigError("configuration.order_quantities must be a non-empty object")
    quantities = {
        symbol: _config_decimal(quantity, f"configuration.order_quantities[{symbol}]")
        for symbol, quantity in quantities_raw.items()
    }

    return ShadowExecutionConfig(
        config_id=config_id,
        source=source,
        policy=policy,
        order_quantities=quantities,
        kill_switch=_config_bool(raw, "kill_switch", "configuration"),
        connection_target=_config_str(raw, "connection_target", "configuration"),
    )


def load_shadow_execution_config(path: str | Path) -> ShadowExecutionConfig:
    """Load the production configuration from a JSON file (strict loader:
    duplicate keys, invalid UTF-8, oversized inputs and non-finite numbers
    are explicit errors)."""
    path = Path(path)
    raw = load_strict_json_file(
        path, description="shadow execution configuration", error_cls=ShadowConfigError
    )
    return parse_shadow_execution_config(raw, source=str(path))
