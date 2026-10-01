"""Public API surface and packaging metadata.

Guards the claims the README makes: the documented names must be importable
from the top-level package, and the console script must point at a real callable.
"""

import importlib

import pytest

import tool_call_retry

README_EXPORTS = [
    "HTTPStatusError",
    "NonRetryableError",
    "RetryExhausted",
    "RetryableError",
    "RetryPolicy",
    "Saga",
    "SagaFailed",
    "SagaJournal",
    "SagaRun",
    "SagaStep",
    "StepStatus",
    "__version__",
]


@pytest.mark.parametrize("name", README_EXPORTS)
def test_documented_names_are_importable(name):
    assert hasattr(tool_call_retry, name), f"README documents tool_call_retry.{name}"
    assert name in tool_call_retry.__all__


def test_version_is_a_release_string():
    assert tool_call_retry.__version__.count(".") == 2


def test_console_script_target_resolves():
    from importlib.metadata import entry_points

    scripts = {
        ep.name: ep.value
        for ep in entry_points(group="console_scripts")
        if ep.name == "tool-call-retry"
    }
    assert scripts, "tool-call-retry console script is not registered"
    module_name, _, attr = scripts["tool-call-retry"].partition(":")
    module = importlib.import_module(module_name)
    assert callable(getattr(module, attr))


def test_python_dash_m_entry_point_exists():
    module = importlib.import_module("tool_call_retry.__main__")
    assert hasattr(module, "main")


def test_package_has_no_import_time_side_effects(tmp_path):
    """Importing must not create a journal or touch the filesystem."""
    import subprocess
    import sys

    before = set(tmp_path.iterdir())
    result = subprocess.run(
        [sys.executable, "-c", "import tool_call_retry; print(tool_call_retry.__version__)"],
        capture_output=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert set(tmp_path.iterdir()) == before


def test_public_names_are_sorted_in_dunder_all():
    """Keeps the export list diffable; catches accidental omissions."""
    assert list(tool_call_retry.__all__) == sorted(tool_call_retry.__all__)
