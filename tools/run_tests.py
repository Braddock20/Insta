"""A very small test runner.

pytest is the right tool, but it is not always installed — a fresh server,
a container without wheels, an air-gapped machine. This runs the same test
files with nothing but the standard library.

    python3 tools/run_tests.py            # every test
    python3 tools/run_tests.py test_media # one file

It understands the part of pytest the suite uses: fixtures (including
``yield`` fixtures), ``pytest.raises`` and ``pytest.mark.parametrize``.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import shutil
import sys
import tempfile
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


# --------------------------------------------------------------- the shim
class _Raises:
    def __init__(self, expected, match=None):
        self.expected = expected
        self.match = match
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"DID NOT RAISE {self.expected}")
        if not issubclass(exc_type, self.expected):
            return False
        self.value = exc
        return True


def _fixture(*args, **kwargs):
    """Mark a function as a fixture (the decorator may or may not take args)."""

    def wrap(fn):
        fn.__is_fixture__ = True
        return fn

    if args and callable(args[0]):
        return wrap(args[0])
    return wrap


def _parametrize(names, values):
    def wrap(fn):
        cases = getattr(fn, "__parametrize__", [])
        cases.append((names, values))
        fn.__parametrize__ = cases
        return fn

    return wrap


pytest = types.ModuleType("pytest")
pytest.fixture = _fixture
pytest.raises = _Raises
pytest.mark = types.SimpleNamespace(
    parametrize=_parametrize, skip=lambda *a, **k: (lambda fn: fn), skipif=lambda *a, **k: (lambda fn: fn)
)
pytest.skip = lambda *a, **k: None
sys.modules.setdefault("pytest", pytest)

# PyYAML is in requirements.txt. When it is not installed, the small
# reader in this folder does the job for the subset the config uses.
try:  # pragma: no cover - depends on the machine
    import yaml  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import miniyaml

    sys.modules["yaml"] = miniyaml


# ----------------------------------------------------------------- runner
def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def collect_fixtures(module, seen: dict) -> None:
    for name, obj in vars(module).items():
        if callable(obj) and getattr(obj, "__is_fixture__", False):
            seen[name] = obj


def build(name, fixtures, cache, finalizers):
    """Resolve one fixture, with its own dependencies."""
    if name in cache:
        return cache[name]
    fn = fixtures.get(name)
    if fn is None:
        raise LookupError(f"no fixture called {name!r}")
    kwargs = {
        param: build(param, fixtures, cache, finalizers)
        for param in inspect.signature(fn).parameters
    }
    result = fn(**kwargs)
    if inspect.isgenerator(result):
        gen = result
        value = next(gen)
        finalizers.append(lambda g=gen: next(g, None))
    else:
        value = result
    cache[name] = value
    return value


def run_file(path: Path) -> tuple[int, int, list[str]]:
    fixtures: dict = {}
    cache: dict = {}
    finalizers: list = []
    passed = failed = 0
    failures: list[str] = []

    # pytest provides this one; here it is a fresh temp folder per test.
    made: list[str] = []
    fixtures["tmp_path"] = lambda: Path(tempfile.mkdtemp(prefix="igbot-test-"))
    fixtures.setdefault("_cleanup", lambda: None)

    conftest = TESTS / "conftest.py"
    if conftest.exists():
        collect_fixtures(load(conftest), fixtures)
    module = load(path)
    collect_fixtures(module, fixtures)

    for name, fn in sorted(vars(module).items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        cases = [({}, "")]
        for names, values in getattr(fn, "__parametrize__", []):
            expanded = []
            for base, label in cases:
                for value in values:
                    value = value if isinstance(value, tuple) else (value,)
                    merged = dict(base)
                    merged.update(dict(zip(names.split(","), value)))
                    expanded.append((merged, label + f"[{value}]"))
            cases = expanded
        for params, label in cases:
            cache.clear()
            finalizers.clear()
            try:
                kwargs = dict(params)
                for param in inspect.signature(fn).parameters:
                    if param not in kwargs:
                        kwargs[param] = build(param, fixtures, cache, finalizers)
                        if param == "tmp_path":
                            made.append(str(kwargs[param]))
                fn(**kwargs)
                passed += 1
            except Exception:  # noqa: BLE001
                failed += 1
                failures.append(
                    f"{path.name}::{name}{label}\n{traceback.format_exc()}"
                )
            finally:
                for fin in reversed(finalizers):
                    try:
                        fin()
                    except Exception:  # noqa: BLE001
                        pass
                for folder in made:
                    shutil.rmtree(folder, ignore_errors=True)
                made.clear()
    return passed, failed, failures


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(ROOT))
    targets = [a for a in argv if not a.startswith("-")]
    files = sorted(TESTS.glob("test_*.py"))
    if targets:
        files = [f for f in files if any(t in f.name for t in targets)]
    if not files:
        print("no test files found")
        return 1

    total_pass = total_fail = 0
    all_failures: list[str] = []
    for path in files:
        passed, failed, failures = run_file(path)
        total_pass += passed
        total_fail += failed
        all_failures.extend(failures)
        mark = "✅" if not failed else "❌"
        print(f"{mark} {path.name}: {passed} passed, {failed} failed")

    if all_failures:
        print("\n" + "=" * 70)
        for failure in all_failures:
            print(failure)
            print("-" * 70)
    print(f"\n{total_pass} passed, {total_fail} failed")
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
