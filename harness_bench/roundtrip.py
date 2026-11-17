"""The round-trip ceiling check, measured against each task's own baseline.

The obvious version of this check — copy the task's reference file into the result
path, run it through the harness, grade the output — does not work as stated,
because for a substantial minority of OSWorld tasks the ``expected`` file is a
*reference* rather than a fixed point.  ``compare_pptx_files`` with
``examine_title_bottom_position`` scores 0 when both files are identical (the
title must have *moved*), and ``check_tabstops`` scores 0 on its own reference
document.  Copying the reference onto itself therefore fails without saying
anything about the harness.

So the check is defined in two steps:

``baseline``
    Put the reference file at the result path and grade it.  A task whose own
    reference does not score 1.0 cannot measure fidelity at all: the grader
    compares against a state the reference is not in.  Such tasks are reported as
    *not decidable* and excluded from the ceiling, not counted as harness losses.
``round trip``
    For the decidable tasks only, import the reference through the harness,
    export it again, and grade.  Any drop from the baseline is information loss
    inside the harness.

Only the harness interface has a round trip.  The script and GUI interfaces have
nothing to import a document into, and the check is skipped for them with the
reason recorded.
"""

from __future__ import annotations

import json
import pathlib
import shutil
from dataclasses import dataclass, field
from typing import Any

from .config import BenchConfig
from .grading import grade
from .interfaces import _run
from .normalize import find_soffice, save_outputs, soffice_env
from .postconfig import run_postconfig
from .tasks import MaterializedTask, Task, _as_list, fetch, materialize

#: Harness export preset that reproduces each input extension.
EXPORT_PRESETS = {
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
    ".odt": "odt",
    ".ods": "ods",
    ".odp": "odp",
    ".pdf": "pdf",
    ".png": "png",
    ".jpg": "jpeg-high",
    ".jpeg": "jpeg-high",
    ".csv": "csv",
}

#: The gimp harness renders images only; anything else comes back as a PNG.
GIMP_EXPORT_PRESETS = {
    ".png": "png",
    ".jpg": "jpeg-high",
    ".jpeg": "jpeg-high",
    ".webp": "png",
    ".bmp": "png",
    ".tiff": "png",
}


def _trip_commands(
    family: str,
    binary: str,
    source: pathlib.Path,
    project: pathlib.Path,
    exported: pathlib.Path,
) -> list[tuple[str, list[str]]]:
    """The import/export sequence differs between the two harnesses.

    The LibreOffice harness models a document and calls the verb ``open``: one
    command imports the file, one renders it back out.

    The gimp harness has no verb that adopts an arbitrary image *as* the project.
    ``project open`` reads a ``.gimp-cli.json``, not a picture, so the faithful
    equivalent of "import and export" is to create a canvas at the image's own
    size, add the image as a layer, and render.  Anything the harness would have
    to preserve beyond that (layer names, selections, colour profiles) is not
    expressible, which is exactly what this check is meant to expose.
    """

    if family == "gimp":
        width, height = _image_size(source)
        preset = GIMP_EXPORT_PRESETS.get(source.suffix.lower(), "png")
        return [
            (
                "import",
                [
                    binary, "--json", "project", "new",
                    "-w", str(width), "-h", str(height), "-o", str(project),
                ],
            ),
            (
                "import",
                [binary, "--json", "--project", str(project), "layer", "add-from-file", str(source)],
            ),
            (
                "export",
                [
                    binary, "--json", "--project", str(project),
                    "export", "render", str(exported), "--preset", preset, "--overwrite",
                ],
            ),
        ]

    preset = EXPORT_PRESETS.get(source.suffix.lower(), "pdf")
    return [
        (
            "import",
            # ``--project`` is deliberately absent: the CLI loads the project
            # file named there *before* running the command, so pointing it at
            # the file ``--output`` is about to create fails with
            # "Document file not found".
            [
                binary, "--json",
                "document", "open", str(source), "--output", str(project),
            ],
        ),
        (
            "export",
            [
                binary, "--json", "--project", str(project),
                "export", "render", str(exported), "--preset", preset, "--overwrite",
            ],
        ),
    ]


