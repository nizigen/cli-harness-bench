"""What each interface exposes, and what it refuses to expose.

The point of the comparison is that the interfaces differ *only* in their
channel.  If the harness interface quietly grew a shell, or the GUI interface
could run Python, the comparison would be meaningless — so that is what these
tests pin down.
"""

from __future__ import annotations

import pytest

from harness_bench import interfaces
from harness_bench.config import TASKS_DIR, BenchConfig
from harness_bench.tasks import Task


def _materialized(app: str, task_id: str, stage_offline, tmp_path):
    task = Task.from_file(TASKS_DIR / app / f"{task_id}.json")
    return stage_offline(task, tmp_path / "work")


@pytest.fixture()
def calc_task(stage_offline, tmp_path):
    return _materialized(
        "libreoffice_calc", "01b269ae-2111-4a07-81fd-3fcd711993b0", stage_offline, tmp_path
    )


@pytest.fixture()
def gimp_task(stage_offline, tmp_path):
    return _materialized("gimp", "2a729ded-3296-423d-aec4-7dd55ed5fbb3", stage_offline, tmp_path)


def test_harness_interface_offers_only_the_binary_and_finish(calc_task):
    interface = interfaces.HarnessInterface(BenchConfig(), calc_task)

    names = [spec.name for spec in interface.tools()]

    assert names == ["run_harness", "finish"]


def test_harness_interface_picks_the_binary_for_the_application(calc_task, gimp_task):
    config = BenchConfig()

    assert interfaces.HarnessInterface(config, calc_task).binary == "cli-anything-libreoffice"
    assert interfaces.HarnessInterface(config, gimp_task).binary == "cli-anything-gimp"


def test_harness_prompt_names_the_working_directory_and_forbids_other_channels(calc_task):
    prompt = interfaces.HarnessInterface(BenchConfig(), calc_task).system_prompt(calc_task)

    assert str(calc_task.workdir) in prompt
    assert "cli-anything-libreoffice" in prompt
    assert "cannot use a shell" in prompt
    for local in calc_task.vm_to_local.values():
        assert str(local.relative_to(calc_task.workdir).as_posix()) in prompt


def test_harness_skill_documentation_is_injected_when_configured(calc_task):
    config = BenchConfig.from_dict(
        {"harness": {"skill_docs": {"libreoffice": "CALC COMMANDS: set-cell ..."}}}
    )
    prompt = interfaces.HarnessInterface(config, calc_task).system_prompt(calc_task)

    assert "CALC COMMANDS: set-cell" in prompt


def test_harness_rejects_a_malformed_argument_list(calc_task):
    interface = interfaces.HarnessInterface(BenchConfig(), calc_task)

    with pytest.raises(interfaces.ToolError):
        interface.call("run_harness", {"args": "calc list-sheets"})
    with pytest.raises(interfaces.ToolError):
        interface.call("run_harness", {"args": [1, 2]})


def test_harness_reports_a_missing_binary_as_a_failed_call(calc_task):
    config = BenchConfig.from_dict({"harness": {"libreoffice": "definitely-not-installed-xyz"}})
    interface = interfaces.HarnessInterface(config, calc_task)

    outcome = interface.call("run_harness", {"args": ["--help"]})

    assert not outcome.ok
    assert "command not found" in outcome.content


def test_script_interface_offers_shell_python_and_finish(calc_task):
    names = [spec.name for spec in interfaces.ScriptInterface(BenchConfig(), calc_task).tools()]

    assert names == ["run_shell", "run_python", "finish"]


def test_script_prompt_names_the_libraries_it_may_use(calc_task):
    prompt = interfaces.ScriptInterface(BenchConfig(), calc_task).system_prompt(calc_task)

    for library in ("openpyxl", "python-pptx", "python-docx", "Pillow"):
        assert library in prompt
    assert "soffice" in prompt


def test_script_python_runs_in_the_task_directory(calc_task):
    interface = interfaces.ScriptInterface(BenchConfig(), calc_task)

    outcome = interface.call("run_python", {"code": "import os; print(os.getcwd())"})

    assert outcome.ok
    assert str(calc_task.workdir) in outcome.content


def test_script_shell_runs_and_reports_a_failure(calc_task):
    interface = interfaces.ScriptInterface(BenchConfig(), calc_task)

    ok = interface.call("run_shell", {"command": "echo hello"})
    assert ok.ok and "hello" in ok.content

    bad = interface.call("run_shell", {"command": "exit 3"})
    assert not bad.ok
    assert "exit 3" in bad.content


def test_gui_interface_refuses_a_tool_outside_the_whitelist(calc_task):
    """The whitelist is the whole reason the GUI arm is comparable."""

    interface = interfaces.GuiInterface(BenchConfig(), calc_task)
    # No MCP server is started: the refusal must happen before any I/O.
    with pytest.raises(interfaces.ToolError, match="not on the allowed list"):
        interface.call("WriteFile", {})


def test_gui_prompt_lists_exactly_the_whitelisted_tools(calc_task):
    config = BenchConfig()
    prompt = interfaces.GuiInterface(config, calc_task).system_prompt(calc_task)

    for tool in config.gui.tools:
        assert tool in prompt
    assert "cannot use a shell" in prompt


class _StubGuiArm:
    """A GUI arm with a fixed tool list, so composition can be tested offline."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def tools(self):
        return [
            interfaces.ToolSpec("Screenshot", "take a screenshot", {"type": "object"}),
            interfaces.ToolSpec("Click", "click somewhere", {"type": "object"}),
            interfaces.ToolSpec("finish", "report", {"type": "object"}),
        ]

    def system_prompt(self, task):
        return "gui arm"

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return interfaces.ToolOutcome(f"gui ran {name}")

    def finalize(self, task):
        return {"interface": "gui"}

    def close(self):
        return None


def test_hybrid_offers_both_channels_and_one_finish(calc_task):
    interface = interfaces.HybridInterface(BenchConfig(), calc_task)
    interface.gui = _StubGuiArm()

    names = [spec.name for spec in interface.tools()]

    assert names == ["run_harness", "Screenshot", "Click", "finish"]


def test_hybrid_routes_gui_tools_to_the_gui_arm(calc_task):
    interface = interfaces.HybridInterface(BenchConfig(), calc_task)
    stub = _StubGuiArm()
    interface.gui = stub

    outcome = interface.call("Screenshot", {})

    assert outcome.ok
    assert stub.calls == [("Screenshot", {})]


def test_hybrid_routes_harness_calls_to_the_harness(calc_task):
    # A binary that cannot exist keeps this independent of what is installed.
    config = BenchConfig.from_dict({"harness": {"libreoffice": "cli-anything-not-installed"}})
    interface = interfaces.HybridInterface(config, calc_task)

    outcome = interface.call("run_harness", {"args": ["--help"]})

    assert "command not found" in outcome.content
    assert interface.harness.calls


def test_unknown_interface_name_is_rejected(calc_task):
    with pytest.raises(KeyError, match="unknown interface"):
        interfaces.build_interface("telepathy", BenchConfig(), calc_task)


def test_tool_spec_renders_the_openai_shape():
    spec = interfaces.ToolSpec("x", "does x", {"type": "object", "properties": {}})

    payload = spec.as_openai()

    assert payload["type"] == "function"
    assert payload["function"]["name"] == "x"
    assert payload["function"]["parameters"]["type"] == "object"
