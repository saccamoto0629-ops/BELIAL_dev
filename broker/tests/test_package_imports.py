"""PR #87: broker/ is a real Python package importable from the repository root.

These tests must not rely on any sys.path manipulation done by the test
process itself, so the decisive checks run in a SUBPROCESS started from the
repository root with a plain ``python -c "import ..."``.
"""

import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

ROOT_LEVEL_IMPORTS = (
    "from broker.broker_interface import Broker",
    "from broker.broker_models import OrderRequest, OrderResult",
    "from broker.fake_broker import FakeBroker",
    "from broker import broker_interface, broker_models, fake_broker",
)


def test_broker_package_has_an_init_file():
    assert (REPO_ROOT / "broker" / "__init__.py").is_file()


def test_root_level_package_imports_succeed_in_a_clean_subprocess():
    # A subprocess with cwd=REPO_ROOT reproduces exactly what a future
    # Live Execution caller (or CI) does: import the package from the root,
    # with no sys.path edits inherited from this test process.
    code = "; ".join(ROOT_LEVEL_IMPORTS) + "; print('ok')"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"root-level import failed:\n{result.stderr}"
    assert result.stdout.strip() == "ok"


def test_in_process_package_imports_expose_the_public_names():
    from broker.broker_interface import Broker
    from broker.broker_models import OrderRequest, OrderResult
    from broker.fake_broker import FakeBroker

    assert Broker is not None
    assert OrderRequest is not None and OrderResult is not None
    assert FakeBroker is not None
