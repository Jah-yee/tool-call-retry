"""CLI smoke tests for run, status, recover, journal (issue #5)."""

import json

import pytest

from tool_call_retry.cli import main
from tool_call_retry.journal import SagaJournal

TOOLS_MODULE = """
CALLS = []
UNDO = []


def charge(amount=10):
    CALLS.append(("charge", amount))
    return {"charge_id": "ch_1"}


def refund(result=None):
    UNDO.append("refund")
    return "refunded"


def ship(address="Rua X"):
    CALLS.append(("ship", address))
    return {"tracking": "ABC"}


def boom():
    from tool_call_retry.errors import NonRetryableError

    raise NonRetryableError("out of stock")
"""


@pytest.fixture
def tools(tmp_path):
    """Write an importable module and make it importable for the CLI."""
    import sys

    path = tmp_path / "faketools.py"
    path.write_text(TOOLS_MODULE)
    tmp_path_str = str(tmp_path)
    sys.path.insert(0, tmp_path_str)
    try:
        yield path
    finally:
        sys.path.remove(tmp_path_str)
        sys.modules.pop("faketools", None)


def write_config(path, steps, **extra):
    import yaml

    payload = {"name": "checkout", "idempotency_key": "order-123", "steps": steps}
    payload.update(extra)
    path.write_text(yaml.safe_dump(payload))
    return path


def test_help_lists_all_four_subcommands(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for command in ("run", "status", "recover", "journal"):
        assert command in out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["run", "status", "recover", "journal"])
