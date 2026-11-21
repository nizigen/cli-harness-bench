"""The four interfaces compared by the benchmark.

Each interface receives the same instruction, the same working directory and the
same budget, and may only touch the machine through its own channel:

``harness``
    The CLI-Anything binaries (``cli-anything-libreoffice`` / ``cli-anything-gimp``),
    plus the ability to read their own ``--help`` and ``SKILL.md``.
``script``
    A shell with Python, openpyxl / python-pptx / python-docx / Pillow, and
    ``soffice --headless``.
``gui``
    Screenshots and synthetic input through a Windows-MCP stdio server, with the
    tool whitelist from the proposal so the agent cannot simply write a script.
``hybrid``
    The harness first, the GUI when the harness cannot express the operation.

The interface object owns three things: the tool catalogue shown to the model,
the execution of one tool call, and the closing step that makes sure the files an
evaluator will read are on disk.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import os
import pathlib
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import BenchConfig, GuiConfig, HarnessConfig
from .normalize import find_soffice, soffice_env
from .tasks import MaterializedTask


class ToolError(RuntimeError):
    """A tool call failed in a way the agent should see."""


@dataclass(frozen=True)
class ToolSpec:
    """One callable tool, in the shape the OpenAI chat API expects."""

    name: str
    description: str
    parameters: dict[str, Any]

    def as_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolOutcome:
    """Result of executing one tool call.

    ``images`` carries raw PNG/JPEG bytes that came back from the tool (a GUI
    screenshot, for instance).  Text-only interfaces leave it empty.
    """

    content: str
    ok: bool = True
    meta: dict[str, Any] = field(default_factory=dict)
    images: list[tuple[str, bytes]] = field(default_factory=list)


class Interface(Protocol):
    """What the agent loop needs from an interface."""

    name: str

    def system_prompt(self, task: MaterializedTask) -> str: ...

    def tools(self) -> list[ToolSpec]: ...

    def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome: ...

    def finalize(self, task: MaterializedTask) -> dict[str, Any]: ...

    def close(self) -> None: ...


def _workspace_listing(task: MaterializedTask, limit: int = 40) -> str:
    """Name the files already sitting in the task directory.

    On OSWorld's desktop the input files are visible; here the agent only has
    the instruction, so an instruction like "add page numbers" would leave it
    guessing the file name. Listing the directory restores that context without
    telling the agent anything about the answer.
    """
    workdir = pathlib.Path(task.workdir)
    if not workdir.exists():
        return ""
    names: list[str] = []
    for path in sorted(workdir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(workdir).as_posix()
        if relative.startswith("_") or "/_" in relative:
            continue
        names.append(relative)
        if len(names) >= limit:
            break
    if not names:
        return ""
    listing = "\n".join(f"- {name}" for name in names)
    return f"\nThe directory already contains these files:\n{listing}\n"


def _kill_tree(process: subprocess.Popen) -> None:
    """Stop a child and every process it started (LibreOffice leaves one)."""

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


def _run(
    command: list[str],
    cwd: pathlib.Path,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> ToolOutcome:
    """Run one command, keeping output in files rather than pipes.

    A harness may itself launch LibreOffice, whose detached worker inherits the
    parent's stdio handles; capturing through pipes would then block forever
    after the immediate child exits.  Writing to temporary files and waiting on
    the process handle keeps the wait bounded.
    """

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=env,
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            return ToolOutcome(f"command not found: {command[0]} ({exc})", ok=False)
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            return ToolOutcome(f"command timed out after {timeout}s: {command[0]}", ok=False)
        out.seek(0)
        err.seek(0)
        output = out.read().decode("utf-8", errors="replace").strip()
        error = err.read().decode("utf-8", errors="replace").strip()

    if returncode != 0:
        body = "\n".join(part for part in (output, error) if part)
        return ToolOutcome(
            f"exit {returncode}\n{body}"[:4000],
            ok=False,
            meta={"exit_code": returncode},
        )
    return ToolOutcome(output[:8000] or "(no output)", ok=True)


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


class HarnessInterface:
    """Only the CLI-Anything binaries are available."""

    name = "harness"

    def __init__(self, config: BenchConfig, task: MaterializedTask) -> None:
        self.config = config
        self.harness: HarnessConfig = config.harness
        self.task = task
        self.binary = self.harness.binary_for(task.task.family)
        self.calls: list[dict[str, Any]] = []
        # The harness shells out to LibreOffice by name, so the project-local
        # tree only becomes visible to it through PATH.
        self.env = soffice_env(find_soffice(config.apps.soffice))

    # -- prompt ----------------------------------------------------------
    def system_prompt(self, task: MaterializedTask) -> str:
        skill = self.harness.skill_docs.get(task.task.family, "")
        skill_block = f"\n\nDocumentation for the CLI:\n{skill}\n" if skill else ""
        return (
            "You are an automation agent working on a Windows desktop machine.\n"
            f"The task refers to files inside this directory: {task.workdir}\n"
            f"{_workspace_listing(task)}\n"
            f"You may only use the command line tool `{self.binary}`. "
            "Run it with `--help` (and `--json` for machine-readable output) to "
            "discover its subcommands. You cannot use a shell, Python, or the GUI.\n"
            "When the task is finished, call `finish` with a short summary. "
            "If the task cannot be done with this tool, call `finish` with "
            'status "infeasible" and explain why.' + skill_block
        )

    # -- tools -----------------------------------------------------------
    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="run_harness",
                description=(
                    f"Run `{self.binary}` with the given arguments, for example "
                    f'["calc", "list-sheets", "--json"]. Working directory is the task directory.'
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Arguments passed to the CLI, without the binary name.",
                        }
                    },
                    "required": ["args"],
                },
            ),
            ToolSpec(
                name="finish",
                description="Report the final status of the task.",
                parameters={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["done", "infeasible"]},
                        "summary": {"type": "string"},
                    },
                    "required": ["status", "summary"],
                },
            ),
        ]

    # -- execution -------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if name == "finish":
            return ToolOutcome("recorded")
        if name != "run_harness":
            raise ToolError(f"unknown tool {name!r}")

        args = arguments.get("args")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ToolError("args must be a list of strings")

        command = [self.binary, *args]
        outcome = _run(command, self.task.workdir, env=self.env)
        self.calls.append({"command": command, "ok": outcome.ok})
        return outcome

    def finalize(self, task: MaterializedTask) -> dict[str, Any]:
        return {"interface": self.name, "binary": self.binary, "calls": self.calls}

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# script
# --------------------------------------------------------------------------

SCRIPT_PYTHON = "python"
SCRIPT_SOFFICE_HINT = (
    "LibreOffice is available as `soffice --headless` for conversions and recalculation."
)


class ScriptInterface:
    """A shell plus the Python document libraries."""

    name = "script"

    def __init__(self, config: BenchConfig, task: MaterializedTask) -> None:
        self.config = config
        self.task = task
        self.calls: list[dict[str, Any]] = []
        # ``soffice`` is promised in the prompt, so the project-local tree has
        # to be on PATH for the shell and Python calls that use it.
        self.env = soffice_env(find_soffice(config.apps.soffice))

    def system_prompt(self, task: MaterializedTask) -> str:
        return (
            "You are an automation agent working on a Windows desktop machine.\n"
            f"The task refers to files inside this directory: {task.workdir}\n"
            f"{_workspace_listing(task)}\n"
            "You may use a shell and Python. Available libraries include openpyxl, "
            "python-pptx, python-docx, Pillow, lxml, pandas and pdfplumber.\n"
            f"{SCRIPT_SOFFICE_HINT}\n"
            "You cannot use the GUI and you cannot use any pre-built application CLI.\n"
            "When the task is finished, call `finish` with a short summary. "
            "If the task cannot be done, call `finish` with status \"infeasible\"."
        )

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="run_shell",
                description=(
                    "Run one shell command in the task directory. "
                    "Use this for file inspection and for `soffice` conversions."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Command line to execute."}
                    },
                    "required": ["command"],
                },
            ),
            ToolSpec(
                name="run_python",
                description="Run a Python script in the task directory and return its output.",
                parameters={
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Python source to execute."}
                    },
                    "required": ["code"],
                },
            ),
            ToolSpec(
                name="finish",
                description="Report the final status of the task.",
                parameters={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["done", "infeasible"]},
                        "summary": {"type": "string"},
                    },
                    "required": ["status", "summary"],
                },
            ),
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if name == "finish":
            return ToolOutcome("recorded")
        if name == "run_shell":
            command = str(arguments.get("command", "")).strip()
            if not command:
                raise ToolError("command must not be empty")
            outcome = _run(["cmd", "/c", command], self.task.workdir, env=self.env)
            self.calls.append({"tool": "shell", "command": command, "ok": outcome.ok})
            return outcome
        if name == "run_python":
            code = str(arguments.get("code", ""))
            if not code.strip():
                raise ToolError("code must not be empty")
            script = self.task.workdir / "_agent_step.py"
            script.write_text(code, encoding="utf-8")
            outcome = _run([SCRIPT_PYTHON, str(script)], self.task.workdir, env=self.env)
            self.calls.append({"tool": "python", "bytes": len(code), "ok": outcome.ok})
            return outcome
        raise ToolError(f"unknown tool {name!r}")

    def finalize(self, task: MaterializedTask) -> dict[str, Any]:
        return {"interface": self.name, "calls": self.calls}

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# gui
# --------------------------------------------------------------------------


class _LoopThread:
    """One event loop, on its own thread, for the life of an MCP session.

    An MCP stdio session is bound to the loop that opened it: its task group and
    streams cannot be driven from a second ``anyio.run`` call.  Marshalling every
    call into a single long-lived loop is what keeps the session usable across
    many tool calls.
    """

    def __init__(self) -> None:
        import asyncio
        import threading

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True, name="gui-loop")
        self._thread.start()

    def _serve(self) -> None:
        import asyncio

        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro, timeout: float | None = None):
        import asyncio

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise

    def close(self) -> None:
        async def cancel_pending():
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        try:
            self.run(cancel_pending(), timeout=30)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=15)
            if self._thread.is_alive():
                raise ToolError("the GUI event loop did not stop")
            self._loop.close()


class GuiInterface:
    """Screenshots and synthetic input through a Windows-MCP stdio server."""

    name = "gui"

    def __init__(self, config: BenchConfig, task: MaterializedTask) -> None:
        self.config = config
        self.gui: GuiConfig = config.gui
        self.task = task
        self.calls: list[dict[str, Any]] = []
        self._loop: _LoopThread | None = None
        self._owner = None
        self._requests = None
        self._tools: dict[str, Any] = {}
        # AgentRunner sets this immediately before an agent tool call and clears
        # it afterwards.  ``None`` means a caller-owned operation (desktop setup
        # or save) and still gets the hard 60 s cap, never the agent's budget.
        self.deadline: float | None = None

    def clear_deadline(self) -> None:
        """Detach from the agent's wall clock before a setup/save operation."""
        self.deadline = None

    def _call_timeout(self) -> float:
        now = time.monotonic()
        deadline = self.deadline if self.deadline is not None else now + 60.0
        return max(0.001, min(60.0, deadline - now))

    # -- MCP plumbing ----------------------------------------------------
    async def _serve_session(self, ready: concurrent.futures.Future) -> None:
        active = None
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            command = self.gui.server_command()
            params = StdioServerParameters(command=command[0], args=list(command[1:]))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    self._requests = asyncio.Queue()
                    ready.set_result({t.name: t for t in listing.tools if t.name in self.gui.tools})
                    while True:
                        request = await self._requests.get()
                        if request is None:
                            break
                        name, arguments, timeout, active = request
                        try:
                            result = await asyncio.wait_for(
                                session.call_tool(name, arguments), timeout=timeout
                            )
                        except Exception as exc:
                            if not active.done():
                                active.set_exception(exc)
                        else:
                            if not active.done():
                                active.set_result(result)
                        active = None
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            if active is not None and not active.done():
                active.set_exception(ToolError(f"GUI session stopped: {type(exc).__name__}: {exc}"))
            raise

    def _ensure_session(self) -> None:
        if self._owner is not None:
            if self._owner.done():
                raise ToolError("the GUI session has stopped; close it before reconnecting")
            return
        self._loop = _LoopThread()
        ready = concurrent.futures.Future()
        self._owner = asyncio.run_coroutine_threadsafe(self._serve_session(ready), self._loop._loop)
        try:
            self._tools = ready.result(timeout=self.gui.start_timeout_s)
        except BaseException as exc:
            try:
                self.close()
            except ToolError as cleanup:
                raise ToolError(f"the GUI server did not start: {exc}; cleanup failed: {cleanup}") from exc
            raise ToolError(f"the GUI server did not start: {type(exc).__name__}: {exc}") from exc

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        self._ensure_session()
        assert self._loop is not None
        response = concurrent.futures.Future()
        timeout = self._call_timeout()
        self._loop._loop.call_soon_threadsafe(
            self._requests.put_nowait, (name, arguments, timeout, response)
        )
        try:
            result = response.result(timeout=timeout)
        except Exception as exc:
            self._owner.cancel()
            try:
                self.close()
            except ToolError as cleanup:
                raise ToolError(f"GUI tool {name!r} failed: {exc}; cleanup failed: {cleanup}") from exc
            raise ToolError(f"GUI tool {name!r} failed: {type(exc).__name__}: {exc}") from exc
        parts: list[str] = []
        images: list[tuple[str, bytes]] = []
        for item in result.content:
            text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
                continue
            data = getattr(item, "data", None)
            if data:
                # A screenshot arrives as a base64 image block.  Dropping it
                # would leave the model blind, so keep the bytes.
                try:
                    raw = base64.b64decode(data)
                except (ValueError, TypeError):
                    parts.append(f"<{getattr(item, 'type', 'content')}: undecodable>")
                    continue
                mime = str(getattr(item, "mimeType", "") or "image/png")
                images.append((mime, raw))
                parts.append(f"<image {mime}, {len(raw)} bytes>")
                continue
            parts.append(f"<{getattr(item, 'type', 'content')}>")
        body = "\n".join(parts) if parts else "(no content)"
        return ToolOutcome(
            body[:8000],
            ok=not getattr(result, "isError", False),
            images=images,
        )

    # -- prompt and tools ------------------------------------------------
    def system_prompt(self, task: MaterializedTask) -> str:
        allowed = ", ".join(self.gui.tools)
        return (
            "You are a computer-use agent driving a real Windows desktop.\n"
            f"The task refers to files inside this directory: {task.workdir}\n"
            f"{_workspace_listing(task)}\n"
            f"You may only use these GUI tools: {allowed}. "
            "Take a screenshot first, then act on what you see. "
            "You cannot use a shell, Python, or any application CLI.\n"
            "When the task is finished, call `finish` with a short summary. "
            "If the task cannot be done, call `finish` with status \"infeasible\"."
        )

    def tools(self) -> list[ToolSpec]:
        self._ensure_session()
        specs: list[ToolSpec] = []
        for name, tool in sorted(self._tools.items()):
            schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
            specs.append(
                ToolSpec(
                    name=name,
                    description=str(getattr(tool, "description", "") or name),
                    parameters=schema,
                )
            )
        specs.append(
            ToolSpec(
                name="finish",
                description="Report the final status of the task.",
                parameters={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["done", "infeasible"]},
                        "summary": {"type": "string"},
                    },
                    "required": ["status", "summary"],
                },
            )
        )
        return specs

    def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if name == "finish":
            return ToolOutcome("recorded")
        if name not in self.gui.tools:
            raise ToolError(f"tool {name!r} is not on the allowed list")
        outcome = self._call_tool(name, arguments)
        self.calls.append({"tool": name, "ok": outcome.ok})
        return outcome

    def finalize(self, task: MaterializedTask) -> dict[str, Any]:
        return {"interface": self.name, "calls": self.calls}

    def close(self) -> None:
        if self._loop is None:
            return
        loop, owner = self._loop, self._owner
        error = None
        try:
            if self._requests is not None and not owner.done():
                loop._loop.call_soon_threadsafe(self._requests.put_nowait, None)
            else:
                owner.cancel()
            try:
                owner.result(timeout=30)
            except concurrent.futures.CancelledError:
                pass
            except Exception as exc:
                error = exc
        finally:
            try:
                loop.close()
            except Exception as exc:
                error = error or exc
            self._loop = None
            self._owner = None
            self._requests = None
            self._tools = {}
        if error is not None:
            raise ToolError(f"the GUI session did not close cleanly: {type(error).__name__}: {error}") from error


