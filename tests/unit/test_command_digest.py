"""Unit checks for T003 target-aware normalized command digests."""

from datetime import date
from uuid import uuid4

import pytest
from carepulse.identity.contracts import normalized_command_digest
from pydantic import BaseModel, ConfigDict


class _StrictCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    physical_presence_confirmed: bool
    expected_appointment_revision: int
    note: str | None = None


class _PermissiveCommand(BaseModel):
    physical_presence_confirmed: bool


def test_target_aware_digest_distinguishes_targets_and_operations() -> None:
    first_id, second_id = uuid4(), uuid4()
    body = _StrictCommand(
        physical_presence_confirmed=True, expected_appointment_revision=0
    )
    first = normalized_command_digest(
        "appointments.check_in", body, target={"appointment_id": first_id}
    )
    second = normalized_command_digest(
        "appointments.check_in", body, target={"appointment_id": second_id}
    )
    other_op = normalized_command_digest(
        "appointments.cancel", body, target={"appointment_id": first_id}
    )
    assert len(first) == 64
    assert first != second
    assert first != other_op


def test_target_aware_digest_canonicalizes_dates_and_omitted_vs_null() -> None:
    target_id = uuid4()
    omitted = _StrictCommand(
        physical_presence_confirmed=True, expected_appointment_revision=1
    )
    explicit_null = _StrictCommand(
        physical_presence_confirmed=True, expected_appointment_revision=1, note=None
    )
    day = date(2026, 11, 24)
    a = normalized_command_digest(
        "queue.placement",
        omitted,
        target={"service_date": day, "appointment_id": target_id},
    )
    b = normalized_command_digest(
        "queue.placement",
        omitted,
        target={"appointment_id": str(target_id), "service_date": "2026-11-24"},
    )
    c = normalized_command_digest(
        "queue.placement",
        explicit_null,
        target={"service_date": day, "appointment_id": target_id},
    )
    assert a == b
    assert a != c


def test_target_aware_digest_rejects_permissive_dto_or_blank_operation() -> None:
    body = _StrictCommand(
        physical_presence_confirmed=True, expected_appointment_revision=0
    )
    with pytest.raises(ValueError):
        normalized_command_digest("   ", body)
    with pytest.raises(ValueError):
        normalized_command_digest(
            "appointments.check_in",
            _PermissiveCommand(physical_presence_confirmed=True),
        )
