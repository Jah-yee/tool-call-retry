"""SQLite journal: idempotency, crash recovery, cleanup, compensation tracking.

Addresses issue #2 (journal) and issue #7 (journal test surface).
"""

import json
import pathlib
import sqlite3
import subprocess
import sys

import pytest

from tool_call_retry.errors import InvalidTransition
from tool_call_retry.journal import SagaJournal
from tool_call_retry.models import StepStatus


def test_schema_is_created_with_issue_2_columns(journal):
    tables = {
        row[0]
        for row in journal._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"sagas", "steps", "retry_attempts"} <= tables
    saga_cols = {row[1] for row in journal._conn.execute("PRAGMA table_info(sagas)")}
    assert {"id", "status", "created_at", "updated_at"} <= saga_cols
    step_cols = {row[1] for row in journal._conn.execute("PRAGMA table_info(steps)")}
    assert {
        "saga_id",
        "step_number",
        "tool_name",
        "tool_args",
        "result",
        "status",
        "idempotency_key",
    } <= step_cols


def test_wal_mode_is_enabled_for_file_backed_journals(journal_path):
    j = SagaJournal(str(journal_path))
    mode = j._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_begin_saga_returns_new_and_then_existing_for_same_key(journal):
    first = journal.begin_saga(idempotency_key="order-123", name="checkout")
    second = journal.begin_saga(idempotency_key="order-123", name="checkout")
    assert first == second
    assert journal.saga_count() == 1


def test_reusing_a_key_returns_the_existing_saga_with_its_steps(journal, sample_records):
    saga_id = journal.begin_saga(idempotency_key="order-123", name="checkout")
    for index, record in enumerate(sample_records, start=1):
        journal.record_step(saga_id, index, record["name"], record["tool_args"])
        journal.mark_step_completed(saga_id, index, record["result"])

    resumed = journal.begin_saga(idempotency_key="order-123", name="checkout")
    assert resumed == saga_id
    run = journal.load_saga(resumed)
    assert run.completed_steps == ["charge_card", "reserve_inventory"]


def test_distinct_keys_create_distinct_sagas(journal):
    a = journal.begin_saga(idempotency_key="order-123")
    b = journal.begin_saga(idempotency_key="order-456")
    assert a != b
    assert journal.saga_count() == 2


def test_step_idempotency_key_is_unique_across_sagas(journal):
    saga_a = journal.begin_saga(idempotency_key="a")
    saga_b = journal.begin_saga(idempotency_key="b")
    journal.record_step(saga_a, 1, "charge", {}, idempotency_key="charge-1")
    with pytest.raises(sqlite3.IntegrityError):
        journal.record_step(saga_b, 1, "charge", {}, idempotency_key="charge-1")


def test_record_and_retrieve_steps_with_status(journal, sample_records):
    saga_id = journal.begin_saga(idempotency_key="order-123", name="checkout")
    for index, record in enumerate(sample_records, start=1):
        journal.record_step(saga_id, index, record["name"], record["tool_args"])
    journal.mark_step_running(saga_id, 1)
    journal.mark_step_completed(saga_id, 1, sample_records[0]["result"])

    run = journal.load_saga(saga_id)
    assert [s.name for s in run.steps] == ["charge_card", "reserve_inventory"]
    assert run.steps[0].status is StepStatus.COMPLETED
    assert run.steps[0].result == {"charge_id": "ch_1"}
    assert run.steps[1].status is StepStatus.PENDING
    assert run.steps[1].tool_args == {"item_id": "sku-9"}


def test_journal_entries_keep_a_monotonic_operation_log(journal):
    saga_id = journal.begin_saga(idempotency_key="order-123")
    journal.record_step(saga_id, 1, "charge", {"amount": 10})
    journal.mark_step_running(saga_id, 1)
    journal.mark_step_completed(saga_id, 1, "ok")

    entries = journal.operations(saga_id)
    assert [e["operation"] for e in entries] == [
        "begin_saga",
        "record_step",
        "step_running",
        "step_completed",
    ]
    assert all(e["timestamp"] > 0 for e in entries)
    assert entries[2]["payload"]["tool_name"] == "charge"


def test_record_retry_attempts_is_auditable(journal):
    saga_id = journal.begin_saga(idempotency_key="order-123")
    journal.record_step(saga_id, 1, "charge", {"amount": 10})
    journal.record_attempt(saga_id, 1, attempt=1, error="TimeoutError: slow", delay=0.1)
    journal.record_attempt(saga_id, 1, attempt=2, succeeded=True)

    attempts = journal.attempts(saga_id)
    assert [a["attempt"] for a in attempts] == [1, 2]
    assert attempts[0]["error"] == "TimeoutError: slow"
    assert attempts[1]["succeeded"] == 1