def _image_size(path: pathlib.Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as image:
        return image.size


#: Why a task cannot be measured, when it cannot.
NOT_DECIDABLE = "reference_is_not_a_fixed_point"
NO_REFERENCE = "no_reference_file"
NO_RESULT = "no_result_file"
NEEDS_SOFFICE = "needs_soffice"
HARNESS_MISSING = "harness_not_installed"
REFERENCE_UNAVAILABLE = "reference_unavailable"
INPUT_UNAVAILABLE = "input_unavailable"
ARTIFACT = "reference_is_a_rendered_artifact"

#: Formats a LibreOffice harness can actually carry through import and export.
#: Anything else (a PDF someone asked for, a screenshot of a slide) is a
#: rendered artifact: there is nothing to import, so a round trip would measure
#: the pipeline rather than the harness.
EDITABLE_SUFFIXES = {
    ".docx", ".doc", ".odt", ".rtf",
    ".xlsx", ".xls", ".ods", ".csv",
    ".pptx", ".ppt", ".odp",
}


@dataclass
class RoundTripResult:
    """Outcome of the ceiling check for one task."""

    task_id: str
    app: str
    applicable: bool
    decidable: bool = False
    baseline: float | None = None
    score: float | None = None
    passed: bool = False
    status: str = ""
    reason: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def lost(self) -> bool:
        """The harness lost information the reference had."""

        return (
            self.decidable
            and self.baseline is not None
            and self.score is not None
            and self.score < self.baseline
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "app": self.app,
            "applicable": self.applicable,
            "decidable": self.decidable,
            "baseline": self.baseline,
            "score": self.score,
            "passed": self.passed,
            "status": self.status,
            "reason": self.reason,
            "steps": self.steps,
        }


def _is_spreadsheet_task(task: Task) -> bool:
    """True when grading reads a spreadsheet, which needs a formula cache.

    ``compare_table`` opens the result with ``data_only=True``, so a workbook
    written by a library that stores formulas without results grades as empty.
    Only LibreOffice can fill the cache in, which makes soffice a hard
    requirement for these tasks rather than a convenience.
    """

    for cfg in _as_list(task.evaluator.get("result")):
        if not isinstance(cfg, dict):
            continue
        declared = cfg.get("path")
        names = declared if isinstance(declared, list) else [declared]
        for name in names:
            if isinstance(name, str) and pathlib.PurePosixPath(name).suffix.lower() in (
                ".xlsx",
                ".xlsm",
                ".ods",
                ".xls",
                ".csv",
            ):
                return True
    return False


def _reference(task: Task) -> dict[str, Any] | None:
    """The first cloud file the evaluator compares against."""
    for cfg in _as_list(task.evaluator.get("expected")):
        if isinstance(cfg, dict) and cfg.get("type") == "cloud_file":
            return cfg
    return None


def _result_path(task: Task, materialized: MaterializedTask) -> pathlib.Path | None:
    for cfg in _as_list(task.evaluator.get("result")):
        if not isinstance(cfg, dict):
            continue
        declared = cfg.get("path")
        if isinstance(declared, list):
            declared = declared[0]
        if declared:
            return materialized.resolve(str(declared))
    return None


def _artifact_problem(task: Task) -> str:
    """ARTIFACT when the reference is a rendered file, not an editable document."""

    reference = _reference(task)
    if reference is None or task.family != "libreoffice":
        return ""
    path = reference.get("path")
    if isinstance(path, list):
        path = path[0]
    if path and pathlib.Path(str(path)).suffix.lower() not in EDITABLE_SUFFIXES:
        return ARTIFACT
    return ""


def _stage_reference(
    config: BenchConfig,
    task: Task,
    materialized: MaterializedTask,
) -> tuple[pathlib.Path | None, str]:
    """Copy the reference file onto the result path."""

    reference = _reference(task)
    if reference is None:
        return None, NO_REFERENCE

    destination = _result_path(task, materialized)
    if destination is None:
        return None, NO_RESULT

    path = reference.get("path")
    if isinstance(path, list):
        path = path[0]
    if not path:
        return None, NO_REFERENCE

    suffix = pathlib.Path(str(path)).suffix.lower()
    if task.family == "libreoffice" and suffix not in EDITABLE_SUFFIXES:
        return None, ARTIFACT

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fetch(str(path), config.download_cache), destination)
    return destination, ""


