"""Configuration objects for the benchmark runner.

Everything the runner needs to know about budgets, splits, models and
interfaces lives here so that a single YAML file pins a whole experiment.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
THIRD_PARTY = REPO_ROOT / "third_party" / "osworld"
TASKS_DIR = THIRD_PARTY / "evaluation_examples" / "examples"
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "bench.yaml"

#: The VM prefix every OSWorld task description and evaluator config uses.
VM_HOME = "/home/user"


@dataclass(frozen=True)
class Budget:
    """Per-run limits frozen in week 1 of the plan."""

    max_tool_calls: int = 50
    timeout_s: int = 900

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Budget":
        raw = raw or {}
        return cls(
            max_tool_calls=int(raw.get("max_tool_calls", 50)),
            timeout_s=int(raw.get("timeout_s", 900)),
        )


@dataclass(frozen=True)
class Split:
    """Development/test split, stratified per application, frozen before any run."""

    dev_fraction: float = 0.4
    seed: int = 20260601

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Split":
        raw = raw or {}
        return cls(
            dev_fraction=float(raw.get("dev_fraction", 0.4)),
            seed=int(raw.get("seed", 20260601)),
        )


@dataclass(frozen=True)
class ModelConfig:
    """One OpenAI-compatible chat endpoint used to drive the agent loop."""

    base_url: str = "http://127.0.0.1:3425/v1"
    model: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    api_key: str = ""
    vision: bool = False
    image_history: int = 2
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout_s: float = 180.0

    def resolved_api_key(self) -> str:
        return self.api_key or os.environ.get(self.api_key_env, "")

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ModelConfig":
        raw = raw or {}
        return cls(
            base_url=str(raw.get("base_url", "http://127.0.0.1:3425/v1")),
            model=str(raw.get("model", "")),
            api_key_env=str(raw.get("api_key_env", "OPENAI_API_KEY")),
            api_key=str(raw.get("api_key", "")),
            vision=bool(raw.get("vision", False)),
            image_history=int(raw.get("image_history", 2)),
            temperature=float(raw.get("temperature", 0.0)),
            max_tokens=int(raw.get("max_tokens", 4096)),
            timeout_s=float(raw.get("timeout_s", 180.0)),
        )


@dataclass(frozen=True)
class HarnessConfig:
    """The CLI harness binaries the harness interface is allowed to call."""

    libreoffice: str = "cli-anything-libreoffice"
    gimp: str = "cli-anything-gimp"
    skill_docs: dict[str, str] = field(default_factory=dict)

    def binary_for(self, app: str) -> str:
        if app == "gimp":
            return self.gimp
        return self.libreoffice

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "HarnessConfig":
        raw = raw or {}
        return cls(
            libreoffice=str(raw.get("libreoffice", "cli-anything-libreoffice")),
            gimp=str(raw.get("gimp", "cli-anything-gimp")),
            skill_docs=dict(raw.get("skill_docs", {}) or {}),
        )


@dataclass(frozen=True)
class GuiConfig:
    """Windows-MCP server settings, restricted to the tools the proposal allows."""

    command: list[str] = field(default_factory=lambda: ["uvx", "windows-mcp"])
    tools: tuple[str, ...] = (
        "Screenshot",
        "Snapshot",
        "Click",
        "Type",
        "Scroll",
        "Move",
        "Shortcut",
        "Wait",
        "WaitFor",
    )
    start_timeout_s: float = 60.0
    idle_gate_s: float = 10.0

    def server_command(self) -> list[str]:
        """The command line that starts the server with the whitelist applied.

        Filtering on the client is not enough: the server would still expose
        ``PowerShell`` and ``FileSystem``, and any client bug would turn the GUI
        arm into a script arm.  ``--tools`` makes the server itself unable to
        run a command, so the comparison stays honest.
        """

        command = list(self.command)
        if "--tools" not in command:
            command += ["--tools", ",".join(self.tools)]
        if "--control-idle-timeout" not in command and self.idle_gate_s > 0:
            command += ["--control-idle-timeout", str(self.idle_gate_s)]
        return command

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "GuiConfig":
        raw = raw or {}
        return cls(
            command=list(raw.get("command", ["uvx", "windows-mcp"])),
            tools=tuple(raw.get("tools", GuiConfig().tools)),
            start_timeout_s=float(raw.get("start_timeout_s", 60.0)),
            idle_gate_s=float(raw.get("idle_gate_s", 10.0)),
        )


@dataclass(frozen=True)
class OwnCliConfig:
    """The project's own CLI, benchmarked on its own task suite.

    ``template`` is a Hugo site skeleton copied into a throwaway sandbox per
    task, so the CLI never touches the real blog checkout.
    """

    binary: str = "cli-anything-nizigen-blog"
    template: str = ""
    hugo: str = ""
    home: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "OwnCliConfig":
        raw = raw or {}
        return cls(
            binary=str(raw.get("binary", "cli-anything-nizigen-blog")),
            template=str(raw.get("template", "")),
            hugo=str(raw.get("hugo", "")),
            home=str(raw.get("home", "")),
        )


@dataclass(frozen=True)
class AppConfig:
    """Locations of the desktop applications the tasks need."""

    soffice: str = ""
    gimp: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "AppConfig":
        raw = raw or {}
        return cls(soffice=str(raw.get("soffice", "")), gimp=str(raw.get("gimp", "")))

    def soffice_or_none(self) -> str | None:
        return self.soffice or None


@dataclass(frozen=True)
class BenchConfig:
    budget: Budget = field(default_factory=Budget)
    split: Split = field(default_factory=Split)
    model: ModelConfig = field(default_factory=ModelConfig)
    harness: HarnessConfig = field(default_factory=HarnessConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)
    owncli: OwnCliConfig = field(default_factory=OwnCliConfig)
    apps: AppConfig = field(default_factory=AppConfig)
    repeats: dict[str, int] = field(
        default_factory=lambda: {"harness": 3, "script": 3, "gui": 1, "hybrid": 1, "owncli": 3}
    )
    runs_dir: str = "runs"
    download_cache: str = ".cache/osworld"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BenchConfig":
        return cls(
            budget=Budget.from_dict(raw.get("budget")),
            split=Split.from_dict(raw.get("split")),
            model=ModelConfig.from_dict(raw.get("model")),
            harness=HarnessConfig.from_dict(raw.get("harness")),
            gui=GuiConfig.from_dict(raw.get("gui")),
            owncli=OwnCliConfig.from_dict(raw.get("owncli")),
            apps=AppConfig.from_dict(raw.get("apps")),
            repeats={**cls().repeats, **dict(raw.get("repeats", {}) or {})},
            runs_dir=str(raw.get("runs_dir", "runs")),
            download_cache=str(raw.get("download_cache", ".cache/osworld")),
        )

    @classmethod
    def load(cls, path: pathlib.Path | str | None = None) -> "BenchConfig":
        target = pathlib.Path(path) if path else DEFAULT_CONFIG_PATH
        if not target.exists():
            return cls()
        return cls.from_dict(yaml.safe_load(target.read_text(encoding="utf-8")) or {})

    def repeat_count(self, interface: str) -> int:
        return int(self.repeats.get(interface, 1))
