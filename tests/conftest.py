"""Shared fixtures.

The task list is loaded once per session: it reads 143 JSON files from the
vendored OSWorld tree, and several test modules only need to inspect it.

``stage_offline`` exists so that tests never touch the network.  ``materialize``
downloads the task's input files from the OSWorld file cache and the grader
downloads the reference answer; a unit test that only cares about path handling
or the shape of a score must not fail because Hugging Face is slow or down.
Staging therefore writes placeholder bytes for both the declared inputs and the
reference file, and the cloud getter is pointed at those placeholders.
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from harness_bench import osworld_bootstrap as boot
from harness_bench import tasks as tasks_mod
from harness_bench.tasks import MaterializedTask, Task, local_path_for, rewrite_vm_path


@pytest.fixture(scope="session")
def all_tasks():
    return tasks_mod.load_tasks()


@pytest.fixture(scope="session")
def adopted(all_tasks):
    return tasks_mod.adopted_tasks(all_tasks)


@pytest.fixture
def stage_offline(monkeypatch):
    """Return a helper that stages a task without any network access."""

    getters_file = boot.import_getter("file")

    def fake_fetch(url: str, cache_dir) -> pathlib.Path:
        """Stand in for ``tasks.fetch`` with a local placeholder download."""

        cache = pathlib.Path(cache_dir)
        cache.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        target = cache / name
        if not target.exists():
            target.write_bytes(b"reference")
        return target

    def fake_get_cloud_file(env, config):
        """Stand in for ``get_cloud_file`` with a placeholder reference file."""

        multi = config.get("multi", False)
        paths = config["path"] if multi else [config["path"]]
        dests = config["dest"] if multi else [config["dest"]]
        gives = set(config.get("gives", [0]))
        returned = []
        for index, (_, dest) in enumerate(zip(paths, dests)):
            target = pathlib.Path(env.cache_dir) / dest
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(b"reference")
            if index in gives:
                returned.append(str(target))
        return returned[0] if len(returned) == 1 else returned

    monkeypatch.setattr(getters_file, "get_cloud_file", fake_get_cloud_file, raising=False)
    monkeypatch.setattr(tasks_mod, "fetch", fake_fetch, raising=False)

    def stage(task: Task, workdir: pathlib.Path) -> MaterializedTask:
        workdir.mkdir(parents=True, exist_ok=True)
        mapping: dict[str, pathlib.Path] = {}
        for entry in task.inputs:
            destination = local_path_for(entry.vm_path, workdir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"staged")
            mapping[entry.vm_path] = destination
        return MaterializedTask(
            task=task,
            workdir=workdir,
            instruction=rewrite_vm_path(task.instruction, workdir),
            vm_to_local=mapping,
        )

    return stage
