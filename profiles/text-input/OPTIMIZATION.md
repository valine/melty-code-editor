# Typing optimization results — 2026-09-14

## Status

**Improved, but the 120 FPS target is not met.** A 120 Hz frame has an 8.33 ms budget. The latest real-input run has an 18.27 ms median edit frame and a 16.48 ms median follow-up frame. Long stalls remain.

Changes are in the editable melty implementation in `/home/lukas/Desktop/latent-descent`; the code-editor entry point itself did not need changes. `optimization.patch` captures only the files changed for this task, including the new tests.

## Desktop measurement

Hyprland desktop MCP, reserved desk-2 / agent-desk-2, 1280×800 app window, default editor features, fresh isolated XDG state. Loaded a scratch copy of `new_core_view.py` (481,054 bytes, 9,718 lines), Ctrl+End, Return, then typed `def my_func(): pass` one character at a time plus Return, with 120 ms batch delay and MCP injection overhead. Verified the resulting text and captured `optimized-typed.png`. No tests or sampling profiler ran during the final typing batch.

The final `runner.py` times `Surface.frame`, with no per-function timers, cProfile, or line tracing enabled during the measurement. It captures the editor's existing `changed` trace signal as `edited`. These are frame execution durations, including rendering/presentation; they are not sustained refresh rate or input-to-photon latency. Idle gaps are excluded.

**Correction to the original report:** its `changed` signal comes from the published active-editor buffer, which lags the actual input frame by one frame. The original approximately 40 ms number describes the follow-up frame, not the frame that first consumes the keystroke. The final harness records both signals.

| Measurement | Original | Latest final run |
|---|---:|---:|
| Actual input/edit frame, median (20 edits) | not separately recorded | 18.27 ms |
| Actual input/edit frame, p95 / maximum | not separately recorded | 39.14 / 49.70 ms |
| Published-buffer follow-up frame, median | 39.72 ms | 16.48 ms |
| Follow-up frame, p95 / maximum | — / 69.59 ms | 22.78 / 165.67 ms |
| Worst frame anywhere in typing phase | 326.71 ms | 165.67 ms |

The comparable median fell by 58.5% (2.41× faster). A prior minimal-instrumentation repeat (`clean-first/frames.jsonl`) measured 18.59 / 17.15 ms medians and a 203.19 ms worst frame. A small-file control with the detailed timers measured approximately 8.2 ms edit/follow-up frames. This rules out claiming that the remaining work is exclusively proportional to file size.

## Implemented

- Skip full-buffer common-indent calculation when there is no outgoing conversion chain.
- Share the edit splice calculation across consumers; update lexer line offsets and line widths incrementally.
- Reuse scope guides when an inline edit preserves indentation and block-opening shape.
- Update inline fold layout instead of reconstructing all displayed lines; retain the exact edited display string into the next frame. Fixed a local variable collision that otherwise defeated that cache. Fold gutter numbers are projected lazily.
- Resume roster extraction from a safe top-level checkpoint before the changed region; avoid assignment regex work on function-local expressions. Cache state is bounded and adds no slots to live FileTable instances.
- Keep import discovery/module parsing out of the large-buffer render-thread incremental lint path. Cache misses fall back to the existing background relint. Completion's imported-name lookup uses the roster instead of reparsing module binds.
- Reuse the completion candidate pool while editing the same identifier; refilter by the current prefix.
- Avoid FIM context assembly when no provider is registered.
- Avoid comparisons of private/excluded state that cannot trigger invalidation; memoize token occurrence highlighting and cull its offscreen rectangles.
- Evict one line-offset cache entry at capacity instead of clearing every buffer's offsets.

Syntax highlighting, folds, guides, completion, and diagnostics remain enabled. Structural edits retain the existing full-rebuild fallback.

## Validation

- Combined tokenizer, folding, guides, FIM, private invalidation, and new incremental-path regression suites: **121 passed, 1 deselected**. The deselected FIM value-shape test requires torch, which is absent from the editor venv. GLFW-not-initialized warnings arise in these headless unit tests.
- After the final compatibility cleanup: **12 passed**, covering helper parity, fold edit frames, and the multi-span roster case.
- Roster/world suites: **18 passed** on the final diagnostic run. Repeated combined runs intermittently fail the global-generation stability assertion in `test_several_live_spans_of_one_file_merge_without_thrashing`, sometimes followed by a background-thread abort at process teardown. The individual test passes. This remains an unresolved test-isolation/concurrency issue; a pristine-HEAD comparison passed, so it is not conclusively classified as pre-existing.
- Completion suite has two failures (lexer gate on the real text_editor source, and expected candidate kind `name` versus `var`). Both were reproduced with pristine HEAD text_editor loaded in an isolated process: 2 failed, 9 passed.
- `git diff --check` passes.

New tests compare incremental results against fresh fold/guide/width/roster builds, including randomized edit sequences; compare changed-region compilation with the original line-diff algorithm; check fold display identity across frames; and verify that cached-only import scans do not discover imports synchronously.

## Remaining work

The 8.33 ms target requires further work. Detailed runs attribute roughly 3–5 ms of edit-frame time to fold reassembly plus region syntax/import checks, and approximately 6–8 ms outside the raw text-editor body to the shared render path. Structural guide updates and cold completion add occasional cost. The 166–203 ms tails are measured but not conclusively attributed; moving selected import work off the input path did not eliminate them. Further timeline work must distinguish synchronous UI work from background Python/GIL contention.

Artifacts: `frames.jsonl` (final), `clean-first/frames.jsonl` (prior minimal repeat), `small-control/` (control), `pass1`…`pass10` (intermediate detailed runs), `runner-detailed.py` (diagnostic harness), `sampling.txt` (whole-loop sample), and `optimized-typed.png` (visual acceptance).
