"""Data models for saga runs, steps and retry attempts.

Every model is a plain dataclass with explicit ``to_dict``/``from_dict`` so the
journal can persist it as JSON without a serialization dependency, and so the
CLI's ``--json`` output is stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from tool_call_retry.errors import InvalidTransition

#: Legal saga-level state changes. Issue #2 lists these in the SQLite schema comment.
SAGA_STATUSES = ("active", "compensating", "completed", "failed")

_SAGA_TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset({"active", "compensating", "completed", "failed"}),
    "compensating": frozenset({"compensating", "completed", "failed"}),
    "completed": frozenset({"completed"}),
    "failed": frozenset({"failed"}),
}


class StepStatus(str, Enum):
    """Per-step state, matching the ``steps.status`` column in issue #2."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    FAILED = "failed"


#: Legal step-level state changes.
STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    # pending -> completed is legal: a step resumed from the journal may already
    # have a recorded result from a previous process (idempotency hit).
    StepStatus.PENDING: frozenset(
        {StepStatus.PENDING, StepStatus.RUNNING, StepStatus.COMPLETED, StepStatus.FAILED}
    ),
    StepStatus.RUNNING: frozenset(
        {
            StepStatus.RUNNING,
            StepStatus.COMPLETED,
            StepStatus.FAILED,
            StepStatus.COMPENSATING,
        }
    ),
    StepStatus.COMPLETED: frozenset(
        {StepStatus.COMPLETED, StepStatus.COMPENSATING}
    ),
    # A failed step re-runs when the saga is retried; a compensated step re-runs
    # because its side effect was rolled back.
    StepStatus.FAILED: frozenset(
        {StepStatus.FAILED, StepStatus.RUNNING, StepStatus.COMPENSATING}
    ),
    StepStatus.COMPENSATING: frozenset(
        {
            StepStatus.COMPENSATING,
            StepStatus.COMPENSATED,
            StepStatus.RUNNING,
            StepStatus.FAILED,
        }
    ),
    StepStatus.COMPENSATED: frozenset({StepStatus.COMPENSATED, StepStatus.RUNNING}),
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RetryAttempt:
    """One attempt of one step, recorded for observability and error reporting."""

    step_id: int
    attempt: int
    delay: float = 0.0
    error: str | None = None
    succeeded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "attempt": self.attempt,
            "delay": self.delay,
            "error": self.error,
            "succeeded": self.succeeded,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RetryAttempt:
        return cls(
            step_id=int(payload["step_id"]),
            attempt=int(payload["attempt"]),
            delay=float(payload.get("delay", 0.0)),
            error=payload.get("error"),
            succeeded=bool(payload.get("succeeded", False)),
        )


@dataclass
class ToolCall:
    """A single tool invocation inside a saga step."""

    step_id: int
    name: str
    tool_args: dict[str, Any] = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    result: Any = None
    error: str | None = None
    attempts: int = 0
    idempotency_key: str | None = None
    attempt_history: list[RetryAttempt] = field(default_factory=list)

    @property
    def retryable(self) -> bool:
        """True when this call failed for a reason another attempt could fix."""
        return self.error is not None and self.status is not StepStatus.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "tool_args": self.tool_args,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "idempotency_key": self.idempotency_key,
            "attempt_history": [a.to_dict() for a in self.attempt_history],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ToolCall:
        return cls(
            step_id=int(payload["step_id"]),
            name=payload["name"],
            tool_args=dict(payload.get("tool_args") or {}),
            status=StepStatus(payload.get("status", "pending")),
            result=payload.get("result"),
            error=payload.get("error"),
            attempts=int(payload.get("attempts", 0)),
            idempotency_key=payload.get("idempotency_key"),
            attempt_history=[
                RetryAttempt.from_dict(a) for a in payload.get("attempt_history") or []
            ],
        )


