"""Private Windows LibreOffice GUI sessions with explicit save evidence.

The context manager prepares and closes; saving is always explicit via finish.
Only the spawned Job Object tree and our fresh temporary profile are cleaned.
"""
from __future__ import annotations

import ctypes
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
from typing import Any

from .config import BenchConfig
from .normalize import collect_result_paths, find_soffice
from .profile import prepare_profile
from .setup_actions import apply_setup, supports_setup
from .tasks import MaterializedTask

class DesktopError(RuntimeError):
    """Refused or failed desktop setup, with machine-readable metadata."""

    def __init__(self, message: str, metadata: dict[str, Any]):
        super().__init__(message)
        self.metadata = metadata

#: Launch commands that only choose a LibreOffice module; they are declarative.
SUPPORTED_LAUNCHES = (
    ["libreoffice", "--impress"],
    ["libreoffice", "--calc"],
    ["libreoffice", "--writer"],
)
MAX_SETUP_SLEEP_S = 30.0

def setup_plan(task: MaterializedTask) -> tuple[list[str], list[dict[str, Any]]]:
    """Map staged opens/launches; reject executable UI preconditions explicitly."""
    arguments: list[str] = []
    unsupported: list[dict[str, Any]] = []
    workdir = task.workdir.resolve()
    for index, action in enumerate(task.task.config):
        kind, params = action.get("type"), action.get("parameters", {})
        if kind == "download":
            continue  # MaterializedTask already staged these.
        if kind == "open" and isinstance(params.get("path"), str):
            path = task.resolve(params["path"]).resolve()
            if path.is_relative_to(workdir) and path.is_file():
                arguments.append(str(path))
                continue
        if kind == "launch" and params.get("command") in SUPPORTED_LAUNCHES:
            arguments.extend(params["command"][1:])
            continue
        if (kind == "sleep"
                and isinstance(params.get("seconds"), (int, float))
                and 0 <= params["seconds"] <= MAX_SETUP_SLEEP_S):
            continue  # Declarative settling delay; no UI state is implied.
        if kind == "execute" and supports_setup(action):
            continue  # Reviewed precondition replayed after the GUI is ready.
        unsupported.append({"index": index, "type": kind, "parameters": params})
    if not arguments:
        unsupported.append({"index": None, "type": "missing_open_or_launch", "parameters": {}})
    return arguments, unsupported

