"""Postconfig replay: run what grading depends on, skip the rest, say which.

The important property is that nothing is silently dropped.  A skipped step must
appear in the report with a reason, and only a real ``--convert-to`` failure is
allowed to fail the run.
"""

from __future__ import annotations

import pytest

from harness_bench import postconfig
from harness_bench.config import TASKS_DIR
from harness_bench.tasks import Task


def _task(app: str, task_id: str) -> Task:
    return Task.from_file(TASKS_DIR / app / f"{task_id}.json")


def test_gui_steps_are_skipped_not_failed(tmp_path, stage_offline):
    task = _task("libreoffice_calc", "01b269ae-2111-4a07-81fd-3fcd711993b0")
    materialized = stage_offline(task, tmp_path / "work")

    report = postconfig.run_postconfig(materialized, soffice=None, scratch=tmp_path / "n")

    assert report.ok
    kinds = {step.action for step in report.steps}
    assert "skip" in kinds
    # Every declared step is accounted for.
    declared = task.evaluator.get("postconfig", [])
    assert len(report.steps) == len(declared)


def test_activate_window_and_sleep_are_recognised():
    assert postconfig._is_gui_only({"type": "activate_window"})
    assert postconfig._is_gui_only({"type": "sleep"})
    assert postconfig._is_gui_only(
        {"type": "execute", "parameters": {"command": ["python", "-c", "import pyautogui"]}}
    )
    assert not postconfig._is_gui_only(
        {"type": "execute", "parameters": {"command": ["libreoffice", "--convert-to", "csv"]}}
    )


def test_a_convert_to_step_without_soffice_fails_the_report(tmp_path, stage_offline):
    """The five CSV tasks genuinely cannot be graded without LibreOffice."""

    task = _task("libreoffice_calc", "0bf05a7d-b28b-44d2-955a-50b41e24012a")
    materialized = stage_offline(task, tmp_path / "work")

    report = postconfig.run_postconfig(materialized, soffice=None, scratch=tmp_path / "n")

    assert not report.ok
    assert "soffice" in report.required_failure.lower()


def test_a_convert_to_step_runs_the_real_command(tmp_path, stage_offline, monkeypatch):
    """When soffice exists the step is executed, not simulated."""

    calls = []

    def fake_convert(command, workdir, soffice, scratch):
        calls.append((command, soffice))
        from harness_bench.normalize import NormalizeResult

        return NormalizeResult("postconfig:convert-to", "convert", True, "")

    monkeypatch.setattr(postconfig, "run_convert_to_csv", fake_convert)

    task = _task("libreoffice_calc", "0bf05a7d-b28b-44d2-955a-50b41e24012a")
    materialized = stage_offline(task, tmp_path / "work")

    report = postconfig.run_postconfig(
        materialized, soffice=r"C:\fake\soffice.exe", scratch=tmp_path / "n"
    )

    assert report.ok
    assert calls, "the convert-to step was never handed to the converter"
    command, soffice = calls[0]
    assert soffice == r"C:\fake\soffice.exe"
    assert any("--convert-to" in part for part in command)


def test_unhandled_execute_is_reported_with_its_command(tmp_path, stage_offline):
    task = _task("libreoffice_calc", "01b269ae-2111-4a07-81fd-3fcd711993b0")
    materialized = stage_offline(task, tmp_path / "work")

    # Replace the evaluator's postconfig with a command nothing handles.
    task.evaluator["postconfig"] = [
        {"type": "execute", "parameters": {"command": ["echo", "hello"]}}
    ]
    report = postconfig.run_postconfig(materialized, soffice=None, scratch=tmp_path / "n")

    assert report.ok
    assert "echo hello" in report.steps[0].detail
