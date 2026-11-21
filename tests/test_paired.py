import dataclasses
import json

import pytest

from harness_bench import paired, runner
from harness_bench.config import DEFAULT_CONFIG_PATH


def test_complete_libreoffice_coverage():
    tasks = paired.selected_tasks()
    assert len(tasks) == 113
    assert len({t.id for t in tasks}) == 113
    assert {t.app for t in tasks} == {
        "libreoffice_calc", "libreoffice_writer", "libreoffice_impress"}


def test_freeze_is_resumable_and_refuses_changed_task(tmp_path):
    task = paired.selected_tasks()[0]
    root = paired.freeze(tmp_path, DEFAULT_CONFIG_PATH, [task])
    assert paired.freeze(tmp_path, DEFAULT_CONFIG_PATH, [task]) == root
    changed = dataclasses.replace(task, id="different")
    with pytest.raises(ValueError, match="changed"):
        paired.freeze(tmp_path, DEFAULT_CONFIG_PATH, [changed])
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["config"]["model"]["api_key"] == ""
    assert manifest["repeats"] == 1
    assert "profile.py" in manifest["source_sha256"]
    assert "desktop_profile" in manifest["dependencies"]


def save_record(root, task, arm, status="graded", inputs=None):
    path = root / arm / task.id / "run" / "record.json"
    path.parent.mkdir(parents=True)
    record = runner.RunRecord(task.id, task.app, arm, 0, str(path.parent))
    record.finished_at = "2026-11-21T18:00:00"
    record.status = "done"
    record.stopped_reason = "finished"
    record.grader_status = status
    record.extra["inputs"] = inputs if inputs is not None else {"input.odt": "samehash"}
    path.write_text(json.dumps(record.as_dict()), encoding="utf-8")
    return path


def test_unavailable_gui_does_not_count_as_a_valid_pair(tmp_path):
    task = paired.selected_tasks()[0]
    save_record(tmp_path, task, "harness")
    save_record(tmp_path, task, "gui", "unsupported_setup")
    result = paired.summarize(tmp_path, [task])
    assert result["finished_runs"] == 2
    assert result["valid_pairs"] == 0
    assert result["arms"]["gui"]["graded"] == 0
    assert result["arms"]["gui"]["unavailable"] == {"unsupported_setup": 1}


def test_different_inputs_do_not_count_as_a_pair(tmp_path):
    task = paired.selected_tasks()[0]
    save_record(tmp_path, task, "harness")
    save_record(tmp_path, task, "gui", inputs={"input.odt": "different"})
    assert paired.summarize(tmp_path, [task])["valid_pairs"] == 0


@pytest.mark.parametrize("has_inputs", [True, False])
def test_empty_input_evidence_only_pairs_tasks_with_no_downloads(tmp_path, has_inputs):
    tasks = paired.selected_tasks()
    task = next(task for task in tasks if bool(task.inputs) == has_inputs)
    for arm in ("harness", "gui"):
        save_record(tmp_path, task, arm, inputs={})
    assert paired.summarize(tmp_path, [task])["valid_pairs"] == (0 if has_inputs else 1)


def test_completed_cells_are_not_replayed_on_resume(tmp_path, monkeypatch):
    task = paired.selected_tasks()[0]
    save_record(tmp_path, task, "harness")
    save_record(tmp_path, task, "gui")
    monkeypatch.setattr(paired.subprocess, "Popen", lambda *a, **k: pytest.fail("replayed"))
    paired.execute(tmp_path, [task], DEFAULT_CONFIG_PATH)
    assert paired.summarize(tmp_path, [task])["valid_pairs"] == 1


@pytest.mark.parametrize("status,reason", [
    ("done", "finished"), ("infeasible", "finished"),
    ("incomplete", "tool_call_budget"), ("incomplete", "wall_clock_budget"),
    ("incomplete", "no_tool_call"),
])
def test_agent_results_are_valid_even_when_unsuccessful(status, reason):
    record = {"finished_at": "now", "status": status, "stopped_reason": reason,
              "grader_status": "graded", "score": 0}
    assert paired.valid_record(record)


