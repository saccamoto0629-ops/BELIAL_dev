"""Console-encoding-safe stream handling shared by the execution CLIs
(Issue #77 / PR #96 review r1; reused by the Issue #78 shadow CLIs).

The Windows-ja default console is cp932 and GitHub's Windows runners pipe
stdout through the ANSI codepage; CLI free text (scenario descriptions,
rejection messages) may not be encodable there, and a strict-encoding stream
would raise ``UnicodeEncodeError`` mid-report -- turning a successful run into
a crash with the wrong exit code. Inside :func:`encoding_safe_streams`
unencodable characters degrade to escape sequences instead; machine-readable
``--json`` output stays pure ASCII at the writer side and is unaffected.

The previous ``errors`` policy of each stream is restored in ``finally`` on
EVERY exit path -- normal return, usage error (``SystemExit`` from argparse),
schema error and unexpected exceptions -- so embedding callers (pytest, other
CLIs) never observe a leftover configuration. Streams that cannot be
reconfigured are left alone. This must only run inside a CLI ``main()``;
importing a CLI module changes nothing.
"""

from __future__ import annotations

import contextlib
import sys


@contextlib.contextmanager
def encoding_safe_streams():
    """Never let the console encoding change a CLI's exit-code contract --
    and never LEAK the changed stream configuration past ``main()``."""
    restores: list[tuple] = []
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        original_errors = getattr(stream, "errors", None)
        if not callable(reconfigure) or original_errors is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            continue
        restores.append((reconfigure, original_errors))
    try:
        yield
    finally:
        for reconfigure, original_errors in reversed(restores):
            try:
                reconfigure(errors=original_errors)
            except (ValueError, OSError):
                pass
