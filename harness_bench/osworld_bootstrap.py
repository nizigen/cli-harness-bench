"""Import OSWorld's grader modules without executing their package ``__init__``.

``desktop_env/evaluators/metrics/__init__.py`` and ``getters/__init__.py`` both
import every sibling module eagerly, so a plain ``import
desktop_env.evaluators.metrics.table`` drags in ``borb``, ``easyocr``,
``pydrive`` and friends and dies.  We therefore register the four package
objects by hand with an explicit ``__path__`` and skip those ``__init__`` files
entirely.

Modules we do import still mention heavy third-party packages at module level
(``chrome.py`` imports ``borb`` after its function definitions, ``vlc.py``
imports ``librosa`` at the top).  For those we install a placeholder module
whose attribute access returns an object that raises :class:`MissingDependency`
the moment it is *called* or used as a base class -- importing stays possible,
and a task that actually needs the package fails with a readable message
instead of a bare ``ModuleNotFoundError``.
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import types

from .config import THIRD_PARTY

#: Packages whose absence must not break importing the grader modules.
OPTIONAL_PACKAGES = (
    "borb",
    "borb.pdf",
    "easyocr",
    "imagehash",
    "acoustid",
    "librosa",
    "fastdtw",
    "mutagen",
    "pydrive",
    "pygame",
    "PyPDF2",
    "gym",
    "gymnasium",
)

_REGISTERED_PACKAGES = (
    "desktop_env",
    "desktop_env.evaluators",
    "desktop_env.evaluators.metrics",
    "desktop_env.evaluators.getters",
)

_bootstrapped = False


class MissingDependency(ImportError):
    """Raised when a grader function needs a package that is not installed."""


class _Placeholder:
    """Stands in for a missing module's attribute; fails only when used."""

    __slots__ = ("_qualname",)

    def __init__(self, qualname: str) -> None:
        self._qualname = qualname

    def __getattr__(self, item: str) -> "_Placeholder":
        if item.startswith("__") and item.endswith("__"):
            raise AttributeError(item)
        return _Placeholder(f"{self._qualname}.{item}")

    def __call__(self, *args, **kwargs):
        raise MissingDependency(
            f"{self._qualname}() was used but the real package is not installed"
        )

    def __mro_entries__(self, bases):
        raise MissingDependency(
            f"{self._qualname} was used as a base class but the real package is not installed"
        )

    def __repr__(self) -> str:
        return f"<missing {self._qualname}>"


class _PoisonModule(types.ModuleType):
    """A module object that resolves every attribute to a placeholder."""

    def __getattr__(self, item: str) -> _Placeholder:
        if item.startswith("__") and item.endswith("__"):
            raise AttributeError(item)
        return _Placeholder(f"{self.__name__}.{item}")


def _poison(name: str) -> types.ModuleType:
    module = _PoisonModule(name)
    module.__path__ = []  # type: ignore[attr-defined]
    module.__package__ = name.rpartition(".")[0]
    sys.modules[name] = module
    return module


def _ensure_optional_packages() -> None:
    for name in OPTIONAL_PACKAGES:
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
        except Exception:
            _poison(name)


def _register_package(name: str, directory: pathlib.Path) -> None:
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    module.__path__ = [str(directory)]  # type: ignore[attr-defined]
    module.__package__ = name
    sys.modules[name] = module


def ensure_osworld_importable() -> pathlib.Path:
    """Make ``desktop_env.evaluators.*`` importable and return its root."""

    global _bootstrapped
    root = THIRD_PARTY
    evaluators = root / "desktop_env" / "evaluators"
    if not evaluators.is_dir():
        raise FileNotFoundError(f"vendored OSWorld evaluators missing at {evaluators}")

    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    _ensure_optional_packages()
    _register_package("desktop_env", root / "desktop_env")
    _register_package("desktop_env.evaluators", evaluators)
    _register_package("desktop_env.evaluators.metrics", evaluators / "metrics")
    _register_package("desktop_env.evaluators.getters", evaluators / "getters")
    _bootstrapped = True
    return root


def import_metric(module: str):
    """Import one ``desktop_env.evaluators.metrics`` submodule by file name."""

    ensure_osworld_importable()
    return importlib.import_module(f"desktop_env.evaluators.metrics.{module}")


def import_getter(module: str):
    """Import one ``desktop_env.evaluators.getters`` submodule by file name."""

    ensure_osworld_importable()
    return importlib.import_module(f"desktop_env.evaluators.getters.{module}")


def available(module_name: str) -> bool:
    """True when the package named by ``module_name`` really is importable."""

    return module_name not in sys.modules or not isinstance(
        sys.modules[module_name], _PoisonModule
    )
