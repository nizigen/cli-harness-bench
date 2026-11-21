"""Offline lifecycle checks: never start LibreOffice, Windows-MCP or a model."""
import ctypes
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import replace
from types import SimpleNamespace

import pytest

from harness_bench import desktop as d
from harness_bench import profile as p
from harness_bench.config import BenchConfig, GuiConfig
from harness_bench.tasks import MaterializedTask, Task, local_path_for


def fake_profile(binary, target, factory, timeout_s, metadata):
    user = target / "user"
    user.mkdir()
    registry = user / "registrymodifications.xcu"
    registry.write_text(
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry"/>', encoding="utf-8")
    p._merge_flags(registry)
    metadata.update(cache_hit=True, registry_sha256="test", baseline_flags=p.FLAGS.copy())


FRAME = {"hwnd": 10, "class": "SALFRAME", "title": "test", "dialog": False, "enabled": True}
REJECTED = {
    "66399b0d-8fda-4618-95c4-bfc6191617e9",
    "6ada715d-3aae-4a32-a6a7-429b2e43fb93",
    "21760ecb-8f62-40d2-8d85-0cee5725cb72",
    "550ce7e7-747b-495f-b122-acdc4d0b8e54",
    "a669ef01-ded5-4099-9ea9-25e99b569840",
}


def stage(task, workdir):
    workdir.mkdir(parents=True, exist_ok=True)
    mapping = {}
    for entry in task.inputs:
        path = local_path_for(entry.vm_path, workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"staged")
        mapping[entry.vm_path] = path
    return MaterializedTask(task, workdir, task.instruction, mapping)


@pytest.fixture
def materialized(tmp_path):
    path = "/home/user/test.odt"
    task = Task("test", "libreoffice_writer", "edit", [
        {"type": "download", "parameters": {"files": [{"path": path, "url": "offline"}]}},
        {"type": "open", "parameters": {"path": path}},
    ], {"result": {"type": "vm_file", "path": path}})
    return stage(task, tmp_path)


class Session:
    instances = []
    shown = [FRAME]
    focus_ok = True

    def __init__(self, command, cwd):
        self.command, self.cwd, self.closed = command, cwd, False
        self.process = SimpleNamespace(wait=lambda timeout=None: 0)
        self.instances.append(self)

    def windows(self):
        return self.shown

    def focus(self, hwnd):
        assert hwnd == FRAME["hwnd"]
        return self.focus_ok

    def close(self):
        self.closed = True


@pytest.fixture
def rig(monkeypatch, materialized):
    Session.instances = []
    monkeypatch.setattr(Session, "shown", [FRAME])
    monkeypatch.setattr(Session, "focus_ok", True)
    monkeypatch.setattr(d, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(d, "_WindowsSession", Session)
    monkeypatch.setattr(d, "find_soffice", lambda _: "C:/LibreOffice/soffice.com")
    monkeypatch.setattr(d, "prepare_profile", fake_profile)
    calls = []

    def call(name, args):
        calls.append((name, args))
        return SimpleNamespace(ok=True, content="accepted")

    gui = SimpleNamespace(call=call)
    obj = d.DesktopTask(BenchConfig(), materialized, gui)
    return obj, calls


def test_audited_adopted_setup(all_tasks, tmp_path):
    tasks = [t for t in all_tasks if t.adopted and t.family == "libreoffice"]
    assert len(tasks) == 113
    rejected = {}
    for task in tasks:
        _, unsupported = d.setup_plan(stage(task, tmp_path / task.id))
        if unsupported:
            rejected[task.id] = unsupported
    assert rejected == {}
    execute_tasks = {t.id for t in tasks if any(a["type"] == "execute" for a in t.config)}
    assert execute_tasks == REJECTED


def test_launch_without_input(all_tasks, tmp_path):
    task = next(t for t in all_tasks if t.id == "2cd43775-7085-45d8-89fa-9e35c0a915cf")
    args, unsupported = d.setup_plan(stage(task, tmp_path))
    assert args == ["--impress"]
    assert unsupported == []


class FakeUser:
    """Minimal user32 stand-in for the focus() contract."""

    def __init__(self, pid=42, thread=200, foreground=0, foreground_thread=0,
                 owned=True, activate=True, attach_ok=True, fail=None):
        self.pid, self.thread = pid, thread
        self.foreground, self.foreground_thread = foreground, foreground_thread
        self.owned, self.activate, self.attach_ok, self.fail = owned, activate, attach_ok, fail
        self.calls = []

    def GetWindowThreadProcessId(self, hwnd, ptr):
        is_foreground = bool(hwnd) and hwnd == self.foreground
        ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint))[0] = 0 if is_foreground else self.pid
        return self.foreground_thread if is_foreground else self.thread

    def GetForegroundWindow(self):
        return self.foreground

    def PeekMessageW(self, message, hwnd, first, last, remove):
        self.calls.append(("peek",))
        return 0

    def AttachThreadInput(self, a, b, flag):
        self.calls.append(("attach", a, b, bool(flag)))
        return self.attach_ok

    def ShowWindow(self, hwnd, command):
        if self.fail == "show":
            raise RuntimeError("show failed")
        self.calls.append(("show", hwnd, command))
        return 1

    def BringWindowToTop(self, hwnd):
        self.calls.append(("bring", hwnd))
        return 1

    def SetForegroundWindow(self, hwnd):
        if self.fail == "set":
            raise RuntimeError("set failed")
        self.calls.append(("set", hwnd))
        if self.activate:
            self.foreground = hwnd
        return 1


