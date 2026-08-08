#!/usr/bin/env python3
"""Local Anthropic credential broker for the codex-autofix workflow.

PR #110 review r3740741454: if ANTHROPIC_API_KEY sits in the
Claude Code process environment, every child process Claude spawns --
including repository test commands from the mutable PR branch -- inherits
and can exfiltrate the repository secret.

This broker removes the secret from Claude's session entirely:

- It runs as the RUNNER user and is the only process holding the real
  ANTHROPIC_API_KEY (taken from its own environment at startup).
- Claude Code runs as a separate unprivileged user with a decoy key and
  ``ANTHROPIC_BASE_URL`` pointed at this listener on 127.0.0.1. That user
  cannot read the runner user's files or ``/proc/<pid>/environ``, so
  neither Claude nor any child it spawns can reach the credential.
- Every request is forwarded to ``api.anthropic.com`` with client-supplied
  credentials stripped and the real key injected upstream.

Residual, accepted exposure: processes in Claude's session can still SEND
requests through the broker while the job runs (the same Anthropic-API
capability Claude itself has) -- but they can never read the key.

Stdlib only. Never logs headers or bodies.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import os
import sys

UPSTREAM_HOST = "api.anthropic.com"
LISTEN_HOST = "127.0.0.1"  # localhost only; never bind a routable interface

# Client headers that must never be forwarded upstream: credentials are
# replaced with the broker's own; the rest are hop-by-hop / recomputed.
STRIPPED_CLIENT_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "host",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
)

# Upstream headers not to relay back verbatim (the broker re-chunks the
# body itself).
STRIPPED_UPSTREAM_HEADERS = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "content-length"}
)


def build_upstream_headers(client_headers, api_key: str) -> dict:
    """Forwardable headers: client credentials out, the real key in."""
    headers = {}
    for name, value in client_headers:
        if name.lower() in STRIPPED_CLIENT_HEADERS:
            continue
        headers[name] = value
    headers["Host"] = UPSTREAM_HOST
    headers["x-api-key"] = api_key
    return headers


class BrokerHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    api_key = ""

    def _proxy(self) -> None:
        # Claude Code sends JSON bodies with Content-Length; chunked request
        # bodies are not supported (the CLI does not use them).
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        connection = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=600)
        try:
            try:
                connection.request(
                    self.command,
                    self.path,
                    body=body,
                    headers=build_upstream_headers(self.headers.items(), self.api_key),
                )
                upstream = connection.getresponse()
            except OSError as error:
                self.send_error(502, f"upstream connection failed: {error.__class__.__name__}")
                return
            self.send_response(upstream.status)
            for name, value in upstream.getheaders():
                if name.lower() in STRIPPED_UPSTREAM_HEADERS:
                    continue
                self.send_header(name, value)
            # Re-chunk so streaming (SSE) responses flow through unbuffered.
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = upstream.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        finally:
            connection.close()

    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy
    do_PATCH = _proxy

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Status lines only -- headers and bodies are never logged, so the
        # credential cannot leak into the workflow log.
        sys.stderr.write("[key-broker] %s\n" % (format % args))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[key-broker] ANTHROPIC_API_KEY is not set; refusing to start", file=sys.stderr)
        return 2
    BrokerHandler.api_key = api_key
    server = http.server.ThreadingHTTPServer((LISTEN_HOST, args.port), BrokerHandler)
    print(f"[key-broker] listening on {LISTEN_HOST}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
