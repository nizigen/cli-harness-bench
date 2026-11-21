"""Our own CLI track: the sandbox, the checks, and the coverage table.

The track exists so the benchmark can say something about a harness we wrote
ourselves, so what matters is that the checks read real files (not the CLI's
output) and that a task the CLI cannot express is counted as such.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from harness_bench import owncli
from harness_bench.config import BenchConfig


@pytest.fixture()
def config() -> BenchConfig:
    return BenchConfig.from_dict({"owncli": {"binary": "cli-anything-nizigen-blog"}})


def _sandbox(tmp_path, config, task_id="blog-01-create-post"):
    task = owncli.by_id(task_id)
    return owncli.make_sandbox(task, tmp_path / "work", config)


def test_the_sandbox_is_a_self_contained_site(tmp_path, config):
    sandbox = _sandbox(tmp_path, config)

    assert (sandbox.site / "hugo.toml").exists()
    assert (sandbox.site / "content" / "post" / "welcome.md").exists()
    assert (sandbox.site / owncli.STALE_PAGE).exists()
    assert sandbox.env["NIZIGEN_BLOG_SOURCE"] == str(sandbox.site)
    assert sandbox.env["NIZIGEN_BLOG_PAGES"] == str(sandbox.pages)
    # The CLI must not be able to reach the real blog checkouts.
    assert pathlib.Path(sandbox.env["NIZIGEN_BLOG_HOME"]) == sandbox.home


def test_every_sandbox_is_a_git_repository(tmp_path, config):
    sandbox = _sandbox(tmp_path, config)

    for repo in (sandbox.site, sandbox.pages):
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr


def test_a_missing_post_fails_the_check(tmp_path, config):
    sandbox = _sandbox(tmp_path, config)

    result = owncli.grade(sandbox.task, sandbox)

    assert result.score == 0.0
    assert result.status == "graded"
    assert all(not metric["ok"] for metric in result.per_metric)


def test_a_correct_post_scores_one(tmp_path, config):
    sandbox = _sandbox(tmp_path, config)
    (sandbox.site / "content" / "post" / "hello-hugo.md").write_text(
        "---\ntitle: \"Hello Hugo\"\ndate: 2026-02-01\ndraft: false\n---\n\n"
        "Hugo renders Markdown.\n",
        encoding="utf-8",
    )

    result = owncli.grade(sandbox.task, sandbox)

    assert result.score == 1.0
    assert "4/4" in result.reason


def test_a_draft_is_recognised_as_a_boolean(tmp_path, config):
    sandbox = _sandbox(tmp_path, config, "blog-02-create-draft")
    (sandbox.site / "content" / "post" / "work-in-progress.md").write_text(
        "---\ntitle: \"Work In Progress\"\ndate: 2026-02-01\ndraft: true\n---\n\n", encoding="utf-8"
    )

    assert owncli.grade(sandbox.task, sandbox).score == 1.0


def test_tags_are_compared_as_a_set(tmp_path, config):
    sandbox = _sandbox(tmp_path, config, "blog-04-tags")
    (sandbox.site / "content" / "post" / "tagged-post.md").write_text(
        "---\ntitle: \"Tagged Post\"\ndate: 2026-02-01\ndraft: false\ntags: [\"hugo\", \"python\"]\n---\n\n",
        encoding="utf-8",
    )

    assert owncli.grade(sandbox.task, sandbox).score == 1.0


def test_a_stale_page_fails_the_clean_build_check(tmp_path, config):
    sandbox = _sandbox(tmp_path, config, "blog-08-clean-build")
    (sandbox.site / "public" / "index.html").write_text("<html></html>", encoding="utf-8")

    result = owncli.grade(sandbox.task, sandbox)

    assert result.score == 0.5
    assert result.per_metric[1]["check"].startswith("file_missing")
    assert result.per_metric[1]["ok"] is False


def test_a_clean_build_scores_one(tmp_path, config):
    sandbox = _sandbox(tmp_path, config, "blog-08-clean-build")
    (sandbox.site / "public" / "index.html").write_text("<html></html>", encoding="utf-8")
    (sandbox.site / owncli.STALE_PAGE).unlink()

    assert owncli.grade(sandbox.task, sandbox).score == 1.0


def test_coverage_names_the_tasks_the_cli_cannot_express():
    coverage = owncli.coverage()

    assert coverage["tasks"] == 12
    assert coverage["expressible"] == 8
    assert set(coverage["gaps"]) == {
        "blog-09-edit-title",
        "blog-10-delete-post",
        "blog-11-backdate-post",
        "blog-12-site-title",
    }


def test_every_task_has_checks_and_an_instruction():
    for task in owncli.load_tasks():
        assert task.instruction.strip()
        assert task.checks
        assert all(check.kind in owncli.CHECKS for check in task.checks)
        assert task.family == "owncli"


def test_a_task_with_a_gap_is_recorded_as_expressiveness(tmp_path, config, monkeypatch):
    """A gap task that scores 0 is the CLI's limit, not the agent's mistake."""

    from harness_bench import owncli_run
    from harness_bench.agent import AgentOutcome

    task = owncli.by_id("blog-10-delete-post")

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, sandbox):
            return AgentOutcome(status="done", summary="deleted", tool_calls=2, stopped_reason="finished")

    monkeypatch.setattr(owncli_run, "AgentRunner", FakeRunner)

    record = owncli_run.run_once(config, task, runs_root=tmp_path / "runs")

    assert record.score == 0.0
    assert record.failure_class == "expressiveness"
    assert "delete" in record.failure_note