class FakeKernel:
    def __init__(self, owned):
        self.owned = owned
        self.current = 100

    def GetCurrentThreadId(self):
        return self.current

    def OpenProcess(self, access, inherit, pid):
        return 7 if pid in self.owned else 0

    def IsProcessInJob(self, handle, job, inside):
        ctypes.cast(inside, ctypes.POINTER(ctypes.c_int))[0] = 1
        return 1

    def CloseHandle(self, handle):
        return 1


def make_session(user, owned=None):
    session = object.__new__(d._WindowsSession)
    session.kernel, session.user, session.job = FakeKernel(owned or {user.pid}), user, 1
    return session


def test_focus_attaches_detaches_and_activates():
    user = FakeUser(thread=200, foreground=9, foreground_thread=300)
    session = make_session(user)
    assert session.focus(10) is True
    assert user.calls == [
        ("peek",), ("attach", 100, 300, True), ("attach", 100, 200, True),
        ("show", 10, 9), ("bring", 10), ("set", 10),
        ("attach", 100, 300, False), ("attach", 100, 200, False)]


def test_focus_refuses_foreign_window_without_touching_input_queue():
    user = FakeUser(pid=42)
    session = make_session(user, owned={99})
    assert session.focus(10) is False
    assert user.calls == []


def test_focus_detaches_even_when_activation_raises():
    user = FakeUser(foreground=9, foreground_thread=300, fail="show")
    session = make_session(user)
    with pytest.raises(RuntimeError, match="show failed"):
        session.focus(10)
    assert user.calls[0] == ("peek",)
    assert [c for c in user.calls if c[0] == "attach" and c[3]] == [
        ("attach", 100, 300, True), ("attach", 100, 200, True)]
    assert [c for c in user.calls if c[0] == "attach" and not c[3]] == [
        ("attach", 100, 300, False), ("attach", 100, 200, False)]


def test_focus_reports_false_when_windows_keeps_another_foreground():
    user = FakeUser(foreground=9, foreground_thread=300, activate=False)
    session = make_session(user)
    assert session.focus(10) is False
    assert user.calls[:1] == [("peek",)]
    assert user.calls[-2:] == [("attach", 100, 300, False), ("attach", 100, 200, False)]


def test_focus_skips_attach_when_already_the_foreground_thread():
    user = FakeUser(thread=100, foreground=9, foreground_thread=100)
    session = make_session(user)
    assert session.focus(10) is True
    assert user.calls == [("peek",), ("show", 10, 9), ("bring", 10), ("set", 10)]


def test_reviewed_execute_setup_is_replayed_and_recorded(rig, monkeypatch):
    obj, calls = rig
    reviewed = {"type": "execute", "parameters": {"command": ["python", "-c", "print(1)"]}}
    obj.task = replace(obj.task, task=replace(
        obj.task.task, config=[*obj.task.task.config, reviewed]))
    seen = []

    def apply(task, gui):
        seen.append((task, gui))
        return [{"action": "click", "ok": True}]

    monkeypatch.setattr(d, "supports_setup", lambda action: action is reviewed)
    monkeypatch.setattr(d, "apply_setup", apply)
    obj.prepare()
    try:
        assert obj.metadata["status"] == "ready"
        assert obj.metadata["setup_actions"] == [{"action": "click", "ok": True}]
        assert seen == [(obj.task, obj.gui)]
    finally:
        obj.close()


