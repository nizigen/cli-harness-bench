"""Reproduce OSWorld's grading on a local Windows machine.

``desktop_env.DesktopEnv.evaluate`` decides three things that matter here and
that we re-implement faithfully:

* ``postconfig`` runs first (window activation plus a Ctrl+S keystroke in the
  original harness).  There is no window manager to talk to off-VM, so it is
  skipped; the runner saves files itself before grading.
* ``func == "infeasible"`` scores 1 only when the agent's last action is a
  ``FAIL`` declaration, and every other task scores 0 when the last action is
  ``FAIL``.
* With a list of metrics the conjunction decides short-circuiting: ``and``
  stops at the first zero, ``or`` stops at the first one, and the remainder is
  averaged or maximised respectively.

The graders themselves are the vendored OSWorld functions, untouched.  Only the
environment object they read through is replaced by :class:`LocalDesktopEnv`,
whose controller resolves OSWorld's ``/home/user/...`` paths into a task working
directory.
"""

from __future__ import annotations

import hashlib
import pathlib
import shutil
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from . import osworld_bootstrap as boot
from .osworld_bootstrap import MissingDependency
from .config import REPO_ROOT
from .tasks import MaterializedTask, Task

#: Metric name -> the ``desktop_env.evaluators.metrics`` module defining it.
METRIC_MODULES: dict[str, str] = {
    # metrics/docs.py
    "check_highlighted_words": "docs",
    "check_italic_font_size_14": "docs",
    "check_tabstops": "docs",
    "compare_docx_files": "docs",
    "compare_docx_images": "docs",
    "compare_docx_tables": "docs",
    "compare_font_names": "docs",
    "compare_line_spacing": "docs",
    "compare_subscript_contains": "docs",
    "compare_unique_train_records": "docs",
    "contains_page_break": "docs",
    "evaluate_colored_words_in_tables": "docs",
    "evaluate_strike_through_last_paragraph": "docs",
    "find_default_font": "docs",
    "has_page_numbers_in_footers": "docs",
    "is_first_line_centered": "docs",
    # metrics/gimp.py
    "check_brightness_decrease_and_structure_sim": "gimp",
    "check_contrast_increase_and_structure_sim": "gimp",
    "check_file_exists_and_structure_sim": "gimp",
    "check_green_background": "gimp",
    "check_image_mirror": "gimp",
    "check_image_size": "gimp",
    "check_palette_and_structure_sim": "gimp",
    "check_saturation_increase_and_structure_sim": "gimp",
    "check_structure_sim": "gimp",
    "check_structure_sim_resized": "gimp",
    "check_textbox_on_leftside": "gimp",
    "check_triangle_position": "gimp",
    # metrics/pdf.py
    "check_pdf_pages": "pdf",
    # metrics/slides.py
    "check_auto_saving_time": "slides",
    "check_continuation_line_indent_no_bullet": "slides",
    "check_image_stretch_and_center": "slides",
    "check_page_number_colors": "slides",
    "check_presenter_console_disable": "slides",
    "check_slide_orientation_Portrait": "slides",
    "check_transition": "slides",
    "compare_pptx_files": "slides",
    "compare_pptx_files_robust": "slides",
    "compare_pptx_files_tolerant": "slides",
    "evaluate_presentation_fill_to_rgb_distance": "slides",
    # metrics/table.py
    "compare_csv": "table",
    "compare_table": "table",
    # metrics/vlc.py
    "compare_images": "vlc",
    # metrics/chrome.py
    "compare_pdfs": "chrome",
}

#: Getter name -> the ``desktop_env.evaluators.getters`` module defining it.
GETTER_MODULES: dict[str, str] = {
    "vm_file": "file",
    "cloud_file": "file",
    "local_file": "file",
    "rule": "misc",
}

INFEASIBLE = "infeasible"


class GraderUnavailable(RuntimeError):
    """The task needs a package that is not installed on this machine."""


class UnsupportedGrader(RuntimeError):
    """The task uses a grader or getter this bridge does not implement."""


def _metric_function(name: str) -> Callable[..., float]:
    if name == INFEASIBLE:
        return _infeasible_metric
    module_name = METRIC_MODULES.get(name)
    if module_name is None:
        raise UnsupportedGrader(f"no metric module mapped for {name!r}")
    module = boot.import_metric(module_name)
    try:
        return getattr(module, name)
    except AttributeError as exc:
        raise UnsupportedGrader(f"{name} missing from metrics/{module_name}.py") from exc