@dataclass
class SagaRun:
    """In-memory representation of one saga and its ordered steps."""

    saga_id: str
    name: str = ""
    idempotency_key: str | None = None
    status: str = "active"
    steps: list[ToolCall] = field(default_factory=list)
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)

    # -- step bookkeeping -------------------------------------------------
    def add_step(self, name: str, tool_args: dict[str, Any] | None = None) -> ToolCall:
        if any(step.name == name for step in self.steps):
            raise ValueError(f"duplicate step name: {name!r}")
        call = ToolCall(
            step_id=len(self.steps) + 1,
            name=name,
            tool_args=dict(tool_args or {}),
        )
        self.steps.append(call)
        self._touch()
        return call

    def step(self, step_id: int) -> ToolCall:
        for candidate in self.steps:
            if candidate.step_id == step_id:
                return candidate
        raise KeyError(f"unknown step_id: {step_id}")

    def step_by_name(self, name: str) -> ToolCall:
        for candidate in self.steps:
            if candidate.name == name:
                return candidate
        raise KeyError(f"unknown step: {name!r}")

    def step_by_name_safe(self, name: str) -> ToolCall | None:
        """Like :meth:`step_by_name` but returns ``None`` instead of raising."""
        for candidate in self.steps:
            if candidate.name == name:
                return candidate
        return None

    def transition_step(
        self,
        step_id: int,
        new_status: StepStatus | str,
        *,
        result: Any = None,
        error: str | None = None,
        attempt: RetryAttempt | None = None,
    ) -> ToolCall:
        target = StepStatus(new_status)
        current = self.step(step_id)
        if target not in STEP_TRANSITIONS[current.status]:
            raise InvalidTransition(
                f"step {step_id} cannot move from {current.status.value} to {target.value}"
            )
        if attempt is not None:
            current.attempts = max(current.attempts, attempt.attempt)
            current.attempt_history.append(attempt)
        elif target is StepStatus.RUNNING:
            # Entering RUNNING counts as an attempt even when the policy did not
            # report per-attempt records (e.g. a step with retries disabled).
            current.attempts += 1
        if result is not None or target in (StepStatus.COMPLETED, StepStatus.COMPENSATED):
            current.result = result
        if error is not None:
            current.error = error
        current.status = target
        self._touch()
        return current

    def transition_saga_status(self, new_status: str) -> None:
        if new_status not in SAGA_STATUSES:
            raise InvalidTransition(f"unknown saga status: {new_status!r}")
        allowed = _SAGA_TRANSITIONS[self.status]
        if new_status not in allowed:
            raise InvalidTransition(
                f"saga cannot move from {self.status} to {new_status}"
            )
        self.status = new_status
        self._touch()

    def _touch(self) -> None:
        self.updated_at = _utcnow()

    # -- derived views ----------------------------------------------------
    @property
    def completed_steps(self) -> list[str]:
        return [s.name for s in self.steps if s.status is StepStatus.COMPLETED]

    @property
    def failed_step(self) -> ToolCall | None:
        for candidate in self.steps:
            if candidate.status is StepStatus.FAILED:
                return candidate
        return None

    @property
    def completed_steps_to_compensate(self) -> list[str]:
        """Completed steps in reverse execution order — the compensation order."""
        return [s.name for s in reversed(self.steps) if s.status is StepStatus.COMPLETED]

    @property
    def is_terminal(self) -> bool:
        return self.status in ("completed", "failed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "saga_id": self.saga_id,
            "name": self.name,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SagaRun:
        return cls(
            saga_id=payload["saga_id"],
            name=payload.get("name", ""),
            idempotency_key=payload.get("idempotency_key"),
            status=payload.get("status", "active"),
            steps=[ToolCall.from_dict(s) for s in payload.get("steps") or []],
            created_at=payload.get("created_at") or _utcnow(),
            updated_at=payload.get("updated_at") or _utcnow(),
        )


__all__ = [
    "RetryAttempt",
    "SAGA_STATUSES",
    "STEP_TRANSITIONS",
    "SagaRun",
    "StepStatus",
    "ToolCall",
]
