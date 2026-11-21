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
  interfaces.py         the four OSWorld arms
  agent.py              the tool-calling loop
  runner.py             one run = one directory of evidence
  roundtrip.py          the harness ceiling check
  owncli.py             our own CLI's task set, sandbox and checks
  owncli_run.py         the fifth arm: our own CLI, same loop and budget
  report.py             the tables
  cli.py                the command line
configs/bench.yaml      the whole experiment in one file
configs/split.json      the frozen development/test split
third_party/osworld/    the vendored tasks and graders (Apache-2.0)
tests/                  the test suite
```

## Two tracks

The OSWorld track above answers the proposal's question about a *third-party*
harness. It says nothing about whether the method transfers to a harness we
write ourselves, so there is a second track on our own blog CLI:

```
python -m harness_bench owncli --dry-run   # the 12 tasks and their coverage gaps
python -m harness_bench owncli             # drive the agent, then check the files
```

Same model, same budget, same record format, same report. The difference is that
the checks read the site the CLI produced — front matter, `public/` output —
never the CLI's own stdout, so a run cannot pass by describing what it would do.
Each task gets a fresh git site repo, a pages repo and a private
`NIZIGEN_BLOG_HOME`, so a run cannot touch the real blog checkouts.

Twelve tasks. When the track was written the CLI could express only **8** of
them; the four gaps (editing an existing post, deleting a post, backdating a
post, editing the site title) were recorded as `expressiveness` rather than
blamed on the agent, and closing them is what `post show/edit/delete`,
`post new --date` and `site show/set` exist for. With those commands in place
all **12** are expressible and a scripted reference solution scores 12/12.

## CLI against GUI

The same model, budget and record format, on tasks each interface can actually
reach. `auto/deepseek-v4.1-flash`, 50 tool calls, one run each.

| task | own CLI | GUI | third-party harness |
| --- | --- | --- | --- |
| edit a post's title (`blog-09-edit-title`) | **1.00** — 9 calls, 13k tokens, 17 s | 0.50 — 50 calls, 1.0M tokens, 8 screenshots | not applicable |
| set the whole document to Times New Roman (`0e763496`) | not applicable | **1.00** — 50 calls, 707k tokens, 22 screenshots | 0.00 — 50 calls, 164k tokens |

The traces suggest the following explanations; one run per task cannot establish a general advantage:

- The LibreOffice harness *has* the verb — `style create` plus `style apply
  <name> <index>` — but it applies to one content index at a time. Fifteen
  paragraphs used up the whole budget; `document json` already showed
  `font_name: "Times New Roman"` when the run stopped, with no export step left.
  The GUI selects everything and sets the font once, so it wins on **batch
  semantics**.
- Editing one line of YAML front matter is the reverse. The GUI spent 50 calls
  on Ctrl+H, double clicks, `Home`/`Shift+End` and two undo presses and the file
  still read `title: "Welcome"`; one CLI command does it. Precise text editing
  is where coordinate clicks lose.

The GUI runs report substantially higher cumulative API token usage. Screenshots and repeated UI-tree context both contribute; PNG byte size is not a token count or a billing estimate.


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
python -m harness_bench owncli                 # our own CLI, our own tasks
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
model:    {base_url, model, api_key_env, vision, image_history, temperature, max_tokens, timeout_s}
budget:   {max_tool_calls: 50, timeout_s: 900}
split:    {dev_fraction: 0.4, seed: 20260601}
harness:  {libreoffice, gimp, skill_docs}
gui:      {command, tools, start_timeout_s, idle_gate_s}
apps:     {soffice, gimp}
owncli:   {binary, template, hugo, home}
repeats:  {harness: 3, script: 3, gui: 1, hybrid: 1, owncli: 3}
```

The GUI tool list is a whitelist enforced on **both** sides: the client refuses a
tool that is not on it, and `GuiConfig.server_command()` passes it to the server
as `--tools`, so the server never exposes `PowerShell`, `FileSystem`, `Scrape`,
`Registry`, `Process` or `Clipboard` in the first place. An agent that can write
a script is no longer a GUI agent.

