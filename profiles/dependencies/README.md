# Dependency collection profile — September 15, 2026

## AST-free import extraction

The production extractor now uses Python 3.12's C tokenizer and a specialized
suite/import scanner in `model/dependency_imports.py`. It builds no AST and does
not call `compile`. Ordinary expression lines are discarded; retained data is
limited to imports, suite headers/bodies, exception clauses and raises. This
follows the existing `melty_scan` tokenizer/custom-parser approach, with no
expression model. Four isolated workers remain for larger projects; their cyclic
GC settings no longer need changing.

**Same-tree comparison, five full collections including the installed venv:**
AST median **484 ms**, scanner median **348 ms** (28% faster). These runs reparse
all files, without a persistent import cache. Raw results are in
`ast-parser-comparison/` and `token-imports-environment/`; a separate fresh-process
run is saved under `token-imports-fresh-environment/`. Source-only medians in
`token-imports-repos/`: nanoGPT 5 ms, Whisper 15 ms, Ultralytics 124 ms, timm 139 ms,
Ignite 117 ms, TorchMetrics 150 ms, examples 48 ms.

**161 tests pass.** `tests/dependency_import_oracle.py` retains the old AST visitor
only as a test reference. Tests compare every import record, source line and
optional flag on all seven pinned repositories. An additional check matched
all 589 owned Python files in the current latent-descent checkout. Full table
snapshots remain unchanged. The scanner-specific suite adds 70 cases, including
121 nested-suite combinations within one test: aliases, relative/multiline and
semicolon imports, encodings/Unicode identifiers, comments/strings/f-strings,
main guards, loops, async, match/case, ImportError handlers/rethrows and except*.
A guard test forbids both `ast.parse` and `compile` during extraction.

Error policy is now explicit: lexical failures, malformed import clauses and
recognized suite-structure errors discard that file's records and report an
error. This is not a complete Python grammar validator. An unrelated incomplete
expression such as `value =` can leave valid imports visible; the editor's syntax
checking remains responsible for general syntax errors. No repository code is
executed. `dependency_imports` now accepts source text instead of an AST node.

## Follow-up: full collection below one second

The full latent-descent collection with its installed environment now takes
**475 ms median**, with **587 ms on the first call** (five unprofiled calls;
OS caches were not flushed). This is down from 1,424 ms after the first pass,
and 2,550 ms originally. Every call reparses all owned Python files; this gain
does not depend on a previously populated import cache.

For at least 128 source files, `collect_imports` distributes parsing across four
short-lived isolated Python subprocesses, launched with `posix_spawn`-compatible
arguments. The same AST visitor is now in the stdlib-only `dependency_imports.py`
module. Workers read source without executing it, return compact import records
and per-file errors, and exit after the batch. Parent collection preserves file
order and all snapshot semantics. Small projects avoid startup/IPC overhead.
If a worker cannot run, its batch falls back to in-process parsing. Cyclic GC is
disabled only inside the isolated workers; acyclic ASTs are reference-counted
and discarded per file. The editor's GC settings are untouched.

Latest source-repo medians: nanoGPT 7 ms, Whisper 18 ms, Ultralytics 142 ms,
timm 172 ms, Ignite 142 ms, TorchMetrics 174 ms, examples 65 ms. Raw results are
in `parallel-repos/` and `parallel-environment/`. Wall-clock timings include
worker startup, parsing, IPC, and exit; the cProfile reports show only the parent
process and its waits, not individual worker CPU profiles.

Validation: **90 tests passed** in the relevant combined suite; the collector's
29 tests also passed after strengthening the worker test to forbid fallback.
The pinned repository snapshots and full installed-environment snapshot remain
identical. New cases compare real workers with in-process extraction, including
syntax errors, Latin-1 files, deleted files, subsequent edits, no execution of
source code, and worker-launch failure fallback. No automatic updating or import
cache has been enabled. The tradeoff is up to four parsing processes per large
collection, rather than one core; this should be remeasured on lower-core machines.

## First-pass result

The shared collector in `latent-descent/src/lsd/gl_gui/model/project_dependencies.py`
now reuses directory ownership and virtual-environment ancestry decisions within
one collection, reuses local-module existence checks, and visits AST statement
containers without recursively walking expression trees. Match cases, handlers,
functions, classes, and async bodies remain covered. Symlink files still resolve
their own ownership. No cross-request cache was added.

Collection wall-clock medians, three unprofiled calls per repository in one
process (first call included; OS caches were not flushed):

| Repository | Before | After | Speedup |
| --- | ---: | ---: | ---: |
| nanoGPT | 13 ms | 8 ms | 1.7× |
| Whisper | 35 ms | 19 ms | 1.8× |
| Ultralytics | 837 ms | 408 ms | 2.1× |
| timm | 1,027 ms | 587 ms | 1.7× |
| Ignite | 751 ms | 369 ms | 2.0× |
| TorchMetrics | 1,113 ms | 440 ms | 2.5× |
| PyTorch examples | 147 ms | 74 ms | 2.0× |
| latent-descent + its installed `.venv` | 2,550 ms | 1,424 ms | 1.8× |

The seven pinned source-repo benchmarks deliberately use no selected environment
to isolate source/declaration collection. Their sole expected error is “No virtual
environment selected.” The final row measures full collection including installed
metadata and the selected interpreter; it has no errors. The latent-descent tree
is the working checkout, including existing uncommitted changes, not a pinned
benchmark fixture. These are collector timings, not window rendering or input
latency measurements. Different hardware, disk caches and repo contents affect
absolute timings.

