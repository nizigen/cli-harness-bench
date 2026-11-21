"""Output normalisation: what the runner does to files before grading.

Without soffice installed, the interesting behaviour is that nothing is
silently pretended to have worked: a spreadsheet is marked ``unavailable``, and
a non-spreadsheet is passed through untouched.
"""

from __future__ import annotations

import pathlib

import pytest

from harness_bench import normalize


def test_find_soffice_honours_an_explicit_path(tmp_path):
    fake = tmp_path / "soffice.exe"
    fake.write_bytes(b"stub")
    assert normalize.find_soffice(str(fake)) == str(fake)


def test_find_soffice_returns_none_when_nothing_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(normalize.shutil, "which", lambda name: None)
    monkeypatch.setattr(normalize.os.environ, "get", lambda key, default=None: str(tmp_path))
    assert normalize.find_soffice("") is None


def test_missing_output_is_reported_as_missing(tmp_path):
    report = normalize.save_outputs([tmp_path / "absent.xlsx"], None, tmp_path / "scratch")

    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.action == "missing"
    assert not entry.ok
    assert "no such file" in entry.detail


def test_non_spreadsheet_is_passed_through(tmp_path):
    document = tmp_path / "doc.docx"
    document.write_bytes(b"PK\x03\x04")

    report = normalize.save_outputs([document], None, tmp_path / "scratch")

    assert report.entries[0].action == "none"
    assert report.entries[0].ok


def test_spreadsheet_without_soffice_is_marked_unavailable(tmp_path):
    book = tmp_path / "book.xlsx"
    book.write_bytes(b"PK\x03\x04")

    report = normalize.save_outputs([book], None, tmp_path / "scratch")

    entry = report.entries[0]
    assert entry.action == "unavailable"
    assert not entry.ok
    assert "soffice not found" in entry.detail
    assert not report.available


def test_report_is_serialisable(tmp_path):
    document = tmp_path / "doc.pdf"
    document.write_bytes(b"%PDF-1.4")
    report = normalize.save_outputs([document], "/usr/bin/soffice", tmp_path / "scratch")

    payload = report.as_dict()
    assert payload["soffice"] == "/usr/bin/soffice"
    assert payload["entries"][0]["path"].endswith("doc.pdf")


def test_recalculate_refuses_an_unsupported_suffix(tmp_path):
    book = tmp_path / "book.txt"
    book.write_text("x", encoding="utf-8")

    result = normalize.recalculate(book, "/usr/bin/soffice", tmp_path / "scratch")

    assert result.action == "skip"
    assert result.ok


def test_recalculate_survives_a_broken_binary(tmp_path):
    book = tmp_path / "book.xlsx"
    book.write_bytes(b"PK\x03\x04")

    result = normalize.recalculate(book, str(tmp_path / "nope.exe"), tmp_path / "scratch")

    assert not result.ok
    assert result.action == "recalculate"


def test_convert_to_command_is_parsed_and_localised(tmp_path):
    """A postconfig convert-to step must be replayed against local paths."""

    captured = {}

    def fake_run_soffice(command, scratch, timeout=180.0):
        captured["command"] = command
        # The conversion claims to have produced the CSV next to the source.
        outdir = pathlib.Path(command[command.index("--outdir") + 1])
        (outdir / "sheet.csv").write_text("a,b", encoding="utf-8")
        return 0, ""

    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "book.xlsx").write_bytes(b"PK\x03\x04")

    original = normalize._run_soffice
    normalize._run_soffice = fake_run_soffice
    try:
        result = normalize.run_convert_to_csv(
            [
                "libreoffice",
                "--convert-to",
                "csv:Text - txt - csv (StarCalc):44,34,UTF-8",
                "--outdir",
                "/home/user",
                "/home/user/book.xlsx",
            ],
            workdir,
            "/usr/bin/soffice",
            tmp_path / "scratch",
        )
    finally:
        normalize._run_soffice = original

    assert result.ok
    command = captured["command"]
    assert command[0] == "/usr/bin/soffice"
    assert str(workdir / "book.xlsx") in command
    assert "/home/user" not in " ".join(command)


def test_convert_to_without_soffice_does_not_run(tmp_path):
    result = normalize.run_convert_to_csv(
        ["libreoffice", "--convert-to", "csv", "/home/user/a.xlsx"],
        tmp_path,
        None,
        tmp_path / "scratch",
    )

    assert result.action == "unavailable"
    assert not result.ok


def test_collect_result_paths_follows_every_result_config(tmp_path, stage_offline):
    from harness_bench.config import TASKS_DIR
    from harness_bench.tasks import Task

    task = Task.from_file(
        TASKS_DIR / "libreoffice_writer" / "0810415c-bde4-4443-9047-d5f70165a697.json"
    )
    materialized = stage_offline(task, tmp_path / "work")

    paths = normalize.collect_result_paths(materialized)

    # Both metric configs point at the same file; the resolver keeps both
    # entries so a mismatch between them stays visible.
    assert len(paths) == 2
    assert all(path.name == "Novels_Intro_Packet.docx" for path in paths)


def test_find_soffice_reports_a_configured_path_that_does_not_exist(tmp_path, monkeypatch):
    """A stale `apps.soffice` must read as missing, not as configured."""

    monkeypatch.setattr(normalize.shutil, "which", lambda name: None)
    monkeypatch.setattr(normalize.os.environ, "get", lambda key, default=None: str(tmp_path))

    assert normalize.find_soffice(str(tmp_path / "gone" / "soffice.com")) is None
