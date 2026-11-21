"""Command line entry point: ``python -m harness_bench <command>``.

The commands follow the order of the plan:

``doctor``     what is missing before a run can start
``tasks``      the adopted task list and why the others were excluded
``split``      freeze the development/test split to a file
``roundtrip``  the harness ceiling check on the standard answers
``run``        drive the agent and grade it
``report``     aggregate ``runs/`` into the tables the report needs

Nothing here decides an experiment on its own; every command reads its
parameters from the YAML configuration so a run is reproducible from one file.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
from typing import Any, Sequence

from . import report as report_mod
from . import roundtrip as roundtrip_mod
from .config import DEFAULT_CONFIG_PATH, REPO_ROOT, BenchConfig
from .grading import grade
from .normalize import find_soffice
from .runner import RUNS_ROOT, load_records, run_batch
from .tasks import (
    Split,
    adopted_tasks,
    exclusion_table,
    load_tasks,
    make_split,
    materialize,
    rewrite_vm_path,
)

SPLIT_PATH = REPO_ROOT / "configs" / "split.json"


def _config(args: argparse.Namespace) -> BenchConfig:
    return BenchConfig.load(getattr(args, "config", None))


def _tasks(args: argparse.Namespace):
    return load_tasks(getattr(args, "tasks_dir", None))


def _select(tasks, args: argparse.Namespace):
    """Filter the adopted tasks by application and by split side."""

    selected = adopted_tasks(tasks)
    apps = getattr(args, "app", None)
    if apps:
        wanted = set(apps)
        selected = [task for task in selected if task.app in wanted]

    side = getattr(args, "split_side", None)
    if side:
        split = _load_split(getattr(args, "split_file", None))
        ids = set(split.dev if side == "dev" else split.test)
        selected = [task for task in selected if task.id in ids]
    return selected


def _load_split(path: str | None = None) -> Split:
    target = pathlib.Path(path) if path else SPLIT_PATH
    if not target.exists():
        raise SystemExit(
            f"no split at {target}; run `python -m harness_bench split` first"
        )
    return Split.from_dict(json.loads(target.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _config(args)
    from . import osworld_bootstrap as boot

    print("python")
    print(f"  version      {sys.version.split()[0]}")
    print(f"  executable   {sys.executable}")

    print("\ngraders")
    for module in ("table", "docs", "slides", "pdf", "gimp", "general", "chrome", "vlc"):
        try:
            boot.import_metric(module)
        except Exception as exc:
            print(f"  metrics/{module:<9} FAIL {type(exc).__name__}: {exc}")
        else:
            print(f"  metrics/{module:<9} ok")
    for module in ("file", "misc"):
        try:
            boot.import_getter(module)
        except Exception as exc:
            print(f"  getters/{module:<9} FAIL {type(exc).__name__}: {exc}")
        else:
            print(f"  getters/{module:<9} ok")

    print("\napplications")
    soffice = find_soffice(config.apps.soffice)
    if soffice:
        print(f"  soffice      {soffice}")
    elif config.apps.soffice:
        print(f"  soffice      NOT FOUND (configured as {config.apps.soffice})")
    else:
        print("  soffice      NOT FOUND")
    print(f"  harness lo   {shutil.which(config.harness.libreoffice) or 'NOT FOUND'}")
    print(f"  harness gimp {shutil.which(config.harness.gimp) or 'NOT FOUND'}")

    print("\nmodel")
    print(f"  endpoint     {config.model.base_url}")
    print(f"  model        {config.model.model or 'NOT CONFIGURED'}")
    key = config.model.resolved_api_key()
    print(f"  api key      {'set' if key else 'missing'}")

    print("\ntasks")
    tasks = load_tasks()
    adopted = adopted_tasks(tasks)
    print(f"  total        {len(tasks)}")
    print(f"  adopted      {len(adopted)}")
    print(f"  excluded     {len(tasks) - len(adopted)}")
    by_app: dict[str, int] = {}
    for task in adopted:
        by_app[task.app] = by_app.get(task.app, 0) + 1
    for app, count in sorted(by_app.items()):
        print(f"  {app:<20} {count}")
    return 0


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------


def cmd_tasks(args: argparse.Namespace) -> int:
    tasks = _tasks(args)
    adopted = adopted_tasks(tasks)

    if args.excluded:
        rows = exclusion_table(tasks)
        print(report_mod.markdown_table(rows, [("id", "task"), ("app", "app"), ("reason", "reason")]))
        return 0

    by_app: dict[str, int] = {}
    for task in adopted:
        by_app[task.app] = by_app.get(task.app, 0) + 1

    print(f"{len(tasks)} tasks, {len(adopted)} adopted, {len(tasks) - len(adopted)} excluded")
    for app, count in sorted(by_app.items()):
        print(f"  {app:<20} {count}")

    if args.verbose:
        rows = [
            {
                "id": task.id,
                "app": task.app,
                "funcs": ",".join(task.funcs),
                "conj": task.conj,
                "inputs": len(task.inputs),
            }
            for task in adopted
        ]
        print()
        print(
            report_mod.markdown_table(
                rows, [("id", "task"), ("app", "app"), ("funcs", "grader"), ("conj", "conj"), ("inputs", "inputs")]
            )
        )
    return 0


# --------------------------------------------------------------------------
# split
# --------------------------------------------------------------------------


def cmd_split(args: argparse.Namespace) -> int:
    config = _config(args)
    tasks = _tasks(args)
    split = make_split(
        tasks,
        dev_fraction=args.dev_fraction if args.dev_fraction is not None else config.split.dev_fraction,
        seed=args.seed if args.seed is not None else config.split.seed,
    )

    target = pathlib.Path(args.output) if args.output else SPLIT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(split.as_dict(), indent=1), encoding="utf-8")

    print(f"wrote {target}")
    print(f"  seed         {split.seed}")
    print(f"  dev fraction {split.dev_fraction}")
    print(f"  dev          {len(split.dev)}")
    print(f"  test         {len(split.test)}")

    apps = {task.id: task.app for task in load_tasks()}
    for side in ("dev", "test"):
        counts: dict[str, int] = {}
        for task_id in (split.dev if side == "dev" else split.test):
            app = apps.get(task_id, "?")
            counts[app] = counts.get(app, 0) + 1
        print(f"  {side:<12} " + ", ".join(f"{app} {count}" for app, count in sorted(counts.items())))
    return 0


# --------------------------------------------------------------------------
# roundtrip
# --------------------------------------------------------------------------


def cmd_roundtrip(args: argparse.Namespace) -> int:
    config = _config(args)
    tasks = _select(_tasks(args), args)
    if not tasks:
        print("no tasks selected")
        return 1

    print(f"checking the harness round trip on {len(tasks)} tasks")
    results: list[roundtrip_mod.RoundTripResult] = []

    def report_one(result: roundtrip_mod.RoundTripResult) -> None:
        results.append(result)
        if not result.applicable:
            print(f"  skip {result.task_id} ({result.app}) {result.reason[:80]}")
        elif not result.decidable:
            print(f"  n/a  {result.task_id} ({result.app}) {result.status}")
        elif result.score is None:
            print(f"  ERR  {result.task_id} ({result.app}) {result.status} {result.reason[:70]}")
        else:
            mark = "pass" if result.passed else "LOST"
            print(
                f"  {mark} {result.task_id} ({result.app}) "
                f"{result.baseline:.2f} -> {result.score:.2f}"
            )

    roundtrip_mod.check_all(config, tasks, RUNS_ROOT, on_result=report_one)

    summary = roundtrip_mod.summarize(results)
    print()
    print(f"tasks        {summary['tasks']}")
    print(f"decidable    {summary['decidable']}")
    print(f"measured     {summary['measured']}")
    print(f"survived     {summary['survived']}")
    print(f"lost         {summary['lost']}")
    if summary["ceiling"] is not None:
        print(f"ceiling      {summary['ceiling']:.1%} of the measured tasks")
    for label, key in (
        ("not decidable", "not_decidable"),
        ("no reference", "no_reference"),
        ("needs soffice", "needs_soffice"),
        ("harness missing", "harness_missing"),
        ("reference unavailable", "reference_unavailable"),
        ("input unavailable", "input_unavailable"),
        ("rendered artifacts", "rendered_artifacts"),
        ("measure failed", "failed"),
    ):
        if summary[key]:
            print(f"{label:<13}{summary[key]}")

    output = pathlib.Path(args.output) if args.output else REPO_ROOT / "runs" / "roundtrip.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"summary": summary, "results": [r.as_dict() for r in results]}, indent=1),
        encoding="utf-8",
    )
    print(f"wrote {output}")
    return 0


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    config = _config(args)
    tasks = _select(_tasks(args), args)
    if not tasks:
        print("no tasks selected")
        return 1

    interfaces = args.interface or ["harness", "script"]
    repeats = {name: args.repeats for name in interfaces} if args.repeats else None

    split = None
    if args.split_side or args.tag_split:
        split = _load_split(args.split_file)

    if not args.dry_run and not config.model.model:
        raise SystemExit(
            "no model configured; set model.model in the config file or pass --model"
        )

    print(f"{len(tasks)} tasks x {len(interfaces)} interfaces")
    if args.dry_run:
        print("dry run: materializing tasks and grading the standard answers only")

    def on_record(record) -> None:
        status = "ok" if record.score >= 1.0 else "fail"
        print(
            f"  {record.interface:<8} {record.task_id} r{record.repeat} "
            f"{status} score={record.score:.2f} calls={record.tool_calls} "
            f"{record.failure_class}"
        )

    if args.dry_run:
        return _dry_run(config, tasks, interfaces, split, args.runs_dir)

    records = []
    for interface_name in interfaces:
        times = (repeats or {}).get(interface_name, config.repeat_count(interface_name))
        for task in tasks:
            for repeat in range(times):
                from .runner import run_once

                record = run_once(
                    config,
                    task,
                    interface_name,
                    repeat=repeat,
                    runs_root=args.runs_dir,
                    keep_workdir=args.keep_workdir,
                )
                if split is not None:
                    record.extra["split"] = split.side_of(task.id)
                    record.extra["round"] = args.round
                    path = pathlib.Path(record.run_dir) / "record.json"
                    path.write_text(json.dumps(record.as_dict(), indent=1, ensure_ascii=False), encoding="utf-8")
                records.append(record)
                on_record(record)
    print(f"\n{len(records)} runs recorded under {args.runs_dir}")
    return 0


# --------------------------------------------------------------------------
# owncli
# --------------------------------------------------------------------------


def cmd_owncli(args: argparse.Namespace) -> int:
    """Run our own CLI on our own tasks, through the same agent loop."""

    from . import owncli as owncli_mod
    from .owncli_run import run_batch

    config = _config(args)
    tasks = owncli_mod.load_tasks()
    if args.task:
        wanted = set(args.task)
        tasks = [task for task in tasks if task.id in wanted]
        missing = wanted - {task.id for task in tasks}
        if missing:
            raise SystemExit(f"unknown own-cli task(s): {', '.join(sorted(missing))}")
    if not tasks:
        print("no tasks selected")
        return 1

    coverage = owncli_mod.coverage()
    print(
        f"{len(tasks)} own-cli tasks, "
        f"{coverage['expressible']}/{coverage['tasks']} expressible by the CLI today"
    )

    if args.dry_run:
        for task in tasks:
            print(f"  {task.id:<32} {'gap: ' + task.gap if task.gap else 'expressible'}")
        return 0

    if not config.model.model:
        raise SystemExit(
            "no model configured; set model.model in the config file or pass --model"
        )

    def on_record(record) -> None:
        status = "ok" if record.score >= 1.0 else "fail"
        print(
            f"  {record.task_id:<32} r{record.repeat} {status} score={record.score:.2f} "
            f"calls={record.tool_calls} tokens={record.prompt_tokens + record.completion_tokens} "
            f"{record.failure_class}"
        )

    records = run_batch(
        config,
        tasks,
        repeats=args.repeats,
        runs_root=args.runs_dir,
        on_record=on_record,
    )
    print(f"\n{len(records)} runs recorded under {args.runs_dir}")
    return 0


def _dry_run(config: BenchConfig, tasks, interfaces, split, runs_dir=None) -> int:
    """Materialize each task and grade its own standard answer.

    This is the week-1 exit criterion without spending a token: it proves the
    download, the path rewriting, the normalisation and the grader all line up.
    """

    from .normalize import save_outputs
    from .postconfig import run_postconfig
    from .tasks import _as_list, fetch

    root = pathlib.Path(runs_dir) if runs_dir else REPO_ROOT / "runs"
    soffice = find_soffice(config.apps.soffice)
    graded_count = 0
    failures: list[str] = []
    unavailable = 0
    for task in tasks:
        work = root / "_dry" / task.id
        try:
            materialized = materialize(task, work / "work", config.download_cache)
        except OSError as exc:
            print(f"  {task.id} ({task.app}) input unavailable: {exc}")
            unavailable += 1
            continue

        if task.is_infeasible:
            continue

        golds = [
            cfg
            for cfg in _as_list(task.evaluator.get("expected"))
            if isinstance(cfg, dict) and cfg.get("type") == "cloud_file"
        ]
        results = _as_list(task.evaluator.get("result"))
        if not results:
            print(f"  {task.id} ({task.app}) no result file, cannot self-grade")
            continue

        declared = results[0].get("path")
        if isinstance(declared, list):
            declared = declared[0]
        destination = materialized.resolve(str(declared))
        destination.parent.mkdir(parents=True, exist_ok=True)

        if golds:
            gold_path = golds[0].get("path")
            if isinstance(gold_path, list):
                gold_path = gold_path[0]
            try:
                reference = fetch(str(gold_path), config.download_cache)
            except OSError as exc:
                print(f"  {task.id} ({task.app}) reference unavailable: {exc}")
                unavailable += 1
                continue
            shutil.copyfile(reference, destination)
        elif len(results) == 1:
            print(f"  {task.id} ({task.app}) checks the produced file directly, no answer to copy")
            continue

        post = run_postconfig(materialized, soffice, work / "normalize")
        save_outputs(
            [materialized.resolve(str(cfg.get("path")))
             for cfg in results if isinstance(cfg.get("path"), str)],
            soffice,
            work / "normalize",
        )

        outcome = grade(materialized, ["DONE"], work / "cache")
        if not outcome.available:
            unavailable += 1
            print(f"  {task.id} ({task.app}) not gradeable: {outcome.reason[:120]}")
            continue
        graded_count += 1
        if outcome.score < 1.0:
            failures.append(task.id)
            print(
                f"  FAIL {task.id} ({task.app}) score={outcome.score:.2f} "
                f"{outcome.reason[:120]}"
                + ("" if post.ok else f" [postconfig: {post.required_failure[:60]}]")
            )

    print(f"\n{graded_count} tasks self-graded, {len(failures)} failed, {unavailable} not gradeable (download)")
    for task_id in failures:
        print(f"  {task_id}")
    if failures and soffice is None:
        print("\nsoffice was not found; spreadsheet outputs cannot be recalculated")
    return 0 if not failures else 1


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def cmd_report(args: argparse.Namespace) -> int:
    records = load_records(args.runs_dir)
    if not records:
        print(f"no records under {args.runs_dir}")
        return 1

    summary = report_mod.summarize_all(records)
    rows = [
        {
            "interface": entry["interface"],
            "runs": entry["runs"],
            "tasks": entry["tasks"],
            "success": entry["success_rate"],
            "false_completion": entry["false_completion_rate"],
            "calls": entry["mean_tool_calls"],
            "tokens": entry["mean_tokens"],
            "seconds": entry["mean_elapsed_s"],
            "stability": entry["stability"],
        }
        for entry in summary["interfaces"]
    ]
    print("## Main results\n")
    print(
        report_mod.markdown_table(
            rows,
            [
                ("interface", "interface"),
                ("runs", "runs"),
                ("tasks", "tasks"),
                ("success", "success"),
                ("false_completion", "false completion"),
                ("calls", "tool calls"),
                ("tokens", "tokens"),
                ("seconds", "seconds"),
                ("stability", "stability"),
            ],
        )
    )

    harness = next((e for e in summary["interfaces"] if e["interface"] == "harness"), None)
    if harness and harness["failure_classes"]:
        print("\n## Harness failure classes\n")
        classes = [{"class": name, "runs": count} for name, count in harness["failure_classes"].items()]
        print(report_mod.markdown_table(classes, [("class", "class"), ("runs", "runs")]))

    if {"harness", "script"} <= {e["interface"] for e in summary["interfaces"]}:
        comparison = report_mod.paired_comparison(records, "harness", "script")
        print("\n## Harness vs script (paired)\n")
        print(f"- tasks both ran: {comparison['tasks']}")
        print(f"- harness only: {comparison.get('a_only')}, script only: {comparison.get('b_only')}")
        print(f"- McNemar exact p: {comparison.get('mcnemar_p')}")

    curve = report_mod.refinement_curve(records)
    if curve["rounds"]:
        print("\n## Refinement rounds\n")
        print(
            report_mod.markdown_table(
                curve["rounds"],
                [
                    ("round", "round"),
                    ("dev_rate", "dev"),
                    ("dev_tasks", "dev tasks"),
                    ("test_rate", "test"),
                    ("test_tasks", "test tasks"),
                    ("gap", "gap"),
                ],
            )
        )

    if args.output:
        target = pathlib.Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print(f"\nwrote {target}")
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness_bench",
        description="Benchmark CLI-Anything harnesses against GUI, scripts and hybrid interfaces.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="YAML configuration file")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="report what is missing before a run")
    doctor.set_defaults(func=cmd_doctor)

    tasks = sub.add_parser("tasks", help="list adopted tasks and exclusions")
    tasks.add_argument("--tasks-dir", default=None)
    tasks.add_argument("--excluded", action="store_true", help="show why tasks were excluded")
    tasks.add_argument("--verbose", action="store_true", help="one row per adopted task")
    tasks.set_defaults(func=cmd_tasks)

    split = sub.add_parser("split", help="freeze the development/test split")
    split.add_argument("--tasks-dir", default=None)
    split.add_argument("--dev-fraction", type=float, default=None)
    split.add_argument("--seed", type=int, default=None)
    split.add_argument("--output", default=None)
    split.set_defaults(func=cmd_split)

    trip = sub.add_parser("roundtrip", help="harness ceiling check on standard answers")
    trip.add_argument("--tasks-dir", default=None)
    trip.add_argument("--app", nargs="*", default=None)
    trip.add_argument("--split-side", choices=("dev", "test"), default=None)
    trip.add_argument("--split-file", default=None)
    trip.add_argument("--output", default=None)
    trip.set_defaults(func=cmd_roundtrip)

    run = sub.add_parser("run", help="drive the agent and grade the result")
    run.add_argument("--tasks-dir", default=None)
    run.add_argument("--interface", nargs="*", default=None, choices=list(report_mod.INTERFACE_ORDER))
    run.add_argument("--app", nargs="*", default=None)
    run.add_argument("--split-side", choices=("dev", "test"), default=None)
    run.add_argument("--split-file", default=None)
    run.add_argument("--tag-split", action="store_true", help="record the split side and round in each record")
    run.add_argument("--round", default="round0", help="refinement round label")
    run.add_argument("--repeats", type=int, default=None, help="override repeats for every interface")
    run.add_argument("--runs-dir", default=str(RUNS_ROOT))
    run.add_argument("--keep-workdir", action="store_true")
    run.add_argument("--dry-run", action="store_true", help="grade the standard answers without calling a model")
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="aggregate runs into report tables")
    report.add_argument("--runs-dir", default=str(RUNS_ROOT))
    report.add_argument("--output", default=None)
    report.set_defaults(func=cmd_report)

    own = sub.add_parser("owncli", help="run our own CLI-Anything harness on our own blog tasks")
    own.add_argument("--task", nargs="*", default=None, help="only these task ids")
    own.add_argument("--repeats", type=int, default=None)
    own.add_argument("--runs-dir", default=str(RUNS_ROOT))
    own.add_argument("--dry-run", action="store_true", help="list the tasks and their coverage gaps")
    own.set_defaults(func=cmd_owncli)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
