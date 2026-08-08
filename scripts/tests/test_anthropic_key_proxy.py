"""Issue #109 / PR #110 review r3740741454: tests for the Anthropic
credential broker used by the codex-autofix workflow.

The broker is the only process holding the real ANTHROPIC_API_KEY; Claude
Code (running as a separate user with a decoy key) reaches the API through
it. These tests pin the credential-handling contract -- client-supplied
credentials are stripped, the real key is injected, the listener stays on
localhost -- without any network access.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "anthropic_key_proxy.py"
SPEC = importlib.util.spec_from_file_location("anthropic_key_proxy", MODULE_PATH)
assert SPEC and SPEC.loader
anthropic_key_proxy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = anthropic_key_proxy
SPEC.loader.exec_module(anthropic_key_proxy)


def test_client_credentials_are_stripped_and_real_key_injected():
    headers = anthropic_key_proxy.build_upstream_headers(
        [
            ("Authorization", "Bearer decoy"),
            ("X-Api-Key", "broker-managed"),
            ("x-api-key", "broker-managed"),
            ("Host", "127.0.0.1:8377"),
            ("Content-Type", "application/json"),
            ("anthropic-version", "2023-06-01"),
        ],
        "real-key",
    )
    assert headers["x-api-key"] == "real-key"
    assert headers["Host"] == anthropic_key_proxy.UPSTREAM_HOST
    assert headers["Content-Type"] == "application/json"
    assert headers["anthropic-version"] == "2023-06-01"
    # no decoy credential survives under any spelling
    lowered = {name.lower(): value for name, value in headers.items()}
    assert "authorization" not in lowered
    assert lowered["x-api-key"] == "real-key"


def test_hop_by_hop_headers_are_not_forwarded():
    headers = anthropic_key_proxy.build_upstream_headers(
        [
            ("Connection", "keep-alive"),
            ("Transfer-Encoding", "chunked"),
            ("Content-Length", "42"),
            ("Upgrade", "h2c"),
        ],
        "real-key",
    )
    assert set(headers) == {"Host", "x-api-key"}


def test_listener_is_localhost_only():
    assert anthropic_key_proxy.LISTEN_HOST == "127.0.0.1"


def test_upstream_is_the_anthropic_api():
    assert anthropic_key_proxy.UPSTREAM_HOST == "api.anthropic.com"


def test_refuses_to_start_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert anthropic_key_proxy.main(["--port", "8377"]) == 2
