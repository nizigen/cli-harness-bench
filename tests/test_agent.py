"""The agent loop's bookkeeping, driven by a scripted fake model.

The loop decides how a run is classified later, so what matters is that the
budget is honoured, that a ``finish`` call sets the declared status, and that
the action history handed to the grader matches OSWorld's shape.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness_bench import agent
from harness_bench.config import BenchConfig
from harness_bench.interfaces import ToolOutcome, ToolSpec


class FakeInterface:
    """Answers tool calls from a script and counts them."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def system_prompt(self, task) -> str:
        return "you are being tested"

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec("run_harness", "run a command", {"type": "object", "properties": {}}),
            ToolSpec("finish", "declare the outcome", {"type": "object", "properties": {}}),
        ]

    def call(self, name: str, arguments: dict) -> ToolOutcome:
        self.calls.append((name, arguments))
        return ToolOutcome(content=f"ran {name}", ok=True)

    def finalize(self, task) -> dict:
        return {}

    def close(self) -> None:
        self.closed = True


def _message(content="", tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _call(call_id: str, name: str, arguments: str):
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=arguments)
    )


class FakeClient:
    """A chat-completions client that replays a list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("the loop asked for more responses than scripted")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture()
def materialized(tmp_path, stage_offline):
    from harness_bench.config import TASKS_DIR
    from harness_bench.tasks import Task

    task = Task.from_file(
        TASKS_DIR / "libreoffice_calc" / "01b269ae-2111-4a07-81fd-3fcd711993b0.json"
    )
    return stage_offline(task, tmp_path / "work")


def _config(**model_overrides) -> BenchConfig:
    """A config with a model set, built the same way the YAML loader builds it."""

    return BenchConfig.from_dict({"model": {"model": "test-model", **model_overrides}})


def _runner(config, client, interface) -> agent.AgentRunner:
    runner = agent.AgentRunner(config, interface)
    runner._client = lambda: client  # no network in the unit tests
    return runner


def test_finish_sets_the_declared_status(materialized):
    interface = FakeInterface()
    client = FakeClient(
        [
            _message(tool_calls=[_call("1", "run_harness", '{"command": "info"}')]),
            _message(
                tool_calls=[_call("2", "finish", '{"status": "done", "summary": "all good"}')]
            ),
        ]
    )

    outcome = _runner(_config(), client, interface).run(materialized)

    assert outcome.status == "done"
    assert outcome.summary == "all good"
    assert outcome.tool_calls == 2
    assert outcome.stopped_reason == "finished"
    assert [name for name, _ in interface.calls] == ["run_harness", "finish"]


def test_infeasible_becomes_a_fail_action_for_the_grader(materialized):
    interface = FakeInterface()
    client = FakeClient(
        [
            _message(
                tool_calls=[
                    _call("1", "finish", '{"status": "infeasible", "summary": "cannot do this"}')
                ]
            )
        ]
    )

    outcome = _runner(_config(), client, interface).run(materialized)

    assert outcome.declared_infeasible
    assert outcome.action_history() == ["FAIL"]


def test_a_done_run_reports_a_done_action(materialized):
    outcome = agent.AgentOutcome(status="done")
    assert outcome.action_history() == [{"action_type": "DONE"}]


def test_the_tool_call_budget_stops_the_loop(materialized):
    config = BenchConfig.from_dict(
        {"model": {"model": "test-model"}, "budget": {"max_tool_calls": 2}}
    )
    interface = FakeInterface()
    # Every response asks for one more call; the budget must cut it off.
    client = FakeClient(
        [
            _message(tool_calls=[_call(str(i), "run_harness", "{}")])
            for i in range(5)
        ]
    )

    outcome = _runner(config, client, interface).run(materialized)

    assert outcome.stopped_reason == "tool_call_budget"
    assert outcome.tool_calls == 2


@pytest.mark.parametrize("batch_sizes, limit", [([60], 50), ([49, 4], 50), ([3], 2)])
def test_tool_call_budget_is_enforced_inside_each_batch(materialized, batch_sizes, limit):
    config = BenchConfig.from_dict(
        {"model": {"model": "test-model"}, "budget": {"max_tool_calls": limit}}
    )
    interface = FakeInterface()
    client = FakeClient([
        _message(tool_calls=[_call(f"{batch}-{i}", "run_harness", "{}") for i in range(size)])
        for batch, size in enumerate(batch_sizes)
    ])

    outcome = _runner(config, client, interface).run(materialized)

    assert outcome.tool_calls == len(interface.calls) == limit
    assert sum(len(step.tool_calls) for step in outcome.steps) == limit
    assert outcome.stopped_reason == "tool_call_budget"
    assert len(client.requests) == len(batch_sizes)


def test_finish_beyond_budget_cannot_declare_success(materialized):
    config = BenchConfig.from_dict(
        {"model": {"model": "test-model"}, "budget": {"max_tool_calls": 1}}
    )
    interface = FakeInterface()
    client = FakeClient([_message(tool_calls=[
        _call("1", "run_harness", "{}"),
        _call("2", "finish", '{"status": "done"}'),
    ])])
    outcome = _runner(config, client, interface).run(materialized)
    assert outcome.tool_calls == len(interface.calls) == 1
    assert outcome.status == "incomplete"
    assert outcome.stopped_reason == "tool_call_budget"


def test_a_model_error_is_recorded_not_raised(materialized):
    interface = FakeInterface()
    client = FakeClient([RuntimeError("upstream exploded")])

    outcome = _runner(_config(), client, interface).run(materialized)

    assert outcome.stopped_reason == "model_error"
    assert outcome.steps[0].error
    assert "upstream exploded" in outcome.steps[0].error


def test_a_response_without_tool_calls_ends_the_loop(materialized):
    interface = FakeInterface()
    client = FakeClient([_message(content="I think it is already done.")])

    outcome = _runner(_config(), client, interface).run(materialized)

    assert outcome.stopped_reason == "no_tool_call"
    assert outcome.status == "incomplete"
    assert outcome.steps[0].assistant_text == "I think it is already done."


def test_invalid_json_arguments_fail_the_call_without_crashing(materialized):
    interface = FakeInterface()
    client = FakeClient(
        [
            _message(tool_calls=[_call("1", "run_harness", "{not json")]),
            _message(tool_calls=[_call("2", "finish", '{"status": "done"}')]),
        ]
    )

    outcome = _runner(_config(), client, interface).run(materialized)

    first = outcome.steps[0].tool_calls[0]
    assert first["ok"] is False
    assert "not valid JSON" in first["result"]
    # The loop keeps going, so the model can recover.
    assert outcome.status == "done"


def test_token_usage_is_accumulated(materialized):
    interface = FakeInterface()
    response = _message(tool_calls=[_call("1", "finish", '{"status": "done"}')])
    response.usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7)
    client = FakeClient([response])

    outcome = _runner(_config(), client, interface).run(materialized)

    assert outcome.prompt_tokens == 11
    assert outcome.completion_tokens == 7


def test_the_transcript_keeps_the_tool_exchange(materialized):
    interface = FakeInterface()
    client = FakeClient(
        [
            _message(tool_calls=[_call("1", "run_harness", '{"command": "info"}')]),
            _message(tool_calls=[_call("2", "finish", '{"status": "done"}')]),
        ]
    )

    outcome = _runner(_config(), client, interface).run(materialized)

    roles = [entry["role"] for entry in outcome.transcript]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]
    tool_entry = outcome.transcript[3]
    assert tool_entry["tool_call_id"] == "1"
    assert tool_entry["content"] == "ran run_harness"


def test_a_run_without_a_configured_model_refuses_to_start(materialized):
    config = BenchConfig()
    runner = agent.AgentRunner(config, FakeInterface())

    with pytest.raises(RuntimeError, match="no model configured"):
        runner.run(materialized)


class ScreenshotInterface(FakeInterface):
    """A GUI-shaped interface: a screenshot call returns pixels."""

    def call(self, name: str, arguments: dict) -> ToolOutcome:
        self.calls.append((name, arguments))
        if name == "Screenshot":
            return ToolOutcome(content="captured", ok=True, images=[("image/png", b"\x89PNG")])
        return ToolOutcome(content=f"ran {name}", ok=True)


def test_a_screenshot_reaches_the_model_as_an_image_part(materialized):
    interface = ScreenshotInterface()
    client = FakeClient(
        [
            _message(tool_calls=[_call("1", "Screenshot", "{}")]),
            _message(tool_calls=[_call("2", "finish", '{"status": "done"}')]),
        ]
    )

    outcome = _runner(_config(), client, interface).run(materialized)

    tool_entry = outcome.transcript[3]
    assert isinstance(tool_entry["content"], list)
    kinds = [part["type"] for part in tool_entry["content"]]
    assert kinds == ["text", "image_url"]
    url = tool_entry["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert outcome.images_sent == 1


def test_older_screenshots_are_dropped_from_what_is_resent(materialized):
    """Only the most recent ``image_history`` screenshots stay in the prompt."""

    interface = ScreenshotInterface()
    script = [_message(tool_calls=[_call(str(i), "Screenshot", "{}")]) for i in range(1, 4)]
    script.append(_message(tool_calls=[_call("9", "finish", '{"status": "done"}')]))
    client = FakeClient(script)

    outcome = _runner(_config(image_history=1), client, interface).run(materialized)

    tool_entries = [entry for entry in outcome.transcript if entry["role"] == "tool"]
    with_images = [entry for entry in tool_entries if isinstance(entry["content"], list)]
    assert len(with_images) == 1
    assert with_images[0]["content"][-1]["type"] == "image_url"
    # The dropped ones keep their text, so the record still shows what happened.
    assert tool_entries[0]["content"] == "captured"
    assert outcome.images_sent == 3