# --------------------------------------------------------------------------
# hybrid
# --------------------------------------------------------------------------


class HybridInterface:
    """The harness first, the GUI when the harness cannot express the operation."""

    name = "hybrid"

    def __init__(self, config: BenchConfig, task: MaterializedTask) -> None:
        self.harness = HarnessInterface(config, task)
        self.gui = GuiInterface(config, task)
        self.task = task

    @property
    def deadline(self) -> float | None:
        return self.gui.deadline

    @deadline.setter
    def deadline(self, value: float | None) -> None:
        self.gui.deadline = value

    def clear_deadline(self) -> None:
        self.gui.clear_deadline()

    def system_prompt(self, task: MaterializedTask) -> str:
        return (
            self.harness.system_prompt(task)
            + "\n\nIf the CLI cannot express an operation, you may fall back to the GUI "
            "tools. Prefer the CLI: every GUI action is slower and less reliable."
        )

    def tools(self) -> list[ToolSpec]:
        specs = self.harness.tools()[:-1]  # drop finish, re-added once below
        gui_specs = [spec for spec in self.gui.tools() if spec.name != "finish"]
        finish = [spec for spec in self.harness.tools() if spec.name == "finish"]
        return specs + gui_specs + finish

    def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if name == "run_harness" or name == "finish":
            return self.harness.call(name, arguments)
        return self.gui.call(name, arguments)

    def finalize(self, task: MaterializedTask) -> dict[str, Any]:
        return {
            "interface": self.name,
            "harness": self.harness.finalize(task),
            "gui": self.gui.finalize(task),
        }

    def close(self) -> None:
        self.gui.close()


INTERFACES = {
    "harness": HarnessInterface,
    "script": ScriptInterface,
    "gui": GuiInterface,
    "hybrid": HybridInterface,
}


def build_interface(name: str, config: BenchConfig, task: MaterializedTask) -> Interface:
    try:
        factory = INTERFACES[name]
    except KeyError:
        raise KeyError(f"unknown interface {name!r}; choose from {sorted(INTERFACES)}")
    return factory(config, task)
