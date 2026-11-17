"""Aggregation must not invent numbers.

The report tables are what the write-up quotes, so the arithmetic and the
paired statistics are pinned down here against small hand-checkable inputs.
"""

from __future__ import annotations

import pytest

from harness_bench import report


def _record(task_id, interface, score, **extra):
    record = {
        "task_id": task_id,
        "app": extra.pop("app", "libreoffice_calc"),
        "interface": interface,
        "score": score,
        "grader_status": extra.pop("grader_status", "graded"),
        "status": extra.pop("status", "done"),
        "tool_calls": extra.pop("tool_calls", 4),
        "prompt_tokens": extra.pop("prompt_tokens", 100),
        "completion_tokens": extra.pop("completion_tokens", 20),
        "elapsed_s": extra.pop("elapsed_s", 10.0),
        "failure_class": extra.pop("failure_class", ""),
        "extra": extra.pop("extra", {}),
    }
    record.update(extra)
    return record


def test_success_rate_is_the_mean_of_graded_runs():
    records = [
        _record("t1", "harness", 1.0),
        _record("t2", "harness", 0.0),
        _record("t3", "harness", 1.0),
        _record("t4", "harness", 0.0, grader_status="grader_unavailable"),
    ]
    summary = report.summarize_interface(records, "harness")

    # The unavailable run is excluded from the success rate but not from the
    # run count, because it still consumed a task.
    assert summary.success_rate == pytest.approx(2 / 3)
    assert summary.runs == 4
    assert summary.tasks == 4


def test_false_completion_counts_only_claimed_success():
    records = [
        _record("t1", "harness", 1.0, status="done"),
        _record("t2", "harness", 0.0, status="done"),
        _record("t3", "harness", 0.0, status="gave_up"),
    ]
    summary = report.summarize_interface(records, "harness")
    assert summary.false_completion_rate == pytest.approx(1 / 3)


def test_stability_is_the_majority_share_of_repeats():
    records = [
        _record("t1", "harness", 1.0),
        _record("t1", "harness", 1.0),
        _record("t1", "harness", 0.0),
        _record("t2", "harness", 0.0),
        _record("t2", "harness", 0.0),
        _record("t2", "harness", 0.0),
    ]
    summary = report.summarize_interface(records, "harness")

    # t1 agrees 2/3, t2 agrees 3/3.
    assert summary.stability == pytest.approx((2 / 3 + 1.0) / 2)
    assert summary.tasks == 2


def test_single_run_is_flagged_as_unmeasurable():
    summary = report.summarize_interface([_record("t1", "gui", 1.0)], "gui")
    assert summary.stability is None
    assert any("stability" in note for note in summary.notes)


def test_bootstrap_interval_brackets_the_mean():
    scores = [1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0]
    low, high = report.bootstrap_ci(scores, iterations=500)
    mean = sum(scores) / len(scores)
    assert low <= mean <= high
    assert 0.0 <= low and high <= 1.0


def test_bootstrap_needs_more_than_one_value():
    assert report.bootstrap_ci([1.0]) is None


def test_mcnemar_matches_the_textbook_values():
    # 5 vs 0 discordant pairs: p = 2 * (1/32) = 0.0625
    assert report.mcnemar_exact(5, 0) == pytest.approx(0.0625)
    # 10 vs 2: two-sided exact
    assert report.mcnemar_exact(10, 2) == pytest.approx(0.03857421875, abs=1e-9)
    # No discordant pairs means no evidence of a difference.
    assert report.mcnemar_exact(0, 0) == 1.0


def test_paired_comparison_uses_only_shared_tasks():
    records = [
        _record("t1", "harness", 1.0),
        _record("t2", "harness", 0.0),
        _record("t3", "harness", 1.0),
        _record("t1", "script", 1.0),
        _record("t2", "script", 1.0),
        _record("t4", "script", 1.0),
    ]
    comparison = report.paired_comparison(records, "harness", "script")

    assert comparison["tasks"] == 2
    assert comparison["both"] == 1
    assert comparison["b_only"] == 1
    assert comparison["a_only"] == 0
    assert comparison["neither"] == 0


def test_paired_comparison_reports_an_empty_overlap():
    comparison = report.paired_comparison(
        [_record("t1", "harness", 1.0), _record("t2", "script", 1.0)],
        "harness",
        "script",
    )
    assert comparison["tasks"] == 0
    assert "note" in comparison


def test_repeats_collapse_to_the_best_score_in_a_comparison():
    """A task counts as solved for an interface if any repeat solved it."""

    records = [
        _record("t1", "harness", 0.0),
        _record("t1", "harness", 1.0),
        _record("t1", "script", 0.0),
        _record("t1", "script", 0.0),
    ]
    comparison = report.paired_comparison(records, "harness", "script")
    assert comparison["a_only"] == 1
    assert comparison["b_only"] == 0


def test_refinement_curve_tracks_dev_and_test_separately():
    records = [
        _record("t1", "harness", 0.0, extra={"round": "round0", "split": "dev"}),
        _record("t2", "harness", 1.0, extra={"round": "round0", "split": "dev"}),
        _record("t3", "harness", 1.0, extra={"round": "round0", "split": "test"}),
        _record("t1", "harness", 1.0, extra={"round": "round1", "split": "dev"}),
        _record("t2", "harness", 1.0, extra={"round": "round1", "split": "dev"}),
        _record("t3", "harness", 1.0, extra={"round": "round1", "split": "test"}),
    ]
    curve = report.refinement_curve(records)["rounds"]

    assert [entry["round"] for entry in curve] == ["round0", "round1"]
    assert curve[0]["dev_rate"] == pytest.approx(0.5)
    assert curve[0]["test_rate"] == pytest.approx(1.0)
    assert curve[1]["dev_rate"] == pytest.approx(1.0)
    assert curve[1]["gap"] == pytest.approx(0.0)


def test_failure_table_lists_only_failed_runs():
    records = [
        _record("t1", "harness", 1.0),
        _record("t2", "harness", 0.0, failure_class="expressiveness"),
    ]
    rows = report.failure_table(records, "harness")
    assert [row["task_id"] for row in rows] == ["t2"]
    assert rows[0]["failure_class"] == "expressiveness"


def test_markdown_table_formats_floats_and_blanks():
    rendered = report.markdown_table(
        [{"a": 1.0, "b": None}, {"a": 0.5, "b": "x"}],
        [("a", "Alpha"), ("b", "Beta")],
    )
    lines = rendered.splitlines()
    assert lines[0] == "| Alpha | Beta |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 1.000 | – |"
    assert lines[3] == "| 0.500 | x |"


def test_load_records_skips_unreadable_files(tmp_path):
    good = tmp_path / "a" / "record.json"
    good.parent.mkdir(parents=True)
    good.write_text('{"task_id": "t1", "interface": "harness", "score": 1.0}', encoding="utf-8")

    broken = tmp_path / "b" / "record.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{not json", encoding="utf-8")

    records = report.load_records(tmp_path)
    assert [record["task_id"] for record in records] == ["t1"]
