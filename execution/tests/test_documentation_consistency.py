"""Issue #132: the repository text must not contradict the mode registry.

Why this test exists
--------------------

Registering ``demo`` as a runnable execution mode invalidated statements
scattered across ~20 documents and a few docstrings ("the runner drives
fake / shadow only", "demo is not integrated", "no runner exists"). Those
statements are not decoration: an operator reading them concludes the demo
path does not exist, and the PR #136 review found three separate rounds of
leftovers because each sweep was a hand-written grep that missed
paraphrases and whole files.

So the claims that are now FALSE are pinned here instead of re-swept by
hand. This is the documentation equivalent of the AST pins in
``test_submission_boundary.py``: it does not check that prose is good, only
that a specific set of statements known to be false cannot reappear.

Scope and limits
----------------

- It pins a fixed set of statements that are false as of Issue #132, not a
  general "docs are accurate" property -- no test can check that.
- A future Issue that changes the mode registry again must update this
  list; the failure message says so.
- ``docs/development/reports/`` is excluded: release notes are historical
  records of what was true at that release (``demo`` genuinely was
  unregistered in v1.5), and rewriting them would falsify the record.
"""

import pathlib
import re

import pytest

from execution.live_runner import RUNNER_SUPPORTED_MODES
from execution.safety_gate import (
    BROKER_TYPE_BY_MODE,
    ExecutionMode,
    REAL_BROKER_MODES,
    UNIMPLEMENTED_BROKER_MODES,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Text files whose prose describes the current system.
SCANNED_SUFFIXES = {".md", ".py"}

#: Historical records: true when written, deliberately not rewritten.
EXCLUDED_PREFIXES = (
    "docs/development/reports/",
    "docs/archive/",
    # This file necessarily quotes the false statements it forbids.
    "execution/tests/test_documentation_consistency.py",
)

#: Statements that became FALSE when Issue #132 registered ``demo``, as
#: regular expressions.
#:
#: Precision matters in BOTH directions. Each entry must match the stale
#: claim and must NOT match a corrected sentence:
#:
#: - "Fake / Shadow / Demo" may never trip a "Fake / Shadow" entry, so the
#:   entries carry their limiting word ("のみ" / "限定" / "だけ");
#: - "**常駐** runner は未実装" / "**継続運転する** runner は未実装" are
#:   still TRUE (the continuous-operation process really is unimplemented),
#:   so the runner-existence entries exclude those qualifiers rather than
#:   matching any "runner ... 未実装";
#: - "v1.5 時点では ... 未登録" is a TRUE historical statement inside a
#:   version-scoped sentence, so the registration entry excludes it.
#:
#: An over-broad entry here is not harmless: it would push an author to
#: delete an accurate caveat about continuous operation, which is exactly
#: the safety statement this Issue must keep.
_RESIDENT = r"(?<!常駐 )(?<!常駐)(?<!継続運転する )(?<!定期実行 )(?<!定期実行)"

FALSE_AFTER_DEMO_INTEGRATION = (
    # the runner's mode list
    r"Fake / Shadow (のみ|限定|に限って)",
    r"`fake` / `shadow` (のみ|だけ|限定)",
    r"`fake` と `shadow` だけ",
    r"Fake / Shadow only",
    # demo's Execution-layer status
    r"`demo` は mode 定義のみ",
    r"`BROKER_TYPE_BY_MODE` への `demo` 登録は未実装",
    r"(?<!v1\.5 時点では )`BROKER_TYPE_BY_MODE` へ `demo` は未登録",
    r"(OANDA )?Demo integration is a separate Issue",
    r"OANDA Demo の runner 統合は未実装",
    r"実 Broker(OANDA Demo / Live)への runner 統合は未実装",
    r"OANDA Demo / Live へは runner から接続できない",
    # the runner core's existence (NOT the resident / continuous process,
    # which is genuinely still unimplemented)
    _RESIDENT + r"runner (は)?未実装",
)


def _scanned_files():
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        if any(part.startswith(".") or part in {"__pycache__", "venv", "node_modules"}
               for part in pathlib.Path(relative).parts):
            continue
        if relative.startswith(EXCLUDED_PREFIXES):
            continue
        yield relative, path


@pytest.mark.parametrize("statement", FALSE_AFTER_DEMO_INTEGRATION)
def test_no_document_repeats_a_statement_demo_integration_made_false(statement):
    """Issue #132 / PR #136 review: these statements described the system
    before ``demo`` became a runnable mode. If one reappears, either the
    text is stale or the mode registry changed -- in the second case, update
    this list together with the registry."""
    offenders = []
    for relative, path in _scanned_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(statement, line):
                offenders.append(f"{relative}:{lineno}")
    assert offenders == [], (
        f"{statement!r} is no longer true (Issue #132 registered 'demo' as a runnable "
        f"execution mode) but still appears at: {offenders}. Update the text, or -- if "
        "the mode registry itself changed -- update FALSE_AFTER_DEMO_INTEGRATION."
    )


def test_the_mode_registry_is_what_the_documentation_claims():
    """The other half of the pin: the statements above are only false while
    the registry actually says so. If this fails, the docs may be right and
    the list above is what needs changing."""
    assert RUNNER_SUPPORTED_MODES == {
        ExecutionMode.FAKE,
        ExecutionMode.SHADOW,
        ExecutionMode.DEMO,
    }
    assert ExecutionMode.LIVE not in RUNNER_SUPPORTED_MODES
    assert ExecutionMode.DEMO in BROKER_TYPE_BY_MODE
    assert REAL_BROKER_MODES == {ExecutionMode.DEMO, ExecutionMode.LIVE}
    assert UNIMPLEMENTED_BROKER_MODES == frozenset()


def test_live_is_still_documented_as_un_released():
    """The counterweight: this suite must not be usable to argue that live
    was released. The statement that keeps live closed has to stay
    present in the two documents that own it."""
    concept = (REPO_ROOT / "docs" / "product" / "concept.md").read_text(encoding="utf-8")
    operations = (REPO_ROOT / "docs" / "operations" / "operations.md").read_text(
        encoding="utf-8"
    )
    assert "未解禁" in concept
    assert "Live 運用開始の事前条件" in operations


# --- the pin's own tests ---------------------------------------------------
#
# A pattern with a typo matches nothing and passes silently, which would
# make the whole file a no-op. So the patterns are exercised against the
# ACTUAL sentences this PR removed, and against the corrected ones.

REMOVED_BY_THIS_ISSUE = (
    "駆動できる mode は **`fake` と `shadow` だけ**",
    "Issue #131 の runner core は `fake` / `shadow` のみを駆動し",
    "runner core は Fake / Shadow のみ実装済み",
    "Issue #131 で **runner core も Fake / Shadow に限って実装済み**",
    '"""Issue #131: the Live Execution runner core (Fake / Shadow only).',
    "driven by this runner (OANDA Demo integration is a separate Issue, and",
    "**OANDA Demo / Live へは runner から接続できない**",
    "Live Execution(基盤実装済み・runner 未実装・運用未解禁)",
    "`demo` は mode 定義のみで Execution 層への組み込み未実装",
)

KEPT_ON_PURPOSE = (
    # the corrected mode list must never trip the pin
    "駆動できる mode は **`fake` / `shadow` / `demo`**",
    "runner core は Issue #131 / #132 で `fake` / `shadow` / `demo` まで実装済み",
    "Fake / Shadow / Demo are three different things",
    "- **Fake / Shadow / Demo の責務**",
    # ... and neither may the TRUE caveats about continuous operation,
    # which are the safety statements this Issue must keep
    "継続運転する常駐 runner は未実装",
    "常駐 runner 未実装・実口座運用は未解禁",
    "定期実行 runner は未実装",
    "v1.5 時点では `BROKER_TYPE_BY_MODE` へ `demo` は未登録",
)


@pytest.mark.parametrize("sentence", REMOVED_BY_THIS_ISSUE)
def test_the_pin_catches_the_statements_this_issue_removed(sentence):
    """If none of the patterns match a sentence this PR actually deleted,
    the pin would not have prevented the review rounds that produced it."""
    assert any(re.search(pattern, sentence) for pattern in FALSE_AFTER_DEMO_INTEGRATION), (
        f"no pattern matches {sentence!r}; the pin would not catch its reintroduction"
    )


@pytest.mark.parametrize("sentence", KEPT_ON_PURPOSE)
def test_the_pin_does_not_flag_a_correct_or_still_true_statement(sentence):
    """The costly failure mode: an over-broad pattern that pressures an
    author into deleting an accurate caveat (especially "the continuous
    runner is unimplemented", which must survive)."""
    matched = [pattern for pattern in FALSE_AFTER_DEMO_INTEGRATION if re.search(pattern, sentence)]
    assert matched == [], f"{sentence!r} is correct but matched {matched}"
