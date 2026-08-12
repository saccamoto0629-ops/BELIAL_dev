"""OANDA Demo Adapter — the DEMO-ONLY Broker implementation (Issue #79).

*** DEMO ONLY. THE LIVE ENDPOINT IS REFUSED AT CONSTRUCTION (FAIL-CLOSED). ***

The OANDA v20 translation body lives in ``broker/oanda_v20_adapter.py``
(extracted unchanged by Issue #82 so the LIVE adapter reuses exactly the
same reviewed translation instead of a second copy). THIS module keeps
everything that makes the adapter demo-only, and nothing here is weaker
than before the split:

- the endpoint allowlist: ``OANDA_API_URL`` must be EXACTLY the demo
  endpoint; a live host marker is refused first, with fixed-text errors
  that never echo the configured value;
- the demo environment variables (``OANDA_API_TOKEN`` /
  ``OANDA_ACCOUNT_ID`` / ``OANDA_API_URL`` /
  ``OANDA_HTTP_TIMEOUT_SECONDS``) — the LIVE adapter reads a DISJOINT set
  of variables (Issue #82), so demo and live credentials can never be the
  same configuration;
- the immutable connection identity ``demo`` / ``oanda-demo``.

Secrets, error taxonomy, reissue handling, redaction and the "no automatic
resend" contract are documented in ``broker/oanda_v20_adapter.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from .oanda_v20_adapter import (
    DEFAULT_TIMEOUT_SECONDS,
    OANDA_LIVE_HOST_MARKER,
    OandaV20Adapter,
    TransportConnectionError,
    TransportError,
    TransportTimeoutError,
    require_non_empty_env,
    require_positive_timeout,
    timeout_seconds_from_env,
)

__all__ = [
    "ENV_ACCOUNT_ID",
    "ENV_API_TOKEN",
    "ENV_API_URL",
    "ENV_HTTP_TIMEOUT_SECONDS",
    "OANDA_DEMO_API_URL",
    "OANDA_DEMO_CONNECTION_TARGET",
    "OandaDemoAdapter",
    "OandaDemoConfig",
    "TransportConnectionError",
    "TransportError",
    "TransportTimeoutError",
]

# The ONLY endpoint this adapter will ever talk to. Anything else — the live
# host most of all — is refused at construction time (fail-closed allowlist,
# Issue #79).
OANDA_DEMO_API_URL = "https://api-fxpractice.oanda.com"

# The connection-identity target label (Issue #75 contract). A constant,
# NON-SECRET label — never the endpoint URL — so nothing configured via
# environment variables crosses into the execution layer (PR #101 review).
OANDA_DEMO_CONNECTION_TARGET = "oanda-demo"

# Environment variable names (values are read from the process environment
# only; never hard-code or log the values — docs/operations/operations.md).
# DEMO-ONLY names: the live adapter reads OANDA_LIVE_* instead (Issue #82).
ENV_API_TOKEN = "OANDA_API_TOKEN"
ENV_ACCOUNT_ID = "OANDA_ACCOUNT_ID"
ENV_API_URL = "OANDA_API_URL"
ENV_HTTP_TIMEOUT_SECONDS = "OANDA_HTTP_TIMEOUT_SECONDS"


@dataclass(frozen=True)
class OandaDemoConfig:
    """Construction-time configuration for :class:`OandaDemoAdapter`.

    ``api_token`` and ``account_id`` are secrets: excluded from ``repr()``
    and never included in exception messages or logs. The endpoint is
    validated against the demo allowlist here, so a live endpoint can never
    reach the adapter at all (fail-closed at startup, Issue #79).
    """

    api_token: str = field(repr=False)
    account_id: str = field(repr=False)
    # The endpoint is environment-managed like the credentials (Issue #79);
    # it is excluded from repr() as well (PR #101 review).
    endpoint: str = field(default=OANDA_DEMO_API_URL, repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.api_token, str) or not self.api_token.strip():
            raise ValueError(f"api_token must be a non-empty string (set {ENV_API_TOKEN})")
        if not isinstance(self.account_id, str) or not self.account_id.strip():
            raise ValueError(f"account_id must be a non-empty string (set {ENV_ACCOUNT_ID})")
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise ValueError(f"endpoint must be a non-empty string (set {ENV_API_URL})")
        # Rejection messages are FIXED TEXT (PR #101 re-review): neither the
        # configured value nor any endpoint URL is ever echoed into the
        # exception, so a mistyped live URL or credentials-in-URL mistake
        # cannot leak through the startup failure.
        normalized = self.endpoint.strip().rstrip("/")
        if OANDA_LIVE_HOST_MARKER in normalized.lower():
            raise ValueError(
                "OANDA Demo endpoint configuration is invalid: a live endpoint "
                "was configured; this adapter is demo-only and refuses to start"
            )
        if normalized != OANDA_DEMO_API_URL:
            raise ValueError(
                "OANDA Demo endpoint configuration is invalid: only the demo "
                "endpoint is allowed; this adapter refuses to start"
            )
        object.__setattr__(self, "endpoint", normalized)
        require_positive_timeout(self.timeout_seconds)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "OandaDemoConfig":
        """Build the config from environment variables ONLY (Issue #79).

        ``OANDA_API_TOKEN`` / ``OANDA_ACCOUNT_ID`` are required;
        ``OANDA_API_URL`` defaults to the demo endpoint and anything else is
        refused; ``OANDA_HTTP_TIMEOUT_SECONDS`` optionally overrides the
        request timeout.
        """
        env = os.environ if environ is None else environ
        api_token = require_non_empty_env(env.get(ENV_API_TOKEN), ENV_API_TOKEN)
        account_id = require_non_empty_env(env.get(ENV_ACCOUNT_ID), ENV_ACCOUNT_ID)
        endpoint = env.get(ENV_API_URL)
        if endpoint is None or not endpoint.strip():
            endpoint = OANDA_DEMO_API_URL
        return cls(
            api_token=api_token,
            account_id=account_id,
            endpoint=endpoint,
            timeout_seconds=timeout_seconds_from_env(env, ENV_HTTP_TIMEOUT_SECONDS),
        )


class OandaDemoAdapter(OandaV20Adapter):
    """Demo-only OANDA v20 implementation of ``broker_interface.Broker``.

    The whole class body is the shared engine (``OandaV20Adapter``); the
    four declarations below are what make it demo-only, and none of them is
    configurable at runtime.
    """

    CONFIG_TYPE = OandaDemoConfig
    ENDPOINT_LABEL = "OANDA demo"
    IDENTITY_MODE = "demo"
    IDENTITY_TARGET = OANDA_DEMO_CONNECTION_TARGET
