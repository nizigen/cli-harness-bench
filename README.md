# cli-harness-bench

Benchmark for the question in [`docs/proposal.md`](docs/proposal.md): **what is a
CLI-Anything harness actually worth on third-party desktop tasks?**

The same 133 OSWorld tasks are given to four interfaces — a CLI harness, a GUI
agent, a bare script, and a harness-with-GUI-fallback — with the same model, the
same budget and the same grader. Everything is measured, nothing is estimated.

## What is here

```
harness_bench/          the runner
  tasks.py              load the 143 OSWorld tasks, decide which are gradable
  grading.py            re-implement OSWorld's evaluate() off-VM
  osworld_bootstrap.py  import the vendored graders without their heavy deps
  normalize.py          recalculate spreadsheet caches before grading
  postconfig.py         replay the postconfig steps grading actually needs
  interfaces.py         the four arms
  agent.py              the tool-calling loop
  runner.py             one run = one directory of evidence
  roundtrip.py          the harness ceiling check
  report.py             the tables
  cli.py                the command line
configs/bench.yaml      the whole experiment in one file
configs/split.json      the frozen development/test split
third_party/osworld/    the vendored tasks and graders (Apache-2.0)
tests/                  the test suite
```

## The task set

OSWorld's GIMP and LibreOffice tasks, at commit `5123bfdbbc`, vendored under
`third_party/osworld/`. Only the instruction, the input files and the grader are
used; the OSWorld virtual machine is not.

| app | tasks | adopted | why the others are out |
| --- | --- | --- | --- |
| gimp | 26 | 20 | 6 need a GIMP config file or shell output that only exists inside the VM |
| libreoffice_calc | 47 | 45 | 2 grade against a local file or a reference table that cannot be downloaded |
| libreoffice_writer | 23 | 23 | — |
| libreoffice_impress | 47 | 45 | 2 grade audio in a slide or an accessibility tree |
| **total** | **143** | **133** | |

`python -m harness_bench tasks --excluded` prints the ten exclusions with their
reason, computed from the evaluator configs rather than hard-coded.

The split is stratified per application with a fixed seed, 40% development and
60% test, frozen before any run: **53 development, 80 test**.

## Grading

OSWorld's `evaluate()` is re-implemented, not approximated:

- `postconfig` steps that grading depends on are replayed; window activation and
  `pyautogui` steps are skipped and recorded as skipped.
- `func` may be a list; `conj` may be `or`; `and` short-circuits on 0 and `or` on 1.
- `infeasible` tasks score 1 only when the agent's last action was `FAIL`.
- A missing result file scores 0, exactly as upstream does.
- A missing third-party package is reported as `grader_unavailable` with the
  package name, never silently scored 0.

Spreadsheet outputs are recalculated with headless LibreOffice before grading,
because `openpyxl` writes formulas without cached values and `compare_table`
reads the cached values.

## The ceiling check

A task can only measure the harness if the standard answer survives a round trip
through the harness. `roundtrip` therefore does two things per task:

1. **baseline** — score the reference file as if it were the agent's output.
   If it does not score 1.0, the reference is not a fixed point, and the task is
   reported as `not_decidable` rather than blamed on the harness.
2. **round trip** — import the reference through the harness, export it again,
   re-score. A drop against the baseline is a real harness loss.

```bash
python -m harness_bench roundtrip
```

Every task ends in exactly one status, so the denominators are always explicit:

| Status | Meaning |
| --- | --- |
| `graded` | the round trip ran and produced a score |
| `reference_is_not_a_fixed_point` | the reference itself scores below 1.0; not the harness's fault |
| `no_reference_file` | the task has no answer file to round trip |
| `needs_soffice` | a spreadsheet task with no LibreOffice to recalculate it |
| `harness_not_installed` | the harness binary for that app is not on `PATH` |
| `reference_unavailable` | the answer file could not be fetched (network), so nothing was measured |
| `input_unavailable` | the task's input files could not be fetched |

The last two exist because a download failure must never be mistaken for a
harness loss. `fetch()` uses a short `(10, 30)`-second timeout so an unreachable
host fails one task in seconds instead of stalling the whole batch.

## Running

