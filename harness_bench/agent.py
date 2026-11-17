"""A small OpenAI-compatible agent loop with a hard tool-call budget.

The loop is deliberately boring: it sends the transcript, executes every tool
call the model asks for, and stops on a ``finish`` call, on the call budget, or
on the wall-clock budget.  Everything it observes is recorded so a later
inspection can tell an agent mistake from a harness defect.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .config import BenchConfig, Budget
from .interfaces import Interface, ToolError
from .tasks import MaterializedTask

FINISH_TOOL = "finish"


@dataclass
class Step:
    """One assistant turn plus the tool results it produced."""

    index: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    assistant_text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "assistant_text": self.assistant_text,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "error": self.error,
        }


@dataclass
class AgentOutcome:
    """What the loop produced, in the shape the runner records."""

    status: str = "incomplete"
    summary: str = ""
    steps: list[Step] = field(default_factory=list)
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_s: float = 0.0
    stopped_reason: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)

    @property
    def declared_done(self) -> bool:
        return self.status == "done"

    @property
    def declared_infeasible(self) -> bool:
        return self.status == "infeasible"

    def action_history(self) -> list[Any]:
        """The history OSWorld's infeasible check looks at."""

        if self.declared_infeasible:
            return ["FAIL"]
        return [{"action_type": "DONE"}]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "elapsed_s": round(self.elapsed_s, 3),
            "stopped_reason": self.stopped_reason,
            "steps": [step.as_dict() for step in self.steps],
        }


class BudgetExceeded(RuntimeError):
    """The run hit the tool-call or wall-clock limit."""


class AgentRunner:
    """Drives one task through one interface."""

    def __init__(self, config: BenchConfig, interface: Interface) -> None:
        self.config = config
        self.interface = interface
        self.budget: Budget = config.budget

    # -- client ----------------------------------------------------------
    def _client(self):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(f"the openai package is required: {exc}")

        model = self.config.model
        return OpenAI(
            base_url=model.base_url,
            api_key=model.resolved_api_key() or "not-needed",
            timeout=model.timeout_s,
        )

    # -- loop ------------------------------------------------------------
    def run(self, task: MaterializedTask) -> AgentOutcome:
        model_cfg = self.config.model
        if not model_cfg.model:
            raise RuntimeError(
                "no model configured: set model.model in the config file "
                "(an OpenAI-compatible endpoint is required)"
            )

        client = self._client()
        tools = [spec.as_openai() for spec in self.interface.tools()]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.interface.system_prompt(task)},
            {"role": "user", "content": task.instruction},
        ]

        outcome = AgentOutcome()
        started = time.monotonic()

        while True:
            if outcome.tool_calls >= self.budget.max_tool_calls:
                outcome.stopped_reason = "tool_call_budget"
                break
            if time.monotonic() - started > self.budget.timeout_s:
                outcome.stopped_reason = "wall_clock_budget"
                break

            step = Step(index=len(outcome.steps))
            try:
                response = client.chat.completions.create(
                    model=model_cfg.model,
                    messages=messages,
                    tools=tools,
                    temperature=model_cfg.temperature,
                    max_tokens=model_cfg.max_tokens,
                )
            except Exception as exc:
                step.error = f"{type(exc).__name__}: {exc}"
                outcome.steps.append(step)
                outcome.stopped_reason = "model_error"
                break

            usage = getattr(response, "usage", None)
            if usage is not None:
                step.prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                step.completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                outcome.prompt_tokens += step.prompt_tokens
                outcome.completion_tokens += step.completion_tokens

            choice = response.choices[0]
            message = choice.message
            step.assistant_text = message.content or ""
            calls = list(getattr(message, "tool_calls", None) or [])

            assistant_entry: dict[str, Any] = {
                "role": "assistant",
                "content": message.content or "",
            }
            if calls:
                assistant_entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in calls
                ]
            messages.append(assistant_entry)

            if not calls:
                outcome.steps.append(step)
                outcome.stopped_reason = outcome.stopped_reason or "no_tool_call"
                break

            finished = False
            for call in calls:
                outcome.tool_calls += 1
                name = call.function.name
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    result_text = f"arguments were not valid JSON: {exc}"
                    ok = False
                    arguments = {}
                else:
                    try:
                        result = self.interface.call(name, arguments)
                        result_text = result.content
                        ok = result.ok
                    except ToolError as exc:
                        result_text = str(exc)
                        ok = False

                step.tool_calls.append(
                    {"name": name, "arguments": arguments, "ok": ok, "result": result_text[:2000]}
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result_text}
                )

                if name == FINISH_TOOL:
                    outcome.status = str(arguments.get("status", "incomplete"))
                    outcome.summary = str(arguments.get("summary", ""))
                    finished = True

            outcome.steps.append(step)
            if finished:
                outcome.stopped_reason = "finished"
                break

        outcome.elapsed_s = time.monotonic() - started
        outcome.transcript = messages
        if not outcome.stopped_reason:
            outcome.stopped_reason = "loop_exit"
        return outcome