def test_unknown_execute_still_rejected_before_start(rig, monkeypatch):
    obj, _ = rig
    obj.task = replace(obj.task, task=replace(obj.task.task, config=[
        *obj.task.task.config,
        {"type": "execute", "parameters": {"command": ["python", "-c", "import os"]}}]))
    monkeypatch.setattr(d, "supports_setup", lambda action: False)
    with pytest.raises(d.DesktopError) as error:
        obj.prepare()
    assert error.value.metadata["status"] == "unsupported_setup"
    assert error.value.metadata["unsupported_setup"][0]["type"] == "execute"
    assert not Session.instances


@pytest.mark.parametrize("kind,params", [
    ("execute", {"command": ["python", "-c", "raise SystemExit()"]}),
    ("launch", {"command": ["libreoffice", "--headless"]}),
    ("activate_window", {"window_name": "anything"}),
    ("sleep", {"seconds": 31}),
    ("open", {"path": "../outside.odt"}),
    ("open", {"path": "/home/user/absent.odt"}),
])
def test_unsupported_refused_before_start(rig, kind, params):
    obj, _ = rig
    task = replace(obj.task.task, config=[*obj.task.task.config, {"type": kind, "parameters": params}])
    obj.task = replace(obj.task, task=task)
    with pytest.raises(d.DesktopError) as error:
        obj.prepare()
    assert error.value.metadata["status"] == "unsupported_setup"
    assert error.value.metadata["unsupported_setup"][0]["type"] == kind
    assert not Session.instances
    assert obj.profile is None


def test_private_profile_and_context_cleanup(rig):
    obj, calls = rig
    with pytest.raises(ValueError), obj:
        profile = obj.profile
        active = Session.instances[-1]
        assert not active.closed
        assert obj.metadata["cache_hit"]
        command = active.command
        assert command[0].endswith("soffice.com")
        assert f"-env:UserInstallation={profile.as_uri()}" in command
        assert not any("headless" in arg for arg in command)
        assert command[-1] == str(obj.task.resolve("/home/user/test.odt").resolve())
        xml = ET.parse(profile / "user" / "registrymodifications.xcu")
        ns = "{http://openoffice.org/2001/registry}"
        props = {}
        assert len(xml.getroot()) == 3
        for item in xml.getroot():
            props.setdefault(item.attrib[ns + "path"], {}).update({
                p.attrib[ns + "name"]: p.findtext("value") for p in item})
        assert props["/org.openoffice.Office.Common/Misc"] == {
            "FirstRun": "false", "ShowTipOfTheDay": "false"}
        assert props["/org.openoffice.Office.Common/Save/Document"] == {
            "WarnAlienFormat": "false"}
        assert obj.metadata["baseline_flags"] == {
            "FirstRun": False, "ShowTipOfTheDay": False, "WarnAlienFormat": False}
        raise ValueError("agent failed")
    assert Session.instances[-1].closed
    assert not profile.exists()
    assert calls == []
    obj.close()


def test_profile_failure_never_starts_gui(rig, monkeypatch):
    obj, _ = rig

    def failed(*args):
        raise OSError("invalid profile cache")

    monkeypatch.setattr(d, "prepare_profile", failed)
    with pytest.raises(d.DesktopError, match="invalid profile cache"):
        obj.prepare()
    assert obj.metadata["status"] == "prepare_error"
    assert not Session.instances
    assert "command" not in obj.metadata
    assert obj.profile is None


def test_fresh_profile_per_task(rig):
    obj, _ = rig
    obj.prepare()
    first = obj.profile
    second = d.DesktopTask(obj.config, obj.task, obj.gui)
    try:
        second.prepare()
        assert second.profile != first
    finally:
        obj.close()
        second.close()


def test_startup_timeout_cleans_profile(rig):
    obj, _ = rig
    obj.config = replace(obj.config, gui=GuiConfig(start_timeout_s=0))
    with pytest.raises(d.DesktopError, match="deadline"):
        obj.prepare()
    assert obj.metadata["status"] == "prepare_error"
    assert Session.instances[-1].closed
    assert obj.profile is None


def test_startup_dialog_is_reported_and_cleaned(rig, monkeypatch):
    obj, _ = rig
    monkeypatch.setattr(Session, "shown", [dict(FRAME, dialog=True, title="Import error")])
    with pytest.raises(d.DesktopError, match="dialog"):
        obj.prepare()
    assert obj.metadata["windows"][0]["title"] == "Import error"
    assert Session.instances[-1].closed