def _score(
    config: BenchConfig,
    materialized: MaterializedTask,
    scratch: pathlib.Path,
    soffice: str | None,
) -> tuple[float, str, str]:
    """Postconfig, normalise and grade the current state of the sandbox."""

    post = run_postconfig(materialized, soffice, scratch)
    paths = [
        materialized.resolve(str(cfg.get("path")))
        for cfg in _as_list(materialized.task.evaluator.get("result"))
        if isinstance(cfg, dict) and isinstance(cfg.get("path"), str)
    ]
    save_outputs(paths, soffice, scratch)

    if not post.ok:
        return 0.0, "postconfig_failed", post.required_failure

    outcome = grade(materialized, ["DONE"], scratch / "cache")
    return outcome.score, outcome.status, outcome.reason


def baseline(
    config: BenchConfig,
    task: Task,
    runs_root: pathlib.Path | str,
) -> tuple[float | None, str, str]:
    """Grade the task's own reference file: the highest score any interface can get."""

    if task.is_infeasible:
        return None, "infeasible", "an infeasible task has no reference file"

    artifact = _artifact_problem(task)
    if artifact:
        return None, artifact, "the reference is a rendered artifact, not an editable document"

    work = (pathlib.Path(runs_root) / "_roundtrip" / task.id / "baseline").resolve()
    try:
        materialized = materialize(task, work / "work", config.download_cache)
    except OSError as exc:
        return None, INPUT_UNAVAILABLE, f"input files could not be fetched: {exc}"
    soffice = find_soffice(config.apps.soffice)

    _, problem = _stage_reference(config, task, materialized)
    if problem:
        return None, problem, ""

    score, status, reason = _score(config, materialized, work / "normalize", soffice)
    return score, status, reason


def check_task(
    config: BenchConfig,
    task: Task,
    runs_root: pathlib.Path | str,
) -> RoundTripResult:
    """Measure whether the harness can carry the reference without losing score."""

    result = RoundTripResult(task_id=task.id, app=task.app, applicable=False)

    if task.is_infeasible:
        result.reason = "infeasible task, no reference to round trip"
        return result

    artifact = _artifact_problem(task)
    if artifact:
        result.status = artifact
        result.reason = (
            "the reference is a rendered artifact (PDF or image), not an editable "
            "document, so it cannot be carried through the harness's import and export"
        )
        return result

    work = (pathlib.Path(runs_root) / "_roundtrip" / task.id).resolve()
    result.applicable = True
    try:
        materialized = materialize(task, work / "work", config.download_cache)
    except OSError as exc:
        result.status = INPUT_UNAVAILABLE
        result.reason = f"input files could not be fetched: {exc}"
        return result
    soffice = find_soffice(config.apps.soffice)

    # -- step 0: can this machine carry anything through the harness at all --
    binary = config.harness.binary_for(task.family)
    if shutil.which(binary) is None:
        result.status = HARNESS_MISSING
        result.reason = (
            f"{binary} is not on PATH, so the round trip cannot be measured on this machine"
        )
        return result

    if soffice is None and _is_spreadsheet_task(task):
        result.status = NEEDS_SOFFICE
        result.reason = (
            "spreadsheet grading reads cached formula results, which only "
            "LibreOffice produces; install soffice to measure this task"
        )
        return result

    # -- step 1: the task's own reference must score 1.0 ------------------
    destination, problem = _stage_reference(config, task, materialized)
    if problem:
        result.reason = problem
        result.status = problem
        if problem == ARTIFACT:
            result.reason = (
                "the reference is a rendered artifact (PDF or image), not an editable "
                "document, so it cannot be carried through the harness's import and export"
            )
        return result

    base_score, base_status, base_reason = _score(
        config, materialized, work / "baseline", soffice
    )
    result.baseline = base_score
    result.steps.append(
        {"step": "baseline", "score": base_score, "status": base_status, "reason": base_reason[:300]}
    )

    if base_status != "graded":
        result.status = REFERENCE_UNAVAILABLE
        result.reason = (
            f"the task's own reference could not be graded ({base_status}: "
            f"{base_reason[:200]}), so the round trip is unmeasurable right now"
        )
        return result

    if base_score < 1.0:
        result.status = NOT_DECIDABLE
        result.reason = (
            f"the task's own reference scores {base_score:.2f} ({base_reason[:160]}), "
            "so a lower score after a round trip would not be the harness's fault"
        )
        return result

    result.decidable = True

    # -- step 2: carry it through the harness ----------------------------
    project = work / "project.json"
    exported = work / "export" / destination.name
    exported.parent.mkdir(parents=True, exist_ok=True)

    for role, command in _trip_commands(task.family, binary, destination, project, exported):
        step = _run(command, work, env=soffice_env(soffice))
        result.steps.append(
            {"step": role, "ok": step.ok, "output": step.content[:600]}
        )
        if not step.ok:
            result.status = f"{role}_failed"
            result.reason = step.content[:400]
            return result

    if not exported.exists():
        result.status = "export_failed"
        result.reason = "the harness reported success but wrote no file"
        return result

    shutil.copyfile(exported, destination)

    trip_score, trip_status, trip_reason = _score(
        config, materialized, work / "roundtrip", soffice
    )
    result.score = trip_score
    result.status = trip_status
    result.reason = trip_reason
    result.passed = trip_score >= base_score
    result.steps.append(
        {"step": "grade", "score": trip_score, "status": trip_status, "reason": trip_reason[:300]}
    )

    (work / "roundtrip.json").write_text(
        json.dumps(result.as_dict(), indent=1, ensure_ascii=False), encoding="utf-8"
    )
    return result


