# Tool Call Retry

> Saga-pattern runtime for AI agent tool calls. Automatic compensation, idempotency keys, and rollback on partial failure. LLM-native inverse derivation.

## The Problem

AI agents orchestrate multi-step workflows across tools (APIs, databases, filesystems). When step 3 of 5 fails:

- **No rollback**: Steps 1-2 left system in inconsistent state.
- **Hard-coded compensation**: Can't pre-code inverse for every possible LLM-chosen tool.
- **No idempotency**: Re-running creates duplicates (double-charge, double-write).
- **Lost context**: Agent must re-reason about what happened and what to do.

Existing tools (kompensa, saga-agent) are **developer-centric** — they require hard-coded compensation. But LLM agents choose tools at runtime; the inverse depends on the result.

## The Solution

`tool-call-retry` provides **agent-native saga**:

```
Step 1: Charge credit card      → Success
Step 2: Reserve inventory       → Success
Step 3: Ship package            💥 FAIL (out of stock)
         ↓ (auto-compensate)
Step 2: Release reservation    ✅ Compensated (LLM-derived inverse)
Step 1: Refund charge           ✅ Compensated (LLM-derived inverse)
```

The compensation is derived at runtime from the **tool call + its result** — not hard-coded.

## Key Innovation

**LLM-native inverse derivation.** Instead of hard-coding `undo_reserve()` for `reserve()`, the tool asks the LLM (or uses tool-defined inverse hints) to derive the compensating action from:

- The original tool call
- The result it returned
- The current system state

```
tool_call: POST /api/ship {order_id: 123} → {tracking: "ABC", status: "shipped"}
inverse:   POST /api/ship/cancel {tracking: "ABC"}
```

## Features

- **Runtime compensation** — Inverse derived from tool call + result, not hard-coded.
- **Idempotency keys** — Prevents duplicate side effects on retry.
- **Partial rollback** — Only reverse completed steps, not pending ones.
- **Agent-native** — Designed for LLM agents, not just deterministic services.
- **Drift-safe journal** — Atomic operation log survives crashes.
- **Framework-agnostic** — Works with any agent or orchestrator.

## Install

```bash
pip install tool-call-retry
```

## Quick Start

```python
from tool_call_retry import Saga

saga = Saga(idempotency_key="order-123")

@saga.step
def charge_card(amount):
    return payment_client.charge(amount)

@saga.step
def reserve_inventory(item_id):
    return inventory_client.reserve(item_id)

@saga.step
def ship_package(address):
    return shipping_client.ship(address)  # This fails!

try:
    saga.execute()
except SagaFailed as e:
    print(f"Failed at step {e.step}: {e.error}")
    print(f"Compensated steps: {e.compensated}")
    # System is back to consistent state
```

## How It Works

```
┌─────────────────────────────────────────────────────────┐
│                    Saga Runtime                          │
├─────────────────────────────────────────────────────────┤
│  Step Queue → Execute → Journal → Compensate (on fail)  │
│                                                         │
│  1. Tool call                                            │
│  2. Validate result                                      │
│  3. Derive inverse (LLM or tool-defined)                 │
│  4. Append to journal                                    │
│  5. On failure: reverse completed steps using inverses   │
└─────────────────────────────────────────────────────────┘
```

## Comparison

| Tool | Runtime Comp. | Idempotency | LLM-Native | Agent-Ready |
|------|---------------|-------------|------------|-------------|
| **tool-call-retry** | ✅ | ✅ | ✅ | ✅ |
| kompensa | ❌ (hard-coded) | ✅ | ❌ | ❌ |
| saga-agent | ✅ | ❌ | ✅ | ❌ |
| agentrelay | ❌ (hard-coded) | ✅ | ❌ | ❌ |
| agent-undo | ❌ (rollback only) | ❌ | ✅ | ✅ |

## Drift Detection Integration

Pair with `driftcheck` (yunaremaia) to detect when tool schemas or compensation logic drift from their specifications.

## Roadmap

- [ ] Pluggable inverse derivation strategies (LLM, rule-based, tool-defined)
- [ ] Parallel saga execution
- [ ] `agent-checkpoint` integration for crash recovery
- [ ] `mcp-response-guard` integration for schema validation on compensation
- [ ] CLI wrapper: `tool-call-retry -- mcp-client call <tool>`

## License

MIT
