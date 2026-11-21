"""Our own CLI-Anything harness, on our own tasks.

The OSWorld track asks whether a *third-party* harness earns its keep. This
track asks the same question about a harness we wrote ourselves
(``cli-anything-nizigen-blog``), because a benchmark that only ever measures
other people's work cannot tell us whether the method transfers.

The tasks are small, deterministic edits to a Hugo site, and every check reads
files: the content tree the CLI wrote, or the site it built. Nothing here reads
the CLI's own output, so a run cannot pass by describing what it would do.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import shutil
import stat
import subprocess
from typing import Any, Callable

from .grading import GradeResult

# --------------------------------------------------------------------------
# sandbox
# --------------------------------------------------------------------------

SITE_CONFIG = """baseURL = 'https://bench.invalid/'
languageCode = 'en-us'
title = 'Bench Site'

[markup.goldmark.renderer]
unsafe = true
"""

LAYOUT_INDEX = (
    "<!doctype html><html><body><h1>{{ .Site.Title }}</h1>"
    "{{ range .Site.RegularPages }}<a href=\"{{ .RelPermalink }}\">{{ .Title }}</a>{{ end }}"
    "</body></html>\n"
)
LAYOUT_SINGLE = "<!doctype html><html><body><h1>{{ .Title }}</h1>{{ .Content }}</body></html>\n"
LAYOUT_LIST = (
    "<!doctype html><html><body><h1>{{ .Title }}</h1>"
    "{{ range .Pages }}<a href=\"{{ .RelPermalink }}\">{{ .Title }}</a>{{ end }}"
    "</body></html>\n"
)

SEED_POSTS = {
    "content/post/welcome.md": "---\ntitle: \"Welcome\"\ndate: 2026-01-01\ndraft: false\n---\n\n"
    "The first post.\n",
    "content/post/obsolete.md": "---\ntitle: \"Obsolete\"\ndate: 2026-01-02\ndraft: false\n---\n\n"
    "This post should be deleted.\n",
}

# A stale page the build has to clean up; only ``build --clean`` removes it.
STALE_PAGE = "public/stale.html"


@dataclasses.dataclass
class Sandbox:
    """One materialized blog task: a git site repo, a pages repo, a CLI home."""

    task: "OwnTask"
    workdir: pathlib.Path
    site: pathlib.Path
    pages: pathlib.Path
    home: pathlib.Path
    env: dict[str, str]

    @property
    def instruction(self) -> str:
        return self.task.instruction

    def resolve(self, path: str) -> pathlib.Path:
        return self.site / path


def _git_init(path: pathlib.Path) -> None:
    """Create a repository with one commit.

    The CLI inspects ``git log`` and refuses to work on a repository without
    commits, so an empty repository would make every task look like a CLI defect
    rather than a task.
    """

    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=120,
        )

    run("init", "-q")
    run("config", "user.email", "bench@invalid")
    run("config", "user.name", "Bench")
    run("add", "-A")
    run("commit", "-q", "-m", "bench: initial state", "--allow-empty")


def _force_remove(path: pathlib.Path) -> None:
    """Delete a sandbox tree even when git made its objects read-only.

    A sandbox is created and thrown away on every run, so a second run over the
    same directory must not die on a read-only pack file.
    """

    def on_error(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=on_error)


def make_sandbox(task: "OwnTask", workdir: pathlib.Path, config: Any) -> Sandbox:
    """Build the site, pages repo and private CLI home for one task."""

    workdir = pathlib.Path(workdir).resolve()
    _force_remove(workdir)
    site = workdir / "site"
    pages = workdir / "pages"
    home = workdir / "cli-home"

    for relative, text in (
        ("hugo.toml", SITE_CONFIG),
        ("layouts/index.html", LAYOUT_INDEX),
        ("layouts/_default/single.html", LAYOUT_SINGLE),
        ("layouts/_default/list.html", LAYOUT_LIST),
        (STALE_PAGE, "<!doctype html><title>stale</title>\n"),
        *SEED_POSTS.items(),
    ):
        target = site / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (site / "content" / "post").mkdir(parents=True, exist_ok=True)

    _git_init(site)
    _git_init(pages)
    home.mkdir(parents=True, exist_ok=True)

    hugo = str(config.owncli.hugo or "hugo")
    env = {
        "NIZIGEN_BLOG_HOME": str(home),
        "NIZIGEN_BLOG_SOURCE": str(site),
        "NIZIGEN_BLOG_PAGES": str(pages),
        "HUGO_BIN": hugo,
    }
    return Sandbox(task=task, workdir=workdir, site=site, pages=pages, home=home, env=env)


# --------------------------------------------------------------------------
# a deliberately independent front-matter reader
# --------------------------------------------------------------------------

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


def read_post(path: pathlib.Path) -> dict[str, Any]:
    """Parse the subset of YAML the CLI writes: scalars and inline lists."""

    if not path.exists():
        return {}
    match = _FRONT_MATTER.match(path.read_text(encoding="utf-8", errors="replace"))
    if not match:
        return {}
    head, body = match.group(1), match.group(2)
    fields: dict[str, Any] = {"body": body}
    for line in head.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        value = raw.strip()
        if value.startswith("[") and value.endswith("]"):
            items = [item.strip().strip("\"'") for item in value[1:-1].split(",")]
            fields[key.strip()] = [item for item in items if item]
        elif value.lower() in ("true", "false"):
            fields[key.strip()] = value.lower() == "true"
        else:
            fields[key.strip()] = value.strip("\"'")
    return fields


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Check:
    """One assertion about the sandbox after the run."""

    kind: str
    params: dict[str, Any] = dataclasses.field(default_factory=dict)

    def describe(self) -> str:
        return f"{self.kind}({', '.join(f'{k}={v!r}' for k, v in self.params.items())})"


def _first(sandbox: Sandbox, pattern: str) -> pathlib.Path | None:
    matches = sorted(sandbox.site.glob(pattern))
    return matches[0] if matches else None


def _check_file_exists(sandbox: Sandbox, path: str) -> tuple[bool, str]:
    target = sandbox.resolve(path)
    return target.exists(), f"{path} exists={target.exists()}"


def _check_file_missing(sandbox: Sandbox, path: str) -> tuple[bool, str]:
    target = sandbox.resolve(path)
    return not target.exists(), f"{path} exists={target.exists()}"


def _check_front_matter(sandbox: Sandbox, path: str, field: str, equals: Any) -> tuple[bool, str]:
    target = _first(sandbox, path)
    if target is None:
        return False, f"no file matching {path}"
    fields = read_post(target)
    actual = fields.get(field)
    ok = actual == equals
    return ok, f"{target.name}: {field}={actual!r} expected {equals!r}"


def _check_tags(sandbox: Sandbox, path: str, tags: list[str]) -> tuple[bool, str]:
    target = _first(sandbox, path)
    if target is None:
        return False, f"no file matching {path}"
    actual = read_post(target).get("tags") or []
    ok = sorted(actual) == sorted(tags)
    return ok, f"{target.name}: tags={actual!r} expected {sorted(tags)!r}"


def _check_body_contains(sandbox: Sandbox, path: str, text: str) -> tuple[bool, str]:
    target = _first(sandbox, path)
    if target is None:
        return False, f"no file matching {path}"
    body = read_post(target).get("body", "")
    ok = text in body
    return ok, f"{target.name}: body contains {text!r}={ok}"


def _check_built_contains(sandbox: Sandbox, path: str, text: str) -> tuple[bool, str]:
    target = sandbox.site / "public" / path
    if not target.exists():
        return False, f"public/{path} missing"
    content = target.read_text(encoding="utf-8", errors="replace")
    ok = text in content
    return ok, f"public/{path} contains {text!r}={ok}"


def _check_text_contains(sandbox: Sandbox, path: str, text: str) -> tuple[bool, str]:
    target = sandbox.resolve(path)
    if not target.exists():
        return False, f"{path} missing"
    ok = text in target.read_text(encoding="utf-8", errors="replace")
    return ok, f"{path} contains {text!r}={ok}"


CHECKS: dict[str, Callable[..., tuple[bool, str]]] = {
    "file_exists": _check_file_exists,
    "file_missing": _check_file_missing,
    "front_matter": _check_front_matter,
    "tags": _check_tags,
    "body_contains": _check_body_contains,
    "built_contains": _check_built_contains,
    "text_contains": _check_text_contains,
}


def grade(task: "OwnTask", sandbox: Sandbox) -> GradeResult:
    """Score a finished run as the fraction of checks that hold."""

    per_metric: list[dict[str, Any]] = []
    passed = 0
    for check in task.checks:
        function = CHECKS[check.kind]
        try:
            ok, detail = function(sandbox, **check.params)
        except (OSError, ValueError) as exc:
            ok, detail = False, f"{check.describe()} raised {type(exc).__name__}: {exc}"
        if ok:
            passed += 1
        per_metric.append({"check": check.describe(), "ok": ok, "detail": detail})
    total = len(task.checks)
    score = passed / total if total else 0.0
    reason = f"{passed}/{total} checks passed"
    return GradeResult(score=score, status="graded", reason=reason, per_metric=per_metric)


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class OwnTask:
    id: str
    instruction: str
    checks: tuple[Check, ...]
    #: What the CLI would need to express the task; empty when it already can.
    gap: str = ""

    @property
    def app(self) -> str:
        return "blog"

    @property
    def family(self) -> str:
        return "owncli"

    @property
    def funcs(self) -> list[str]:
        return [check.kind for check in self.checks]


TASKS: tuple[OwnTask, ...] = (
    OwnTask(
        id="blog-01-create-post",
        instruction=(
            "Create a published post titled 'Hello Hugo' whose body is exactly "
            "'Hugo renders Markdown.' Use the default slug."
        ),
        checks=(
            Check("file_exists", {"path": "content/post/hello-hugo.md"}),
            Check("front_matter", {"path": "content/post/hello-hugo.md", "field": "title", "equals": "Hello Hugo"}),
            Check("front_matter", {"path": "content/post/hello-hugo.md", "field": "draft", "equals": False}),
            Check("body_contains", {"path": "content/post/hello-hugo.md", "text": "Hugo renders Markdown."}),
        ),
    ),
    OwnTask(
        id="blog-02-create-draft",
        instruction="Create a draft post titled 'Work In Progress'.",
        checks=(
            Check("file_exists", {"path": "content/post/work-in-progress.md"}),
            Check("front_matter", {"path": "content/post/work-in-progress.md", "field": "draft", "equals": True}),
        ),
    ),
    OwnTask(
        id="blog-03-custom-slug",
        instruction=(
            "Create a published post titled 'Weekly Notes' and make sure its file is "
            "content/post/weekly-notes-2026.md."
        ),
        checks=(
            Check("file_exists", {"path": "content/post/weekly-notes-2026.md"}),
            Check("front_matter", {"path": "content/post/weekly-notes-2026.md", "field": "title", "equals": "Weekly Notes"}),
        ),
    ),
    OwnTask(
        id="blog-04-tags",
        instruction="Create a published post titled 'Tagged Post' with the tags python and hugo.",
        checks=(
            Check("file_exists", {"path": "content/post/tagged-post.md"}),
            Check("tags", {"path": "content/post/tagged-post.md", "tags": ["python", "hugo"]}),
        ),
    ),
    OwnTask(
        id="blog-05-build-renders",
        instruction=(
            "Create a published post titled 'Build Me' with the body 'Rendered by Hugo.', "
            "then build the site."
        ),
        checks=(
            Check("file_exists", {"path": "content/post/build-me.md"}),
            Check("built_contains", {"path": "post/build-me/index.html", "text": "Rendered by Hugo."}),
        ),
    ),
    OwnTask(
        id="blog-06-draft-not-built",
        instruction=(
            "Create a draft post titled 'Hidden Draft', then build the site. The draft "
            "must not appear in the published output."
        ),
        checks=(
            Check("file_exists", {"path": "content/post/hidden-draft.md"}),
            Check("front_matter", {"path": "content/post/hidden-draft.md", "field": "draft", "equals": True}),
            Check("file_missing", {"path": "public/post/hidden-draft/index.html"}),
        ),
    ),
    OwnTask(
        id="blog-07-three-posts-one-tag",
        instruction=(
            "Create three published posts titled 'Alpha', 'Beta' and 'Gamma', each tagged "
            "'bench', then build the site."
        ),
        checks=(
            Check("file_exists", {"path": "content/post/alpha.md"}),
            Check("file_exists", {"path": "content/post/beta.md"}),
            Check("file_exists", {"path": "content/post/gamma.md"}),
            Check("tags", {"path": "content/post/gamma.md", "tags": ["bench"]}),
            Check("file_exists", {"path": "public/tags/bench/index.html"}),
        ),
    ),
    OwnTask(
        id="blog-08-clean-build",
        instruction=(
            "Rebuild the site so that public/ holds the current site and no leftover "
            "files from an earlier build."
        ),
        checks=(
            Check("file_exists", {"path": "public/index.html"}),
            Check("file_missing", {"path": STALE_PAGE}),
        ),
    ),
    OwnTask(
        id="blog-09-edit-title",
        instruction="Change the title of the existing post content/post/welcome.md to 'Welcome Back'.",
        checks=(
            Check("front_matter", {"path": "content/post/welcome.md", "field": "title", "equals": "Welcome Back"}),
            Check("body_contains", {"path": "content/post/welcome.md", "text": "The first post."}),
        ),
    ),
    OwnTask(
        id="blog-10-delete-post",
        instruction="Delete the post content/post/obsolete.md.",
        checks=(Check("file_missing", {"path": "content/post/obsolete.md"}),),
    ),
    OwnTask(
        id="blog-11-backdate-post",
        instruction="Create a published post titled 'Backdated' dated 2020-01-02.",
        checks=(
            Check("front_matter", {"path": "content/post/backdated.md", "field": "title", "equals": "Backdated"}),
            Check("front_matter", {"path": "content/post/backdated.md", "field": "date", "equals": "2020-01-02"}),
        ),
    ),
    OwnTask(
        id="blog-12-site-title",
        instruction="Change the site title in hugo.toml to 'Bench Notes' without disturbing the rest of the file.",
        checks=(
            Check("text_contains", {"path": "hugo.toml", "text": "title = 'Bench Notes'"}),
            Check("text_contains", {"path": "hugo.toml", "text": "[markup.goldmark.renderer]"}),
        ),
    ),
)


def load_tasks() -> list[OwnTask]:
    return list(TASKS)


def by_id(task_id: str) -> OwnTask:
    for task in TASKS:
        if task.id == task_id:
            return task
    raise KeyError(f"unknown own-cli task {task_id!r}")


def coverage() -> dict[str, Any]:
    """How much of the task set our own CLI can express at all."""

    expressible = [task for task in TASKS if not task.gap]
    return {
        "tasks": len(TASKS),
        "expressible": len(expressible),
        "gaps": {task.id: task.gap for task in TASKS if task.gap},
    }
