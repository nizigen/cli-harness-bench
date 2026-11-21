"""Driving our own CLI through the same agent loop as the OSWorld track.

The point of the track is a like-for-like comparison, so the only thing that
differs from the OSWorld runs is the interface: same model, same budget, same
transcript and record format, same report code. Everything that is specific to
blog tasks lives in :mod:`harness_bench.owncli`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import shutil
from typing import Any

from .agent import AgentOutcome, AgentRunner
from .config import BenchConfig
from .interfaces import ToolError, ToolOutcome, ToolSpec, _run
from .owncli import OwnTask, Sandbox, grade, make_sandbox
from .runner import RUNS_ROOT, RunRecord, classify_failure


class OwnCliInterface:
    """Only ``cli-anything-nizigen-blog`` is available."""

    name = "owncli"

    def __init__(self, config: BenchConfig, sandbox: Sandbox) -> None:
        self.config = config
        self.owncli = config.owncli
        self.sandbox = sandbox
        self.binary = self.owncli.binary
        self.calls: list[dict[str, Any]] = []
        # The CLI resolves its repositories from the environment, and it needs a
        # private config home so a run cannot touch the real blog checkouts.
        self.env = {**os.environ, **sandbox.env}

    def system_prompt(self, task: Sandbox) -> str:
        return (
            "You are an automation agent maintaining a Hugo blog on a Windows machine.\n"
            f"The site repository is {self.sandbox.site}\n"
            f"The published-pages repository is {self.sandbox.pages}\n\n"
            f"You may only use the command line tool `{self.binary}`. "
            "Run it with `--help` to discover its subcommands and with `--json` for "
            "machine-readable output. You cannot use a shell, Python, or the GUI.\n"
            "When the task is finished, call `finish` with a short summary. "
            "If the task cannot be done with this tool, call `finish` with "
            'status "infeasible" and explain why.'
        )

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="run_cli",
                description=(
                    f"Run `{self.binary}` with the given arguments, for example "
                    '["post", "new", "Hello", "--publish"]. The working directory is '
                    "the site repository."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Arguments passed to the CLI, without the binary name.",
                        }
                    },
                    "required": ["args"],
                },
            ),
            ToolSpec(
                name="finish",
                description="Report the final status of the task.",
                parameters={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["done", "infeasible"]},
                        "summary": {"type": "string"},
                    },
                    "required": ["status", "summary"],
                },
            ),
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if name == "finish":
            return ToolOutcome("recorded")
        if name != "run_cli":
            raise ToolError(f"unknown tool {name!r}")

        args = arguments.get("args")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ToolError("args must be a list of strings")

        command = [self.binary, *args]
        outcome = _run(command, self.sandbox.site, env=self.env)
        self.calls.append({"command": command, "ok": outcome.ok})
        return outcome

    def finalize(self, task: Sandbox) -> dict[str, Any]:
        return {"interface": self.name, "binary": self.binary, "calls": self.calls}

    def close(self) -> None:
        return None


def _timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def run_once(
    config: BenchConfig,
    task: OwnTask,
    repeat: int = 0,
    runs_root: pathlib.Path | str = RUNS_ROOT,
    keep_workdir: bool = False,
) -> RunRecord:
    """Materialize one blog task, drive the agent, then check what it produced."""

    started_at = dt.datetime.now().isoformat(timespec="seconds")
    run_dir = pathlib.Path(runs_root) / "owncli" / task.id / f"{_timestamp()}-r{repeat}"
    run_dir.mkdir(parents=True, exist_ok=True)
    workdir = run_dir / "work"

    record = RunRecord(
        task_id=task.id,
        app=task.app,
        interface="owncli",
        repeat=repeat,
        run_dir=str(run_dir),
        started_at=started_at,
    )

    sandbox = make_sandbox(task, workdir, config)
    record.extra["sandbox"] = {
        "site": str(sandbox.site),
        "pages": str(sandbox.pages),
        "home": str(sandbox.home),
        "gap": task.gap,
    }

    interface = OwnCliInterface(config, sandbox)
    outcome = AgentOutcome()
    try:
        outcome = AgentRunner(config, interface).run(sandbox)  # type: ignore[arg-type]
    except Exception as exc:  # a crashed run is still evidence
        outcome.status = "error"
        outcome.summary = f"{type(exc).__name__}: {exc}"
        outcome.stopped_reason = "runner_error"
    finally:
        try:
            record.extra["interface"] = interface.finalize(sandbox)
        except Exception as exc:  # finalize must never mask the real failure
            record.extra["interface"] = {"error": f"{type(exc).__name__}: {exc}"}
        interface.close()

    (run_dir / "transcript.json").write_text(
        json.dumps(outcome.transcript, indent=1, ensure_ascii=False), encoding="utf-8"
    )

    grade_result = grade(task, sandbox)

    record.status = outcome.status
    record.summary = outcome.summary
    record.score = grade_result.score
    record.grader_status = grade_result.status
    record.grader_reason = grade_result.reason
    record.tool_calls = outcome.tool_calls
    record.prompt_tokens = outcome.prompt_tokens
    record.completion_tokens = outcome.completion_tokens
    record.elapsed_s = outcome.elapsed_s
    record.stopped_reason = outcome.stopped_reason
    record.finished_at = dt.datetime.now().isoformat(timespec="seconds")
    record.extra["per_metric"] = grade_result.per_metric
    # A task the CLI cannot express at all is expressiveness, not an agent slip.
    if task.gap and grade_result.score < 1.0:
        record.failure_class = "expressiveness"
        record.failure_note = task.gap
    else:
        record.failure_class, record.failure_note = classify_failure(
            outcome, grade_result, None
        )

    (run_dir / "record.json").write_text(
        json.dumps(record.as_dict(), indent=1, ensure_ascii=False), encoding="utf-8"
    )

    if not keep_workdir:
        shutil.rmtree(workdir, ignore_errors=True)

    return record


def run_batch(
    config: BenchConfig,
    tasks: list[OwnTask],
    repeats: int | None = None,
    runs_root: pathlib.Path | str = RUNS_ROOT,
    on_record=None,
) -> list[RunRecord]:
    """Run every blog task ``repeats`` times."""

    times = config.repeat_count("owncli") if repeats is None else repeats
    records: list[RunRecord] = []
    for task in tasks:
        for repeat in range(times):
            record = run_once(config, task, repeat=repeat, runs_root=runs_root)
            records.append(record)
            if on_record is not None:
                on_record(record)
    return records
