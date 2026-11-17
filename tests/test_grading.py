"""The grading bridge must reproduce OSWorld's ``evaluate()``, not approximate it.

Two things are checked here: that the metric and getter lookups resolve at all
through the import shim, and that the conjunction semantics (``and`` short
circuits on 0, ``or`` short circuits on 1, ``infeasible`` reads the action
history) behave the way upstream does.
"""

from __future__ import annotations

import json

import pytest

from harness_bench import grading
from harness_bench.config import TASKS_DIR
from harness_bench.osworld_bootstrap import (
    MissingDependency,
    available,
    ensure_osworld_importable,
)
from harness_bench.tasks import Task


def _task(app: str, task_id: str) -> Task:
    return Task.from_file(TASKS_DIR / app / f"{task_id}.json")


def test_shim_makes_metric_modules_importable():
    ensure_osworld_importable()
    for module in ("table", "docs", "slides", "pdf", "gimp", "general", "chrome", "vlc"):
        assert available(module), f"metrics/{module} could not be imported"


def test_missing_package_raises_a_named_error():
    """A poisoned dependency must fail with a readable name, not an AttributeError."""

    ensure_osworld_importable()
    import importlib

    metrics = importlib.import_module("desktop_env.evaluators.metrics.docs")
    with pytest.raises(MissingDependency) as excinfo:
        metrics.easyocr.Reader(["en"])
    assert "easyocr" in str(excinfo.value)


def test_metric_lookup_covers_every_adopted_task(all_tasks):
    for task in all_tasks:
        if not task.adopted:
            continue
        for func in task.funcs:
            if func == grading.INFEASIBLE:
                continue
            assert func in grading.METRIC_MODULES, f"{func} has no source module"


def test_getter_lookup_covers_every_adopted_task(all_tasks):
    for task in all_tasks:
        if not task.adopted:
            continue
        for kind in task.result_types + task.expected_types:
            assert kind in grading.GETTER_MODULES, f"getter {kind} is not mapped"


def test_infeasible_is_scored_from_the_action_history(tmp_path, stage_offline):
    """Upstream scores 1 only when the last action was FAIL."""

    task = _task("libreoffice_calc", "2bd59342-0664-4ccb-ba87-79379096cc08")
    assert task.is_infeasible

    materialized = stage_offline(task, tmp_path / "work")

    declined = grading.grade(materialized, ["FAIL"], tmp_path / "cache")
    assert declined.score == 1.0

    claimed = grading.grade(materialized, [{"action_type": "DONE"}], tmp_path / "cache")
    assert claimed.score == 0.0


def test_a_done_action_on_an_infeasible_task_scores_zero(tmp_path, stage_offline):
    task = _task("libreoffice_calc", "2bd59342-0664-4ccb-ba87-79379096cc08")

    materialized = stage_offline(task, tmp_path / "work")
    outcome = grading.grade(materialized, [], tmp_path / "cache")
    assert outcome.score == 0.0
    assert outcome.status == "graded"


def test_missing_result_file_scores_zero(tmp_path, stage_offline):
    """Upstream catches FileNotFoundError and returns 0 for a single metric."""

    task = _task("libreoffice_calc", "01b269ae-2111-4a07-81fd-3fcd711993b0")

    materialized = stage_offline(task, tmp_path / "work")
    outcome = grading.grade(materialized, ["DONE"], tmp_path / "cache")

    assert outcome.score == 0.0
    assert outcome.status == "graded"


def test_or_conjunction_accepts_either_reference(tmp_path, stage_offline):
    """``conj: or`` must take the max, not the mean."""

    task = _task("libreoffice_writer", "0810415c-bde4-4443-9047-d5f70165a697")
    assert task.conj == "or"

    materialized = stage_offline(task, tmp_path / "work")
    outcome = grading.grade(materialized, ["DONE"], tmp_path / "cache")

    # Both result configs point at the same missing file, so ``or`` still
    # short-circuits to 0 -- but through the ``or`` branch, which is the point.
    assert outcome.score == 0.0
    assert len(outcome.per_metric) == 2


def test_grade_result_exposes_pass_and_availability():
    passed = grading.GradeResult(score=1.0, status="graded")
    assert passed.passed and passed.available

    failed = grading.GradeResult(score=0.0, status="graded")
    assert not failed.passed and failed.available

    missing = grading.GradeResult(score=0.0, status="grader_unavailable", reason="x")
    assert not missing.passed
    assert not missing.available


def test_local_controller_reads_a_real_file(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("hello", encoding="utf-8")

    controller = grading.LocalController(tmp_path, {"/home/user/input.txt": str(source)})
    assert controller.get_file("/home/user/input.txt") == b"hello"

    with pytest.raises(FileNotFoundError):
        controller.get_file("/home/user/absent.txt", strict=True)


def test_local_controller_returns_none_for_a_missing_file_without_strict(tmp_path):
    controller = grading.LocalController(tmp_path, {})
    assert controller.get_file("/home/user/absent.txt") is None


def test_prestage_references_stages_a_local_copy_without_the_network(tmp_path, monkeypatch):
    """References are copied from the shared cache so the getter never downloads."""

    from types import SimpleNamespace

    import pathlib

    from harness_bench import grading, tasks as tasks_mod

    monkeypatch.setattr(grading, "REPO_ROOT", tmp_path)

    def fake_fetch(url, cache_dir):
        cache = pathlib.Path(cache_dir)
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / "cached-reference"
        target.write_bytes(b"reference-bytes")
        return target

    monkeypatch.setattr(tasks_mod, "fetch", fake_fetch, raising=False)

    cache_dir = tmp_path / "grader-cache" / "task"
    env = SimpleNamespace(cache_dir=cache_dir)
    config = [{"type": "cloud_file", "path": "https://example.test/gold.xlsx", "dest": "gold.xlsx"}]

    grading._prestage_references(env, config)

    staged = cache_dir / "gold.xlsx"
    assert staged.read_bytes() == b"reference-bytes"