Screenshots are delivered to the model as `image_url` content parts, not as text
placeholders; `model.image_history` keeps only the most recent ones attached and
folds older ones to a one-line summary, so a long GUI run does not grow the
prompt without bound. Full transcripts still keep everything.


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

The full preregistered experiment has not been run. The results below are engineering checks and exploratory single-run comparisons, not a population-level estimate. Earlier writer smoke runs include 51/52-call budget violations and are excluded from strict comparisons; the paired runs use the corrected 50-call limit.

| Check | Outcome |
| --- | --- |
| `pytest tests -q` | 113 passed |
| `run --dry-run` (all 133 tasks) | 107 self-graded, 94 score 1.0, 13 fail, 21 not gradeable |
| `roundtrip` (all 133 tasks) | finished; **70 measured, 12 survived, 58 lost, ceiling 17.1%**. See `runs/roundtrip.json` |
| `doctor` | soffice, `cli-anything-libreoffice`, `cli-anything-gimp`, 10 graders — all found |
| GUI arm | `windows-mcp` starts in ~6 s; a `Screenshot` returns a real 1.4–2.9 MB PNG that reaches the model as an image part |
| own-CLI arm | 12 blog tasks, 12 expressible; a scripted reference solution scores 12/12. Real model: **12/12 at 1.00**, 292 calls / 1.08M tokens / 954 s total |
| third-party harness arm | writer dev set, 50-call budget: **1 of 8** reaches 1.00; the other 7 stop at `tool_call_budget` in these exploratory runs; the font-task trace shows repeated per-paragraph style application |

Ceiling-check tallies (every task imports and re-exports through LibreOffice,
~1–2 min each), out of the 133 adopted tasks:

| Family | decidable | survived | lost | not decidable | no reference | artifacts |
| --- | --- | --- | --- | --- | --- | --- |
| gimp | 2 | 1 | 1 | 7 | 2 | 0 |
| libreoffice_calc | 37 | 7 | 30 | 1 | 0 | 2 |
| libreoffice_writer | 12 | 4 | 8 | 1 | 0 | 1 |
| libreoffice_impress | 19 | 0 | 19 | 4 | 13 | 0 |
| **total** | **70** | **12** | **58** | **13** | **15** | **3** |

Plus 11 infeasible tasks (nothing to round trip) and 21 tasks whose reference
answer could not be downloaded (Hugging Face was unreachable) — the latter are
`grader_unavailable` and never enter the denominator.

Three references are rendered artifacts (two PDFs, one PNG): there is no
editable document to import, so a round trip says nothing about the harness.
They are counted as `rendered_artifacts`, not as losses.

Inspected examples reveal concrete mechanisms: charts
dropped on export, `0.00%` number formats degraded to `General`, formulas
written as `=of:=B2-C2-…` so the recalculation yields `#VALUE!`; on impress the
import/export model keeps only text boxes, so every shape and group is lost
(6 slides with 5–13 shapes each come back as 1–2 text boxes). See
`docs/report-template.md` §4.

The 13 self-grading failures are the tasks whose reference file is not a fixed
point of its own grader (7 GIMP, 1 Writer, 5 Impress); they are explained one by
one in `docs/report-template.md` §3. With LibreOffice in place the five Calc
tasks that used to fail on `postconfig: soffice not found` now grade correctly.

The 21 "not gradeable" tasks are a **network** limit, not a software one:
HuggingFace was unreachable while this was measured, so their reference file
could not be fetched. They are counted separately and never as harness losses.

## Licence and provenance

The runner is MIT (`LICENSE`). `third_party/osworld/` is Apache-2.0, copied from
[xlang-ai/OSWorld](https://github.com/xlang-ai/OSWorld) at `5123bfdbbc`; see
`third_party/osworld/LICENSE`.
