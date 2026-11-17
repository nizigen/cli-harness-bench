"""Normalise agent output before grading.

Two things have to happen before OSWorld's graders can be trusted on output
produced by an agent:

* Spreadsheets written by ``openpyxl`` carry formulas without cached results,
  and ``compare_table`` reads values through ``data_only=True``.  The file has
  to be reopened and recalculated by LibreOffice, exactly as the proposal
  describes.
* The original harness saves through a GUI keystroke.  Off-VM the runner owns
  saving, so every declared output file must be flushed to disk first.

``soffice`` is optional: when it is missing, normalisation reports itself as
unavailable and grading proceeds on the file as written, with the limitation
recorded in the run record.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

from .config import REPO_ROOT

#: Extensions that need a LibreOffice recalculation pass.
SPREADSHEET_SUFFIXES = frozenset({".xlsx", ".xlsm", ".ods", ".xls"})

#: Filter names LibreOffice expects for the conversions we perform.
CONVERT_FILTERS = {
    ".xlsx": "Calc MS Excel 2007 XML",
    ".ods": "calc8",
    ".xls": "MS Excel 97",
}

#: Registry settings that force a full recalculation on load.
RECALC_PROFILE = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"
           xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <item oor:path="/org.openoffice.Office.Calc/Formula/Load">
  <prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop>
 </item>
 <item oor:path="/org.openoffice.Office.Calc/Formula/Load">
  <prop oor:name="ODFRecalcMode" oor:op="fuse"><value>0</value></prop>
 </item>
</oor:items>
"""


@dataclass
class NormalizeResult:
    """What normalisation did to one file."""

    path: str
    action: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "action": self.action, "ok": self.ok, "detail": self.detail}


@dataclass
class NormalizeReport:
    entries: list[NormalizeResult] = field(default_factory=list)
    soffice: str | None = None

    @property
    def available(self) -> bool:
        return self.soffice is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "soffice": self.soffice,
            "entries": [entry.as_dict() for entry in self.entries],
        }


def _console_variant(path: pathlib.Path) -> pathlib.Path:
    """Prefer the console wrapper beside a GUI-subsystem LibreOffice binary.

    ``soffice.exe`` is linked as a GUI subsystem program: when its output is
    redirected it writes nothing and the caller cannot observe the exit code.
    ``soffice.com`` sits next to it and behaves like a normal console program,
    so every invocation that captures output has to go through it.
    """

    if path.suffix.lower() == ".exe":
        console = path.with_suffix(".com")
        if console.is_file():
            return console
    return path


def find_soffice(configured: str = "") -> str | None:
    """Locate ``soffice``: explicit setting, then PATH, then standard installs."""

    if configured:
        candidate = pathlib.Path(configured)
        if not candidate.is_absolute():
            candidate = REPO_ROOT / candidate
        if candidate.is_file():
            return str(_console_variant(candidate))

    for name in ("soffice.com", "soffice", "soffice.exe", "libreoffice"):
        found = shutil.which(name)
        if found:
            return str(_console_variant(pathlib.Path(found)))

    for root in (
        pathlib.Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LibreOffice" / "program",
        pathlib.Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "LibreOffice" / "program",
    ):
        for name in ("soffice.com", "soffice.exe", "soffice"):
            candidate = root / name
            if candidate.is_file():
                return str(_console_variant(candidate))
    return None


def soffice_env(soffice: str | None) -> dict[str, str]:
    """Environment that lets a child process find the same LibreOffice.

    The CLI-Anything harnesses locate ``soffice`` themselves through ``which``
    and a short list of standard install directories, so a project-local tree
    is invisible to them unless its directory is prepended to ``PATH``.  The
    harness would otherwise report "LibreOffice is not installed" while the
    runner can see it perfectly well.
    """

    env = dict(os.environ)
    if soffice:
        directory = str(pathlib.Path(soffice).parent)
        env["PATH"] = directory + os.pathsep + env.get("PATH", "")
    return env


def shared_profile_root() -> pathlib.Path:
    """Profile directory reused by every conversion this process starts.

    LibreOffice builds a fresh profile from scratch when it cannot find one,
    which costs about 19 s per launch on Windows versus about 7 s when the
    profile already exists.  Conversions here run one at a time, so a single
    shared profile is safe and roughly triples the throughput of a full run.
    """

    return REPO_ROOT / ".cache" / "soffice-profile"


def _profile_dir(root: pathlib.Path) -> pathlib.Path:

    profile = root / "lo-profile"
    user = profile / "user"
    user.mkdir(parents=True, exist_ok=True)
    xcu = user / "registrymodifications.xcu"
    if not xcu.exists():
        xcu.write_text(RECALC_PROFILE, encoding="utf-8")
    return profile


def _kill_tree(process: subprocess.Popen) -> None:
    """Stop a LibreOffice console stub and every process it started."""

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - last resort
        process.kill()


def _run_soffice(
    command: list[str], scratch: pathlib.Path, timeout: float = 180.0
) -> tuple[int, str]:
    """Run a headless LibreOffice command without inheriting our stdio pipes.

    On Windows LibreOffice starts a detached ``soffice.bin`` that inherits any
    pipe handed to the console stub.  ``subprocess.run(capture_output=True)``
    then blocks until that pipe reaches EOF, which never happens once the stub
    exits and the worker keeps the write end open, so the plain capture
    deadlocks.  Sending the child's output to a file and putting it in its own
    process group makes the wait bounded and lets a wedged conversion be killed.
    """

    scratch.mkdir(parents=True, exist_ok=True)
    log = scratch / "soffice.log"
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    with open(log, "wb") as handle:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            returncode = -1
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    return returncode, text


