"""The ceiling check must not count unmeasurable tasks as harness losses.

The bookkeeping here is what keeps the headline number honest: a task whose own
reference does not score 1.0 says nothing about the harness, and a task blocked
by a missing binary or a missing LibreOffice says nothing either.
"""

from __future__ import annotations

import pytest

from harness_bench import roundtrip
from harness_bench.roundtrip import RoundTripResult


def _result(**kwargs) -> RoundTripResult:
    defaults = dict(task_id="t", app="libreoffice_calc", applicable=True)
    defaults.update(kwargs)
    return RoundTripResult(**defaults)


def test_lost_requires_a_decidable_task_with_a_real_drop():
    assert _result(decidable=True, baseline=1.0, score=0.0).lost
    assert not _result(decidable=True, baseline=1.0, score=1.0).lost
    assert not _result(decidable=False, baseline=1.0, score=0.0).lost
    assert not _result(decidable=True, baseline=None, score=0.0).lost
    assert not _result(decidable=True, baseline=1.0, score=None).lost


def test_ceiling_is_computed_over_measured_tasks_only():
    results = [
        _result(task_id="a", decidable=True, baseline=1.0, score=1.0, passed=True),
        _result(task_id="b", decidable=True, baseline=1.0, score=0.0, passed=False),
        # Not decidable: the reference is not a fixed point.
        _result(task_id="c", status=roundtrip.NOT_DECIDABLE),
        # Blocked by the environment, not by the harness.
        _result(task_id="d", status=roundtrip.HARNESS_MISSING),
        _result(task_id="e", status=roundtrip.NEEDS_SOFFICE),
        _result(task_id="f", status=roundtrip.NO_REFERENCE),
        # Infeasible tasks have nothing to round trip.
        _result(task_id="g", applicable=False),
    ]

    summary = roundtrip.summarize(results)

    assert summary["tasks"] == 7
    assert summary["decidable"] == 2
    assert summary["measured"] == 2
    assert summary["survived"] == 1
    assert summary["lost"] == 1
    assert summary["ceiling"] == pytest.approx(0.5)
    assert summary["not_decidable"] == 1
    assert summary["harness_missing"] == 1
    assert summary["needs_soffice"] == 1
    assert summary["no_reference"] == 1
    assert summary["lost_task_ids"] == ["b"]


def test_a_measurement_that_never_produced_a_score_is_reported_separately():
    results = [
        _result(task_id="a", decidable=True, baseline=1.0, score=None, status="import_failed")
    ]
    summary = roundtrip.summarize(results)

    assert summary["failed"] == 1
    assert summary["failed_task_ids"] == ["a"]
    # It was decidable but never measured, so it cannot enter the ceiling.
    assert summary["measured"] == 0
    assert summary["ceiling"] is None


def test_by_app_counts_only_decidable_tasks():
    results = [
        _result(task_id="a", app="gimp", decidable=True, baseline=1.0, score=1.0, passed=True),
        _result(task_id="b", app="gimp", status=roundtrip.NOT_DECIDABLE),
        _result(task_id="c", app="libreoffice_impress", status=roundtrip.HARNESS_MISSING),
    ]
    summary = roundtrip.summarize(results)

    assert summary["by_app"]["gimp"] == {
        "tasks": 2,
        "decidable": 1,
        "survived": 1,
        "lost": 0,
    }
    assert summary["by_app"]["libreoffice_impress"]["decidable"] == 0


def test_spreadsheet_detection_follows_the_result_path():
    from harness_bench.config import TASKS_DIR
    from harness_bench.tasks import Task

    calc = Task.from_file(
        TASKS_DIR / "libreoffice_calc" / "01b269ae-2111-4a07-81fd-3fcd711993b0.json"
    )
    writer = Task.from_file(
        TASKS_DIR / "libreoffice_writer" / "0810415c-bde4-4443-9047-d5f70165a697.json"
    )

    assert roundtrip._is_spreadsheet_task(calc)
    assert not roundtrip._is_spreadsheet_task(writer)


def test_export_preset_covers_every_supported_document_extension():
    for suffix in (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".pdf"):
        assert suffix in roundtrip.EXPORT_PRESETS
    for suffix in (".png", ".jpg", ".jpeg"):
        assert suffix in roundtrip.GIMP_EXPORT_PRESETS


def test_environment_failures_are_not_mistaken_for_a_bad_reference():
    """A download failure must not be reported as "the reference is not a fixed point"."""

    results = [
        _result(task_id="a", status=roundtrip.REFERENCE_UNAVAILABLE),
        _result(task_id="b", status=roundtrip.INPUT_UNAVAILABLE),
        _result(task_id="c", status=roundtrip.NOT_DECIDABLE),
    ]

    summary = roundtrip.summarize(results)

    assert summary["reference_unavailable"] == 1
    assert summary["input_unavailable"] == 1
    assert summary["not_decidable"] == 1
    assert summary["decidable"] == 0
