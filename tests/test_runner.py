import json
from types import SimpleNamespace

import pytest

from harness_bench import runner
from harness_bench.config import BenchConfig
from harness_bench.tasks import Task


def task():
    return Task("fixture", "libreoffice_writer", "Edit", [], {"func": "infeasible"})


def test_materialization_failure_has_a_terminal_record(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("fixture download unavailable")
    monkeypatch.setattr(runner, "materialize", fail)
    record = runner.run_once(BenchConfig(), task(), "harness", runs_root=tmp_path)
    assert record.grader_status == "runner_error"
    assert "download unavailable" in record.grader_reason
    assert record.finished_at
    saved = json.loads(next(tmp_path.rglob("record.json")).read_text())
    assert saved["status"] == "error"
    assert saved["extra"]["budget"]["max_tool_calls"] == 50
    assert next(tmp_path.rglob("outcome.json")).is_file()


def test_setup_failure_is_not_reported_as_an_agent_task_failure(tmp_path, monkeypatch):
    from harness_bench import tasks
    from harness_bench import desktop
    staged = tasks.MaterializedTask(task(), tmp_path / "work", "Edit", {})
    monkeypatch.setattr(runner, "materialize", lambda *a: staged)
    monkeypatch.setattr(runner, "find_soffice", lambda *a: "not-used")

    class GUI:
        def finalize(self, task):
            return {}
        def close(self):
            self.closed = True

    gui = GUI()
    monkeypatch.setattr(runner, "build_interface", lambda *a: gui)

    class SetupError(RuntimeError):
        metadata = {"unsupported_setup": [{"type": "execute"}]}

    class Desktop:
        def __init__(self, *args):
            pass
        def prepare(self):
            raise SetupError("Linux coordinate setup")
        def close(self):
            pass

    monkeypatch.setattr(desktop, "DesktopTask", Desktop)
    record = runner.run_once(BenchConfig(), task(), "gui", runs_root=tmp_path / "runs")
    assert record.grader_status == "unsupported_setup"
    assert record.failure_class == "grader_unavailable"
    assert record.tool_calls == 0
    assert gui.closed


def test_save_dialog_preserves_session_metadata_and_always_closes_transport(tmp_path, monkeypatch):
    from harness_bench import desktop, tasks
    from harness_bench.agent import AgentOutcome
    staged = tasks.MaterializedTask(task(), tmp_path / "work", "Edit", {})
    monkeypatch.setattr(runner, "materialize", lambda *a: staged)
    monkeypatch.setattr(runner, "find_soffice", lambda *a: "not-used")
    events = []

    class GUI:
        def clear_deadline(self):
            events.append("deadline-cleared")
        def finalize(self, task):
            raise OSError("finalize fixture failed")
        def close(self):
            events.append("transport-closed")

    class Desktop:
        def __init__(self, *args):
            self.metadata = {"status": "ready", "profile": "owned-profile"}
        def prepare(self):
            return self.metadata
        def finish(self):
            assert events == ["deadline-cleared"]
            save = {"ok": False, "status": "dialog", "verified": False}
            self.metadata["save"] = save
            return save
        def close(self):
            events.append("desktop-closed")
            self.metadata["closed"] = True

    class Agent:
        def __init__(self, *args):
            self.outcome = AgentOutcome(status="done", tool_calls=2, stopped_reason="finished")
        def run(self, task):
            return self.outcome

    monkeypatch.setattr(runner, "build_interface", lambda *a: GUI())
    monkeypatch.setattr(desktop, "DesktopTask", Desktop)
    monkeypatch.setattr(runner, "AgentRunner", Agent)
    monkeypatch.setattr(runner, "grade", lambda *a: (_ for _ in ()).throw(AssertionError("must not grade")))
    record = runner.run_once(BenchConfig(), task(), "gui", runs_root=tmp_path / "runs")
    assert record.grader_status == "save_failed"
    assert record.extra["desktop"]["profile"] == "owned-profile"
    assert record.extra["desktop"]["save"]["status"] == "dialog"
    assert record.extra["desktop"]["closed"]
    assert "finalize fixture failed" in record.extra["interface_finalize_error"]
    assert record.tool_calls == 2
    assert events == ["deadline-cleared", "desktop-closed", "transport-closed"]


@pytest.mark.parametrize("reason,valid", [("wall_clock_budget", True), ("model_error", False)])
def test_gui_agent_termination_remains_visible_after_save_and_grade(tmp_path, monkeypatch, reason, valid):
    from harness_bench import desktop, paired, tasks
    from harness_bench.agent import AgentOutcome
    from harness_bench.grading import GradeResult

    staged = tasks.MaterializedTask(task(), tmp_path / "work", "Edit", {})
    monkeypatch.setattr(runner, "materialize", lambda *a: staged)
    monkeypatch.setattr(runner, "find_soffice", lambda *a: "not-used")
    monkeypatch.setattr(runner, "collect_result_paths", lambda *a: [])
    monkeypatch.setattr(runner, "run_postconfig", lambda *a: SimpleNamespace(
        ok=True, as_dict=lambda: {"ok": True}))
    monkeypatch.setattr(runner, "save_outputs", lambda *a: SimpleNamespace(as_dict=lambda: {}))
    monkeypatch.setattr(runner, "grade", lambda *a: GradeResult(1.0, "graded", "", []))

    class GUI:
        def clear_deadline(self):
            pass
        def finalize(self, task):
            return {}
        def close(self):
            pass

    class Desktop:
        def __init__(self, *args):
            self.metadata = {"status": "ready"}
        def prepare(self):
            return self.metadata
        def finish(self):
            self.metadata["save"] = {"ok": True}
            return self.metadata["save"]
        def close(self):
            self.metadata["closed"] = True

    class Agent:
        def __init__(self, *args):
            self.outcome = AgentOutcome(status="incomplete", stopped_reason=reason)
        def run(self, task):
            return self.outcome

    monkeypatch.setattr(runner, "build_interface", lambda *a: GUI())
    monkeypatch.setattr(desktop, "DesktopTask", Desktop)
    monkeypatch.setattr(runner, "AgentRunner", Agent)
    record = runner.run_once(BenchConfig(), task(), "gui", runs_root=tmp_path / "runs")
    assert record.stopped_reason == reason
    assert record.extra["inputs"] == {}
    assert paired.valid_record(record.as_dict()) is valid