def _getter_function(name: str) -> Callable[..., Any]:
    module_name = GETTER_MODULES.get(name)
    if module_name is None:
        raise UnsupportedGrader(f"no getter module mapped for {name!r}")
    module = boot.import_getter(module_name)
    try:
        return getattr(module, f"get_{name}")
    except AttributeError as exc:
        raise UnsupportedGrader(f"get_{name} missing from getters/{module_name}.py") from exc


def _infeasible_metric(*_args: Any, **_kwargs: Any) -> float:
    """Placeholder: the real decision happens before any metric is called."""

    return 0.0


@dataclass
class LocalController:
    """Minimal stand-in for OSWorld's VM controller.

    Only the file read path is real; everything that needs a live desktop or a
    guest agent raises instead of silently returning a wrong answer.
    """

    workdir: pathlib.Path
    vm_to_local: dict[str, pathlib.Path] = field(default_factory=dict)

    def resolve(self, vm_path: str) -> pathlib.Path:
        key = str(vm_path)
        if key in self.vm_to_local:
            return pathlib.Path(self.vm_to_local[key])
        from .tasks import local_path_for

        return local_path_for(key, self.workdir)

    def get_file(self, path: str, strict: bool = False) -> bytes | None:
        local = self.resolve(path)
        if not local.exists():
            if strict:
                raise FileNotFoundError(f"{path} not found in sandbox ({local})")
            return None
        if local.is_dir():
            raise IsADirectoryError(f"{path} is a directory ({local})")
        return local.read_bytes()

    def execute_python_command(self, command: str) -> str:
        raise UnsupportedGrader("execute_python_command needs a VM agent")

    def get_accessibility_tree(self) -> str:
        raise UnsupportedGrader("get_accessibility_tree needs a live desktop")

    def get_terminal_output(self) -> str:
        raise UnsupportedGrader("get_terminal_output needs a VM agent")

    def get_vm_desktop_path(self) -> str:
        return str(self.workdir)

    def get_vm_directory_tree(self, path: str) -> list[dict[str, Any]]:
        local = self.resolve(path)
        if not local.is_dir():
            return []
        return [
            {"name": child.name, "type": "dir" if child.is_dir() else "file"}
            for child in sorted(local.iterdir())
        ]

    def get_vm_screen_size(self) -> dict[str, int]:
        raise UnsupportedGrader("get_vm_screen_size needs a live desktop")

    def get_vm_wallpaper(self) -> bytes:
        raise UnsupportedGrader("get_vm_wallpaper needs a live desktop")

    def get_vm_window_size(self, app_class_name: str) -> dict[str, int]:
        raise UnsupportedGrader("get_vm_window_size needs a live desktop")


@dataclass
class LocalDesktopEnv:
    """The subset of ``DesktopEnv`` that the vendored getters read."""

    cache_dir: pathlib.Path
    controller: LocalController
    vm_platform: str = "windows"
    vm_ip: str = "127.0.0.1"
    server_port: int = 5000
    chromium_port: int = 9222
    vlc_port: int = 8080
    vm_machine: str = "local"
    current_use_proxy: bool = False
    enable_proxy: bool = False
    is_environment_used: bool = False
    setup_controller: Any = None

    def __post_init__(self) -> None:
        self.cache_dir = pathlib.Path(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def setup_controller_setup(self, *_args: Any, **_kwargs: Any) -> bool:
        return True


@dataclass
class GradeResult:
    """Outcome of one grading call."""

    score: float
    status: str = "graded"
    reason: str = ""
    per_metric: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.score >= 1.0

    @property
    def available(self) -> bool:
        return self.status == "graded"

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "status": self.status,
            "reason": self.reason,
            "per_metric": self.per_metric,
        }


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _last_action_failed(action_history: list[Any]) -> bool:
    if not action_history:
        return False
    last = action_history[-1]
    if isinstance(last, str):
        return last.strip().upper() == "FAIL"
    if isinstance(last, dict):
        return str(last.get("action_type", "")).strip().upper() == "FAIL"
    return False


