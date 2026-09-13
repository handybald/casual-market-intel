"""Phase 2A regression tests (fourth review): the .mq5 exporter and the
Python ingestion side disagreed about what an "EndDate"/`end_date`
means at the exact-boundary level. The exporter's own window loop
always used the raw EndDate value as an EXCLUSIVE midnight boundary,
while `_window_key_for_chunk` in src/data/fetch/mql5.py special-cased
"is this chunk's end a natural calendar-month end?" to decide whether
to add a day -- correct only by coincidence when a requested end date
happens to land on a natural month boundary, and wrong for any
partial-month window (a single-day range, or the last, clipped chunk
of a multi-month range).

The fix: ONE public contract -- both StartDate/end_date are INCLUSIVE
user-facing calendar dates, converted ONCE to an internal half-open
[start midnight, day-after-end midnight) representation, with NO
month-end special-casing anywhere. `tests/_mql5_exporter_sim.py` is a
line-for-line Python simulation of the exporter's (fixed) OnStart()
window loop -- since no MetaTrader compiler/runtime exists in this
environment, this is how "the exporter and Python actually agree" is
proven: by comparing Python's real production window-key function
against a faithful simulation of the exporter's logic, not by grepping
source text for matching function names. This is documentation/
behavioral-simulation verification, NOT a substitute for compiling and
running the real script inside MetaTrader.
"""
import datetime as dt

from src.data.dates import iter_date_chunks
from src.data.fetch.mql5 import _window_key_for_chunk
from tests._mql5_exporter_sim import exporter_windows


def _assert_agrees(start: dt.date, end: dt.date) -> None:
    sim_windows = exporter_windows(start, end)
    chunks = list(iter_date_chunks(start, end, "month"))
    assert len(sim_windows) == len(chunks), (sim_windows, chunks)

    for (sim_start, sim_end), chunk in zip(sim_windows, chunks):
        expected_start_str = sim_start.strftime("%Y.%m.%d 00:00:00")
        expected_end_str = sim_end.strftime("%Y.%m.%d 00:00:00")
        py_start_str, py_end_str = _window_key_for_chunk(chunk.start, chunk.end)
        assert (py_start_str, py_end_str) == (expected_start_str, expected_end_str), (
            f"chunk {chunk} -> python computed {(py_start_str, py_end_str)} "
            f"but exporter simulation computed {(expected_start_str, expected_end_str)}"
        )


def test_single_day_range_agrees():
    """The exact failure mode: a one-day, non-month-end-aligned range."""
    _assert_agrees(dt.date(2024, 1, 15), dt.date(2024, 1, 15))


def test_multi_month_range_with_partial_last_chunk_agrees():
    """The named review scenario: a full first month followed by a
    partial (clipped, non-month-end) final chunk."""
    _assert_agrees(dt.date(2024, 1, 1), dt.date(2024, 2, 10))


def test_partial_month_extension_agrees():
    """Sept 1-10 bootstrap, later extended to Sept 1-20 -- both partial,
    non-month-end windows must independently agree."""
    _assert_agrees(dt.date(2024, 9, 1), dt.date(2024, 9, 10))
    _assert_agrees(dt.date(2024, 9, 1), dt.date(2024, 9, 20))


def test_leap_day_end_date_agrees():
    _assert_agrees(dt.date(2024, 2, 1), dt.date(2024, 2, 29))
    _assert_agrees(dt.date(2024, 2, 1), dt.date(2024, 2, 28))  # non-leap-boundary partial


def test_year_boundary_range_agrees():
    _assert_agrees(dt.date(2023, 12, 15), dt.date(2024, 1, 15))


def test_full_calendar_month_range_agrees():
    """A request that DOES land exactly on natural month boundaries --
    must still agree now that the special-casing is gone (agreement
    here was already accidental before the fix; must remain correct
    after removing the special case)."""
    _assert_agrees(dt.date(2024, 3, 1), dt.date(2024, 3, 31))


def test_multi_year_wide_range_agrees():
    _assert_agrees(dt.date(2016, 1, 1), dt.date(2026, 9, 10))
