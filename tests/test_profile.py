"""Offline profile-cache checks: fake sessions, never LibreOffice or a GUI."""
import json
import pathlib
import xml.etree.ElementTree as ET

import pytest

from harness_bench import profile as p

OOR = "{http://openoffice.org/2001/registry}"


class FakeSession:
    """Emulates one bootstrap launch: dialogs, then a clean frame."""

    script: list[list[dict]] = []
    calls: list[tuple[str, object]] = []
    extra = ""
    exit_code = 0

    def __init__(self, command, cwd):
        assert "--writer" in command and "--headless" not in command
        self.command, self.closed, self.dismissed = command, False, []
        self.round = FakeSession.script.pop(0) if FakeSession.script else [[FRAME], [FRAME]]
        self.process = type("P", (), {"wait": lambda _, timeout=None: FakeSession.exit_code})()
        profile = pathlib.Path(cwd)
        registry = profile / "user" / "registrymodifications.xcu"
        if not registry.exists():
            registry.parent.mkdir(parents=True, exist_ok=True)
            registry.write_text(
                '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Misc">'
                '<prop oor:name="FirstRun" oor:op="fuse"><value>true</value></prop></item>'
                + FakeSession.extra + '</oor:items>', encoding="utf-8")
            (profile / "user" / "other.dat").write_bytes(b"generated")

    def windows(self):
        return self.round[0] if self.round else []

    def close_window(self, hwnd):
        FakeSession.calls.append(("close", hwnd))
        if len(self.round) > 1:
            self.round = self.round[1:]

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    monkeypatch.setattr(p, "SETTLE_S", 0)
    monkeypatch.setattr(FakeSession, "extra", "")
    monkeypatch.setattr(FakeSession, "exit_code", 0)


@pytest.fixture
def binary(tmp_path):
    program = tmp_path / "program"
    program.mkdir()
    (program / "soffice.com").write_bytes(b"com")
    (program / "soffice.bin").write_bytes(b"bin")
    return program / "soffice.com"


FRAME = {"hwnd": 1, "class": "SALFRAME", "title": "LibreOffice", "dialog": False, "enabled": True}
WELCOME = {"hwnd": 2, "class": "#32770", "title": "欢迎使用 LibreOffice！", "dialog": True, "enabled": True}
TIP = {"hwnd": 3, "class": "#32770", "title": "每日贴士：1/224", "dialog": True, "enabled": True}


def _flags(path):
    root = ET.parse(path).getroot()
    found = {}
    for item in root:
        for prop in item:
            found[prop.get(OOR + "name")] = prop.findtext("value")
    return found


def test_bootstrap_dismisses_known_dialogs_and_merges_flags(binary, tmp_path):
    FakeSession.script = [
        [[WELCOME, FRAME], [FRAME], [FRAME]],
        [[TIP, FRAME], [FRAME], [FRAME]],
        [[FRAME], [FRAME], [FRAME]],
    ]
    FakeSession.calls = []
    metadata = {}
    target = tmp_path / "task-profile"
    p.prepare_profile(binary, target, FakeSession, 5, metadata, tmp_path / "cache")
    assert metadata["cache_hit"] is False
    assert [r["dialogs"] for r in metadata["rounds"]] == [
        ["欢迎使用 LibreOffice！"], ["每日贴士：1/224"], []]
    assert metadata["rounds"][0]["exit_code"] == 0
    assert FakeSession.calls == [("close", 2), ("close", 1), ("close", 3), ("close", 1), ("close", 1)]
    assert _flags(target / "user" / "registrymodifications.xcu") == {
        "FirstRun": "false", "ShowTipOfTheDay": "false", "WarnAlienFormat": "false"}
    manifest = json.loads((tmp_path / "cache" / metadata["binary_hash"] / "manifest.json").read_text())
    assert manifest["registry_sha256"] == metadata["registry_sha256"]
    assert manifest["files"]["user/other.dat"]


def test_unknown_dialog_aborts_bootstrap_without_closing_it(binary, tmp_path):
    FakeSession.script = [[[dict(WELCOME, title="Convert document"), FRAME]]]
    FakeSession.calls = []
    cache = tmp_path / "cache"
    with pytest.raises(OSError, match="unknown bootstrap dialog"):
        p.prepare_profile(binary, tmp_path / "t", FakeSession, 5, {}, cache)
    assert FakeSession.calls == []  # Unknown dialog stays for inspection.
    assert not [entry for entry in cache.iterdir() if entry.name != ".lock"]  # Nothing published.
    assert not (tmp_path / "t").exists()  # No private profile handed to the GUI.