`before/`, `after/`, and `environment-{before,after}/` contain raw timing JSON,
cProfile dumps, and cumulative-time text reports. Each cProfile call runs after
the unprofiled timing calls; profiler overhead is excluded from the table.

On timm, cProfile AST visitor calls fell from 728,356 to 59,673, while total calls
fell from 11.7 million to 1.6 million. AST parsing now dominates the remaining work
(0.46 seconds of the 0.78-second instrumented run). The installed-environment
profile also points to parsing/traversal, with interpreter inventory around 18 ms.

## Reproduce

From `melty_code_editor`:

```sh
.venv/bin/python profile_dependencies.py \
  ~/Desktop/pytorch-review/{nanoGPT,whisper,ultralytics,timm,ignite,torchmetrics,examples} \
  --output profiles/dependencies/current --repeat 5

.venv/bin/python profile_dependencies.py ~/Desktop/latent-descent \
  --environment ~/Desktop/latent-descent/.venv \
  --output profiles/dependencies/current-environment --repeat 5
```

The harness reads source and package metadata; it does not execute repository
modules or install dependencies. It uses the same collector invoked by the button.
The saved “before” outputs came from the original collector, retained temporarily
outside the working tree during verification.

## Regression coverage

The shared library now has `tests/test_dependency_collection.py`, plus pinned
JSON fixtures and a download helper under `tests/fixtures/dependency_repos/`.
All seven existing repo checkouts were clean and at the manifest revisions when
captured. Complete before/after snapshots matched for all seven repos and the
installed-environment run. Fixtures freeze import source locations, optional
imports, declarations, and table output. An independent full-AST-walk oracle
checks static imports in every owned Python file.

Validation: **86 passed** across the new collector suite, dependency aliases,
edits, fixes, scopes, project dependencies, nested requirements, TOML,
environments, and editor project ownership. The repo fixtures were required via
`MELTY_DEPENDENCY_REPOS`, so none silently skipped. No GUI code changed.

## Strategy for automatic updates

Recommended next implementation: maintain a per-project dependency index and
publish its snapshots through the existing background worker and injected
`DependencyState`. Start collection when the panel opens. Keep the current table
visible while updates run, with a small “Updating” indication and an error/stale
indicator if collection fails.

1. **Cache import records per file.** Store `(module, line, optional)` records,
   source classification, and parse errors. Invalidate with disk stat signals
   (`mtime_ns`, size, inode where useful), pending-edit generations, and explicit
   watcher events. Never hash or compare whole files to detect changes. Reparse
   only dirty files; remove records on deletion. Cache records rather than keeping
   every large AST alive.
2. **Read the editor's text.** Python currently uses `tokenize.open` on disk;
   requirements/TOML already use `project_code[path].text()`. Move Python reading
   to pending/sync-frame truth before enabling live updates, retaining encoding
   handling for disk-only files. Edits, undo/redo, revert, and accepted external
   changes must advance the appropriate generation. Do not silently substitute
   conflicting external text for the buffer the user sees.
3. **Track three independent inputs.** Source-file events dirty imports;
   requirements and transitive `-r` include events dirty declarations; changes
   to selected environment metadata dirty installed packages/module aliases.
   Track included files outside the project too. Environment selection/recreation,
   `pyvenv.cfg`, interpreter replacement, dist-info/egg-info, RECORD/top_level,
   `.pth`, and editable target layout can all affect resolution. Successful install
   actions explicitly dirty environment inventory. Changing scope or TOML group
   should refilter cached imports, without reparsing them.
4. **Use one dirty queue per project.** Watch callbacks enqueue paths/generations
   only; they do not parse or draw. Reuse `FileWatch.global_listeners` and directory
   watches with explicit lifetime management. Debounce edits around 250 ms as an
   initial setting to measure. Allow one worker plus a queued latest request;
   compare project/environment/selection and generation before publishing so an
   older job cannot overwrite a newer selection. Assign the result to injected
   state on the UI thread and use Melty's existing invalidation path.
5. **Handle directory changes explicitly.** New/deleted/renamed source files,
   project markers, marked-project metadata, and environment folders affect
   ownership/local-module classification. Rebuild that portion of the index on
   structural events. The current `FileWatch._on_moved` forwards the destination;
   dependency tracking also needs the old path removed. Add typed/source-and-dest
   event support or reconcile the affected directory. A low-frequency background
   reconciliation while the panel is open catches lost events; no scans per frame.
6. **Make partial syntax visible.** During typing, retain the last successful
   records for an invalid file but label its contribution stale. Once it parses,
   replace its records atomically. Preserve counts across files so removing one
   import does not remove a package still used elsewhere.

Suggested delivery sequence: pending-aware per-file cache and equivalence tests;
then automatic scheduling on open/edit/filesystem change; then independent
manifest/environment invalidation and lifecycle cleanup. Keep Refresh as an
explicit full reconciliation option.

Acceptance tests for that follow-up: a one-file edit reparses only that file;
unchanged refresh parses zero files; pending edits/undo and external atomic saves
appear; rename/delete removes old locations; malformed text is labelled stale;
requirements include edits and environment installs refresh the correct columns;
selection changes during a running job cannot publish an obsolete snapshot;
closing the panel releases subscriptions and idle work. Measure UI latency under
rapid edits as well as worker time before selecting final debounce/reconcile
intervals. Automatic updating is a proposed next step, not enabled by this change.
