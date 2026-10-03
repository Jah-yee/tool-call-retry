# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-03

First release. This is an **early MVP**: the repository has four commits, all of
the functionality below landed in a single `feat:` commit. The API is usable but
not yet stable, and there is no publish workflow.

### Not on PyPI

**`tool-call-retry` is not on PyPI and `pip install tool-call-retry` does not
work.** Install from git:

```bash
pip install git+https://github.com/yunaremaia/tool-call-retry.git
```

The repository has no publish workflow and no PyPI project. Both PyPI endpoints
were checked and the name is currently unclaimed, but nothing has been uploaded.

### Added

**Saga runtime**

- `Saga` with `@saga.tool(...)`-decorated steps, executed in declaration order.
  On failure, completed steps are compensated in **reverse** order.
- Synchronous (`Saga.execute`) and asynchronous (`Saga.aexecute`) execution paths.
- Per-step arguments bound at declaration time, and context injection at
  execution time.
- Failure reporting through `SagaFailed.summary()` and `.root_cause()`, which
  describe what succeeded, what failed, and what was compensated, in one sentence
  rather than a traceback.

**Retry policy**

- `RetryPolicy` with exponential backoff following AWS "full jitter": the
  uncapped window is `base_delay * multiplier ** (attempt - 1)`, capped by
  `max_delay`, then sampled uniformly from `[0, window]`.
- Three jitter strategies — `full` (uniform in `[0, window]`), `sequential`
  (uniform over an increasing sub-range) and `none`.
- Attempt and time budgets, so a retry loop stops on whichever bound is hit first.
- `RetryPolicy.run(...)` as the retry driver, and `delay_for(attempt, rng=...)`
  accepting an injected RNG so jitter is testable without sleeping.

**Errors**

- A typed hierarchy: `ToolCallRetryError` as the base, with `RetryableError` and
  `NonRetryableError` branches.
- `ToolCallError` carrying an explicit `retryable` flag.
- `HTTPStatusError` that classifies by status: `408`, `409`, `425`, `429` and any
  `5xx` are retried; every other status, including `4xx` client errors, is
  terminal. A rate-limited call therefore backs off instead of hammering.
- `RetryExhausted` and `SagaFailed`, both with a `summary()`.
- `InvalidTransition` for illegal journal state changes.

**Journal (persistence)**

- `SagaJournal` backed by SQLite, with `:memory:` for ephemeral use.
- WAL journaling and `synchronous = FULL`, so a committed step is durable across
  a crash rather than lost in a buffer.
- Idempotency keys: re-running a saga with the same key resumes the existing
  saga instead of duplicating its side effects.
- Step-level state transitions, compensation tracking, and resume of an
  interrupted saga.

**CLI**

- `tool-call-retry run --config saga.yml` — execute a saga from a YAML config.
- `status --saga-id`, `journal --saga-id`, `list`, `recover [--resume]` and
  `cleanup --max-age` to inspect, resume and prune sagas.
- `tool-call-retry` console script, plus `python -m tool_call_retry`.
- Human-readable and `--json` output modes.

**Packaging and docs**

- `pyproject.toml` with the `tool-call-retry` console script, `Development
  Status :: 3 - Alpha`, and a `dev` extra for `pytest`, `pytest-asyncio` and
  `ruff`.
- 135 tests across the runtime, policy, journal, models, CLI and package metadata.
- GitHub Actions CI, an MIT `LICENSE`, and a README with a worked example and a
  runnable `examples/checkout.yml`.

### Known limitations

- Early MVP: the API is not yet stable and may change in a patch release.
- Compensation is best-effort. A compensation step that itself raises is recorded
  as failed, but there is no nested saga for driving compensation to completion.
- A saga is single-process. Two processes driving the same journal concurrently is
  not coordinated.

[0.1.0]: https://github.com/yunaremaia/tool-call-retry/releases/tag/v0.1.0