@pytest.mark.parametrize("changes", [
    {"status": "error"}, {"stopped_reason": "model_error"},
    {"stopped_reason": "runner_error"}, {"stopped_reason": "unknown"},
    {"finished_at": ""}, {"grader_status": "unsupported_setup"},
    {"extra": {"desktop": {"save": {"ok": False}}}},
    {"extra": {"desktop": {"ok": False, "status": "dialog"}}},
    {"extra": {"desktop_close_error": "failed"}},
    {"extra": {"interface_close_error": "failed"}},
    {"extra": {"interface_finalize_error": "failed"}},
    {"extra": {"desktop": {"cleanup_error": "failed"}}},
])
def test_infrastructure_errors_never_count_as_success(tmp_path, changes):
    task = paired.selected_tasks()[0]
    save_record(tmp_path, task, "harness")
    path = save_record(tmp_path, task, "gui")
    record = json.loads(path.read_text())
    record.update(changes, score=1)
    path.write_text(json.dumps(record), encoding="utf-8")
    assert not paired.valid_record(record)
    result = paired.summarize(tmp_path, [task])
    assert result["valid_pairs"] == 0
    assert result["arms"]["gui"]["success"] == 0


def test_missing_input_evidence_does_not_make_a_pair(tmp_path):
    task = paired.selected_tasks()[0]
    for arm in ("harness", "gui"):
        path = save_record(tmp_path, task, arm)
        record = json.loads(path.read_text())
        record["extra"].pop("inputs")
        path.write_text(json.dumps(record), encoding="utf-8")
    assert paired.summarize(tmp_path, [task])["valid_pairs"] == 0


def test_retry_errors_replays_only_invalid_arm_and_preserves_old_record(tmp_path, monkeypatch):
    task = paired.selected_tasks()[0]
    save_record(tmp_path, task, "harness")
    old_path = save_record(tmp_path, task, "gui", status="runner_error")
    old_bytes = old_path.read_bytes()
    calls = []

    class Worker:
        def __init__(self, command, **kwargs):
            calls.append(command)
            self.pid = 123

        def wait(self, timeout):
            path = tmp_path / "gui" / task.id / "retry" / "record.json"
            path.parent.mkdir(parents=True)
            record = json.loads(old_bytes)
            record.update(run_dir=str(path.parent), grader_status="graded")
            path.write_text(json.dumps(record), encoding="utf-8")
            return 0

    monkeypatch.setattr(paired.subprocess, "Popen", Worker)
    paired.execute(tmp_path, [task], DEFAULT_CONFIG_PATH, retry_errors=True)
    assert len(calls) == 1
    assert calls[0][-1] == "gui"
    assert old_path.read_bytes() == old_bytes
    assert paired.summarize(tmp_path, [task])["valid_pairs"] == 1


@pytest.mark.parametrize("retry_errors,status,reason,grader", [
    (False, "error", "model_error", "graded"),
    (True, "incomplete", "tool_call_budget", "graded"),
    (True, "incomplete", "wall_clock_budget", "graded"),
    (True, "infeasible", "finished", "graded"),
])
def test_resume_does_not_implicitly_retry_errors_or_valid_agent_failures(
        tmp_path, monkeypatch, retry_errors, status, reason, grader):
    task = paired.selected_tasks()[0]
    for arm in ("harness", "gui"):
        path = save_record(tmp_path, task, arm, status=grader)
        record = json.loads(path.read_text())
        record.update(status=status, stopped_reason=reason)
        path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(paired.subprocess, "Popen", lambda *a, **k: pytest.fail("replayed"))
    paired.execute(tmp_path, [task], DEFAULT_CONFIG_PATH, retry_errors=retry_errors)