```bash
python -m harness_bench doctor                 # what is missing
python -m harness_bench tasks --excluded       # the task set
python -m harness_bench split                  # freeze the split
python -m harness_bench roundtrip              # the ceiling check
python -m harness_bench run --interface harness script --split-side dev
python -m harness_bench report                 # the tables
```

`run --dry-run` grades the standard answers without calling a model. It is the
week-1 exit criterion: it proves the download, the path rewriting, the
normalisation and the grader all agree before a single token is spent.

A run writes everything it learned:

```
runs/<interface>/<task-id>/<timestamp>-r<n>/
    record.json       status, score, cost, failure class
    transcript.json   the full model transcript
    postconfig.json   which grading prerequisites ran, and which were skipped
    normalize.json    what happened to the produced files
```

## Configuration

One YAML file pins the whole experiment:

```yaml
model:    {base_url, model, api_key_env, vision, temperature, max_tokens, timeout_s}
budget:   {max_tool_calls: 50, timeout_s: 900}
split:    {dev_fraction: 0.4, seed: 20260601}
harness:  {libreoffice, gimp, skill_docs}
gui:      {command, tools, start_timeout_s, idle_gate_s}
apps:     {soffice, gimp}
repeats:  {harness: 3, script: 3, gui: 1, hybrid: 1}
```

The GUI tool list is a whitelist. If a Windows-MCP server exposes `WriteFile` or
`RunCommand`, the interface still refuses them, because an agent that can write a
script is no longer a GUI agent.

## Requirements

Grading needs `openpyxl pandas python-docx python-pptx pymupdf pypdf pdfplumber
rapidfuzz scikit-image cssselect xmltodict formulas odfpy tldextract`. The GUI arm
needs a Windows-MCP server. Spreadsheet grading and the five CSV tasks need
LibreOffice (`soffice`).

`doctor` checks all of it and says what is missing; nothing is installed
automatically.

### Local LibreOffice without a system install

LibreOffice can live inside the repository instead of `Program Files`. The
checked-in configuration expects exactly that:

```
tools/LibreOffice_26.8.3_Win_x86-64.msi      # the download
tools/libreoffice/program/soffice.com        # administrative-install unpack
```

```powershell
# unpack without writing the registry or touching Program Files
msiexec.exe /a tools\LibreOffice_26.8.3_Win_x86-64.msi /qn `
  TARGETDIR="$PWD\tools\libreoffice"

# point the configuration at it (already the default in configs/bench.yaml)
# apps: {soffice: tools/libreoffice/program/soffice.com}
```

Use `soffice.com`, not `soffice.exe`: the `.exe` is a GUI-subsystem binary and
prints nothing when its output is redirected. `find_soffice()` prefers the
console variant automatically, and `soffice_env()` puts its directory on `PATH`
so that a harness which calls `shutil.which("soffice")` finds it too.
`tools/` is git-ignored.

## Tests

```bash
python -m pytest tests -q
```

The tests use a fixture that stages tasks without downloading, so the suite runs
offline.

## Status of this machine

Nothing here is a result: the experiment has not been run. What has been
verified is that the pipeline is wired correctly, on a machine that now has
LibreOffice and both harnesses available locally.

| Check | Outcome |
| --- | --- |
| `pytest tests -q` | 91 passed |
| `run --dry-run` (all 133 tasks) | 107 self-graded, 94 score 1.0, 13 fail, 21 not gradeable |
| `roundtrip` (all 133 tasks) | see `runs/roundtrip.json` |
| `doctor` | soffice, `cli-anything-libreoffice`, `cli-anything-gimp`, 10 graders — all found |

The 13 self-grading failures are the tasks whose reference file is not a fixed
point of its own grader (7 GIMP, 1 Writer, 5 Impress); they are explained one by
one in `docs/report-template.md` §3. With LibreOffice in place the five Calc
tasks that used to fail on `postconfig: soffice not found` now grade correctly.

The 21 "not gradeable" tasks are a **network** limit, not a software one:
HuggingFace was unreachable while this was measured, so their reference file
could not be fetched. They are counted separately and never as harness losses.

## Licence and provenance

`third_party/osworld/` is Apache-2.0, copied from
[xlang-ai/OSWorld](https://github.com/xlang-ai/OSWorld) at `5123bfdbbc`; see
`third_party/osworld/LICENSE`.
