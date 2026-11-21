"""Reviewed OSWorld UI preconditions, translated without executing Python source."""
from __future__ import annotations

import ctypes
import time
from typing import Any

# Exact source matching prevents a changed upstream script being silently ignored.
_REVIEWED = {
    'import pyautogui; import time; pyautogui.press("down", presses=40, interval=0.01); time.sleep(1); pyautogui.scroll(-2)':
        [("down", 40), ("wait", 1), ("scroll", 2)],
    'import pyautogui; import time; time.sleep(15); pyautogui.press("down", presses=8, interval=0.01); time.sleep(1); pyautogui.scroll(-2)':
        [("wait", 15), ("down", 8), ("wait", 1), ("scroll", 2)],
    'import pyautogui; import time;  time.sleep(4); pyautogui.doubleClick(x={SCREEN_WIDTH_HALF}, y={SCREEN_HEIGHT_HALF}); time.sleep(0.5);pyautogui.mouseDown(); pyautogui.mouseUp(); time.sleep(0.5);':
        [("wait", 4), ("center_double", 0), ("wait", .5), ("center", 0), ("wait", .5)],
    "import pyautogui; import time;  time.sleep(4); pyautogui.click(170, 250); time.sleep(1);pyautogui.press('down'); time.sleep(1); pyautogui.press('down'); time.sleep(1); pyautogui.press('down'); time.sleep(1); pyautogui.press('down'); time.sleep(1); ":
        [("wait", 4), ("click", (170, 250)), ("wait", 1),
         ("down", 1), ("wait", 1), ("down", 1), ("wait", 1),
         ("down", 1), ("wait", 1), ("down", 1), ("wait", 1)],
    'import pyautogui; import time; pyautogui.click({SCREEN_WIDTH_HALF}, {SCREEN_HEIGHT_HALF}); time.sleep(0.5);':
        [("center", 0), ("wait", .5)],
}


def supports_setup(action: dict[str, Any]) -> bool:
    command = action.get("parameters", {}).get("command")
    return (action.get("type") == "execute" and isinstance(command, list)
            and len(command) == 3 and command[:2] == ["python", "-c"]
            and isinstance(command[2], str) and command[2] in _REVIEWED)


def _screen_center() -> list[int]:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetSystemMetrics.argtypes = [ctypes.c_int]
    user.GetSystemMetrics.restype = ctypes.c_int
    return [user.GetSystemMetrics(0) // 2, user.GetSystemMetrics(1) // 2]


def apply_setup(task: Any, gui: Any) -> list[dict[str, Any]]:
    """Use the same restricted GUI connection before the agent budget starts."""
    evidence = []
    for index, action in enumerate(task.task.config):
        if action.get("type") != "execute":
            continue
        if not supports_setup(action):
            raise ValueError("unreviewed OSWorld execute precondition")
        source = action["parameters"]["command"][2]
        calls = []
        for kind, value in _REVIEWED[source]:
            if kind == "wait":
                time.sleep(value)
                continue
            if kind == "down":
                operations = [("Shortcut", {"shortcut": "down"})] * value
            elif kind == "scroll":
                operations = [("Scroll", {"direction": "down", "wheel_times": value})]
            else:
                loc = list(value) if kind == "click" else _screen_center()
                operations = [("Click", {"loc": loc, "clicks": 2 if kind == "center_double" else 1})]
            for name, arguments in operations:
                result = gui.call(name, arguments)
                calls.append({"tool": name, "arguments": arguments, "ok": result.ok})
                if not result.ok:
                    raise RuntimeError(f"OSWorld setup {index}: {name} failed: {result.content}")
        evidence.append({"index": index, "source": source, "calls": calls})
    return evidence