def test_retry_crash_writes_new_terminal_record_instead_of_reusing_old(tmp_path, monkeypatch):
    task = paired.selected_tasks()[0]
    save_record(tmp_path, task, "harness")
    old_path = save_record(tmp_path, task, "gui", status="runner_error")
    old_bytes = old_path.read_bytes()

    class Worker:
        pid = 123

        def __init__(self, *args, **kwargs):
            pass

        def wait(self, timeout):
            return 1

    monkeypatch.setattr(paired.subprocess, "Popen", Worker)
    paired.execute(tmp_path, [task], DEFAULT_CONFIG_PATH, retry_errors=True)
    assert old_path.read_bytes() == old_bytes
    records = list(old_path.parent.parent.glob("*/record.json"))
    assert len(records) == 2
    assert paired.latest_record(tmp_path, task.id, "gui")["grader_status"] == "worker_error"


def test_retry_flag_is_forwarded_to_execution(tmp_path, monkeypatch):
    import sys

    calls = []
    monkeypatch.setattr(paired, "freeze", lambda root, config, tasks, **kwargs: root)
    monkeypatch.setattr(paired, "execute", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["paired", "--root", str(tmp_path), "--execute", "--retry-errors"])
    paired.main()
    assert calls == [{"retry_errors": True, "arms": ("harness", "gui")}]
    monkeypatch.setattr(sys, "argv", ["paired", "--root", str(tmp_path), "--execute",
                                      "--interface", "gui"])
    paired.main()
    assert calls[-1] == {"retry_errors": False, "arms": ("gui",)}


