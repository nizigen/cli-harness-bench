"""The task loader and the split have to agree with the proposal's table.

These tests read the vendored OSWorld JSON, so they fail loudly if the vendored
copy is ever replaced with a different upstream commit.
"""

from __future__ import annotations

from harness_bench import tasks as tasks_mod
from harness_bench.config import TASKS_DIR


def test_every_vendored_app_is_loaded(all_tasks):
    counts = {}
    for task in all_tasks:
        counts[task.app] = counts.get(task.app, 0) + 1
    assert counts == {
        "gimp": 26,
        "libreoffice_calc": 47,
        "libreoffice_writer": 23,
        "libreoffice_impress": 47,
    }
    assert len(all_tasks) == 143


def test_adoption_matches_the_proposal_table(all_tasks):
    adopted = tasks_mod.adopted_tasks(all_tasks)
    assert len(adopted) == 133
    assert len(all_tasks) - len(adopted) == 10

    per_app = {}
    for task in adopted:
        per_app[task.app] = per_app.get(task.app, 0) + 1
    assert per_app == {
        "gimp": 20,
        "libreoffice_calc": 45,
        "libreoffice_writer": 23,
        "libreoffice_impress": 45,
    }


def test_exclusions_are_all_explained(all_tasks):
    excluded = [task for task in all_tasks if not task.adopted]
    assert len(excluded) == 10
    for task in excluded:
        assert task.exclusion_reason, f"{task.id} was excluded without a reason"
    kinds = {task.exclusion_reason.split(":")[0] for task in excluded}
    assert kinds == {
        "result getter not reproducible off-VM",
        "expected getter not reproducible off-VM",
    }


def test_infeasible_tasks_are_adopted(all_tasks):
    infeasible = [task for task in all_tasks if task.is_infeasible]
    assert len(infeasible) == 11
    assert all(task.adopted for task in infeasible)


def test_split_is_stratified_and_stable(all_tasks):
    first = tasks_mod.make_split(all_tasks, dev_fraction=0.4, seed=20260601)
    second = tasks_mod.make_split(all_tasks, dev_fraction=0.4, seed=20260601)

    assert first.as_dict() == second.as_dict()
    assert len(first.dev) == 53
    assert len(first.test) == 80
    assert not set(first.dev) & set(first.test)

    adopted = {task.id for task in tasks_mod.adopted_tasks(all_tasks)}
    assert set(first.dev) | set(first.test) == adopted


def test_split_quota_per_application(all_tasks):
    split = tasks_mod.make_split(all_tasks, dev_fraction=0.4, seed=20260601)
    app_of = {task.id: task.app for task in all_tasks}
    dev_counts = {}
    for task_id in split.dev:
        app = app_of[task_id]
        dev_counts[app] = dev_counts.get(app, 0) + 1
    assert dev_counts == {
        "gimp": 8,
        "libreoffice_calc": 18,
        "libreoffice_impress": 18,
        "libreoffice_writer": 9,
    }


def test_rewrite_vm_path_maps_the_home_directory(tmp_path):
    work = tmp_path
    # The VM layout is preserved below the working directory, so every path in
    # the task keeps the relative position it had inside the guest.
    assert tasks_mod.rewrite_vm_path("/home/user/Desktop/a.xlsx", work) == str(
        work / "Desktop" / "a.xlsx"
    )
    assert tasks_mod.rewrite_vm_path("/home/user/b.docx", work) == str(work / "b.docx")
    # A path that is not under the VM home is left alone.
    assert tasks_mod.rewrite_vm_path("/tmp/c.txt", work) == "/tmp/c.txt"


def test_rewrite_vm_path_keeps_nested_directories(tmp_path):
    rewritten = tasks_mod.rewrite_vm_path("/home/user/Documents/report/x.odt", tmp_path)
    assert rewritten == str(tmp_path / "Documents" / "report" / "x.odt")


def test_rewrite_vm_path_replaces_paths_inside_prose(tmp_path):
    """The instruction text is rewritten too, not only the config paths."""

    rewritten = tasks_mod.rewrite_vm_path(
        "Open /home/user/Desktop/a.docx and save it as PDF.", tmp_path
    )
    assert str(tmp_path / "Desktop" / "a.docx") in rewritten
    assert "/home/user" not in rewritten


def test_rewrite_vm_path_handles_the_bare_home(tmp_path):
    assert tasks_mod.rewrite_vm_path("/home/user", tmp_path) == str(tmp_path)


def test_local_path_for_anchors_relative_paths_at_the_vm_home(tmp_path):
    """``a097acff`` declares its input as ``Downloads/x.pptx``, not an absolute path.

    Read literally that path escapes the sandbox and the file lands wherever the
    process happens to run; OSWorld means it relative to the guest home.
    """

    resolved = tasks_mod.local_path_for("Downloads/slides.pptx", tmp_path)
    assert resolved == tmp_path / "Downloads" / "slides.pptx"
    assert tasks_mod.local_path_for("/home/user/Desktop/a.docx", tmp_path) == (
        tmp_path / "Desktop" / "a.docx"
    )


def test_every_declared_input_stays_inside_the_sandbox(all_tasks, tmp_path):
    for task in tasks_mod.adopted_tasks(all_tasks):
        for entry in task.inputs:
            resolved = tasks_mod.local_path_for(entry.vm_path, tmp_path)
            assert resolved.is_relative_to(tmp_path), (task.id, entry.vm_path, resolved)


def test_evaluator_fields_are_derived_not_stored():
    """The split must not be able to drift from the evaluator."""

    path = TASKS_DIR / "libreoffice_calc" / "01b269ae-2111-4a07-81fd-3fcd711993b0.json"
    task = tasks_mod.Task.from_file(path)

    assert task.funcs == ["compare_table"]
    assert task.result_types == ["vm_file"]
    assert task.expected_types == ["cloud_file"]
    assert task.conj == "and"
    assert task.adopted
    assert [entry.vm_path for entry in task.inputs] == [
        "/home/user/Student_Level_Fill_Blank.xlsx"
    ]


def test_multi_metric_task_keeps_its_conjunction():
    path = (
        TASKS_DIR
        / "libreoffice_writer"
        / "0810415c-bde4-4443-9047-d5f70165a697.json"
    )
    task = tasks_mod.Task.from_file(path)

    assert task.funcs == ["compare_line_spacing", "compare_line_spacing"]
    assert task.conj == "or"
    assert task.adopted


def test_every_offline_task_has_an_input_to_download(all_tasks):
    """Everything the harness can be asked to edit arrives as a downloaded file.

    One task only configures a bare ``launch`` (it asks for a LibreOffice
    preference change), so it has no input to stage; it is still gradable, it
    just starts from an empty sandbox.
    """

    no_input = []
    for task in tasks_mod.adopted_tasks(all_tasks):
        if task.is_infeasible or task.inputs:
            continue
        no_input.append(task.id)
    assert no_input == ["2cd43775-7085-45d8-89fa-9e35c0a915cf"]
