"""Aggregate run records into the tables the report needs.

Every number here is derived from ``record.json`` files on disk, so a table can
always be traced back to the runs that produced it.
"""

from __future__ import annotations

import json
import math
import pathlib
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

INTERFACE_ORDER = ("gui", "harness", "script", "hybrid", "owncli")

FAILURE_CLASSES = (
    "expressiveness",
    "fidelity",
    "harness_defect",
    "agent_error",
    "declared_infeasible",
    "grader_unavailable",
)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


@dataclass
class InterfaceSummary:
    """Per-interface aggregates over the tasks it ran."""

    interface: str
    runs: int = 0
    tasks: int = 0
    success_rate: float | None = None
    false_completion_rate: float | None = None
    mean_tool_calls: float | None = None
    mean_tokens: float | None = None
    mean_elapsed_s: float | None = None
    stability: float | None = None
    failure_classes: dict[str, int] = field(default_factory=dict)
    per_app: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "interface": self.interface,
            "runs": self.runs,
            "tasks": self.tasks,
            "success_rate": self.success_rate,
            "false_completion_rate": self.false_completion_rate,
            "mean_tool_calls": self.mean_tool_calls,
            "mean_tokens": self.mean_tokens,
            "mean_elapsed_s": self.mean_elapsed_s,
            "stability": self.stability,
            "failure_classes": self.failure_classes,
            "per_app": self.per_app,
            "notes": self.notes,
        }


def _group_by_task(records: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record["task_id"])].append(record)
    return grouped


def _stability(scores: Sequence[float]) -> float | None:
    """Share of repeated runs that agree with the majority outcome."""

    if len(scores) < 2:
        return None
    outcomes = [1 if score >= 1.0 else 0 for score in scores]
    majority = max(sum(outcomes), len(outcomes) - sum(outcomes))
    return majority / len(outcomes)


def summarize_interface(records: list[dict[str, Any]], interface: str) -> InterfaceSummary:
    rows = [record for record in records if record.get("interface") == interface]
    summary = InterfaceSummary(interface=interface, runs=len(rows))
    if not rows:
        return summary

    graded = [row for row in rows if row.get("grader_status") == "graded"]
    grouped = _group_by_task(rows)

    summary.tasks = len(grouped)
    summary.success_rate = _mean([float(row["score"]) for row in graded]) if graded else None
    summary.false_completion_rate = (
        _mean([1.0 if row.get("status") == "done" and float(row["score"]) < 1.0 else 0.0
               for row in graded])
        if graded
        else None
    )
    summary.mean_tool_calls = _mean([float(row.get("tool_calls", 0)) for row in rows])
    summary.mean_tokens = _mean(
        [
            float(row.get("prompt_tokens", 0) + row.get("completion_tokens", 0))
            for row in rows
        ]
    )
    summary.mean_elapsed_s = _mean([float(row.get("elapsed_s", 0.0)) for row in rows])

    stabilities = [
        value
        for value in (
            _stability([float(r["score"]) for r in task_rows]) for task_rows in grouped.values()
        )
        if value is not None
    ]
    summary.stability = _mean(stabilities)

    classes: dict[str, int] = defaultdict(int)
    for row in rows:
        classes[str(row.get("failure_class", ""))] += 1
    summary.failure_classes = dict(sorted(classes.items()))

    per_app: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        per_app[str(row.get("app", ""))].append(row)
    for app, app_rows in sorted(per_app.items()):
        app_graded = [row for row in app_rows if row.get("grader_status") == "graded"]
        summary.per_app[app] = {
            "runs": len(app_rows),
            "success_rate": _mean([float(row["score"]) for row in app_graded]) if app_graded else None,
            "tasks": len({row["task_id"] for row in app_rows}),
        }

    if summary.tasks and len(rows) / summary.tasks < 2:
        summary.notes.append("single run per task; stability not measurable")
    return summary


def summarize_all(records: list[dict[str, Any]]) -> dict[str, Any]:
    interfaces = sorted(
        {str(record.get("interface", "")) for record in records},
        key=lambda name: (INTERFACE_ORDER.index(name) if name in INTERFACE_ORDER else 99, name),
    )
    return {
        "interfaces": [summarize_interface(records, name).as_dict() for name in interfaces],
        "runs": len(records),
        "tasks": len({record.get("task_id") for record in records}),
    }


def bootstrap_ci(
    scores: Sequence[float],
    iterations: int = 2000,
    confidence: float = 0.95,
    seed: int = 12345,
) -> tuple[float, float] | None:
    """Percentile bootstrap confidence interval over per-task scores."""

    if len(scores) < 2:
        return None
    rng = random.Random(seed)
    values = list(scores)
    means = []
    for _ in range(iterations):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(sum(sample) / len(sample))
    means.sort()
    alpha = (1 - confidence) / 2
    low = means[int(alpha * len(means))]
    high = means[min(len(means) - 1, int((1 - alpha) * len(means)))]
    return (low, high)


