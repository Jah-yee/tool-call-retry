"""Shared pytest fixtures.

Issue #7 asks for reusable fixtures so journal, saga and CLI tests all use the
same setup. Everything here is in-memory or tmp_path scoped; no test touches a
real database file outside of ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tool_call_retry.journal import SagaJournal
from tool_call_retry.models import SagaRun

# Terminal vs transient tool-call records reused across suites.
SAMPLE_RECORDS = [
    {"name": "charge_card", "tool_args": {"amount": 10}, "result": {"charge_id": "ch_1"}},
    {
        "name": "reserve_inventory",
        "tool_args": {"item_id": "sku-9"},
        "result": {"reservation": "r_1"},
    },
]


@pytest.fixture
def journal() -> SagaJournal:
    """In-memory SQLite journal (WAL is a no-op for :memory: but schema is identical)."""
    return SagaJournal(":memory:")


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    """Filesystem path for a saga journal inside a throwaway directory."""
    return tmp_path / "saga.db"


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Temporary saga run directory for CLI/YAML-config tests."""
    path = tmp_path / "run"
    path.mkdir()
    return path


@pytest.fixture
def sample_records() -> list[dict]:
    return [dict(record) for record in SAMPLE_RECORDS]


@pytest.fixture
def saga_run() -> SagaRun:
    """A two-step run with the first step completed (the classic partial-failure shape)."""
    run = SagaRun(saga_id="saga-1", name="checkout", idempotency_key="order-123")
    run.add_step("charge_card", {"amount": 10})
    run.transition_step(1, "running")
    run.transition_step(1, "completed", result={"charge_id": "ch_1"})
    run.add_step("ship_package", {"address": "Rua X"})
    return run
