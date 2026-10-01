"""Tool implementations referenced by ``checkout.yml``.

These stand in for real API clients. They record what happened so running the
example shows the compensation chain, and ``ship_package`` fails on demand by
setting ``FAIL_SHIPPING = True`` — that is the rollback path from the README.
"""

from __future__ import annotations

from tool_call_retry.errors import NonRetryableError

#: Set to True to make ship_package fail and trigger compensation.
FAIL_SHIPPING = False

EVENTS: list[str] = []


def charge_card(amount: int) -> dict[str, object]:
    EVENTS.append(f"charge_card({amount})")
    return {"charge_id": "ch_1", "amount": amount}


def refund_card(result: dict[str, object] | None = None) -> str:
    EVENTS.append(f"refund_card({result})")
    return "refunded"


def reserve_inventory(sku: str) -> dict[str, object]:
    EVENTS.append(f"reserve_inventory({sku})")
    return {"reservation_id": "rsv_1", "sku": sku}


def release_inventory(result: dict[str, object] | None = None) -> str:
    EVENTS.append(f"release_inventory({result})")
    return "released"


def ship_package(address: str) -> dict[str, object]:
    EVENTS.append(f"ship_package({address})")
    if FAIL_SHIPPING:
        raise NonRetryableError("out of stock")
    return {"tracking": "ABC123"}
