# Projects: plan (2026-09-14)

Goal: a user can mark folders as projects; melty_code_editor (and the studio) treat every
project as a first-class root for editing, global search, the symbol / usage graph, and git,
instead of behaving as if the latent-descent checkout were the only project.

## 0. What assumes a single project today (from the survey)

| Area | Where | Assumption |
|---|---|---|
| Editability | `address.py:44-96` | `_PROJECT_ROOT = parents[5]`; `_EDITABLE_ROOTS` is already a list with `add_editable_root`, `project_root_of` (marker walk: .git, pyproject, setup.*) |
| Analysis root | `libcst_conversion.py:8707` | `_SRC_PREFIX = parents[4]` (latent-descent/src); jedi `Project` pinned there (`:660`); index watch only on it (`:9567`) |
| Usage graph | `symbol_roster.py:328,359,872,1131` | `module_to_path` searches only (repo, repo/src); universe walks only src; `_mod_path_cache`, `by_name`, `by_leaf`, one global generation |
| Auto-import | `code_checks.py:1040` | `project_importables` = sys.modules starting with `"src."` |
| Import graph | `file_graph.py:121,425` | `IMPORT_ROOT_DEPTH = 3`; one `_CURRENT` graph; callers default root to checkout src |
| Trigram index | `text_index.py` | already per root; bug: `startswith(root)` without separator (`:128,:514,:717`) |
| Global search | `new_core_view.py:4734`, `app_search.py` | `GlobalSearch.code_roots` class attr, callable roots; studio corpus = loaded modules, app corpus = per-root symbol tables |
| Git | `git.py:44,970` | `REPO_ROOT = parents[5]` seeds four module globals; `_REPOS` per root exists; pollers, `_consumers`, `_last_unsaved` global; Git Changes window `input_value` bound at decoration time; `_git_head_text`, compare column, merge use the current global |
| Persisted compare state | `open_files.py:3206`, `draw_state.misc` | `compare_with`, `_cmt_*` memos, NavUndo compare tokens are not repo-keyed |
| File watch | `melty.py:241` | `watch_project_files(root=_PROJECT_ROOT)` (takes a root already) |
| Ignore sets | 5 divergent copies | text_index, address, melty.py, file_graph, new_core_view |
| Literals | `jump_to_code.py:143`, `terminal_playground.py:657`, `load_save_util.py:343`, `new_core_view.py:1547` | hardcoded `/home/lukas/Desktop/latent-descent` or `"/src/"` split |
| App | `editor.py:71` | `project_roots()` = git root of each tab, only fed to search |

Sibling app precedents to copy: `folder_files.py:371-450` (root→proxy, root→window ds, one poller
over all roots), `chat/metadata.py:12` (ordered collapsible project groups), `melty_file_browser`
explorer `context_menu` hook.

Constraints learned: `@Melty.on_load` never fires in a melty app (use instance `on_load(self, vis, root)`
with `vis=root=None`); persisted fields must be assigned in `__init__` (load_save_v2 saves only
declared fields); `@window(input_value=...)` captures the object at decoration time.

## 1. Design (revised 2026-09-14: the mark lives in file meta)

