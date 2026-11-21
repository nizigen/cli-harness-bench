"""Run one task through one interface and record everything that happened.

A run is a directory on disk:

``runs/<interface>/<task-id>/<timestamp>/``
    ``record.json``    the structured outcome (status, score, cost, steps)
    ``transcript.json`` the full model transcript
    ``postconfig.json`` which grading prerequisites ran
    ``normalize.json``  what happened to the produced files

Recording to disk as we go means an interrupted overnight GUI run still leaves
evidence behind.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

from .agent import AgentOutcome, AgentRunner
from .config import BenchConfig, REPO_ROOT
from .grading import GradeResult, grade
from .interfaces import build_interface
from .normalize import collect_result_paths, find_soffice, save_outputs
from .postconfig import run_postconfig
from .tasks import MaterializedTask, Task, materialize

RUNS_ROOT = REPO_ROOT / "runs"


def _timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


@dataclass
class RunRecord:
    """Everything worth knowing about one (task, interface, repeat) triple."""

    task_id: str
    app: str
    interface: str
    repeat: int
    run_dir: str
    status: str = "error"
    summary: str = ""
    score: float = 0.0
    grader_status: str = ""
    grader_reason: str = ""
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_s: float = 0.0
    stopped_reason: str = ""
    started_at: str = ""
    finished_at: str = ""
    failure_class: str = ""
    failure_note: str = ""
    postconfig: dict[str, Any] = field(default_factory=dict)
    normalize: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def declared_done(self) -> bool:
        return self.status == "done"

    @property
    def false_completion(self) -> bool:
        """Declared done but the grader disagrees."""

        return self.declared_done and self.score < 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "app": self.app,
            "interface": self.interface,
            "repeat": self.repeat,
            "run_dir": self.run_dir,
            "status": self.status,
            "summary": self.summary,
            "score": self.score,
            "grader_status": self.grader_status,
            "grader_reason": self.grader_reason,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "elapsed_s": round(self.elapsed_s, 3),
            "stopped_reason": self.stopped_reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "failure_class": self.failure_class,
            "failure_note": self.failure_note,
            "postconfig": self.postconfig,
            "normalize": self.normalize,
            "extra": self.extra,
        }


def classify_failure(
    outcome: AgentOutcome,
    grade_result: GradeResult,
    roundtrip_ok: bool | None,
) -> tuple[str, str]:
    """Assign one of the proposal's four harness failure classes.

    ``expressiveness``  the interface has no way to express the operation
    ``fidelity``        the round trip through the interface loses information
    ``harness_defect``  a command exists but errored or produced the wrong result
    ``agent_error``     the interface worked, the agent did not

    The classification is a heuristic first pass; the proposal requires human
    review of it, so the note always carries the evidence it was based on.
    """

    if grade_result.status != "graded":
        return "grader_unavailable", grade_result.reason

    # A run that scored full marks did not fail, however many exploratory calls
    # errored along the way; classifying it would inflate every failure table.
    if grade_result.score >= 1.0:
        return "", ""

    if outcome.declared_infeasible:
        return "declared_infeasible", outcome.summary[:400]

    if outcome.stopped_reason in ("tool_call_budget", "wall_clock_budget"):
        return "agent_error", f"ran out of budget ({outcome.stopped_reason})"

    if outcome.stopped_reason in ("model_error", "no_tool_call"):
        return "agent_error", f"loop stopped: {outcome.stopped_reason}"

    failed_calls = [
        call
        for step in outcome.steps
        for call in step.tool_calls
        if not call["ok"] and call["name"] != "finish"
    ]
    if failed_calls and roundtrip_ok is False:
        return "fidelity", "round trip loses information and calls failed"

    if failed_calls:
        sample = failed_calls[0]
        return "harness_defect", f"{sample['name']} failed: {sample['result'][:300]}"

    if roundtrip_ok is False:
        return "fidelity", "the standard answer does not survive the round trip"

    if outcome.declared_done and grade_result.score < 1.0:
        return "agent_error", f"declared done, grader said {grade_result.score}: {grade_result.reason}"

    if outcome.tool_calls == 0:
        return "expressiveness", "the agent never invoked the interface"

    return "agent_error", grade_result.reason or outcome.stopped_reason


def run_once(
    config: BenchConfig,
    task: Task,
    interface_name: str,
    repeat: int = 0,
    runs_root: pathlib.Path | str = RUNS_ROOT,
    keep_workdir: bool = False,
    roundtrip_ok: bool | None = None,
) -> RunRecord:
    """Materialize the task, drive the agent, then grade what it produced."""

    started_at = dt.datetime.now().isoformat(timespec="seconds")
    run_dir = pathlib.Path(runs_root).resolve() / interface_name / task.id / f"{_timestamp()}-r{repeat}"
    run_dir.mkdir(parents=True, exist_ok=True)
    workdir = run_dir / "work"
    cache_root = run_dir / "grade-cache"

    record = RunRecord(
        task_id=task.id,
        app=task.app,
        interface=interface_name,
        repeat=repeat,
        run_dir=str(run_dir),
        started_at=started_at,
    )

    def persist(name, value):
        target = run_dir / name
        staging = target.with_suffix(target.suffix + ".tmp")
        staging.write_text(json.dumps(value, indent=1, ensure_ascii=False), encoding="utf-8")
        staging.replace(target)

    record.status = "running"
    record.extra.update({"model": config.model.model, "budget": {
        "max_tool_calls": config.budget.max_tool_calls, "timeout_s": config.budget.timeout_s}})
    persist("record.json", record.as_dict())
    interface = desktop = runner = None
    materialized = None
    outcome = AgentOutcome()
    grade_result = GradeResult(0.0, "runner_error", "run did not reach grading", [])

    def checkpoint(current):
        persist("outcome.json", current.as_dict())
        persist("transcript.json", current.transcript)

    try:
        materialized = materialize(task, workdir, config.download_cache)
        soffice = find_soffice(config.apps.soffice)
        record.extra["soffice"] = soffice
        import hashlib
        record.extra["inputs"] = {
            str(p.relative_to(workdir)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in workdir.rglob("*") if p.is_file()
        }
        interface = build_interface(interface_name, config, materialized)
        if interface_name == "gui":
            from .desktop import DesktopTask
            desktop = DesktopTask(config, materialized, interface)
            record.extra["desktop"] = desktop.prepare()
        runner = AgentRunner(config, interface)
        runner.on_step = checkpoint
        outcome = runner.run(materialized)
        if desktop is not None:
            # Grading's declared Ctrl+S postconfig has its own bounded phase.
            interface.clear_deadline()
            save = desktop.finish()
            record.extra["desktop"] = desktop.metadata
            if not save["ok"]:
                from .desktop import DesktopError
                desktop.metadata["status"] = "save_failed"
                raise DesktopError(f"GUI save failed: {save}", desktop.metadata)
            desktop.close()
            desktop = None
        post = run_postconfig(materialized, soffice, run_dir / "normalize")
        record.postconfig = post.as_dict()
        persist("postconfig.json", record.postconfig)
        normalize_report = save_outputs(
            collect_result_paths(materialized), soffice, run_dir / "normalize"
        )
        record.normalize = normalize_report.as_dict()
        persist("normalize.json", record.normalize)
        if not post.ok:
            grade_result = GradeResult(0.0, "postconfig_failed", post.required_failure, [])
        else:
            grade_result = grade(materialized, outcome.action_history(), cache_root)
        # Keep the exact scored artifacts even when disposable work is removed.
        artifact_dir = run_dir / "artifacts"
        for p in collect_result_paths(materialized):
            p = pathlib.Path(p)
            if p.is_file() and p.is_relative_to(workdir):
                dest = artifact_dir / p.relative_to(workdir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
    except Exception as exc:
        if runner is not None:
            outcome = runner.outcome
        message = f"{type(exc).__name__}: {exc}"
        outcome.status = "error"
        outcome.summary = message
        outcome.stopped_reason = "runner_error"
        grade_result = GradeResult(0.0, "runner_error", message, [])
        metadata = getattr(exc, "metadata", None)
        if metadata:
            record.extra["desktop"] = metadata
            if metadata.get("unsupported_setup"):
                grade_result.status = "unsupported_setup"
            elif metadata.get("status") == "save_failed":
                grade_result.status = "save_failed"
    finally:
        if desktop is not None:
            try:
                desktop.close()
            except Exception as exc:
                record.extra["desktop_close_error"] = f"{type(exc).__name__}: {exc}"
        if interface is not None:
            try:
                record.extra["interface"] = interface.finalize(materialized)
            except Exception as exc:
                record.extra["interface_finalize_error"] = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    interface.close()
                except Exception as exc:
                    record.extra["interface_close_error"] = f"{type(exc).__name__}: {exc}"
        checkpoint(outcome)

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
    record.failure_class, record.failure_note = classify_failure(
        outcome, grade_result, roundtrip_ok
    )

    persist("record.json", record.as_dict())

    if not keep_workdir:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(cache_root, ignore_errors=True)

    return record


def run_batch(
    config: BenchConfig,
    tasks: list[Task],
    interfaces: list[str],
    repeats: dict[str, int] | None = None,
    runs_root: pathlib.Path | str = RUNS_ROOT,
    on_record=None,
) -> list[RunRecord]:
    """Run every task through every interface, honouring per-interface repeats."""

    records: list[RunRecord] = []
    counts = repeats or {name: config.repeat_count(name) for name in interfaces}

    for interface_name in interfaces:
        times = int(counts.get(interface_name, 1))
        for task in tasks:
            for repeat in range(times):
                record = run_once(
                    config, task, interface_name, repeat=repeat, runs_root=runs_root
                )
                records.append(record)
                if on_record is not None:
                    on_record(record)
    return records


def load_records(runs_root: pathlib.Path | str = RUNS_ROOT) -> list[dict[str, Any]]:
    """Read every ``record.json`` under ``runs_root``."""

    root = pathlib.Path(runs_root)
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("record.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return records
