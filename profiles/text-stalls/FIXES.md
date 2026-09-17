# Long-stall fixes — 2026-09-15

## Result

The repeatable 169–182 ms typing stall is substantially reduced. Three fresh-launch desktop typing runs peaked at **60.00, 55.40, and 70.60 ms**. A pause/resume run peaked at **66.20 ms**. There were **no frames over 100 ms during 177 verified edits**, and none elsewhere in the non-warmup portions of these captures (including trailing background work; the largest such frame was 70.71 ms).

Median actual edit time remains **18.4–18.7 ms**. This work targets the long pauses; **120 FPS / 8.33 ms is still not achieved**. These are Surface.frame execution times, not input-to-photon latency. A short test cannot guarantee the absence of every future stall.

| Desktop test | Edits | Worst frame | Frames >100 ms |
|---|---:|---:|---:|
| Fresh launch 1 | 43 | 60.00 ms | 0 |
| Fresh launch 2 | 43 | 55.40 ms | 0 |
| Pause/resume, 650 ms pauses | 48 | 66.20 ms | 0 |
| Fresh launch 3 | 43 | 70.60 ms | 0 |

Same approximately 481 KB / 9,700-line new_core_view source, normal features, isolated XDG state, 1280×800 window on reserved desk-2. Input was delivered character-by-character through Hyprland desktop MCP. The source checkout had small concurrent user edits, so these are current source copies rather than byte-identical copies of the original baseline fixture. Each run's edited fixture and raw timeline are retained in its directory. No tests or sampling profiler ran during the reported input batches. The source's current whitespace edits in unrelated files were preserved.

## Changes in latent-descent

### Incremental end-of-file parsing — core_syntax.py

Tail insertions and edits now parse the final statement plus its trailing gap instead of triggering full-file extraction, deserialization, materialization and merge. Including the final statement preserves extensions of a function/class body. Semicolon-separated statements on the same physical line are included together to preserve columns. A new name that collides with a retained top-level name conservatively uses the full parser's global disambiguation. Oversized regions and unsupported boundaries retain the existing fallback.

### Isolated compiler — syntax_check.py and syntax_check_worker.py

Moved the existing pure compile/dedent/context-wrapper logic into a small standalone helper. Large background checks run in a dedicated CPython 3.12 subinterpreter, with an independent interpreter lock; small and render-thread checks retain the direct path. Only source text, wrapper prefixes and a small diagnostic payload cross the boundary. All SyntaxError location fields are transported explicitly: pickling SyntaxError alone loses location slots for some compiler errors.

The interpreter is created, used and destroyed on its owning worker thread. Calls serialize through a queue, and the process-level worker survives module reloads. The pure helper is loaded again per request so source changes apply. App exits were verified to leave no editor process running. If the platform lacks the subinterpreter APIs, checks fall back to the original local compiler; that fallback preserves diagnostics but cannot provide the same isolation.

A separate cold-worker check compiled 50,000 assignments in ~146 ms while a 1 ms main-thread heartbeat's maximum gap was ~1.06 ms. This verifies that the compilation itself no longer monopolizes the main interpreter.

### Background handoffs — code_checks.py and core_syntax.py

Added existing UI-yield checkpoints around AST parsing, through collector traversal and token scanning, between and within bound-name regex matches, and through lint worklists. Parse results yield before deserialization, before materialization and merge, and periodically during materialization. Main/GL thread guards remain in place: the render thread never sleeps to yield to itself. Diagnostics remain enabled and resume after input goes quiet.

Native AST parsing and individual native regex/deserialization operations are still not preemptible within a call. These changes break up combined stalls; they do not promise every background stage is below 8 ms. The existing background runner's result coalescing remains in use; this change does not introduce a new stale-job cancellation protocol.

## Verification

- Broad selected compiler, parser, lint and incremental-layout suites: **119 passed, 1 deselected, 312 subtests passed**. The deselected real-source incremental parser case was run separately below.
- Final tail/compiler regression file: **15 passed**. Covers bounded append paths, function/class body extension, no final newline, decorators/comments/imports, semicolon columns, duplicate names, typing/deletion, exact round trips and compiler diagnostic transport/shutdown.
- Real-source incremental parser suite: **4 passed, 81 deselected, 41 subtests passed**, including large source files and middle-of-file edits. Repeated after the final boundary fix.
- Scoped git diff whitespace check passes for the modified tracked files. The checkout has unrelated whitespace warnings in user-edited files; those files were not changed by this task.
- All standard desktop runs launched and closed normally. An additional experimental cross-thread stack-sampling harness aborted during startup (`munmap_chunk(): invalid pointer`), before recording a parse span. It was excluded; its cause was not established. The subsequent standard harness launch/typing/exit succeeded. The experiment is retained under `diagnostic-after/`, not counted as a benchmark or a successful check.

## Remaining work

Cold completion still contributes roughly 22–28 ms of synchronous work. A separate 40–60 ms wait outside the raw text-editor body remains in some frames; its coincidence with compiler activity is not sufficient to attribute it to the now-isolated compile call. Further work should target these smaller tails and measure event-delivery-to-render latency, plus mid-file performance under longer sessions. Full-fallback finalization and native cold-lint work can still be expensive for other edit patterns.

Artifacts: `after-summary.json`, `after1/timeline.jsonl`, `after2/timeline.jsonl`, `after3/timeline.jsonl`, and `after3/typed.png`. Earlier attribution remains in README.md and the baseline directories. The reserved desktop was released after validation.
