# melty_code_editor

A Melty app: `editor.py` draws `meltygui_pro.draw_code_editor` with the app's
`OpenFiles`. MeltyGUI provides the window/rendering toolkit, shared text and
inspection views, file browser, and save lifecycle. MeltyGUI Pro provides the
IDE, project/environment management, dependency tools, Git, and global search.

Both sibling checkouts are installed editable in this app's `.venv`:
`uv pip install --python .venv/bin/python -r requirements.txt` (run here).
Use `meltygui` / `meltygui_pro` imports; never add `src`, `lsd`, legacy `melty`,
or checkout paths to sys.path. Native ImGui is `from meltygui import imgui`.

Keep `app_id='melty-code-editor'` so the existing session restores. Import
`meltygui_pro` before loading persisted state to register its class migrations.
View-local state uses injected `DictConversion` objects. Draw nested/native
windows each frame with `open_requested`; both backends must behave identically.
Size / place a window with `initial={"width":, "height":, "window_pos":}` (applied once);
`width=` / `height=` / `window_pos=` kwargs apply every frame and pin it (meltygui `docs/APPS.md`).

Run: `./melty-code-editor FILE...`.
Smoke test: `MELTY_BENCH=1 .venv/bin/python editor.py FILE`.
Read-only files / library installs are refused up front.
A project is any folder (saved = marked). `draw_project_selector` (meltygui_pro) is drawn by
the Files tile (`project_tree.py`) and by `draw_code_editor` when no tree links it. A tree
owns the project selection. Code Editor, File Editor, Tasks and Claude Code receive
`files_view: DrawState[draw_project_tree]` through the tile framework. The consumer
link picker chooses Auto, a Files tile, or Unlinked; no sibling discovery is used. The tab bar shows `OpenFiles.paths_in(selected project)`. Claude Code filters both conversation lists to that project and its subfolders; Unlinked shows all projects.
File → New Project… is `new_project.py` over `project_templates/`: one sub-folder per
template, each a `create(name, ...)` function returning `{path: str | bytes}`; its
parameters are the window's inputs. A view returns its input's type (a `str` there).
Search → Search… / Ctrl+Shift+F uses `meltygui_pro.global_search`, over the app's
project roots (`project_roots()` in editor.py).
The Tasks tile is `tasks.py` (`draw_tasks`, `@render_func(multi_instance=True)`). Each project's
task list lives in `ProjectTasks.projects[root]`, persisted as `project_tasks` beside OpenFiles,
and merges `[tool.melty.tasks]` from the pending `pyproject.toml` through `manifest_data`.
Right-click → Run on a Python editor pane adds/reuses a task for that exact module and project.
`CodeEditorSettings.Tasks.save_tasks = False` keeps definitions in the app session; True also
writes generated entries to the manifest using its normal pending-save lifecycle. Module entries
have a root-relative `module` path and run current editor text with package context in the project's
interpreter. Ordinary entries remain command strings or `cmd` / `cwd` / `env` tables.
One `TaskState` per tile owns its process and streamed output; `stop_task` ends the process group
(Stop and app exit). The picker follows its injected Files view, with selected-tab/project
fallbacks when unlinked, and persists its selected task per project. Run identity/output remain separate. Run → Tasks ▸ and Ctrl+Shift+R use
the same task runner. A request opens a Tasks tile if absent. The old Run File/F5 console is removed.
File Editor keeps its file/version state private in `draw_state.misc`. Its
`diff_with: DrawState[draw_file_editor]` parameter selects the comparison view
through the link menu; Self contained disables comparison. Saved legacy targets
are migrated into tile bindings.
The toolkit guide is `../meltygui/docs/APPS.md`; package ownership and migration
notes are `../meltygui-pro/docs/PACKAGE_SPLIT.md`.

## Icons
The only icon font is Font Awesome 5 Free (`meltygui/resources/fontawesome-webfont.ttf`), merged
into every UI font. Before using an icon, look its codepoint up in `meltygui/model/icon_model.py`
(`FA_ICONS`, generated from that font's cmap); a codepoint not listed there renders as `?`. Never
use Octicons / Nerd Font / Material / FA6-only codepoints, and write icons as `\uXXXX` escapes in
normal `str` literals (no raw strings, no literal glyph pasted into source).
## Designing and writing new features

**Persist by default.** Save all app state by default so the app feels consistent
between launches. Assume each feature's state persists through draw state, injected
state objects, the app model, or automatic code changes, whichever owns that state.
Decide exclusions from automatic persistence case by case, only when necessary for
project load time or app stability. Use `@no_save` for those deliberate exclusions;
do not assume a feature's state is temporary just because it was created at runtime.

**Code is data.** Melty is Lisp-like by design and encourages treating code as data.
Apply that principle when designing features and choosing how to represent, inspect,
edit, and persist their state and behavior.

The toolkit-wide rule is `../meltygui/docs/APPS.md` → "Designing a feature"; here it
reads:

**Locality.** Each thing lives next to what it belongs to, at the smallest scope that holds
all of its users. Apply it to code, state, UI and lifetime, and let structure grow only when a
second user appears somewhere else:

- **Code**: a feature is one file in the app that uses it (`new_project.py`, `project_tree.py`
  are the models): its state class, its handful of functions and its view together. Move a part
  into meltygui / meltygui_pro when a second app or view needs it, or the package-split rules
  require it, and say what asked for it.
- **State and config**: state lives on the thing it describes. View state is one injected
  `DictConversion` per view (`@no_save` for process-lifetime fields); config a user edits lives
  in the project it configures (`[tool.melty.<feature>]` in `pyproject.toml`), read the way the
  app already reads it, in the flattest form that works; machine-local state lives in the
  injected state or the file-meta store. Prefer plain data (`dict`, tuples, strings); add a class,
  registry or service when two things must share it, not because a feature might grow.
- **UI**: a feature appears beside the things it acts on and takes its context (project, file,
  selection) from those neighbours, the way the most recent features here do. It is built from
  the widgets already here; menus and chords go in `editor.py`; both backends identical.
- **Lifetime**: what a feature starts (a process, a thread, a watch) belongs to it: stopped
  by its own explicit action and at app exit, never as a side effect of something unrelated.
  Background work is a daemon thread writing plain fields and calling `request_render()`, the
  view invalidating while it runs; heavier machinery only when that pattern cannot do it.

The older subsystems (the run-file console, the dependency manager, the chat service) grew under
different pressures; take proven recipes from them (spawning, streaming, waking the UI), not a new
feature's shape.

**A design doc is a page**: the state fields, the function signatures, the view, the build order,
and a "later" list. The first version ships the core; extras wait in "later" until someone wants
them. When a design genuinely needs more than this, say why in a sentence and go ahead.
