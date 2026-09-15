# Melty Code Editor startup

Measured September 14, 2026, native Wayland/EGL on a reserved Hyprland desktop.
Eight alternating fresh-process launches of each version, opening the same
11,131-byte Python file. Each process creates its own display, GL contexts,
font state and windows. Normal OS, driver and font disk caches remain.

| Milestone | Before median | After median | Improvement |
| --- | ---: | ---: | ---: |
| First frame presented | 519 ms | 396 ms | 123 ms / 24% |
| Selected file's actual text presented | 652 ms | 526 ms | 126 ms / 19% |

First-frame ranges were 503–531 ms before and 385–408 ms after. Text-ready
ranges were 635–663 ms before and 514–547 ms after. Both versions took three
frames to present the real text. Presentation means the buffer swap returned,
not physical display scanout; text readiness does not mean all background
analysis or autocomplete indexing has finished.

Raw results: [startup-final.json](profiles/startup-final.json). This is the
completed comparison of the final code. Earlier runs had heavy system-load
spikes or lost their test seat; they are not used for the table above.

## Changes

1. **Start Melty before importing the editor views.** `melty.boot(app_id=...)`
   exposes the existing idempotent bootstrap. The code editor calls it before
   its eager imports, then resolves `draw_code_editor` through Melty's public
   API, which waits for the import worker. EGL initialization and context
   creation now overlap the view imports. Help exits before booting.
2. **Avoid loading GLFW for native-window constants.** The shared window API
   and native backend use a small table of GLFW-compatible numeric constants.
   Tests compare every entry with the installed pyGLFW API. The GLFW fallback
   still loads and uses the real implementation.
3. **Keep metadata inspection from loading GLFW.** Source analysis probes
   `window_api.__module__`; forwarding missing module metadata to GLFW caused
   an unnecessary late library import. Missing dunder attributes now raise
   `AttributeError` directly. Every final native sample remained free of GLFW
   imports through text readiness.

The editor's direct close operation also uses `melty.window_api`, so native
handles go through their own backend.

## Profile findings

Before the changes, most imports completed before graphics initialization even
started. The final paired phase medians moved as follows:

| Phase, elapsed from process launch | Before | After |
| --- | ---: | ---: |
| Window API ready | 332 ms | 56 ms |
| Owner context current | 429 ms | 193 ms |
| Melty import worker finished | 338 ms | 287 ms |
| Fonts loaded | 433 ms | 311 ms |
| Visible window created | 477 ms | 355 ms |

These phases overlap; their elapsed times must not be added. Import profiling
identified LibCST dataclass construction, NumPy and PyOpenGL as major remaining
costs. Merely delaying the usage-picker's LibCST import did not help because
another required import reached the same code; that experiment was reverted.
The UI and font quality, syntax analysis, and first-file loading behavior remain
enabled. No resident helper or cross-process GL context reuse was introduced.

Import-worker detail: [import-profile.txt](profiles/import-profile.txt) and
[import-worker.pstats](profiles/import-worker.pstats). Instrumented profiles
identify expensive code; the performance table uses unprofiled processes.

## Reproduce

Reserve a desktop using the machine's desktop-control tooling, then use its
reported Wayland socket:

```sh
startup_sample_dir=$(mktemp -d)
cp profiles/baseline_editor.py "$startup_sample_dir/sample.py"
WAYLAND_DISPLAY=<reserved-agent-seat-socket> .venv/bin/python profile_startup.py \
  "$startup_sample_dir/sample.py" --compare-editor profiles/baseline_editor.py \
  --runs 8 --output profiles/startup-repeat.json
```

Omit `--compare-editor` to measure only the current editor. Add `--runs 1
--profile profiles/current.pstats` for a cProfile capture; do not use those
instrumented timings as the performance comparison. The harness uses isolated
session state and checkpoints completed samples if a later launch fails.

Validation: 49 targeted tests passed. A live native window displayed the file,
accepted an edit, and saved it on close. A forced-GLFW launch also presented
successfully. The live edit used a disposable file.
