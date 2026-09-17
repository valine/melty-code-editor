> Implemented: see [fixes and repeat measurements](FIXES.md). This document records the preceding investigation.

# Long-stall investigation — September 14–15, 2026

## Findings

The large pauses are predominantly the UI waiting while background Python work runs. They are distinct from the approximately 18 ms median edit cost.

Five launches through Hyprland desktop MCP reproduced a **169–182 ms frame during the actual typing burst**, with **19–23 ms of main-thread CPU**. The repeat without sampling in the final capture measured **175.68 ms elapsed / 22.22 ms UI CPU**. All 43 injected edits were consumed in each run.

### 1. End-of-file edits trigger a full reparse and expensive completion of that parse

The test loads a 481,054-byte scratch copy of `new_core_view.py`, goes to the end, presses Return, and types `def my_func(): pass`, Return, `def other_func(): pass`, Return, one character per MCP action (120 ms batch delay plus injection overhead).

`core_syntax.reparse_incremental` falls back to `reparse_reusing` when an edit is outside the existing statement extents (including the file tail). This fallback actually occurred in the captured typing run.

Final capture, same background `bg:_run_chain_in` thread:

| Operation | Start–end, monotonic seconds | Elapsed |
|---|---|---:|
| Full `scan_extract` (includes isolated scan and deserialization) | 7074.895979–7075.165568 | 269.59 ms |
| `materialize_parse` | 7075.165625–7075.194790 | 29.16 ms |
| Remaining work in `reparse_reusing`, mainly tree merge/reuse | 7075.194838–7075.221881 | ~27.04 ms |
| Whole-buffer `_compile_check` | 7075.227705–7075.274630 | 46.93 ms |
| **UI frame** | **7075.100825–7075.276501** | **175.68 ms / 22.22 ms CPU** |
| UI `post_frame` within that frame | 7075.120041–7075.276482 | 156.44 ms / 3.12 ms CPU |

The scanner already uses a subinterpreter with its own interpreter lock. Therefore **do not attribute all 270 ms of scan_extract to holding the UI's lock**. That wrapper also includes deserialization back in the application's interpreter; the individual boundary has not yet been separately timed. The materialization, tree reuse, and compile stages then execute in the application's interpreter. The compile alone used 46.84 ms of CPU.

The time alignment, small UI CPU usage, and nearly immediate completion of `post_frame` when the worker finishes strongly support interpreter-lock contention. The renderer makes native graphics calls which release the lock; a CPU-heavy Python worker can repeatedly delay its reacquisition. This is also the mechanism already described by the repository's `_park_while_frame` helper. A slow `post_frame` wall timer must not be interpreted as 156 ms of GPU/render computation.

### 2. Background relint is another substantial stall source

A separate capture recorded a **183.56 ms UI frame / 11.3 ms UI CPU** overlapping **177.70 ms of check_source_incremental / 172.28 ms worker CPU**:

- AST parse: 52.05 ms elapsed, 47.75 ms CPU.
- Initial whole-buffer `_buffer_bound_names`: 50.26 ms elapsed, 50.11 ms CPU.
- Collector/checking, allocation and remaining work fill the interval.
- GC callbacks recorded 10.49 ms generation-0 and 8.03 ms generation-1 collections on the lint worker; these are nested within the above work, not additive independent totals.
- UI post_frame: 127.43 ms elapsed / 2.95 ms CPU.

**Timing distinction:** this relint stall happened before the first character of the actual typing burst, while the earlier harness phase was already named `typing`. It establishes a background-repaint stall and a hazard when input resumes, but it is not a measured mid-burst relint stall in this capture. The full-reparse stall above is measured during actual typing in every run.

`_run_relint` waits for quiet input only at entry, then calls the lint and suggestion passes. Once a long pass starts, it does not become interruptible merely because it is on a background thread. The incremental lint's first call seeds itself with a full lint and full bound-name scan.

### 3. Smaller synchronous spikes are separate

Cold `_completion_pool` measured approximately 22–26 ms of UI CPU, producing roughly 52–57 ms edit frames. These deserve attention after the 170+ ms stalls; they are genuine UI computation, unlike most of the longest frame's elapsed time.

## Fix order

1. **Keep file-tail edits incremental.** Parse/reconcile the changed tail instead of routing common EOF typing through full-file extraction, deserialization and tree reuse. Preserve statement identity and test blank tails, decorators, incomplete syntax, imports and undo.
2. **Isolate the whole-file compiler check.** Run pure compile/error extraction under an independent interpreter lock or process, returning a small error payload. Another ordinary Python thread does not provide isolation. Preserve duplicate-argument and context-dependent syntax diagnostics.
3. **Budget parse-result finalization.** Measure deserialization separately, add bounded cooperative frame handoffs to pure-Python materialization/merge work, and drop superseded parse results before paying avoidable finalization costs. Waiting once before starting is insufficient.
4. **Prevent cold lint from monopolizing the interpreter.** Split/shorten the bound-name scans and collector work; isolate native AST parsing where practical. Add cooperative checkpoints between bounded chunks and coalesce overlapping relint/reparse requests for the same file. Live namespace-dependent lint cannot simply be moved wholesale into an isolated interpreter without preserving its context.
5. Repeat the same desktop test, plus pause/resume bursts and edits in the middle of a large function. Track worst frame, frames over 50/100 ms, and input-event-to-frame delay; frame duration alone can miss time spent unable to begin the frame.

This turn adds profiling artifacts only; no further production implementation changes were made. The 120 FPS target remains unmet.

## Artifacts and method

- `runner.py`: thresholded function spans, `Surface.frame` elapsed/thread CPU timing, existing draw_text section timings, and GC callbacks. No cProfile. Logging runs once per frame and only for spans over 10 ms / GC over 2 ms.
- `timeline.jsonl`: final capture with parser-stage timers.
- `attributed-lint/timeline.jsonl`: relint/AST/bound-name attribution.
- `expanded/`, `first/`: earlier unsampled replications.
- `sampled/timeline.jsonl`, `gil-trace.json`: exploratory py-spy capture. It reproduced the stall but is not the basis of precise attribution here; use direct same-clock spans above. Do not treat Chrome trace B/E span widths for intermittently sampled threads as exact CPU occupancy.
- `summary.json`: actual typing burst boundaries and per-run maxima. Boundaries use the first and last actual editor `changed=True` frames, excluding pre-burst setup even when the phase label says typing.

The isolated desktop was released and the scratch editor was closed normally. Only scratch source copies were edited by the test.