def test_resume_saga_returns_last_incomplete_step(journal, sample_records):
    saga_id = journal.begin_saga(idempotency_key="order-123")
    for index, record in enumerate(sample_records, start=1):
        journal.record_step(saga_id, index, record["name"], record["tool_args"])
    journal.mark_step_running(saga_id, 1)
    journal.mark_step_completed(saga_id, 1, sample_records[0]["result"])

    pending = journal.resume_saga(saga_id)
    assert pending == [2]
    assert journal.load_saga(saga_id).steps[1].status is StepStatus.PENDING


def test_resume_saga_marks_interrupted_running_steps_as_pending(journal):
    """A step left in 'running' means the process died mid-call."""
    saga_id = journal.begin_saga(idempotency_key="order-123")
    journal.record_step(saga_id, 1, "charge", {})
    journal.mark_step_running(saga_id, 1)
    journal.record_step(saga_id, 2, "ship", {})
    journal.mark_step_running(saga_id, 2)
    journal.mark_step_completed(saga_id, 1, "ok")

    pending = journal.resume_saga(saga_id)
    assert pending == [2]
    assert journal.load_saga(saga_id).steps[1].status is StepStatus.PENDING


def test_resume_a_completed_saga_has_nothing_pending(journal):
    saga_id = journal.begin_saga(idempotency_key="order-123")
    journal.record_step(saga_id, 1, "charge", {})
    journal.mark_step_running(saga_id, 1)
    journal.mark_step_completed(saga_id, 1, "ok")
    journal.mark_saga_completed(saga_id)
    assert journal.resume_saga(saga_id) == []


def test_begin_and_record_compensation_are_tracked_per_step(journal):
    saga_id = journal.begin_saga(idempotency_key="order-123")
    for index, name in enumerate(["charge", "reserve"], start=1):
        journal.record_step(saga_id, index, name, {})
        journal.mark_step_running(saga_id, index)
        journal.mark_step_completed(saga_id, index, f"{name}-ok")

    journal.begin_compensation(saga_id)
    for index, undo_result in ((2, "released"), (1, "refunded")):
        journal.begin_step_compensation(saga_id, index)
        journal.record_compensation(saga_id, index, undo_result)
    journal.mark_saga_failed(saga_id)

    run = journal.load_saga(saga_id)
    assert run.status == "failed"
    assert [s.status for s in run.steps] == [
        StepStatus.COMPENSATED,
        StepStatus.COMPENSATED,
    ]
    assert run.steps[0].result == "refunded"
    ops = [e["operation"] for e in journal.operations(saga_id)]
    assert "begin_compensation" in ops and "record_compensation" in ops


def test_begin_compensation_rejects_unknown_saga(journal):
    with pytest.raises(KeyError):
        journal.begin_compensation("nope")


def test_mark_step_completed_on_unknown_saga_raises(journal):
    with pytest.raises(KeyError):
        journal.mark_step_completed("nope", 1, "ok")


def test_mark_step_failed_records_the_error(journal):
    saga_id = journal.begin_saga(idempotency_key="order-123")
    journal.record_step(saga_id, 1, "ship", {})
    journal.mark_step_running(saga_id, 1)
    journal.mark_step_failed(saga_id, 1, "HTTPStatusError(404, 'Not Found')")

    run = journal.load_saga(saga_id)
    assert run.steps[0].status is StepStatus.FAILED
    assert "404" in run.steps[0].error
    assert run.failed_step.name == "ship"


def test_invalid_marking_rejected_by_journal(journal):
    saga_id = journal.begin_saga(idempotency_key="order-123")
    journal.record_step(saga_id, 1, "charge", {})
    journal.mark_step_completed(saga_id, 1, "ok")  # legal: an idempotency hit
    with pytest.raises(InvalidTransition):
        journal.mark_step_completed(saga_id, 1, "again")  # completed is terminal


def test_reset_for_retry_reopens_compensated_steps(journal):
    saga_id = journal.begin_saga(idempotency_key="order-123")
    journal.record_step(saga_id, 1, "charge", {})
    journal.mark_step_running(saga_id, 1)
    journal.mark_step_completed(saga_id, 1, "ok")
    journal.record_step(saga_id, 2, "ship", {})
    journal.begin_compensation(saga_id)
    journal.begin_step_compensation(saga_id, 1)
    journal.record_compensation(saga_id, 1, "refunded")

    assert journal.reset_for_retry(saga_id) == [1]
    run = journal.load_saga(saga_id)
    assert run.steps[0].status is StepStatus.PENDING
    assert run.steps[0].result is None
    assert run.steps[1].status is StepStatus.PENDING


def test_state_survives_reopening_the_database(journal_path):
    j = SagaJournal(str(journal_path))
    saga_id = j.begin_saga(idempotency_key="order-123", name="checkout")
    j.record_step(saga_id, 1, "charge_card", {"amount": 10})
    j.mark_step_running(saga_id, 1)
    j.mark_step_completed(saga_id, 1, {"charge_id": "ch_1"})
    del j

    reopened = SagaJournal(str(journal_path))
    run = reopened.load_saga(saga_id)
    assert run.status == "active"
    assert run.completed_steps == ["charge_card"]
    assert reopened.resume_saga(saga_id) == []