def test_each_subcommand_has_help_text(command, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([command, "--help"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip()


def test_run_executes_a_successful_saga(tools, tmp_path, capsys):
    db = tmp_path / "saga.db"
    config = write_config(
        tmp_path / "saga.yml",
        [
            {"name": "charge", "do": "faketools:charge", "args": {"amount": 10}},
            {"name": "ship", "do": "faketools:ship"},
        ],
        journal=str(db),
    )
    code = main(["run", "--config", str(config), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert [s["name"] for s in payload["steps"]] == ["charge", "ship"]
    assert payload["steps"][0]["result"] == {"charge_id": "ch_1"}


def test_run_compensates_and_exits_1_on_failure(tools, tmp_path, capsys):
    db = tmp_path / "saga.db"
    config = write_config(
        tmp_path / "saga.yml",
        [
            {
                "name": "charge",
                "do": "faketools:charge",
                "compensate": "faketools:refund",
            },
            {"name": "ship", "do": "faketools:boom"},
        ],
        journal=str(db),
    )
    code = main(["run", "--config", str(config), "--json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["failed_step"] == "ship"
    assert payload["compensated"] == ["charge"]
    assert "compensated" in payload["summary"]

    import faketools

    assert faketools.UNDO == ["refund"]


def test_run_human_output_on_failure(tools, tmp_path, capsys):
    config = write_config(
        tmp_path / "saga.yml",
        [{"name": "ship", "do": "faketools:boom"}],
        journal=str(tmp_path / "saga.db"),
    )
    code = main(["run", "--config", str(config)])
    assert code == 1
    # Failure diagnostics go to stderr so stdout stays machine-parseable.
    err = capsys.readouterr().err
    assert "failed" in err
    assert "out of stock" in err, "the underlying cause must be reported"


def test_run_rejects_a_config_pointing_at_a_missing_callable(tools, tmp_path, capsys):
    config = write_config(
        tmp_path / "saga.yml",
        [{"name": "charge", "do": "faketools:nope"}],
        journal=str(tmp_path / "saga.db"),
    )
    assert main(["run", "--config", str(config), "--json"]) == 2
    assert "nope" in capsys.readouterr().err


def test_run_missing_config_file_exits_2(capsys):
    assert main(["run", "--config", "/nonexistent/saga.yml"]) == 2
    assert "not found" in capsys.readouterr().err.lower()


def test_run_accepts_a_retry_policy_block(tools, tmp_path, capsys):
    config = write_config(
        tmp_path / "saga.yml",
        [{"name": "charge", "do": "faketools:charge"}],
        journal=str(tmp_path / "saga.db"),
        retry={"max_attempts": 5, "base_delay": 0.01, "jitter": "none"},
    )
    assert main(["run", "--config", str(config), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


def test_status_reports_a_saga(tools, tmp_path, capsys):
    db = tmp_path / "saga.db"
    config = write_config(
        tmp_path / "saga.yml",
        [{"name": "charge", "do": "faketools:charge"}],
        journal=str(db),
    )
    assert main(["run", "--config", str(config), "--json"]) == 0
    capsys.readouterr()
    saga_id = SagaJournal(str(db)).list_sagas()[0]["id"]

    assert main(["status", "--saga-id", saga_id, "--db", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["saga_id"] == saga_id
    assert payload["status"] == "completed"
    assert payload["completed_steps"] == ["charge"]


def test_status_unknown_saga_exits_2(tmp_path, capsys):
    db = tmp_path / "saga.db"
    SagaJournal(str(db)).close()
    assert main(["status", "--saga-id", "nope", "--db", str(db)]) == 2
    assert "unknown saga" in capsys.readouterr().err.lower()


def test_recover_lists_interrupted_sagas(tools, tmp_path, capsys):
    db = tmp_path / "saga.db"
    journal = SagaJournal(str(db))
    saga_id = journal.begin_saga(idempotency_key="order-crash", name="checkout")
    journal.record_step(saga_id, 1, "charge", {})
    journal.mark_step_running(saga_id, 1)
    journal.close()

    assert main(["recover", "--db", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [p["id"] for p in payload["pending"]] == [saga_id]
    assert payload["pending"][0]["interrupted_steps"] == 1


def test_recover_resumes_a_saga_when_asked(tools, tmp_path, capsys):
    db = tmp_path / "saga.db"
    journal = SagaJournal(str(db))
    saga_id = journal.begin_saga(idempotency_key="order-123", name="checkout")
    journal.record_step(saga_id, 1, "charge", {})
    journal.mark_step_running(saga_id, 1)
    journal.mark_step_completed(saga_id, 1, {"charge_id": "ch_1"})
    journal.record_step(saga_id, 2, "ship", {})
    journal.close()

    config = write_config(
        tmp_path / "saga.yml",
        [
            {"name": "charge", "do": "faketools:charge"},
            {"name": "ship", "do": "faketools:ship"},
        ],
        journal=str(db),
    )
    code = main(["recover", "--db", str(db), "--resume", "--config", str(config), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["resumed"][0]["status"] == "completed"

    import faketools

    # 'charge' was already done, so only 'ship' re-ran.
    assert [name for name, _ in faketools.CALLS] == ["ship"]


def test_recover_with_nothing_pending(tools, tmp_path, capsys):
    db = tmp_path / "saga.db"
    journal = SagaJournal(str(db))
    saga_id = journal.begin_saga(idempotency_key="k")
    journal.record_step(saga_id, 1, "charge", {})
    journal.mark_step_running(saga_id, 1)
    journal.mark_step_completed(saga_id, 1, "ok")
    journal.mark_saga_completed(saga_id)
    journal.close()

    assert main(["recover", "--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pending"] == []


def test_journal_shows_the_operation_log(tools, tmp_path, capsys):
    db = tmp_path / "saga.db"
    config = write_config(
        tmp_path / "saga.yml",
        [{"name": "charge", "do": "faketools:charge"}],
        journal=str(db),
    )
    assert main(["run", "--config", str(config), "--json"]) == 0
    capsys.readouterr()
    saga_id = SagaJournal(str(db)).list_sagas()[0]["id"]

    assert main(["journal", "--saga-id", saga_id, "--db", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    operations = [e["operation"] for e in payload["operations"]]
    assert operations[:3] == ["begin_saga", "record_step", "step_running"]
    assert "step_completed" in operations


def test_journal_human_output(tools, tmp_path, capsys):
    db = tmp_path / "saga.db"
    config = write_config(
        tmp_path / "saga.yml",
        [{"name": "charge", "do": "faketools:charge"}],
        journal=str(db),
    )
    main(["run", "--config", str(config), "--json"])
    capsys.readouterr()
    saga_id = SagaJournal(str(db)).list_sagas()[0]["id"]

    assert main(["journal", "--saga-id", saga_id, "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "begin_saga" in out
    assert "step_completed" in out


def test_list_sagas_subcommand(tools, tmp_path, capsys):
    db = tmp_path / "saga.db"
    config = write_config(
        tmp_path / "saga.yml",
        [{"name": "charge", "do": "faketools:charge"}],
        journal=str(db),
    )
    main(["run", "--config", str(config), "--json"])
    capsys.readouterr()
    assert main(["list", "--db", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sagas"][0]["name"] == "checkout"


def test_cleanup_subcommand(tools, tmp_path, capsys):
    db = tmp_path / "saga.db"
    journal = SagaJournal(str(db))
    saga_id = journal.begin_saga(idempotency_key="k")
    journal.record_step(saga_id, 1, "charge", {})
    journal.mark_step_running(saga_id, 1)
    journal.mark_step_completed(saga_id, 1, "ok")
    journal.mark_saga_completed(saga_id)
    journal.close()

    code = main(["cleanup", "--db", str(db), "--max-age", "0", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["removed"] >= 0


def test_unknown_subcommand_exits_2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])
    assert excinfo.value.code == 2
