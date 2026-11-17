"""Replay the parts of OSWorld's ``postconfig`` that grading actually depends on.

OSWorld runs ``postconfig`` through its VM setup controller before scoring.  Most
steps are window activation and a Ctrl+S keystroke: they exist because the GUI
agent never saves its work, and off-VM the runner saves files itself.  Five calc
tasks, however, declare a real ``libreoffice --convert-to csv`` command and then
grade that CSV as a second result file, so the command has to run for real.

Everything else is reported as skipped with a reason, never silently ignored.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

from .normalize import NormalizeResult, run_convert_to_csv
from .tasks import MaterializedTask, rewrite_vm_path


@dataclass
class PostconfigReport:
    steps: list[NormalizeResult] = field(default_factory=list)
    required_failure: str = ""

    @property
    def ok(self) -> bool:
        return not self.required_failure

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "required_failure": self.required_failure,
            "steps": [step.as_dict() for step in self.steps],
        }


def _is_gui_only(step: dict[str, Any]) -> bool:
    kind = step.get("type")
    if kind in ("activate_window", "sleep"):
        return True
    if kind == "execute":
        command = " ".join(str(part) for part in step.get("parameters", {}).get("command", []))
        return "pyautogui" in command
    return False


def run_postconfig(
    materialized: MaterializedTask,
    soffice: str | None,
    scratch: pathlib.Path | str,
) -> PostconfigReport:
    """Execute the postconfig steps that grading cannot do without."""

    report = PostconfigReport()
    steps = materialized.task.evaluator.get("postconfig") or []

    for step in steps:
        if not isinstance(step, dict):
            continue
        kind = step.get("type")
        if _is_gui_only(step):
            report.steps.append(
                NormalizeResult(
                    f"postconfig:{kind}",
                    "skip",
                    True,
                    "GUI-only step; the runner saves outputs itself",
                )
            )
            continue

        if kind == "execute":
            command = [
                rewrite_vm_path(str(part), materialized.workdir)
                for part in step.get("parameters", {}).get("command", [])
            ]
            if any("--convert-to" in part for part in command):
                result = run_convert_to_csv(
                    command, materialized.workdir, soffice, pathlib.Path(scratch)
                )
                report.steps.append(result)
                if not result.ok:
                    report.required_failure = result.detail or "convert-to failed"
                continue
            report.steps.append(
                NormalizeResult(
                    "postconfig:execute",
                    "skip",
                    True,
                    f"unhandled command: {' '.join(command)[:120]}",
                )
            )
            continue

        report.steps.append(
            NormalizeResult(f"postconfig:{kind}", "skip", True, "unhandled step type")
        )

    return report