def test_startup_requires_our_document_foreground(rig, monkeypatch):
    obj, _ = rig
    monkeypatch.setattr(Session, "shown", [dict(FRAME, title="Some other document")])
    obj.config = replace(obj.config, gui=GuiConfig(start_timeout_s=0.3))
    with pytest.raises(d.DesktopError, match="foreground"):
        obj.prepare()
    assert Session.instances[-1].closed


def test_save_verified_against_session_baseline(rig):
    obj, _ = rig
    target = obj.task.resolve("/home/user/test.odt")
    with obj:
        target.write_bytes(b"saved by the agent during its own turn")
        result = obj.finish(timeout_s=0)
        assert result["verified"]
        assert result["changed"] == [str(target)]
        assert result["since_request"] == []


def test_save_same_gui_unmodified_is_unverified(rig):
    obj, calls = rig
    with obj:
        result = obj.finish(timeout_s=0)
        assert result == obj.metadata["save"]
        assert result["status"] == "save_requested"
        assert result["ok"] and not result["verified"]
        assert calls == [("Shortcut", {"shortcut": "ctrl+s"})]


def test_save_stable_changed_result(rig):
    obj, _ = rig
    target = obj.task.resolve("/home/user/test.odt")

    def call(name, args):
        target.write_bytes(b"saved new contents")
        return SimpleNamespace(ok=True, content="saved")

    obj.gui.call = call
    with obj:
        result = obj.finish(timeout_s=1)
        assert result["verified"]
        assert result["changed"] == [str(target)]


def test_disappeared_file_does_not_verify_save(rig):
    obj, _ = rig
    target = obj.task.resolve("/home/user/test.odt")

    def call(*_):
        target.unlink()
        return SimpleNamespace(ok=True, content="accepted")

    obj.gui.call = call
    with obj:
        result = obj.finish(timeout_s=0)
        assert not result["verified"]
        assert result["files"][0]["exists"] is False


@pytest.mark.parametrize("windows", [[dict(FRAME, dialog=True)], [dict(FRAME, enabled=False)]])
def test_save_existing_dialog_never_sends_keys(rig, monkeypatch, windows):
    obj, calls = rig
    with obj:
        monkeypatch.setattr(Session, "shown", windows)
        result = obj.finish(timeout_s=0)
        assert result["status"] == "dialog"
        assert not result["ok"]
        assert calls == []


def test_save_as_dialog_after_shortcut(rig, monkeypatch):
    obj, _ = rig

    def call(*_):
        monkeypatch.setattr(Session, "shown", [dict(FRAME, dialog=True, title="Save As")])
        return SimpleNamespace(ok=True, content="accepted")

    obj.gui.call = call
    with obj:
        result = obj.finish(timeout_s=0)
        assert result["status"] == "dialog"
        assert result["windows"][0]["title"] == "Save As"
        assert not result["verified"]


def test_shortcut_failure_is_error(rig):
    obj, _ = rig
    obj.gui.call = lambda *_: SimpleNamespace(ok=False, content="MCP failure")
    with obj:
        result = obj.finish(timeout_s=0)
        assert result["status"] == "error"
        assert not result["ok"]
        assert result["detail"] == "MCP failure"


def test_cannot_focus_does_not_send_keys(rig, monkeypatch):
    obj, calls = rig
    obj.prepare()
    monkeypatch.setattr(Session, "focus_ok", False)
    try:
        result = obj.finish(timeout_s=0)
    finally:
        obj.close()
    assert result["status"] == "error"
    assert "focus" in result["error"]
    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows native Job API")
def test_job_close_kills_owned_process_but_not_other_process(tmp_path):
    """Exercise real Win32 cleanup without launching any GUI software."""
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    session = d._WindowsSession(command, tmp_path)
    other = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert session.owns(session.process.pid)
        assert not session.owns(other.pid)
        # Simulate runner death: kernel closes handles without calling close().
        session.kernel.CloseHandle(session.job)
        session.job = None
        session.process.wait(timeout=5)
        assert other.poll() is None
    finally:
        session.close()
        other.kill()
        other.wait(timeout=5)


def test_failed_cleanup_preserves_profile_for_retry(rig, monkeypatch):
    obj, _ = rig
    obj.prepare()
    profile = obj.profile
    with monkeypatch.context() as patch:
        patch.setattr(Session, "close", lambda _: (_ for _ in ()).throw(OSError("kill failed")))
        obj.close()
    assert obj.metadata["cleanup_error"].endswith("kill failed")
    assert profile.exists()
    obj.close()
    assert not profile.exists()