@pytest.fixture
def dependency_files(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from harness_bench.config import BenchConfig

    scripts = tmp_path / "Python" / "Scripts"
    scripts.mkdir(parents=True)
    executable = scripts / "cli-anything-libreoffice.exe"
    executable.write_bytes(b"launcher")
    project = tmp_path / "editable"
    package = project / "cli_anything" / "libreoffice"
    package.mkdir(parents=True)
    source = package / "libreoffice_cli.py"
    source.write_text("def main(): pass\n", encoding="utf-8")
    (project / "pyproject.toml").write_text("version = '1.0.1'\n", encoding="utf-8")
    office = tmp_path / "office"
    office.mkdir()
    for name in ("soffice.com", "soffice.exe", "soffice.bin"):
        (office / name).write_bytes(name.encode())

    class Distribution:
        metadata = {"Name": "cli-anything-libreoffice"}
        version = "1.0.1"
        entry_points = [SimpleNamespace(group="console_scripts", name="cli-anything-libreoffice",
                                       value="cli_anything.libreoffice.libreoffice_cli:main")]

        def locate_file(self, path):
            return tmp_path / "unavailable-installed-copy" / path

        def read_text(self, name):
            return json.dumps({"url": project.as_uri(), "dir_info": {"editable": True}})

    monkeypatch.setattr(paired.shutil, "which", lambda command: str(executable))
    monkeypatch.setattr(paired.importlib.metadata, "distributions", lambda **kwargs: [Distribution()])
    monkeypatch.setattr(paired, "find_soffice", lambda configured: str(office / "soffice.com"))
    monkeypatch.setattr(paired, "_file_version", lambda path: "25.2.1.2")
    config = BenchConfig.from_dict({"model": {"model": "alias/model", "api_key": "DO-NOT-STORE"}})
    return config, source, executable, office


def test_dependency_identity_locks_editable_source_binary_versions_and_model(dependency_files):
    config, source, executable, office = dependency_files
    identity = paired.dependency_identity(config)
    cli = identity["cli"]
    assert cli["version"] == "1.0.1"
    assert cli["package_root"] == str(source.parent.resolve())
    assert cli["source_sha256"] == {source.name: paired._sha256(source)}
    assert cli["executable_sha256"] == paired._sha256(executable)
    assert "pyproject.toml" in cli["project_files_sha256"]
    assert identity["libreoffice"]["files"]["soffice.bin"] == {
        "version": "25.2.1.2", "sha256": paired._sha256(office / "soffice.bin")}
    assert identity["model"]["model"] == "alias/model"
    assert identity["model"]["resolved_revision"] == "unknown"
    assert "DO-NOT-STORE" not in json.dumps(identity)


@pytest.mark.parametrize("changed", ["source", "launcher", "office", "version", "model"])
def test_freeze_rejects_dependency_drift(tmp_path, monkeypatch, dependency_files, changed):
    config, source, executable, office = dependency_files
    monkeypatch.setattr(paired.BenchConfig, "load", lambda path: config)
    task = paired.selected_tasks()[0]
    root = paired.freeze(tmp_path / "batch", DEFAULT_CONFIG_PATH, [task])
    assert paired.freeze(root, DEFAULT_CONFIG_PATH, [task]) == root
    if changed == "source":
        source.write_text("def main(): return 1\n", encoding="utf-8")
    elif changed == "launcher":
        executable.write_bytes(b"new launcher")
    elif changed == "office":
        (office / "soffice.bin").write_bytes(b"new office")
    elif changed == "version":
        monkeypatch.setattr(paired, "_file_version", lambda path: "26.1.0.0")
    else:
        config = dataclasses.replace(config, model=dataclasses.replace(config.model, model="different/model"))
    with pytest.raises(ValueError, match="changed"):
        paired.freeze(root, DEFAULT_CONFIG_PATH, [task])


def test_missing_dependencies_and_pe_resources_are_explicit_unknown(tmp_path, monkeypatch):
    from harness_bench.config import BenchConfig

    monkeypatch.setattr(paired.shutil, "which", lambda command: None)
    monkeypatch.setattr(paired, "find_soffice", lambda configured: None)
    identity = paired.dependency_identity(BenchConfig())
    assert identity["cli"]["version"] == "unknown"
    assert identity["cli"]["package_root"] == "unknown"
    assert identity["libreoffice"]["executable"] is None
    path = tmp_path / "not-an-executable.exe"
    path.write_bytes(b"placeholder")
    assert paired._file_version(path) == "unknown"


def test_execute_freeze_refuses_uninitialized_baseline_without_creating_manifest(tmp_path, monkeypatch, dependency_files):
    config, *_ = dependency_files
    monkeypatch.setattr(paired.BenchConfig, "load", lambda path: config)
    monkeypatch.setattr(paired, "profile_identity", lambda path: {"initialized": False})
    with pytest.raises(ValueError, match="baseline is not initialized"):
        paired.freeze(tmp_path, DEFAULT_CONFIG_PATH, [paired.selected_tasks()[0]], require_profile=True)
    assert not (tmp_path / "manifest.json").exists()


def test_profile_baseline_content_is_frozen_and_drift_rejects_resume(tmp_path, monkeypatch, dependency_files):
    config, *_ = dependency_files
    monkeypatch.setattr(paired.BenchConfig, "load", lambda path: config)
    registry = tmp_path / "baseline" / "user" / "registrymodifications.xcu"
    registry.parent.mkdir(parents=True)
    registry.write_bytes(b"baseline settings")

    def profile_identity(binary):
        digest = paired._sha256(registry)
        return {"initialized": True, "path": str(registry.parent.parent), "binary_hash": "key",
                "binaries": {"soffice.com": "com-hash", "soffice.bin": "bin-hash"},
                "registry_sha256": digest, "files": {"user/registrymodifications.xcu": digest}}

    monkeypatch.setattr(paired, "profile_identity", profile_identity)
    task = paired.selected_tasks()[0]
    root = paired.freeze(tmp_path / "runs", DEFAULT_CONFIG_PATH, [task], require_profile=True)
    manifest = json.loads((root / "manifest.json").read_text())
    identity = manifest["dependencies"]["desktop_profile"]
    assert identity == profile_identity(None)
    assert paired.freeze(root, DEFAULT_CONFIG_PATH, [task], require_profile=True) == root
    registry.write_bytes(b"changed settings")
    with pytest.raises(ValueError, match="changed"):
        paired.freeze(root, DEFAULT_CONFIG_PATH, [task], require_profile=True)