def recalculate(path: pathlib.Path, soffice: str, scratch: pathlib.Path) -> NormalizeResult:
    """Reopen a spreadsheet in LibreOffice so formula results are stored."""

    suffix = path.suffix.lower()
    target_filter = CONVERT_FILTERS.get(suffix)
    if target_filter is None:
        return NormalizeResult(str(path), "skip", True, f"no filter for {suffix}")

    scratch.mkdir(parents=True, exist_ok=True)
    profile = _profile_dir(shared_profile_root())
    outdir = scratch / "out"
    outdir.mkdir(parents=True, exist_ok=True)

    command = [
        soffice,
        f"-env:UserInstallation=file:///{profile.as_posix().lstrip('/')}",
        "--headless",
        "--norestore",
        "--convert-to",
        f"{suffix.lstrip('.')}:{target_filter}",
        "--outdir",
        str(outdir),
        str(path),
    ]
    try:
        returncode, output = _run_soffice(command, scratch)
    except OSError as exc:
        return NormalizeResult(str(path), "recalculate", False, f"{type(exc).__name__}: {exc}")

    produced = outdir / path.name
    if returncode != 0 or not produced.exists():
        detail = output.strip()[:400]
        return NormalizeResult(
            str(path), "recalculate", False, f"exit {returncode}: {detail}"
        )

    shutil.copyfile(produced, path)
    return NormalizeResult(str(path), "recalculate", True, "formula cache refreshed")


def save_outputs(
    paths: list[pathlib.Path],
    soffice: str | None,
    scratch: pathlib.Path | str,
) -> NormalizeReport:
    """Flush and normalise every declared output file that exists."""

    work = pathlib.Path(scratch)
    report = NormalizeReport(soffice=soffice)
    for path in paths:
        if not path.exists():
            report.entries.append(
                NormalizeResult(str(path), "missing", False, "agent produced no such file")
            )
            continue
        if path.suffix.lower() not in SPREADSHEET_SUFFIXES:
            report.entries.append(NormalizeResult(str(path), "none", True, ""))
            continue
        if soffice is None:
            report.entries.append(
                NormalizeResult(
                    str(path),
                    "unavailable",
                    False,
                    "soffice not found; formula results stay as written",
                )
            )
            continue
        report.entries.append(recalculate(path, soffice, work / path.stem))
    return report


def collect_result_paths(materialized) -> list[pathlib.Path]:
    """Every file the task's evaluator will read, resolved into the sandbox."""

    from .tasks import _as_list

    paths: list[pathlib.Path] = []
    for cfg in _as_list(materialized.task.evaluator.get("result")):
        if not isinstance(cfg, dict):
            continue
        declared = cfg.get("path")
        if isinstance(declared, list):
            paths.extend(materialized.resolve(str(item)) for item in declared)
        elif declared:
            paths.append(materialized.resolve(str(declared)))
    return paths


def run_convert_to_csv(
    command: list[str],
    workdir: pathlib.Path,
    soffice: str | None,
    scratch: pathlib.Path,
) -> NormalizeResult:
    """Replay a ``libreoffice --convert-to`` postconfig step locally.

    Five calc tasks declare a CSV export in their postconfig and grade the CSV
    as a second result file, so the step is part of grading rather than of the
    agent's work.
    """

    from .tasks import rewrite_vm_path

    if soffice is None:
        return NormalizeResult(
            "postconfig:convert-to", "unavailable", False, "soffice not found"
        )

    rewritten: list[str] = []
    skip_next = False
    outdir: str | None = None
    filter_spec: str | None = None
    source: str | None = None

    index = 0
    while index < len(command):
        token = command[index]
        if skip_next:
            skip_next = False
            index += 1
            continue
        if token == "--convert-to":
            filter_spec = command[index + 1]
            skip_next = True
        elif token == "--outdir":
            outdir = command[index + 1]
            skip_next = True
        elif token.startswith("-"):
            pass
        else:
            source = token
        index += 1

    if not source or not filter_spec:
        return NormalizeResult(
            "postconfig:convert-to", "skip", True, "not a convert-to invocation"
        )

    local_source = pathlib.Path(rewrite_vm_path(source, workdir))
    local_outdir = pathlib.Path(rewrite_vm_path(outdir, workdir)) if outdir else workdir
    local_outdir.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    profile = _profile_dir(shared_profile_root())

    rewritten = [
        soffice,
        f"-env:UserInstallation=file:///{profile.as_posix().lstrip('/')}",
        "--headless",
        "--norestore",
        "--convert-to",
        filter_spec,
        "--outdir",
        str(local_outdir),
        str(local_source),
    ]
    try:
        returncode, output = _run_soffice(rewritten, scratch)
    except OSError as exc:
        return NormalizeResult(
            "postconfig:convert-to", "convert", False, f"{type(exc).__name__}: {exc}"
        )
    ok = returncode == 0
    detail = "" if ok else output.strip()[:400]
    return NormalizeResult("postconfig:convert-to", "convert", ok, detail)
