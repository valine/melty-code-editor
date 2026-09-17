# Global search profile

2026-09-16, cProfile of `global_search_results` across melty_code_editor,
meltygui, and meltygui_pro; exact then fuzzy for global_search,
"draw_text imgui.dummy", and project_roots. Includes initial corpus setup;
excludes rendering, input debounce, and the asynchronous full-text pass.
Raw profiles and per-call timings are beside this file.

Six passes: 0.910 s before, 0.335 s after (profiled cumulative time).
Warm exact project_roots: 103.43 ms before, 2.34 ms after.
The phrase intentionally no longer returns either symbol independently.
Corpora differ slightly because source changes and regression tests are indexed.

The main cost was symbol_tables repeatedly extracting dirty overlay symbols
before checking the caller's memo. It now caches tables by segment identity,
overlay file mtime/size, and pending-edit generations. Repeated disk edits
also change the key, and deleted overlay files no longer become file hits.

Scope changes clear all result tiers and cancel older workers. Empty host
roots produce empty results instead of falling back to loaded framework code.
The editor searches marked projects only, so open tabs cannot reintroduce
unmarked projects.