def _normalize_options(evaluator: dict[str, Any], count: int, is_multi: bool):
    raw = evaluator.get("options")
    if is_multi:
        if isinstance(raw, list):
            padded = list(raw) + [None] * (count - len(raw))
            return [item if item else {} for item in padded[:count]]
        return [raw or {} for _ in range(count)]
    return raw or {}


def build_env(materialized: MaterializedTask, cache_root: pathlib.Path | str) -> LocalDesktopEnv:
    """Create the grading environment for one materialized task."""

    cache = pathlib.Path(cache_root) / materialized.task.id
    controller = LocalController(
        workdir=materialized.workdir,
        vm_to_local=dict(materialized.vm_to_local),
    )
    env = LocalDesktopEnv(cache_dir=cache, controller=controller)
    env.setup_controller = env
    return env


def _digest_matches(path: pathlib.Path, digest: str | None) -> bool:
    if not digest:
        return True
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest().lower() == digest.lower()


def _prestage_references(env: LocalDesktopEnv, expected_cfg: list[Any]) -> None:
    """Fetch every cloud reference into the grader cache before evaluating.

    ``get_cloud_file`` short-circuits when the destination already exists with a
    matching digest, so staging the file here keeps the grader off the network.
    The upstream getter issues ``requests.get`` with no read timeout, and a
    stalled TLS connection would otherwise wedge an entire batch on one task.
    Downloads go through the bounded, retrying cache in ``tasks.fetch`` and are
    copied into the per-task cache directory under the name the getter expects.
    """

    from .tasks import fetch

    reference_cache = REPO_ROOT / ".cache" / "osworld"
    for cfg in expected_cfg:
        if not isinstance(cfg, dict) or cfg.get("type") != "cloud_file":
            continue
        if cfg.get("multi", False):
            pairs = list(zip(cfg["path"], cfg["dest"]))
            digests = cfg.get("sha256") or [None] * len(pairs)
        else:
            pairs = [(cfg["path"], cfg["dest"])]
            digests = [cfg.get("sha256")]
        for (url, dest), digest in zip(pairs, digests):
            target = pathlib.Path(env.cache_dir) / dest
            if target.exists() and _digest_matches(target, digest):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fetch(url, reference_cache), target)
            if not _digest_matches(target, digest):
                raise UnsupportedGrader(f"reference digest mismatch for {url}")


def grade(
    materialized: MaterializedTask,
    action_history: list[Any],
    cache_root: pathlib.Path | str,
) -> GradeResult:
    """Score one run exactly the way ``DesktopEnv.evaluate`` would."""

    task = materialized.task
    evaluator = task.evaluator
    funcs = _as_list(evaluator.get("func"))

    if not funcs:
        return GradeResult(0.0, "grader_unavailable", "task has no evaluator func")

    # postconfig is deliberately not executed: it activates a window and sends
    # Ctrl+S, neither of which exists off-VM.  The runner saves files instead.

    if funcs == [INFEASIBLE]:
        score = 1.0 if _last_action_failed(action_history) else 0.0
        return GradeResult(
            score,
            "graded",
            "" if score else "agent did not declare the task infeasible",
            [{"func": INFEASIBLE, "score": score}],
        )

    if _last_action_failed(action_history):
        return GradeResult(0.0, "graded", "agent declared failure", [])

    is_multi = isinstance(evaluator.get("func"), list)
    results_cfg = _as_list(evaluator.get("result"))
    expected_cfg = _as_list(evaluator.get("expected"))
    conj = task.conj
    options = _normalize_options(evaluator, len(funcs), is_multi)

    env = build_env(materialized, cache_root)

    try:
        _prestage_references(env, expected_cfg)
    except (OSError, UnsupportedGrader) as exc:
        return GradeResult(0.0, "grader_unavailable", f"reference unavailable: {exc}")

    try:
        if is_multi:
            return _grade_multi(funcs, results_cfg, expected_cfg, conj, options, env, task)
        return _grade_single(funcs[0], results_cfg, expected_cfg, options, env, task)
    except MissingDependency as exc:
        return GradeResult(0.0, "grader_unavailable", str(exc))
    except UnsupportedGrader as exc:
        return GradeResult(0.0, "grader_unavailable", str(exc))


