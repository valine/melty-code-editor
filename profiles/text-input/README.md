> Historical baseline. See [optimization results](OPTIMIZATION.md) for implemented changes, the corrected edit/follow-up measurement distinction, and current target status.

# Large-file text-input profile — 2026-09-14

## Reproduction

Used the Hyprland desktop MCP (`launch`, `batch`, `type_text`, `key`, `screenshot`) on reserved desk-2 / agent-desk-2. Opened a scratch copy of `latent-descent/src/lsd/gl_gui/view/core_views/new_core_view.py`: 481,054 UTF-8 bytes, 479,027 Python characters, 9,717 newline characters. Default 1280×800 code-editor window, normal highlighting/folding/analysis/completion settings, fresh isolated XDG state. Clicked the text, Ctrl+End, Return, then typed `def my_func(): pass` one character at a time, followed by Return. Per-character batch delay was 120 ms, with additional MCP injection overhead. Verified all 20 edits in the displayed buffer and the final function visually (`typed.png`). This exercises the code editor in this repository, not the separate bare-text-editor app.

`runner.py` wraps Surface.frame; no editor/framework source changes. `first-frames.jsonl` is the clean frame-timing run and separate cProfile pass. `frames.jsonl` is a second run with targeted wall-clock timers; `spans.jsonl` records function duration and thread. Frame duration includes render/presentation work, excludes harness logging and time waiting for the next input. The app is event driven, so idle FPS and averages across idle time are misleading. Reciprocal frame durations below describe the frame budget, not measured sustained refresh rate or input-to-photon latency.

## Results

| Measurement | First run | Targeted-timer repeat |
|---|---:|---:|
| Cursor movement: median frame | 8.59 ms | 9.43 ms |
| Cursor movement: p95 frame | 12.69 ms | 13.82 ms |
| Typing: median of 20 edit frames | 39.72 ms | 39.99 ms |
| Typing: worst edit frame | 69.59 ms | 161.18 ms |
| Worst frame during typing phase, including follow-up work | 326.71 ms | 161.18 ms |

Typical edits consume about 4–5× the cursor-movement frame time: approximately a 25 FPS frame budget versus 106–116 FPS. The worst observed follow-up frame consumed 327 ms. Most animation/follow-up frames are cheaper, so taking the median of *all* typing-phase frames (~10 ms) hides the problem.

## Measured hot paths

Wall-clock timings below are from the second run, on **MainThread**, without cProfile. Inclusive totals overlap; do not sum them as exclusive costs. Timings can include scheduling/GIL delays.

| Function | Calls during typing phase | Median per call | Total |
|---|---:|---:|---:|
| `_scope_guide_segments` | 39 | 11.15 ms | 439 ms |
| `_common_indent` | 61 | 6.19 ms | 380 ms |
| `_def_tints` | 388 | 0.02 ms | 343 ms |
| `extract_table` | 19 | 15.29 ms | 291 ms |
| `_fim_poll` | 388 | 0.03 ms | 283 ms |
| `collect_import_suggestions` | 20 | 1.00 ms | 197 ms |
| `_fold_build` | 20 | 6.25 ms | 122 ms |
| `_completion_pool` | 13 | 5.61 ms | 91 ms |

Specific findings:

1. **Whole-display scope-guide scans nearly twice per keystroke.** `text_editor.py:13950` caches by display-text identity; changed text triggers `_scope_guide_segments` (`:8763`), a scan of all displayed lines. The 39 scans alone cost 439 ms for 20 edits. Folded display reconstruction (`_fold_build`, `:9372`) adds ~6 ms per edit. `_scope_fold_ranges` itself only ran once in this interval; it is not the per-keystroke culprit here.
2. **Full-buffer indentation computation repeats.** `new_converters.py:710`, `_common_indent`, invokes `textwrap.dedent` and splits both original and dedented text. It ran 61 times in the 20-edit interval, costing 380 ms.
3. **Symbol-roster rebuilds are synchronous.** `symbol_roster.py:748`, `_pending_table`, calls `extract_table` on a changed file key. The measured 19 rebuilds ran on MainThread at ~15 ms each. Tint and completion consumers can trigger this cost. `_def_tints` is usually cheap but reached 25 ms; `_fim_poll` reached 31 ms.
4. **Import suggestions have a long tail.** One `collect_import_suggestions` call consumed 101 ms inside the 161 ms edit frame. The clean cProfile pass also shows code-check traversal and import-scanning activity. The large-buffer caller uses `incremental_only=True` (`text_editor.py:16382`), but that only refuses the full fallback; it does not guarantee that `_incremental_scan` and its helpers are cheap. Further nested timers are needed to identify the exact work inside that spike.
5. **Extra follow-up stalls remain.** Some 100+ ms frames have little time in the selected functions. The 327 ms first-run stall is confirmed by frame timing but is not conclusively attributed by these timers. Do not assign it wholly to parsing, linting, or GPU work without a broader timeline.

## Suggested optimization order

- Reuse/update scope-guide and folded-line structures from the actual edit range; avoid whole-display reconstruction and duplicate scans on each character.
- Avoid repeated whole-buffer dedent/indent work for file-host conversion; carry known indentation where possible.
- Keep the last symbol roster usable during typing and update from the changed region or a worker, rather than rebuilding synchronously when a consumer asks.
- Instrument the import-suggestion spike and remaining render-host/post-frame stalls before moving or disabling entire features.

Keep syntax, folding, colors, and completion functional while doing this. No optimization has been applied in this task.

## Artifacts and rerunning

- `runner.py`: guarded entry point (required because the app starts multiprocessing workers).
- `first-frames.jsonl`: clean run plus cProfile phase; use only baseline/typing phases for latency.
- `frames.jsonl`, `spans.jsonl`: targeted timing repeat.
- `typing_profile.prof`, `profile.txt`: cProfile call inventory. Profiling substantially slows input; do not use these durations as user-facing latency estimates. Prefer direct wall timers for cost attribution.
- `typed.png`, `visible-text.txt`: acceptance evidence.
- `exploratory-frames.jsonl`, `exploratory.prof`: excluded preliminary capture; an unguarded harness caused a worker to open another window. Corrected before both reported runs.

To rerun: restore `new_core_view.py` from the source; reserve a desktop; launch `.venv/bin/python profiles/text-input/runner.py` through MCP with a fresh `XDG_STATE_HOME`. Set `phase.txt` to `warmup`, `baseline`, `typing`, or `typing_profile:profile` between input batches. A subsequent phase/frame flushes the profile to disk. Logs append; archive them first. Close the window normally to save only the scratch file, then release the desktop.