def _win32() -> tuple[Any, Any]:
    from ctypes import wintypes as w
    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD),
                    ("SchedulingClass", w.DWORD)]
    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in (
        ("CreateJobObjectW", [w.LPVOID, w.LPCWSTR], w.HANDLE),
        ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
        ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
        ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
        ("CloseHandle", [w.HANDLE], w.BOOL),
        ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        ("IsProcessInJob", [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        ("GetCurrentThreadId", [], w.DWORD),
    ):
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    kernel.ExtendedLimits = ExtendedLimits
    user = ctypes.WinDLL("user32", use_last_error=True)
    def sig(name, args, result=None):
        function = getattr(user, name)
        function.argtypes = args
        if result is not None:
            function.restype = result
    sig("GetWindowThreadProcessId", [w.HWND, ctypes.POINTER(w.DWORD)])
    sig("IsWindowVisible", [w.HWND]); sig("IsWindowEnabled", [w.HWND])
    sig("GetWindowTextW", [w.HWND, w.LPWSTR, ctypes.c_int])
    sig("GetClassNameW", [w.HWND, w.LPWSTR, ctypes.c_int])
    sig("GetWindow", [w.HWND, w.UINT], w.HWND)
    sig("SetForegroundWindow", [w.HWND], w.BOOL); sig("GetForegroundWindow", [], w.HWND)
    sig("ShowWindow", [w.HWND, ctypes.c_int], w.BOOL)
    sig("BringWindowToTop", [w.HWND], w.BOOL)
    sig("AttachThreadInput", [w.DWORD, w.DWORD, w.BOOL], w.BOOL)
    sig("PeekMessageW", [w.LPVOID, w.HWND, w.UINT, w.UINT, w.UINT], w.BOOL)
    sig("PostMessageW", [w.HWND, w.UINT, w.WPARAM, w.LPARAM])
    return kernel, user

class _WindowsSession:
    """Suspend before Job assignment so detached soffice.bin cannot escape."""

    def __init__(self, command: list[str], cwd: pathlib.Path):
        self.kernel, self.user = _win32()
        self.job = self.kernel.CreateJobObjectW(None, None)
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        self.process: subprocess.Popen | None = None
        try:
            limits = self.kernel.ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(
                    self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.process = subprocess.Popen(
                command, cwd=str(cwd), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x00000004 | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            from ctypes import wintypes as w
            handle = w.HANDLE(int(self.process._handle))
            if not self.kernel.AssignProcessToJobObject(self.job, handle):
                error = ctypes.WinError(ctypes.get_last_error())
                self.process.kill()  # Still suspended: it cannot have children.
                self.process.wait(timeout=10)
                raise error
            resume = ctypes.WinDLL("ntdll").NtResumeProcess
            resume.argtypes, resume.restype = [w.HANDLE], ctypes.c_long
            if resume(handle) != 0:
                raise OSError("could not resume the owned LibreOffice process")
        except BaseException:
            try:
                self.close()
            except Exception:
                pass  # The original startup error is the useful one.
            raise

    def owns(self, pid: int) -> bool:
        from ctypes import wintypes as w
        handle = self.kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            inside = w.BOOL()
            queried = self.kernel.IsProcessInJob(handle, self.job, ctypes.byref(inside))
            return bool(queried and inside.value)
        finally:
            self.kernel.CloseHandle(handle)

    def windows(self) -> list[dict[str, Any]]:
        """Visible top-level windows whose process belongs to this session."""
        from ctypes import wintypes as w
        found: list[dict[str, Any]] = []
        callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        self.user.EnumWindows.argtypes = [callback_type, w.LPARAM]
        @callback_type
        def visit(hwnd, _):
            pid = w.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if self.user.IsWindowVisible(hwnd) and self.owns(pid.value):
                title, name = ctypes.create_unicode_buffer(512), ctypes.create_unicode_buffer(128)
                self.user.GetWindowTextW(hwnd, title, 512)
                self.user.GetClassNameW(hwnd, name, 128)
                found.append({"hwnd": hwnd, "title": title.value, "class": name.value,
                              "dialog": bool(self.user.GetWindow(hwnd, 4)) or name.value == "#32770",
                              "enabled": bool(self.user.IsWindowEnabled(hwnd))})
            return True
        if not self.user.EnumWindows(visit, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return found

    def focus(self, hwnd: int) -> bool:
        """Activate one owned window despite Windows' foreground lock.

        Ownership is proven first; only then do we synchronise the input queue
        with the current foreground thread, which is what lets an owning
        process legally raise its own window. Detaching always happens.
        """
        from ctypes import wintypes as w
        pid = w.DWORD()
        thread = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not thread or not pid.value or not self.owns(pid.value):
            return False
        foreground = self.user.GetForegroundWindow()
        foreground_thread = 0
        if foreground:
            foreground_pid = w.DWORD()
            foreground_thread = self.user.GetWindowThreadProcessId(
                foreground, ctypes.byref(foreground_pid))
        current = self.kernel.GetCurrentThreadId()
        # AttachThreadInput rejects a caller without a message queue; this
        # non-destructive PeekMessage creates one for this thread.
        self.user.PeekMessageW(ctypes.create_string_buffer(64), None, 0, 0, 0)
        attached = []
        try:
            for other in (foreground_thread, thread):
                if other and other != current and other not in attached:
                    if self.user.AttachThreadInput(current, other, True):
                        attached.append(other)
            self.user.ShowWindow(hwnd, 9)  # SW_RESTORE
            self.user.BringWindowToTop(hwnd)
            self.user.SetForegroundWindow(hwnd)
            return self.user.GetForegroundWindow() == hwnd
        finally:
            for other in attached:
                self.user.AttachThreadInput(current, other, False)

    def close_window(self, hwnd: int) -> None:
        """Ask one owned window to close; never force-kill a live session."""
        if not self.user.PostMessageW(hwnd, 0x0010, 0, 0):  # WM_CLOSE
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if not self.job:
            return
        job, self.job = self.job, None
        try:
            if not self.kernel.TerminateJobObject(job, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            if self.process:
                self.process.wait(timeout=10)
        finally:
            # KILL_ON_JOB_CLOSE means even this last-resort path kills the tree,
            # including when a worker timeout tears the runner down without us.
            self.kernel.CloseHandle(job)

def _snapshot(paths: list[pathlib.Path]) -> dict[str, tuple[bool, int, int]]:
    state = {}
    for path in paths:
        try:
            stat = path.stat()
            state[str(path)] = (True, stat.st_mtime_ns, stat.st_size)
        except OSError:
            state[str(path)] = (False, 0, 0)
    return state

def _blocked(window: dict[str, Any]) -> bool:
    """Modal dialog or disabled frame: report it, never click it away."""
    return bool(window["dialog"]) or not window["enabled"]

class DesktopTask:
    """Caller owns gui; finish reports save_requested/dialog/error plus verified.
    verified means a declared nonempty result changed and had two stable stats;
    it is filesystem evidence, not a claim about semantic document correctness.
    """

    def __init__(self, config: BenchConfig, task: MaterializedTask, gui: Any):
        self.config, self.task, self.gui = config, task, gui
        self.profile: pathlib.Path | None = None
        self.session: _WindowsSession | None = None
        self.metadata: dict[str, Any] = {"status": "new", "unsupported_setup": []}
        self.baseline: dict[str, tuple[bool, int, int]] = {}
        self.opened: list[str] = []

    def prepare(self) -> dict[str, Any]:
        if self.metadata["status"] != "new":
            raise DesktopError("desktop session cannot be prepared twice", self.metadata)
        arguments, unsupported = setup_plan(self.task)
        if self.task.task.family != "libreoffice":
            unsupported.append({"index": None, "type": "unsupported_app",
                                "parameters": {"app": self.task.task.app}})
        self.metadata["unsupported_setup"] = unsupported
        if unsupported:
            self.metadata["status"] = "unsupported_setup"
            raise DesktopError("desktop setup is not supported", self.metadata)
        try:
            if os.name != "nt":
                raise OSError("desktop initialization requires Windows")
            binary = find_soffice(self.config.apps.soffice)
            if not binary or pathlib.Path(binary).suffix.lower() != ".com":
                raise OSError("soffice.com is required for owned GUI startup")
            self.profile = pathlib.Path(tempfile.mkdtemp(prefix="harness-desktop-"))
            self.metadata["profile"] = str(self.profile)
            deadline = time.monotonic() + self.config.gui.start_timeout_s
            prepare_profile(pathlib.Path(binary), self.profile, _WindowsSession,
                            max(0.01, deadline - time.monotonic()), self.metadata)
            command = [binary, f"-env:UserInstallation={self.profile.as_uri()}",
                       "--norestore", "--nofirststartwizard", *arguments]
            self.metadata.update(command=command)
            self.baseline = _snapshot(collect_result_paths(self.task))
            self.session = _WindowsSession(command, self.task.workdir)
            self.opened = [arg for arg in arguments if pathlib.Path(arg).is_file()]
            delay = sum(float(a.get("parameters", {}).get("seconds", 0))
                        for a in self.task.task.config if a.get("type") == "sleep")
            if delay:
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            while time.monotonic() < deadline:
                windows = self.session.windows()
                if any(window["dialog"] for window in windows):
                    self.metadata["windows"] = windows
                    raise OSError("startup dialog appeared")
                ready = [w for w in windows if w["class"] == "SALFRAME" and w["enabled"]]
                if len(ready) == 1 and self._show(ready[0]):
                    self.metadata.update(status="ready", windows=windows)
                    self.metadata["setup_actions"] = apply_setup(self.task, self.gui)
                    return self.metadata
                time.sleep(0.2)
            self.metadata["windows"] = self.session.windows()
            raise TimeoutError("LibreOffice document was not foreground within deadline")
        except Exception as exc:
            self.metadata.update(status="prepare_error", error=f"{type(exc).__name__}: {exc}")
            self.close()
            raise DesktopError(str(exc), self.metadata) from exc

    def finish(self, timeout_s: float = 10.0) -> dict[str, Any]:
        """Ask the open document to save; report, never dismiss, any dialog."""
        save: dict[str, Any] = {"status": "error", "ok": False, "verified": False}
        self.metadata["save"] = save
        paths = collect_result_paths(self.task)
        try:
            if not self.session or self.metadata["status"] != "ready":
                raise RuntimeError("desktop is not ready")
            windows = self.session.windows()
            if any(_blocked(window) for window in windows):
                save.update(status="dialog", windows=windows)
                return save
            frames = [window for window in windows if window["class"] == "SALFRAME"]
            if len(frames) != 1 or not self.session.focus(frames[0]["hwnd"]):
                raise RuntimeError("cannot safely focus exactly one owned document")
            before = _snapshot(paths)
            outcome = self.gui.call("Shortcut", {"shortcut": "ctrl+s"})
            save.update(shortcut_ok=outcome.ok, detail=outcome.content[:400])
            if not outcome.ok:
                return save
            deadline = time.monotonic() + timeout_s
            previous = before
            while True:
                windows = self.session.windows()
                if any(_blocked(window) for window in windows):
                    save.update(status="dialog", windows=windows)
                    return save
                if not any(window["class"] == "SALFRAME" for window in windows):
                    raise RuntimeError("document closed during save")
                after = _snapshot(paths)
                # Compare with the staged baseline: an agent that saved during
                # its turn is verified too, and only stable nonempty bytes count.
                changed = [p for p, s in after.items()
                           if s[0] and s[2] > 0 and s != self.baseline.get(p) and s == previous[p]]
                if changed or time.monotonic() >= deadline:
                    save.update(status="save_requested", ok=True, verified=bool(changed),
                                changed=changed, unchanged=not changed,
                                since_request=[p for p, s in after.items()
                                               if s != before[p]],
                                files=[{"path": path, "exists": s[0], "bytes": s[2]}
                                       for path, s in after.items()])
                    return save
                previous = after
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        except Exception as exc:
            save["error"] = f"{type(exc).__name__}: {exc}"
            return save

    def _show(self, frame: dict[str, Any]) -> bool:
        """Focus our document and prove its title matches the single open file."""
        if len(self.opened) == 1:
            stem = pathlib.Path(self.opened[0]).stem.lower()
            if stem not in frame["title"].lower():
                return False
        return self.session.focus(frame["hwnd"])

    def close(self) -> None:
        try:
            if self.session:
                self.session.close()
                self.session = None
        except Exception as exc:
            self.metadata["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if self.profile and not self.session:
                try:
                    shutil.rmtree(self.profile)
                    self.profile = None
                except OSError as exc:
                    self.metadata["profile_cleanup_error"] = str(exc)
            self.metadata["closed"] = True

    def __enter__(self) -> "DesktopTask":
        self.prepare()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