def _grade_single(
    func: str,
    results_cfg: list[Any],
    expected_cfg: list[Any],
    options: Any,
    env: LocalDesktopEnv,
    task: Task,
) -> GradeResult:
    metric = _metric_function(func)
    result_getter = _getter_function(str(results_cfg[0]["type"])) if results_cfg else None
    expected_getter = (
        _getter_function(str(expected_cfg[0]["type"])) if expected_cfg else None
    )

    try:
        result_state = result_getter(env, results_cfg[0]) if result_getter else None
    except FileNotFoundError:
        return GradeResult(0.0, "graded", "result file missing", [{"func": func, "score": 0.0}])

    expected_state = expected_getter(env, expected_cfg[0]) if expected_getter else None

    try:
        score = float(
            metric(result_state, expected_state, **options)
            if expected_getter
            else metric(result_state, **options)
        )
    except MissingDependency as exc:
        return GradeResult(0.0, "grader_unavailable", str(exc))
    except Exception as exc:  # the grader itself blew up on real output
        return GradeResult(
            0.0,
            "graded",
            f"grader raised {type(exc).__name__}: {exc}",
            [{"func": func, "score": 0.0}],
        )

    return GradeResult(
        score,
        "graded",
        "" if score else "grader returned 0",
        [{"func": func, "score": score}],
    )


def _grade_multi(
    funcs: list[Any],
    results_cfg: list[Any],
    expected_cfg: list[Any],
    conj: str,
    options: list[Any],
    env: LocalDesktopEnv,
    task: Task,
) -> GradeResult:
    count = len(funcs)
    metrics = [_metric_function(str(name)) for name in funcs]
    result_getters = [
        _getter_function(str(results_cfg[i]["type"])) if i < len(results_cfg) else None
        for i in range(count)
    ]
    expected_getters = [
        _getter_function(str(expected_cfg[i]["type"])) if i < len(expected_cfg) else None
        for i in range(count)
    ]

    scores: list[float] = []
    per_metric: list[dict[str, Any]] = []

    for idx, metric in enumerate(metrics):
        cfg = results_cfg[idx] if idx < len(results_cfg) else None
        getter = result_getters[idx]
        try:
            result_state = getter(env, cfg) if getter and cfg else None
        except FileNotFoundError:
            if conj == "and":
                return GradeResult(
                    0.0,
                    "graded",
                    f"result file missing for {funcs[idx]}",
                    per_metric + [{"func": str(funcs[idx]), "score": 0.0}],
                )
            scores.append(0.0)
            per_metric.append({"func": str(funcs[idx]), "score": 0.0, "reason": "missing"})
            continue

        expected_state = None
        exp_getter = expected_getters[idx]
        if exp_getter is not None and idx < len(expected_cfg):
            expected_state = exp_getter(env, expected_cfg[idx])

        try:
            score = float(
                metric(result_state, expected_state, **options[idx])
                if exp_getter is not None
                else metric(result_state, **options[idx])
            )
        except MissingDependency:
            raise
        except Exception as exc:
            score = 0.0
            per_metric.append(
                {"func": str(funcs[idx]), "score": 0.0, "error": f"{type(exc).__name__}: {exc}"}
            )
            if conj == "and":
                return GradeResult(
                    0.0,
                    "graded",
                    f"grader raised for {funcs[idx]}",
                    per_metric,
                )
            scores.append(0.0)
            continue

        per_metric.append({"func": str(funcs[idx]), "score": score})
        if conj == "and" and score == 0.0:
            return GradeResult(0.0, "graded", f"{funcs[idx]} scored 0", per_metric)
        if conj == "or" and score == 1.0:
            return GradeResult(1.0, "graded", "", per_metric)
        scores.append(score)

    if not scores:
        return GradeResult(0.0, "graded", "no metric produced a score", per_metric)

    total = sum(scores) / len(scores) if conj == "and" else max(scores)
    return GradeResult(total, "graded", "" if total else "no metric passed", per_metric)


def reset_cache(cache_root: pathlib.Path | str, task_id: str) -> None:
    """Drop the grader cache for one task before a fresh run."""

    target = pathlib.Path(cache_root) / task_id
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def new_cache_name(prefix: str, suffix: str = "") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}{suffix}"