def test_merge_flags_preserves_unrelated_registry_entries(binary, tmp_path):
    FakeSession.extra = ('<item oor:path="/org.example.Settings">'
                         '<prop oor:name="Keep" oor:op="fuse"><value>yes</value></prop></item>')
    target = tmp_path / "profile-out"
    p.prepare_profile(binary, target, FakeSession, 5, {}, tmp_path / "cache")
    flags = _flags(target / "user" / "registrymodifications.xcu")
    assert flags["Keep"] == "yes"
    assert flags["FirstRun"] == "false"
    first_run = sum(1 for item in ET.parse(
        target / "user" / "registrymodifications.xcu").getroot()
        for prop in item if prop.get(OOR + "name") == "FirstRun")
    assert first_run == 1  # The generated true value is replaced, not duplicated.


def test_bootstrap_timeout_leaves_no_cache(binary, tmp_path):
    cache = tmp_path / "cache"
    with pytest.raises(TimeoutError, match="exceeded startup deadline"):
        p.prepare_profile(binary, tmp_path / "t", FakeSession, 0, {}, cache)
    assert not [entry for entry in cache.iterdir() if entry.is_dir()]


def test_bootstrap_nonzero_exit_is_reported(binary, tmp_path):
    FakeSession.exit_code = 2
    with pytest.raises(OSError, match="bootstrap exited with 2"):
        p.prepare_profile(binary, tmp_path / "t", FakeSession, 5, {}, tmp_path / "cache")


@pytest.mark.parametrize("mutation", ["bad_json", "schema", "binary_hash", "registry_sha"])
def test_prepare_refuses_inconsistent_manifest(binary, tmp_path, mutation):
    cache = tmp_path / "cache"
    p.prepare_profile(binary, tmp_path / "first", FakeSession, 5, {}, cache)
    manifest = next(cache.iterdir()) / "manifest.json"
    data = json.loads(manifest.read_text())
    if mutation == "bad_json":
        manifest.write_text("{not json", encoding="utf-8")
    elif mutation == "schema":
        data["schema"] = 2
        manifest.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "binary_hash":
        data["binary_hash"] = "0" * 64
        manifest.write_text(json.dumps(data), encoding="utf-8")
    else:
        data["registry_sha256"] = "0" * 64
        manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(OSError, match="invalid profile cache"):
        p.prepare_profile(binary, tmp_path / "second", FakeSession, 5, {}, cache)


def test_identity_reports_valid_then_corrupt_cache(binary, tmp_path):
    cache = tmp_path / "cache"
    p.prepare_profile(binary, tmp_path / "first", FakeSession, 5, {}, cache)
    valid = p.profile_identity(binary, cache)
    assert valid["initialized"] is True and "error" not in valid
    assert valid["registry_sha256"] == valid["files"]["user/registrymodifications.xcu"]
    (next(cache.iterdir()) / "profile" / "user" / "registrymodifications.xcu").write_bytes(b"tampered")
    corrupt = p.profile_identity(binary, cache)
    assert corrupt["initialized"] is False
    assert corrupt["error"].startswith("invalid profile cache")


def test_cache_hit_is_copied_without_new_bootstrap(binary, tmp_path):
    cache = tmp_path / "cache"
    p.prepare_profile(binary, tmp_path / "first", FakeSession, 5, {}, cache)
    FakeSession.script = []
    metadata = {}
    p.prepare_profile(binary, tmp_path / "second", FakeSession, 5, metadata, cache)
    assert metadata["cache_hit"] is True
    assert metadata["rounds"] == []
    assert (tmp_path / "second" / "user" / "other.dat").exists()
    assert metadata["registry_sha256"] == p.profile_identity(binary, cache)["registry_sha256"]


@pytest.mark.parametrize("mutation", ["tamper", "missing_manifest", "symlink"])
def test_poisoned_cache_is_refused(binary, tmp_path, mutation):
    cache = tmp_path / "cache"
    p.prepare_profile(binary, tmp_path / "first", FakeSession, 5, {}, cache)
    key = next(cache.iterdir())
    if mutation == "tamper":
        (key / "profile" / "user" / "other.dat").write_bytes(b"changed")
    elif mutation == "missing_manifest":
        (key / "manifest.json").unlink()
    else:
        (key / "profile" / "user" / "other.dat").unlink()
        (key / "profile" / "user" / "other.dat").symlink_to(binary)
    with pytest.raises(OSError):
        p.prepare_profile(binary, tmp_path / "second", FakeSession, 5, {}, cache)


def test_binary_change_uses_a_new_cache_key(binary, tmp_path):
    cache = tmp_path / "cache"
    p.prepare_profile(binary, tmp_path / "one", FakeSession, 5, {}, cache)
    (binary.parent / "soffice.bin").write_bytes(b"upgraded")
    metadata = {}
    p.prepare_profile(binary, tmp_path / "two", FakeSession, 5, metadata, cache)
    assert metadata["cache_hit"] is False and len(list(cache.iterdir())) == 2


def test_identity_never_bootstraps(binary, tmp_path):
    identity = p.profile_identity(binary, tmp_path / "empty")
    assert identity["initialized"] is False
    assert identity["binaries"]["soffice.com"] and not (tmp_path / "empty").exists()
