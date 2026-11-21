from types import SimpleNamespace

import pytest

from harness_bench import setup_actions as s


def test_all_five_reviewed_upstream_scripts_match(all_tasks):
    actions = [a for t in all_tasks if t.adopted and t.family == 'libreoffice'
               for a in t.config if a['type'] == 'execute']
    assert len(actions) == 5
    assert all(s.supports_setup(a) for a in actions)
    changed = {'type': 'execute', 'parameters': {'command': ['python', '-c', 'print(1)']}}
    assert not s.supports_setup(changed)


def test_table_cursor_moves_and_scrolls_before_agent(all_tasks, monkeypatch):
    task = next(t for t in all_tasks if t.id == '66399b0d-8fda-4618-95c4-bfc6191617e9')
    calls = []
    monkeypatch.setattr(s.time, 'sleep', lambda _: None)
    def call(name, args):
        calls.append((name, args))
        return SimpleNamespace(ok=True, content='ok')
    evidence = s.apply_setup(SimpleNamespace(task=task), SimpleNamespace(call=call))
    assert calls[:40] == [('Shortcut', {'shortcut': 'down'})] * 40
    assert calls[40:] == [('Scroll', {'direction': 'down', 'wheel_times': 2})]
    assert len(evidence[0]['calls']) == 41


def test_failed_setup_stops_instead_of_ignoring(all_tasks, monkeypatch):
    task = next(t for t in all_tasks if t.id == '66399b0d-8fda-4618-95c4-bfc6191617e9')
    monkeypatch.setattr(s.time, 'sleep', lambda _: None)
    gui = SimpleNamespace(call=lambda *_: SimpleNamespace(ok=False, content='not focused'))
    with pytest.raises(RuntimeError, match='not focused'):
        s.apply_setup(SimpleNamespace(task=task), gui)
