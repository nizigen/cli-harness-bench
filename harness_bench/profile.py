"""Version-pinned, document-free LibreOffice profile bootstrap and cache."""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Any

from .config import REPO_ROOT

CACHE_ROOT = REPO_ROOT / "data" / "cache" / "desktop-profiles"
OOR = "http://openoffice.org/2001/registry"
FLAGS = {"FirstRun": False, "ShowTipOfTheDay": False, "WarnAlienFormat": False}
KNOWN_DIALOGS = (
    "欢迎使用libreoffice", "welcometolibreoffice", "每日小贴士", "每日提示", "每日贴士",
    "tipoftheday",
)
#: A clean frame must stay clean this long before the profile is trusted.
SETTLE_S = 2.0


def _sha(path: pathlib.Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _files(profile: pathlib.Path) -> dict[str, str]:
    if any(path.is_symlink() for path in profile.rglob("*")):
        raise OSError("profile cache must not contain symbolic links")
    return {path.relative_to(profile).as_posix(): _sha(path)
            for path in sorted(profile.rglob("*")) if path.is_file()}


def _merge_flags(registry: pathlib.Path) -> None:
    tree = ET.parse(registry)
    root = tree.getroot()
    if root.tag != f"{{{OOR}}}items":
        raise OSError("LibreOffice did not generate a registry items document")
    for name in FLAGS:
        path = "/org.openoffice.Office.Common/" + (
            "Save/Document" if name == "WarnAlienFormat" else "Misc")
        for item in root:
            if item.get(f"{{{OOR}}}path") == path:
                for prop in list(item):
                    if prop.get(f"{{{OOR}}}name") == name:
                        item.remove(prop)
        item = ET.SubElement(root, "item", {f"{{{OOR}}}path": path})
        prop = ET.SubElement(item, "prop", {f"{{{OOR}}}name": name, f"{{{OOR}}}op": "fuse"})
        ET.SubElement(prop, "value").text = "false"
    ET.register_namespace("oor", OOR)
    root.set("xmlns:xs", "http://www.w3.org/2001/XMLSchema")
    tree.write(registry, encoding="utf-8", xml_declaration=True)


def _known(title: str) -> bool:
    normalized = "".join(c for c in title.casefold() if c.isalnum())
    return any(normalized.startswith(prefix) for prefix in KNOWN_DIALOGS)


def _bootstrap(binary: pathlib.Path, profile: pathlib.Path, factory: Any,
               deadline: float, metadata: dict[str, Any]) -> None:
    """Close only known onboarding dialogs, while no task document is open."""
    for _ in range(3):
        command = [str(binary), f"-env:UserInstallation={profile.as_uri()}", "--norestore", "--writer"]
        record: dict[str, Any] = {"command": command, "dialogs": []}
        metadata["rounds"].append(record)
        session = factory(command, profile)
        try:
            quiet_since, dismissed = time.monotonic(), set()
            while time.monotonic() < deadline:
                windows = session.windows()
                record["windows"] = windows
                dialogs = [w for w in windows if w["dialog"]]
                frames = [w for w in windows if w["class"] == "SALFRAME"]
                for window in dialogs:
                    if not _known(window["title"]):
                        raise OSError(f"unknown bootstrap dialog: {window['title']}")
                    if window["hwnd"] not in dismissed:
                        session.close_window(window["hwnd"])
                        dismissed.add(window["hwnd"])
                        record["dialogs"].append(window["title"])
                if dialogs or len(frames) != 1 or not frames[0]["enabled"]:
                    quiet_since = time.monotonic()
                elif time.monotonic() - quiet_since >= SETTLE_S:
                    session.close_window(frames[0]["hwnd"])
                    code = session.process.wait(timeout=max(0.01, deadline - time.monotonic()))
                    record["exit_code"] = code
                    if code:
                        raise OSError(f"profile bootstrap exited with {code}")
                    break
                time.sleep(0.2)
            else:
                raise TimeoutError("profile bootstrap exceeded startup deadline")
        finally:
            session.close()
        if not record["dialogs"]:
            return
    raise OSError("profile bootstrap did not reach a clean document-free launch")


def _manifest(cache: pathlib.Path, key: str) -> dict[str, Any]:
    manifest = json.loads((cache / "manifest.json").read_text(encoding="utf-8"))
    actual = _files(cache / "profile")
    registry = "user/registrymodifications.xcu"
    if (manifest["schema"] != 1 or manifest["binary_hash"] != key or not actual
            or actual != manifest["files"]
            or actual[registry] != manifest["registry_sha256"]):
        raise ValueError("profile cache hashes do not match manifest")
    return manifest


def profile_identity(soffice: pathlib.Path, cache_root: pathlib.Path | None = None) -> dict[str, Any]:
    """Read-only cache lookup for freeze; never bootstraps or launches a GUI."""
    binary = pathlib.Path(soffice).with_name("soffice.com")
    identity = {name: _sha(binary.with_name(name)) for name in (binary.name, "soffice.bin")}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache = (cache_root or CACHE_ROOT) / key
    result: dict[str, Any] = {"initialized": False, "path": str(cache / "profile"),
                              "binaries": identity, "binary_hash": key}
    if not cache.exists():
        return result
    try:
        manifest = _manifest(cache, key)
        result.update(initialized=True, files=manifest["files"], registry_sha256=manifest["registry_sha256"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["error"] = f"invalid profile cache: {exc}"
    return result


def prepare_profile(binary: pathlib.Path, target: pathlib.Path, factory: Any,
                    timeout_s: float, metadata: dict[str, Any],
                    cache_root: pathlib.Path | None = None) -> None:
    """Validate a cached template, or bootstrap one, then copy it privately."""
    identity = {name: _sha(binary.with_name(name)) for name in (binary.name, "soffice.bin")}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache_root = cache_root or CACHE_ROOT
    cache_root.mkdir(parents=True, exist_ok=True)
    cache = cache_root / key
    metadata.update(binary_hash=key, binaries=identity, cache_path=str(cache), cache_hit=False,
                    baseline_flags=FLAGS.copy(), rounds=[])
    if cache.exists():
        try:
            manifest = _manifest(cache, key)
            metadata.update(cache_hit=True, registry_sha256=manifest["registry_sha256"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise OSError(f"invalid profile cache {cache}: {exc}") from exc
    else:
        staging = pathlib.Path(tempfile.mkdtemp(prefix="bootstrap-", dir=cache_root))
        try:
            profile = staging / "profile"
            profile.mkdir()
            _bootstrap(binary, profile, factory, time.monotonic() + timeout_s, metadata)
            registry = profile / "user" / "registrymodifications.xcu"
            _merge_flags(registry)
            # The process exited gracefully before this snapshot; locks must not propagate.
            if (profile / ".lock").exists():
                raise OSError("bootstrap left a locked profile")
            files = _files(profile)
            metadata["registry_sha256"] = files["user/registrymodifications.xcu"]
            manifest = {"schema": 1, "binary_hash": key, "files": files,
                        "registry_sha256": metadata["registry_sha256"]}
            (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            try:
                staging.rename(cache)
            except FileExistsError:
                # Another worker published first; validate its complete template before use.
                prepare_profile(binary, target, factory, timeout_s, metadata, cache_root)
                return
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    shutil.copytree(cache / "profile", target, dirs_exist_ok=True)
