"""Data models: saga/step status enums, state-transition validity, serialization."""

from tool_call_retry.errors import InvalidTransition
from tool_call_retry.models import RetryAttempt, SagaRun, StepStatus, ToolCall


def make_run(**kwargs):
    defaults = dict(
        saga_id="saga-1",
        name="checkout",
        idempotency_key="order-123",
    )
    defaults.update(kwargs)
    return SagaRun(**defaults)


def test_saga_run_starts_active_with_no_steps():
    run = make_run()
    assert run.status == "active"
    assert run.steps == []


def test_add_step_starts_pending_and_numbered_from_one():
    run = make_run()
    first = run.add_step("charge_card", {"amount": 10})
    second = run.add_step("ship", {"address": "Rua X"})
    assert (first.step_id, second.step_id) == (1, 2)
    assert first.status is StepStatus.PENDING
    assert run.steps == [first, second]


def test_duplicate_step_name_is_rejected():
    run = make_run()
    run.add_step("charge_card", {})
    try:
        run.add_step("charge_card", {})
    except ValueError as exc:
        assert "charge_card" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("duplicate step name must raise")


def test_step_transitions_pending_running_completed():
    run = make_run()
    step = run.add_step("charge_card", {"amount": 10})
    run.transition_step(1, StepStatus.RUNNING)
    run.transition_step(1, StepStatus.COMPLETED, result={"charge_id": "ch_1"})
    assert step.status is StepStatus.COMPLETED
    assert step.result == {"charge_id": "ch_1"}
    assert step.attempts == 1


def test_step_transitions_pending_failed():
    run = make_run()
    run.add_step("charge_card", {})
    run.transition_step(1, StepStatus.FAILED, error="timeout")
    assert run.steps[0].status is StepStatus.FAILED


def test_step_transitions_compensation_path():
    run = make_run()
    run.add_step("charge_card", {})
    run.add_step("ship", {})
    run.transition_step(1, StepStatus.RUNNING)
    run.transition_step(1, StepStatus.COMPLETED, result="ok")
    run.transition_saga_status("compensating")
    run.transition_step(1, StepStatus.COMPENSATING)
    run.transition_step(1, StepStatus.COMPENSATED, result="refunded")
    assert run.steps[0].status is StepStatus.COMPENSATED
    assert run.steps[0].result == "refunded"


def test_illegal_step_transition_raises_invalid_transition():
    run = make_run()
    run.add_step("charge_card", {})
    try:
        run.transition_step(1, StepStatus.COMPENSATED)
    except InvalidTransition as exc:
        assert "pending" in str(exc) and "compensated" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("pending -> compensated must raise InvalidTransition")


def test_failed_step_cannot_become_completed():
    run = make_run()
    run.add_step("charge_card", {})
    run.transition_step(1, StepStatus.FAILED, error="boom")
    try:
        run.transition_step(1, StepStatus.COMPLETED, result="ok")
    except InvalidTransition:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("failed -> completed must raise InvalidTransition")


def test_saga_status_transitions_are_validated():
    run = make_run()
    run.transition_saga_status("completed")
    try:
        run.transition_saga_status("active")
    except InvalidTransition:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("completed -> active must raise InvalidTransition")


def test_terminal_step_number_and_compensated_steps():
    run = make_run()
    run.add_step("charge_card", {})
    run.add_step("reserve", {})
    run.add_step("ship", {})
    run.transition_step(1, StepStatus.RUNNING)
    run.transition_step(1, StepStatus.COMPLETED, result="ok")
    run.transition_step(2, StepStatus.RUNNING)
    run.transition_step(2, StepStatus.COMPLETED, result="ok")
    run.transition_step(3, StepStatus.RUNNING)
    run.transition_step(3, StepStatus.FAILED, error="out of stock")

    assert run.failed_step is not None
    assert run.failed_step.name == "ship"
    assert run.completed_steps == ["charge_card", "reserve"]
    assert run.completed_steps_to_compensate == ["reserve", "charge_card"]


def test_saga_run_round_trips_through_dict():
    run = make_run()
    run.add_step("charge_card", {"amount": 10})
    run.transition_step(1, StepStatus.RUNNING)
    run.transition_step(1, StepStatus.COMPLETED, result={"charge_id": "ch_1"})
    run.add_step("ship", {"address": "Rua X"})
    run.transition_step(2, StepStatus.RUNNING)
    run.transition_step(2, StepStatus.FAILED, error="out of stock")

    restored = SagaRun.from_dict(run.to_dict())
    assert restored.saga_id == run.saga_id
    assert restored.status == run.status
    assert [s.name for s in restored.steps] == ["charge_card", "ship"]
    assert restored.steps[0].result == {"charge_id": "ch_1"}
    assert restored.steps[0].status is StepStatus.COMPLETED
    assert restored.steps[1].status is StepStatus.FAILED
    assert restored.failed_step.name == "ship"


def test_retry_attempt_record_serialization():
    attempt = RetryAttempt(step_id=1, attempt=2, delay=0.5, error="HTTP 503")
    assert attempt.succeeded is False
    payload = attempt.to_dict()
    assert payload == {
        "step_id": 1,
        "attempt": 2,
        "delay": 0.5,
        "error": "HTTP 503",
        "succeeded": False,
    }
    assert RetryAttempt.from_dict(payload) == attempt


def test_tool_call_exposes_retryable_flag_from_error_payload():
    run = make_run()
    run.add_step("charge_card", {})
    call = ToolCall(step_id=1, name="charge_card", tool_args={})
    assert call.status is StepStatus.PENDING
    assert call.retryable is False
    call.attempts = 3
    assert call.to_dict()["attempts"] == 3