def check_all(
    config: BenchConfig,
    tasks: list[Task],
    runs_root: pathlib.Path | str,
    on_result=None,
) -> list[RoundTripResult]:
    results: list[RoundTripResult] = []
    for task in tasks:
        outcome = check_task(config, task, runs_root)
        results.append(outcome)
        if on_result is not None:
            on_result(outcome)
    return results


def summarize(results: list[RoundTripResult]) -> dict[str, Any]:
    """How much of the task set the harness can carry, and what is unmeasurable."""

    decidable = [r for r in results if r.decidable]
    survived = [r for r in decidable if r.passed]
    lost = [r for r in decidable if r.lost]
    not_decidable = [r for r in results if r.status == NOT_DECIDABLE]
    no_reference = [
        r for r in results if r.status in (NO_REFERENCE, NO_RESULT) and not r.decidable
    ]
    needs_soffice = [r for r in results if r.status == NEEDS_SOFFICE]
    harness_missing = [r for r in results if r.status == HARNESS_MISSING]
    reference_unavailable = [r for r in results if r.status == REFERENCE_UNAVAILABLE]
    input_unavailable = [r for r in results if r.status == INPUT_UNAVAILABLE]
    artifacts = [r for r in results if r.status == ARTIFACT]
    failed = [
        r
        for r in results
        if r.decidable and r.score is None and r.status not in (HARNESS_MISSING,)
    ]

    by_app: dict[str, dict[str, int]] = {}
    for outcome in results:
        bucket = by_app.setdefault(
            outcome.app, {"tasks": 0, "decidable": 0, "survived": 0, "lost": 0}
        )
        bucket["tasks"] += 1
        if outcome.decidable:
            bucket["decidable"] += 1
            bucket["survived"] += 1 if outcome.passed else 0
            bucket["lost"] += 1 if outcome.lost else 0

    measured = [r for r in decidable if r.score is not None]
    return {
        "tasks": len(results),
        "decidable": len(decidable),
        "measured": len(measured),
        "survived": len(survived),
        "lost": len(lost),
        "lost_task_ids": [r.task_id for r in lost],
        "failed": len(failed),
        "failed_task_ids": [r.task_id for r in failed],
        "ceiling": (len(survived) / len(measured)) if measured else None,
        "not_decidable": len(not_decidable),
        "no_reference": len(no_reference),
        "needs_soffice": len(needs_soffice),
        "harness_missing": len(harness_missing),
        "reference_unavailable": len(reference_unavailable),
        "input_unavailable": len(input_unavailable),
        "rendered_artifacts": len(artifacts),
        "rendered_artifact_task_ids": [r.task_id for r in artifacts],
        "by_app": by_app,
    }
