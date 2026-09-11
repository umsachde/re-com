"""The declared module list matches the modules that actually exist.

This project is a flat layout -- 21 modules at the repo root next to scripts/
and tests/ -- so setuptools' auto-discovery refuses to guess and `pip install
-e .` fails outright unless `[tool.setuptools] py-modules` lists them. That is
the loud half of the failure, and CI caught it on its first run.

The quiet half is what these tests exist for: a new root module that nobody
adds to the list stays perfectly importable from a checkout, because the tests
put the repo root on sys.path -- and is simply missing from an installed copy.
Nothing fails until someone imports it from an install, which on this project
means a live tool call on a user's machine.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# tomllib landed in 3.11; pyproject declares requires-python >= 3.10, so on the
# floor interpreter these checks are skipped rather than faked.
tomllib = pytest.importorskip("tomllib", reason="tomllib needs Python 3.11+")


def _declared():
    with open(ROOT / "pyproject.toml", "rb") as fh:
        config = tomllib.load(fh)
    return set(config["tool"]["setuptools"]["py-modules"])


def _on_disk():
    return {
        path.stem for path in ROOT.glob("*.py")
        if not path.stem.startswith("_") and path.stem != "conftest"
    }


def test_every_root_module_is_declared():
    missing = _on_disk() - _declared()
    assert not missing, (
        f"these modules exist but aren't in pyproject's py-modules: {sorted(missing)}. "
        "They import fine from a checkout and are absent from an installed copy."
    )


def test_every_declared_module_exists():
    # The other direction: a module that was renamed or deleted leaves a stale
    # entry, and setuptools builds a package referencing a file that is gone.
    phantom = _declared() - _on_disk()
    assert not phantom, f"declared in py-modules but not on disk: {sorted(phantom)}"


def test_the_package_is_importable_the_way_the_server_is_launched():
    # `claude mcp add ... -- python /path/to/server.py` is the documented entry
    # point, so server.py must import with only the repo root on the path.
    assert (ROOT / "server.py").exists()
    assert str(ROOT) in sys.path or str(ROOT) in [str(Path(p).resolve()) for p in sys.path]
