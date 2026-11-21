"""Resumable, serial LibreOffice harness/GUI pairs on the adopted task set.

One repeat per arm is an exploratory coverage comparison, separate from the
proposal's three-repeat harness experiment. Each worker owns its run directory.
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import importlib.metadata
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

import yaml

from .config import BenchConfig, DEFAULT_CONFIG_PATH
from .normalize import find_soffice
from .profile import profile_identity
from .runner import run_once
from .tasks import load_tasks


def valid_record(record):
    """A graded agent outcome, excluding setup, model and saving failures."""
    if not record or not record.get("finished_at") or record.get("grader_status") != "graded":
        return False
    if record.get("status") not in {"done", "incomplete", "infeasible"}:
        return False
    if record.get("stopped_reason") not in {
        "finished", "tool_call_budget", "wall_clock_budget", "no_tool_call"
    }:
        return False
    extra = record.get("extra", {})
    if any(extra.get(key) for key in (
        "desktop_close_error", "interface_close_error", "interface_finalize_error"
    )):
        return False
    desktop = extra.get("desktop", {})
    save = desktop.get("save", desktop)
    if save.get("ok") is False or desktop.get("unsupported_setup"):
        return False
    if any(desktop.get(key) for key in ("cleanup_error", "profile_cleanup_error")):
        return False
    return True


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_version(path):
    """Read Windows PE version resources without executing the binary."""
    if os.name != "nt":
        return "unknown"
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    version.VerQueryValueW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR,
                                     ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.UINT)]
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return "unknown"
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, data):
        return "unknown"
    pointer, length = wintypes.LPVOID(), wintypes.UINT()
    if not version.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return "unknown"
    words = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    if length.value < 16 or words[0] != 0xFEEF04BD:
        return "unknown"
    ms, ls = words[2], words[3]
    return f"{ms >> 16}.{ms & 65535}.{ls >> 16}.{ls & 65535}"


def dependency_identity(config):
    """Lock installed files, including editable CLI sources, without running apps."""
    command = config.harness.libreoffice
    executable = shutil.which(command)
    cli = {"command": command, "executable": executable, "version": "unknown",
           "package_root": "unknown", "source_sha256": {}}
    if executable:
        executable = pathlib.Path(executable).resolve()
        cli["executable_sha256"] = _sha256(executable)
        sites = [executable.parent.parent / "site-packages",
                 executable.parent.parent / "Lib" / "site-packages"]
        distributions = list(importlib.metadata.distributions(path=[str(p) for p in sites if p.is_dir()]))
        distributions.extend(importlib.metadata.distributions())
        for distribution in distributions:
            entry = next((e for e in distribution.entry_points
                          if e.group == "console_scripts" and e.name.lower() == executable.stem.lower()), None)
            if entry is None:
                continue
            cli.update(distribution=distribution.metadata["Name"], version=distribution.version,
                       entrypoint=entry.value)
            module = entry.value.split(":", 1)[0]
            package = pathlib.Path(*module.split(".")[:-1])
            source = pathlib.Path(distribution.locate_file(package))
            direct = distribution.read_text("direct_url.json")
            if direct:
                url = urllib.parse.urlparse(json.loads(direct).get("url", ""))
                if url.scheme == "file":
                    editable = pathlib.Path(urllib.request.url2pathname(url.path))
                    candidate = editable / package
                    if candidate.is_dir():
                        source = candidate
                        cli["project_files_sha256"] = {
                            name: _sha256(editable / name) for name in ("pyproject.toml", "setup.py", "setup.cfg")
                            if (editable / name).is_file()
                        }
            if source.is_dir():
                cli["package_root"] = str(source.resolve())
                cli["source_sha256"] = {p.relative_to(source).as_posix(): _sha256(p)
                                        for p in sorted(source.rglob("*.py")) if p.is_file()}
            break
    soffice = find_soffice(config.apps.soffice)
    office = {"executable": soffice, "files": {}}
    if soffice:
        directory = pathlib.Path(soffice).parent
        office["files"] = {name: {"sha256": _sha256(directory / name),
                                  "version": _file_version(directory / name)}
                           for name in ("soffice.com", "soffice.exe", "soffice.bin")
                           if (directory / name).is_file()}
    model = dataclasses.asdict(config.model)
    model.pop("api_key", None)
    model["resolved_revision"] = "unknown"
    model["revision_evidence"] = "not queried; endpoint model aliases may resolve differently over time"
    desktop_profile = {"initialized": False, "reason": "LibreOffice executable unavailable"}
    if soffice:
        try:
            desktop_profile = profile_identity(pathlib.Path(soffice))
        except (OSError, ValueError, KeyError) as exc:
            desktop_profile = {"initialized": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"cli": cli, "libreoffice": office, "desktop_profile": desktop_profile, "model": model}


def selected_tasks(ids=()):
    tasks = [t for t in load_tasks() if t.adopted and t.family == "libreoffice"]
    if ids:
        requested = set(ids)
        known = {t.id for t in tasks}
        if requested - known:
            raise ValueError(f"unknown adopted LibreOffice tasks: {requested - known}")
        tasks = [t for t in tasks if t.id in requested]
    return tasks


def freeze(root, config_path, tasks, require_profile=False):
    root = pathlib.Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = BenchConfig.load(config_path)
    safe_config = dataclasses.asdict(config)
    safe_config["model"]["api_key"] = ""  # secrets stay in the environment
    source_root = pathlib.Path(__file__).parent
    source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(source_root.glob("*.py"))}
    payload = {
        "kind": "exploratory-one-repeat-paired-coverage",
        "source_sha256": source_hashes,
        "dependencies": dependency_identity(config),
        "config": safe_config,
        "tasks": [{"id": t.id, "app": t.app, "sha256": hashlib.sha256(
            pathlib.Path(t.raw_path).read_bytes()).hexdigest()} for t in tasks],
        "interfaces": ["harness", "gui"], "repeats": 1,
    }
    if require_profile and not payload["dependencies"]["desktop_profile"].get("initialized"):
        raise ValueError("desktop profile baseline is not initialized or valid; validate bootstrap before --execute")
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    path = root / "manifest.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing["fingerprint"] != fingerprint:
            raise ValueError("batch configuration or task inputs changed; use a new output directory")
    else:
        payload["fingerprint"] = fingerprint
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        (root / "config.yaml").write_text(yaml.safe_dump(safe_config, allow_unicode=True), encoding="utf-8")
    return root


def latest_record(root, task_id, interface):
    paths = sorted((root / interface / task_id).glob("*/record.json"), key=lambda p: p.stat().st_mtime_ns)
    if not paths:
        return None
    return json.loads(paths[-1].read_text(encoding="utf-8"))


def summarize(root, tasks):
    rows = [latest_record(root, t.id, arm) for t in tasks for arm in ("harness", "gui")]
    rows = [r for r in rows if r is not None and r.get("finished_at")]
    result = {"planned_tasks": len(tasks), "planned_runs": 2 * len(tasks),
              "finished_runs": len(rows), "arms": {}, "valid_pairs": 0}
    for arm in ("harness", "gui"):
        arm_rows = [r for r in rows if r["interface"] == arm]
        graded = [r for r in arm_rows if r["grader_status"] == "graded"]
        valid = [r for r in arm_rows if valid_record(r)]
        result["arms"][arm] = {
            "finished": len(arm_rows), "graded": len(graded), "valid": len(valid),
            "invalid": len(arm_rows) - len(valid),
            "success": sum(r["score"] >= 1 for r in valid),
            "unavailable": dict(collections.Counter(r["grader_status"] for r in arm_rows
                                                     if r["grader_status"] != "graded")),
            "tool_calls": sum(r["tool_calls"] for r in arm_rows),
            "tokens": sum(r["prompt_tokens"] + r["completion_tokens"] for r in arm_rows),
            "agent_seconds": round(sum(r["elapsed_s"] for r in arm_rows), 2),
        }
    for task in tasks:
        pair = [r for r in rows if r["task_id"] == task.id and valid_record(r)]
        if len(pair) == 2:
            inputs = [r.get("extra", {}).get("inputs") for r in pair]
            if (all(isinstance(value, dict) for value in inputs) and inputs[0] == inputs[1]
                    and (inputs[0] or not task.inputs)):
                result["valid_pairs"] += 1
    temp = root / "summary.tmp"
    temp.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(root / "summary.json")
    return result


def execute(root, tasks, config, worker_timeout=1200, retry_errors=False, arms=("harness", "gui")):
    # Arms outer, not tasks outer: the harness arm never touches the desktop,
    # so every CLI cell finishes before the first GUI cell takes the screen.
    for arm in arms:
        for task in tasks:
            existing = latest_record(root, task.id, arm)
            if existing and existing.get("finished_at"):
                if not retry_errors or valid_record(existing):
                    continue
            previous_run = existing.get("run_dir") if existing else None
            print(f"START {task.app} {task.id} {arm}", flush=True)
            log = root / f"{task.id}-{arm}.log"
            with log.open("a", encoding="utf-8") as stream:
                child = subprocess.Popen([
                    sys.executable, "-m", "harness_bench.paired", "--worker", "--root", str(root),
                    "--config", str(config), "--task", task.id, "--interface", arm,
                ], stdout=stream, stderr=subprocess.STDOUT)
                try:
                    code = child.wait(timeout=worker_timeout)
                except subprocess.TimeoutExpired:
                    # The worker's desktop job object kills only its own LO tree.
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                   stdout=stream, stderr=subprocess.STDOUT, check=False)
                    code = child.wait(timeout=30)
            record = latest_record(root, task.id, arm)
            if not record or not record.get("finished_at") or record.get("run_dir") == previous_run:
                from .runner import RunRecord
                import datetime as dt
                run_dir = root / arm / task.id / f"{int(time.time())}-{uuid.uuid4().hex[:8]}-worker-error"
                run_dir.mkdir(parents=True, exist_ok=True)
                failed = RunRecord(task.id, task.app, arm, 0, str(run_dir))
                failed.grader_status = "worker_error"
                failed.grader_reason = f"worker exit {code}; see {log.name}"
                failed.finished_at = dt.datetime.now().isoformat(timespec="seconds")
                (run_dir / "record.json").write_text(json.dumps(failed.as_dict()), encoding="utf-8")
            summary = summarize(root, tasks)
            print(f"PROGRESS {summary['finished_runs']}/{summary['planned_runs']} "
                  f"valid_pairs={summary['valid_pairs']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--config", type=pathlib.Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--retry-errors", action="store_true",
                        help="rerun only invalid completed arms; retain every earlier run")
    parser.add_argument("--interface", choices=["harness", "gui"],
                        help="run only this arm; the batch stays resumable")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    tasks = selected_tasks(args.task)
    if args.worker:
        if len(tasks) != 1 or not args.interface:
            parser.error("worker requires exactly one task and an interface")
        run_once(BenchConfig.load(args.config), tasks[0], args.interface,
                 runs_root=args.root, keep_workdir=True)
        return
    root = freeze(args.root, args.config, tasks, require_profile=args.execute)
    print(json.dumps(summarize(root, tasks), ensure_ascii=False), flush=True)
    if args.execute:
        arms = (args.interface,) if args.interface else ("harness", "gui")
        execute(root, tasks, root / "config.yaml", retry_errors=args.retry_errors, arms=arms)


if __name__ == "__main__":
    main()