def test_state_survives_a_hard_process_kill(journal_path, tmp_path):
    """The real crash-recovery test: a child process is killed mid-saga."""
    src_dir = str(pathlib.Path(__file__).resolve().parent.parent / "src")
    script = tmp_path / "crash.py"
    script.write_text(
        "import os\n"
        "import sys\n"
        f"sys.path.insert(0, {src_dir!r})\n"
        "from tool_call_retry.journal import SagaJournal\n"
        f"j = SagaJournal({str(journal_path)!r})\n"
        "sid = j.begin_saga(idempotency_key='order-crash', name='checkout')\n"
        "j.record_step(sid, 1, 'charge_card', {'amount': 10})\n"
        "j.mark_step_running(sid, 1)\n"
        "j.mark_step_completed(sid, 1, {'charge_id': 'ch_1'})\n"
        "j.record_step(sid, 2, 'ship', {'address': 'Rua X'})\n"
        "os._exit(9)\n"
    )
    env = {"PYTHONPATH": src_dir}
    proc = subprocess.run([sys.executable, str(script)], env=env, capture_output=True)
    assert proc.returncode == 9, proc.stderr.decode()

    reopened = SagaJournal(str(journal_path))
    saga_ids = [s["id"] for s in reopened.list_sagas()]
    assert len(saga_ids) == 1
    run = reopened.load_saga(saga_ids[0])
    assert run.completed_steps == ["charge_card"]
    # Step 2 was recorded but never started: it is what a resume would re-run.
    assert reopened.resume_saga(run.saga_id) == [2]


def test_recover_pending_lists_interrupted_sagas(journal):
    active = journal.begin_saga(idempotency_key="a")
    journal.record_step(active, 1, "charge", {})
    journal.mark_step_running(active, 1)
    journal.mark_step_completed(active, 1, "ok")

    crashed = journal.begin_saga(idempotency_key="b")
    journal.record_step(crashed, 1, "ship", {})
    journal.mark_step_running(crashed, 1)

    done = journal.begin_saga(idempotency_key="c")
    journal.record_step(done, 1, "refund", {})
    journal.mark_step_running(done, 1)
    journal.mark_step_completed(done, 1, "ok")
    journal.mark_saga_completed(done)

    pending = journal.recover_pending()
    assert [p["id"] for p in pending] == [crashed]
    assert pending[0]["interrupted_steps"] == 1


def test_list_sagas_reports_status_and_step_counts(journal):
    saga_id = journal.begin_saga(idempotency_key="a", name="checkout")
    journal.record_step(saga_id, 1, "charge", {})
    journal.mark_step_running(saga_id, 1)
    journal.mark_step_completed(saga_id, 1, "ok")

    rows = journal.list_sagas()
    assert rows[0]["id"] == saga_id
    assert rows[0]["name"] == "checkout"
    assert rows[0]["status"] == "active"
    assert rows[0]["steps"] == 1
    assert rows[0]["completed_steps"] == 1


def test_cleanup_removes_old_completed_sagas_only(journal):
    old = journal.begin_saga(idempotency_key="old")
    journal.record_step(old, 1, "charge", {})
    journal.mark_step_running(old, 1)
    journal.mark_step_completed(old, 1, "ok")
    journal.mark_saga_completed(old)

    active = journal.begin_saga(idempotency_key="active")
    journal.record_step(active, 1, "ship", {})

    assert journal.cleanup(max_age_seconds=60) == 0
    journal._conn.execute(
        "UPDATE sagas SET updated_at = updated_at - 3600 WHERE id = ?", (old,)
    )
    journal._conn.commit()

    assert journal.cleanup(max_age_seconds=60) == 1
    assert [s["id"] for s in journal.list_sagas()] == [active]


def test_saga_id_is_derived_from_the_idempotency_key_for_stability(journal):
    saga_id = journal.begin_saga(idempotency_key="order-123")
    assert saga_id.startswith("saga-")
    assert journal.begin_saga(idempotency_key="order-123") == saga_id


def test_unknown_saga_id_can_be_supplied_explicitly(journal):
    saga_id = journal.begin_saga(saga_id="custom-1", idempotency_key="k")
    assert saga_id == "custom-1"
    assert journal.load_saga("custom-1").saga_id == "custom-1"
    with pytest.raises(KeyError):
        journal.load_saga("missing")


def test_tool_args_and_results_round_trip_through_json(journal):
    payload = {"nested": {"list": [1, 2, {"x": None}]}, "unicode": "ação"}
    saga_id = journal.begin_saga(idempotency_key="a")
    journal.record_step(saga_id, 1, "charge", payload)
    journal.mark_step_running(saga_id, 1)
    journal.mark_step_completed(saga_id, 1, {"ok": True})

    row = journal._conn.execute(
        "SELECT tool_args, result FROM steps WHERE saga_id = ? AND step_number = 1",
        (saga_id,),
    ).fetchone()
    assert json.loads(row[0]) == payload
    assert json.loads(row[1]) == {"ok": True}
    assert journal.load_saga(saga_id).steps[0].tool_args == payload
