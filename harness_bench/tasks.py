"""Load OSWorld tasks, decide which ones this benchmark can grade, split them.

The vendored task files are the untouched OSWorld JSON documents.  Nothing here
hard-codes the counts printed in the proposal: adoption and exclusion are
recomputed from each evaluator, so the numbers in a report always come from the
same rule that produced the task list.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import random
import re
import shutil
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

from .config import TASKS_DIR, VM_HOME

APP_DIRS = ("gimp", "libreoffice_calc", "libreoffice_writer", "libreoffice_impress")

#: ``gimp`` tasks drive GIMP, everything else drives LibreOffice.
APP_FAMILY = {
    "gimp": "gimp",
    "libreoffice_calc": "libreoffice",
    "libreoffice_writer": "libreoffice",
    "libreoffice_impress": "libreoffice",
}

#: Getter types whose values we can reproduce on a local Windows machine.
SUPPORTED_RESULT_GETTERS = frozenset({"vm_file"})
SUPPORTED_EXPECTED_GETTERS = frozenset({"cloud_file", "rule"})

#: Config actions that only make sense with a live desktop session.
GUI_ONLY_ACTIONS = frozenset({"launch", "activate_window"})


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


@dataclass(frozen=True)
class InputFile:
    """One file the task configuration downloads into the sandbox."""

    url: str
    vm_path: str

    @property
    def name(self) -> str:
        return pathlib.PurePosixPath(self.vm_path).name


@dataclass(frozen=True)
class Task:
    """A single OSWorld task, plus everything derived from its evaluator."""

    id: str
    app: str
    instruction: str
    config: list[dict[str, Any]]
    evaluator: dict[str, Any]
    source: str = ""
    related_apps: tuple[str, ...] = ()
    raw_path: str = ""

    # -- derived ---------------------------------------------------------
    @property
    def family(self) -> str:
        return APP_FAMILY[self.app]

    @property
    def funcs(self) -> list[str]:
        return [str(f) for f in _as_list(self.evaluator.get("func"))]

    @property
    def is_infeasible(self) -> bool:
        return self.funcs == ["infeasible"]

    @property
    def result_types(self) -> list[str]:
        return [str(r.get("type")) for r in _as_list(self.evaluator.get("result"))]

    @property
    def expected_types(self) -> list[str]:
        return [str(e.get("type")) for e in _as_list(self.evaluator.get("expected"))]

    @property
    def conj(self) -> str:
        return str(self.evaluator.get("conj", "and"))

    @property
    def inputs(self) -> list[InputFile]:
        files: list[InputFile] = []
        for action in self.config:
            if action.get("type") != "download":
                continue
            for entry in _as_list(action.get("parameters", {}).get("files")):
                url = entry.get("url")
                path = entry.get("path")
                if url and path:
                    files.append(InputFile(url=str(url), vm_path=str(path)))
        return files

    @property
    def needs_desktop(self) -> bool:
        """True when the task configuration cannot be honoured headlessly."""

        for action in self.config:
            kind = action.get("type")
            if kind in GUI_ONLY_ACTIONS:
                return True
            if kind == "execute":
                command = json.dumps(action.get("parameters", {}))
                if "pyautogui" in command or "SCREEN_" in command:
                    return True
        return False

    # -- adoption --------------------------------------------------------
    @property
    def exclusion_reason(self) -> str | None:
        """Why this task cannot be graded here, or ``None`` when it can."""

        if self.is_infeasible:
            return None
        bad_result = [t for t in self.result_types if t not in SUPPORTED_RESULT_GETTERS]
        if bad_result:
            return f"result getter not reproducible off-VM: {sorted(set(bad_result))}"
        bad_expected = [t for t in self.expected_types if t not in SUPPORTED_EXPECTED_GETTERS]
        if bad_expected:
            return f"expected getter not reproducible off-VM: {sorted(set(bad_expected))}"
        return None

    @property
    def adopted(self) -> bool:
        return self.exclusion_reason is None

    @classmethod
    def from_file(cls, path: pathlib.Path) -> "Task":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            id=str(raw["id"]),
            app=path.parent.name,
            instruction=str(raw.get("instruction", "")),
            config=list(raw.get("config", []) or []),
            evaluator=dict(raw.get("evaluator", {}) or {}),
            source=str(raw.get("source", "")),
            related_apps=tuple(raw.get("related_apps", []) or ()),
            raw_path=str(path),
        )


@dataclass
class Split:
    """Development and test task ids, frozen before any run."""

    dev: list[str] = field(default_factory=list)
    test: list[str] = field(default_factory=list)
    seed: int = 0
    dev_fraction: float = 0.4

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "dev_fraction": self.dev_fraction,
            "dev": list(self.dev),
            "test": list(self.test),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Split":
        return cls(
            dev=list(raw.get("dev", [])),
            test=list(raw.get("test", [])),
            seed=int(raw.get("seed", 0)),
            dev_fraction=float(raw.get("dev_fraction", 0.4)),
        )

    def side_of(self, task_id: str) -> str:
        return "dev" if task_id in self.dev else "test"


def load_tasks(tasks_dir: pathlib.Path | str | None = None) -> list[Task]:
    """Load every vendored task file, ordered by application then id."""

    root = pathlib.Path(tasks_dir) if tasks_dir else TASKS_DIR
    tasks: list[Task] = []
    for app in APP_DIRS:
        app_dir = root / app
        if not app_dir.is_dir():
            continue
        for path in sorted(app_dir.glob("*.json")):
            tasks.append(Task.from_file(path))
    return tasks


def adopted_tasks(tasks: Iterable[Task]) -> list[Task]:
    return [task for task in tasks if task.adopted]


def exclusion_table(tasks: Iterable[Task]) -> list[dict[str, str]]:
    return [
        {"id": task.id, "app": task.app, "reason": task.exclusion_reason}
        for task in tasks
        if not task.adopted
    ]


def make_split(
    tasks: Iterable[Task],
    dev_fraction: float = 0.4,
    seed: int = 20260601,
) -> Split:
    """Stratified split: the same fraction of each application goes to dev.

    Quotas are allocated with the largest-remainder method so the total lands
    on ``round(n * dev_fraction)`` while every application keeps its share.
    """

    adopted = sorted(adopted_tasks(tasks), key=lambda t: (t.app, t.id))
    total = len(adopted)
    if total == 0:
        return Split(seed=seed, dev_fraction=dev_fraction)

    by_app: dict[str, list[str]] = {}
    for task in adopted:
        by_app.setdefault(task.app, []).append(task.id)

    target = round(total * dev_fraction)
    quotas: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    for app, ids in by_app.items():
        exact = len(ids) * dev_fraction
        base = int(exact)
        quotas[app] = base
        remainders.append((exact - base, app))

    remaining = target - sum(quotas.values())
    for _, app in sorted(remainders, key=lambda pair: (-pair[0], pair[1])):
        if remaining <= 0:
            break
        quotas[app] += 1
        remaining -= 1

    rng = random.Random(seed)
    dev: list[str] = []
    test: list[str] = []
    for app in sorted(by_app):
        ids = list(by_app[app])
        rng.shuffle(ids)
        take = min(quotas[app], len(ids))
        dev.extend(ids[:take])
        test.extend(ids[take:])

    return Split(
        dev=sorted(dev),
        test=sorted(test),
        seed=seed,
        dev_fraction=dev_fraction,
    )


_VM_PATH = re.compile(r"/home/user(?:/[^\s\"'`,;)]*)?")


def rewrite_vm_path(text: str, workdir: str | pathlib.Path) -> str:
    """Map OSWorld's ``/home/user/...`` paths onto a local working directory.

    Every ``/home/user`` token becomes ``<workdir>/<rest>``, with the separator
    normalised so the result is a valid path on this machine.  Text that merely
    mentions the home directory without a trailing path is replaced too.
    """

    local = pathlib.Path(workdir)

    def replace(match: re.Match[str]) -> str:
        rest = match.group(0)[len(VM_HOME):].lstrip("/")
        if not rest:
            return str(local)
        return str(local / pathlib.PurePosixPath(rest))

    return _VM_PATH.sub(replace, text)


def local_path_for(vm_path: str, workdir: str | pathlib.Path) -> pathlib.Path:
    """Translate one evaluator/config VM path into its sandbox location.

    OSWorld configs are written from the VM user's point of view, so a bare
    relative path such as ``Downloads/slides.pptx`` means
    ``/home/user/Downloads/slides.pptx``.  Resolving it against the process
    working directory instead would drop files outside the sandbox, which one
    task (``a097acff``) actually does.
    """

    text = str(vm_path)
    if not text.startswith("/"):
        text = f"{VM_HOME}/{text}"
    return pathlib.Path(rewrite_vm_path(text, workdir))


def _cache_name(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    suffix = pathlib.PurePosixPath(urllib.parse.urlparse(url).path).name
    return f"{digest}-{suffix}"


CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 30


def fetch(
    url: str,
    cache_dir: pathlib.Path | str,
    timeout: tuple[float, float] | float | None = None,
) -> pathlib.Path:
    """Download ``url`` into the cache once and return the cached path.

    The timeout is deliberately short so an unreachable host fails a task
    quickly instead of stalling a whole batch behind a dead connection.
    """

    cache = pathlib.Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / _cache_name(url)
    if target.exists() and target.stat().st_size > 0:
        return target

    if timeout is None:
        timeout = (CONNECT_TIMEOUT_S, READ_TIMEOUT_S)
    partial = target.with_suffix(target.suffix + ".part")
    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if chunk:
                    handle.write(chunk)
    partial.replace(target)
    return target


@dataclass
class MaterializedTask:
    """A task staged into a throw-away working directory."""

    task: Task
    workdir: pathlib.Path
    instruction: str
    vm_to_local: dict[str, pathlib.Path] = field(default_factory=dict)

    def resolve(self, vm_path: str) -> pathlib.Path:
        if vm_path in self.vm_to_local:
            return self.vm_to_local[vm_path]
        return local_path_for(vm_path, self.workdir)


def materialize(
    task: Task,
    workdir: pathlib.Path | str,
    cache_dir: pathlib.Path | str,
) -> MaterializedTask:
    """Create the working directory and stage every declared input file."""

    work = pathlib.Path(workdir)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    mapping: dict[str, pathlib.Path] = {}
    for entry in task.inputs:
        cached = fetch(entry.url, cache_dir)
        destination = local_path_for(entry.vm_path, work)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cached, destination)
        mapping[entry.vm_path] = destination

    return MaterializedTask(
        task=task,
        workdir=work,
        instruction=rewrite_vm_path(task.instruction, work),
        vm_to_local=mapping,
    )
