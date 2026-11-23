"""Unit tests for B-12 Routine queue positioning rules."""

from uuid import uuid4

from carepulse.queue.checkin import RoutineWaitingEntry, compute_routine_insert_index


def _entries(*numbers: int) -> list[RoutineWaitingEntry]:
    return [
        RoutineWaitingEntry(id=uuid4(), routine_rank=idx + 1, current_number=num)
        for idx, num in enumerate(numbers)
    ]


def test_arrival_fifo_always_appends() -> None:
    existing = _entries(5, 2, 9)
    assert (
        compute_routine_insert_index(
            existing,
            routine_order="ARRIVAL_FIFO",
            late_depth=0,
            routine_cursor=0,
            appointment_number=1,
        )
        == 3
    )


def test_booked_number_on_time_orders_by_preliminary_number() -> None:
    existing = _entries(2, 5, 8)
    # Cursor is 0 (no ordinary Routine-head admission yet) -> #1 is NOT late.
    assert (
        compute_routine_insert_index(
            existing,
            routine_order="BOOKED_NUMBER",
            late_depth=2,
            routine_cursor=0,
            appointment_number=1,
        )
        == 0
    )
    assert (
        compute_routine_insert_index(
            existing,
            routine_order="BOOKED_NUMBER",
            late_depth=2,
            routine_cursor=0,
            appointment_number=4,
        )
        == 1
    )
    assert (
        compute_routine_insert_index(
            existing,
            routine_order="BOOKED_NUMBER",
            late_depth=2,
            routine_cursor=0,
            appointment_number=10,
        )
        == 3
    )


def test_booked_number_late_arrival_edges() -> None:
    # Cursor advanced to 4 -> appointment_number <= 4 is late.
    # 1) Zero Routine Waiting -> index 0
    assert (
        compute_routine_insert_index(
            [],
            routine_order="BOOKED_NUMBER",
            late_depth=3,
            routine_cursor=4,
            appointment_number=2,
        )
        == 0
    )
    # 2) Fewer than late_depth entries -> behind all existing
    assert (
        compute_routine_insert_index(
            _entries(5, 6),
            routine_order="BOOKED_NUMBER",
            late_depth=3,
            routine_cursor=4,
            appointment_number=4,
        )
        == 2
    )
    # 3) Normal late_depth -> after first late_depth entries
    assert (
        compute_routine_insert_index(
            _entries(5, 6, 7, 8),
            routine_order="BOOKED_NUMBER",
            late_depth=2,
            routine_cursor=4,
            appointment_number=3,
        )
        == 2
    )
    # 4) late_depth == 0 -> ahead of Routine Waiting
    assert (
        compute_routine_insert_index(
            _entries(5, 6),
            routine_order="BOOKED_NUMBER",
            late_depth=0,
            routine_cursor=4,
            appointment_number=1,
        )
        == 0
    )
    # 5) Number strictly above cursor is NOT late
    assert (
        compute_routine_insert_index(
            _entries(7, 9),
            routine_order="BOOKED_NUMBER",
            late_depth=2,
            routine_cursor=4,
            appointment_number=5,
        )
        == 0
    )