**The project flag is a folder attribute in the shared file-meta store**, not a per-app model.
`FileMeta.project = False` joins `tint` / `icon` as vocabulary; `mark_project(path, on)`,
`is_project`, `project_roots()` (memoized on the store's generation) and `project_for(path)` live in
`src/lsd/gl_gui/model/file_meta.py` and are exported by `melty`. The store already does the I/O
(debounced atomic writes, cross-process reload), so a folder marked in one app is a project in the
studio and every other app within a poll.

- **Implicit projects**: a tab whose file lies outside every marked project gets one from
  `address.project_root_of(path)` (marker walk). Shown and analysed like a real one, not persisted
  until the user marks it (the Projects → Mark submenu lists them).
- `app._register_projects()` makes every marked root editable source at init; `mark_project` does
  the same at once. Every melty subsystem reads roots from `project_roots()` (plus the open tabs'
  implicit ones via the app's `project_roots` callable); no subsystem keeps its own list.
- Source dirs for dotted-name resolution (Phase 3) will be derived per root: `[root]` plus
  `root/src` when it exists, so latent-descent is the special case, not the rule.
- Folder colour / icon stay ordinary file-meta attributes on the same entry.

## 2. Phases (each lands and is verified on its own)

### Phase 1: registry + app UI — DONE 2026-09-14 (uncommitted)
1. `FileMeta.project` + `mark_project` / `is_project` / `project_roots` / `project_for` in
   `model/file_meta.py`; `FileMetaProxy.generation` for memoizing; `folder_files._apply_meta` keeps
   the flag out of row kwargs.
2. `app._register_projects()` at init → `add_editable_root` per marked root. `melty` exports the
   four functions lazily. (`FileWatch.watch_project_files` per root: deferred to Phase 3 with the
   index watches.)
3. `draw_file_selector(choose_folder=True, context_menu=...)`: "Choose Folder" button.
4. editor.py: `Projects → Add Folder… / Mark ▸ / Unmark ▸`; `FOLDER_MENU` right-click in both
   dialogs; `project_roots()` = marked ∪ implicit; both selector windows sized 720×640.
5. `text_index._under()` replaces the bare `startswith(root)` checks.
6. Found on the way: a nested menu's submenu painted blank until hovered (cached row tiles);
   `_dd_menu_row` now cascade-invalidates the submenu on its closed→open edge.
7. `draw_fast_file_explorer`'s shortcuts column has a second **Projects** section (every marked
   root, path order, same rows, no drag-reorder); `melty_file_browser` FILE_MENU gained Mark as
   Project / Unmark Project — the external view of the project list.
8. `draw_shortcuts` (fast_file_explorer.py, exported by melty): the shortcuts column as its own
   view — the explorer draws it in cell 0; `draw_code_editor(show_shortcuts=True)` draws it as a
   leading column: an OUTER 2-cell ColumnLayout (key "shortcuts", divider persisted as
   `shortcuts_edges` only once dragged), the body's six ColumnLayouts pinned to the second cell via
   `_frame_kwargs`, `_cmp_layout_reframe` offset by the inset. A row click → `OpenFiles.browse_request`
   → editor.py opens the Open dialog there (`draw_file_selector(browse=(dir, token))`).
   Pitfalls hit: a min on the outer's LAST cell is stamped on the window frame edge; a narrow
   launch-fit frame clamps persisted dividers and they never re-expand (the wrapper re-seats the
   shortcuts divider until dragged and restores the files column width after a clamp).
Verified on an agent desktop with a temp store (`MELTY_FILE_META`): menu, Mark/Unmark, the
picker, the right-click item, the browser's section, the editor's column + browse hand-off, and
the flags in the pickle.

### Phase 2: git per project
1. Delete the four module globals in `git.py`; add `repo_for(path)` (→ `_proxies_for(repo_root_for(path))`).
   Thread an explicit repo into `_git_head_text`, `_fetch_compare_base`, `_compare_column_sources`,
   `_draw_commit_file_tabs`, `draw_commit_file_column`, `compare_file_rows.model_for`,
   `merge_files._merge_paths`. `follow_file` shrinks to "ensure watched + status requested".
2. `_watch_refresh_loop` / `_poll_loop` sweep `_REPOS` for every registered project; `ensure_status`
   enqueues a specific proxy; start the poller from `ensure_status`, not the Git Changes window body
   (so melty apps get it).
3. Git Changes window per project: resolve the proxy inside the body from the selected project
   (copy `folder_files.py:371-450`), `_consumers` keyed by root.
4. `FileSystemFiles.changed_files` filters to its own repo; the compare column sources only the
   selected tab's repo.
5. Persist compare state per repo: `compare_with_by_repo` dict on the editor window, `_cmt_*` memo
   keys prefixed by root, NavUndo `CompareChange` carries the root.
6. `repo_root_for` cache invalidates when a project is added (folder marked before `git init`).
Verify: tabs from two repos side by side in both editor instances, Compare With lists each repo's own
commits, HEAD diff correct for both, Git Changes shows the right repo, status refreshes in the app.

### Phase 3: symbol / usage graph per project
1. `symbol_roster`: `module_to_path(dotted, source_dirs)`, `_absolutize` relative to the file's
   project, `universe_paths()` = union over projects, `_mod_path_cache` keyed by (roots, dotted),
   `by_name` / `by_leaf` / `universe` per project root, per-project generation counter (so an edit
   in A does not invalidate B's editors, `text_editor.py:6595`). `_usages_of` passes
   `root=project_for(entry.path).root` to `candidate_paths` and unions across projects for
   cross-project callers (default: same project only; toggle for all).
2. `libcst_conversion`: `_jedi_project(root)` memoised per project with `added_sys_path =
   source_dirs`; replace `startswith(_SRC_PREFIX)` gates with `address.is_editable_source`;
   `_register_index_watch` per project root. `_SRC_PREFIX` remains only as the studio's default.
3. `code_checks.project_importables(project)`: derive from the project's source dirs (walk the
   trigram index's symbol tables, fall back to sys.modules for the checkout).
4. `file_graph`: `_CURRENT` → dict by root, `import_roots(project)` from `source_dirs`; file tree
   and import-graph window take the project as input.
5. One ignore set (`text_index._SKIP_DIRS` + `_LIBRARY_PARTS`) with per-project overrides.
6. `new_converters.code_hosts_for` key gains the project root for function / class refs.
Verify: Ctrl+B usages and def tints work in a second repo; autocomplete import rows come from that
repo; import graph opens for it; latent-descent unchanged.

### Phase 4: editor UI
1. Tab labels disambiguated by project when basenames collide; tab rows grouped by project
   (`layout_tabs`, `open_files.py:3327`; close-hold geometry and drag-drop permutation kept on
   `open_paths`).
2. Project sidebar: export `render_file_tree(root=...)` through `melty` (`_VIEWS`), one tree per
   project with git status tints from `repo_for(root)`, collapsible like `ChatInterfaceState.projects`.
3. Right-click on tab: Reveal in project, Mark folder as project, Close others in project.

### Phase 5: cleanup
Replace the literals (`jump_to_code.py`, `terminal_playground.py`, `load_save_util.find_repo_root`
fallback, `claude_terminals` cwd, `copilot.py` LSP workspace) with `project_for(path)`; the `"/src/"`
label split in `new_core_view.py:1547` becomes relative-to-project. Update melty/README.md and this
repo's README.

## 3. Order, risk, verification

Phase 1 first: it is small, unblocks the other phases (they all read the registry), and is
independently useful (search across repos). Phase 2 before 3: git's per-root registry already exists,
so it is the cheaper generalisation and the most visible bug today (wrong commits in Compare With).
Phase 3 is the largest and touches hot per-keystroke paths; do the roster first (default-on, text
based), jedi / code_checks second, file_graph last.

Risks: `core_render.py` and `new_core_view.py` have uncommitted find-pill work in latent-descent;
rebase Phase 1's search changes on top. `draw_code_editor` runs `instances=2`; per-frame code must
not ping-pong a global. Two app instances share one `session.pkl` (last exit wins) for the project
list; acceptable for now, note it.

Verification per phase: `MELTY_BENCH=1 .venv/bin/python editor.py FILE` smoke; the existing tests
(`tests/test_global_search_multi_term.py` has two pre-existing failures); a real-app check on an
agent desktop with tabs from both `melty_code_editor` and `latent-descent` open.