def mcnemar_exact(a_only: int, b_only: int) -> float:
    """Two-sided exact McNemar p-value for paired task outcomes."""

    total = a_only + b_only
    if total == 0:
        return 1.0
    tail = sum(math.comb(total, k) for k in range(0, min(a_only, b_only) + 1))
    return min(1.0, 2 * tail / (2 ** total))


def paired_comparison(
    records: list[dict[str, Any]],
    interface_a: str,
    interface_b: str,
    task_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Compare two interfaces on the tasks both of them ran."""

    def best_scores(interface: str) -> dict[str, float]:
        grouped = _group_by_task(
            [r for r in records if r.get("interface") == interface and r.get("grader_status") == "graded"]
        )
        return {
            task_id: max(float(row["score"]) for row in rows)
            for task_id, rows in grouped.items()
        }

    scores_a = best_scores(interface_a)
    scores_b = best_scores(interface_b)
    shared = sorted(set(scores_a) & set(scores_b))
    if task_ids is not None:
        allowed = set(task_ids)
        shared = [task_id for task_id in shared if task_id in allowed]

    if not shared:
        return {
            "interface_a": interface_a,
            "interface_b": interface_b,
            "tasks": 0,
            "note": "no tasks were run by both interfaces",
        }

    a_only = sum(1 for t in shared if scores_a[t] >= 1.0 and scores_b[t] < 1.0)
    b_only = sum(1 for t in shared if scores_b[t] >= 1.0 and scores_a[t] < 1.0)
    both = sum(1 for t in shared if scores_a[t] >= 1.0 and scores_b[t] >= 1.0)
    neither = len(shared) - a_only - b_only - both

    return {
        "interface_a": interface_a,
        "interface_b": interface_b,
        "tasks": len(shared),
        "a_success": sum(1 for t in shared if scores_a[t] >= 1.0),
        "b_success": sum(1 for t in shared if scores_b[t] >= 1.0),
        "both": both,
        "neither": neither,
        "a_only": a_only,
        "b_only": b_only,
        "mcnemar_p": mcnemar_exact(a_only, b_only),
        "a_ci": bootstrap_ci([scores_a[t] for t in shared]),
        "b_ci": bootstrap_ci([scores_b[t] for t in shared]),
    }


def failure_table(records: list[dict[str, Any]], interface: str) -> list[dict[str, Any]]:
    """Per-task failure detail, for the manual classification pass."""

    rows: list[dict[str, Any]] = []
    for record in records:
        if record.get("interface") != interface:
            continue
        if float(record.get("score", 0.0)) >= 1.0:
            continue
        rows.append(
            {
                "task_id": record["task_id"],
                "app": record.get("app"),
                "status": record.get("status"),
                "score": record.get("score"),
                "failure_class": record.get("failure_class"),
                "failure_note": record.get("failure_note"),
                "stopped_reason": record.get("stopped_reason"),
                "run_dir": record.get("run_dir"),
            }
        )
    return rows


def refinement_curve(records: list[dict[str, Any]], interface: str = "harness") -> dict[str, Any]:
    """Success rate per refinement round, split into dev and test."""

    rounds: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"dev": [], "test": []})
    for record in records:
        if record.get("interface") != interface or record.get("grader_status") != "graded":
            continue
        round_name = str(record.get("extra", {}).get("round", "round0"))
        side = str(record.get("extra", {}).get("split", ""))
        if side not in ("dev", "test"):
            continue
        rounds[round_name][side].append(float(record["score"]))

    curve = []
    for name in sorted(rounds):
        entry = {"round": name}
        for side in ("dev", "test"):
            scores = rounds[name][side]
            entry[f"{side}_rate"] = _mean(scores) if scores else None
            entry[f"{side}_tasks"] = len(scores)
        dev, test = entry.get("dev_rate"), entry.get("test_rate")
        entry["gap"] = (dev - test) if dev is not None and test is not None else None
        curve.append(entry)
    return {"interface": interface, "rounds": curve}


def load_records(runs_root: pathlib.Path | str) -> list[dict[str, Any]]:
    root = pathlib.Path(runs_root)
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("record.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return records


def markdown_table(rows: list[dict[str, Any]], columns: Sequence[tuple[str, str]]) -> str:
    """Render a list of dicts as a GitHub-flavoured markdown table."""

    header = "| " + " | ".join(label for _, label in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, divider]
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key, "")
            if isinstance(value, float):
                cells.append(f"{value:.3f}")
            elif value is None:
                cells.append("–")
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
