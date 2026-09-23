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
shares its project with editors through a `ProjectLink` on the shared `OpenFiles`
(`link_project`); tiles find each other by the sibling draw_state walk and never write a
sibling's draw_state. The tab bar shows `OpenFiles.paths_in(selected project)`.
File → New Project… is `new_project.py` over `project_templates/`: one sub-folder per
template, each a `create(name, ...)` function returning `{path: str | bytes}`; its
parameters are the window's inputs. A view returns its input's type (a `str` there).
Search → Search… / Ctrl+Shift+F uses `meltygui_pro.global_search`, over the app's
project roots (`project_roots()` in editor.py).
The Files tile is `project_tree.py` (`draw_project_tree`, just `@render_func(multi_instance=True)`;
`draw_file_tree` is meltygui's name and the registry is keyed by `__name__`). It finds the
editor to open in by the sibling draw_state walk (`target_editor`), posts `jump_to_path` +
`jump_to_instance`, and forces that editor past its blit cache so the jump is adopted.
Its rows' right-click menu (`file_menu`: Rename…, New → File / Folder, Add to Projects, Move to Trash) posts a
request on the tile's `_menu` dict; the rows act on it next run and name rows in place. The whole tile's
background is the project folder's file-meta tint, dark and saturated (`tile_fill`), painted by the tile
manager via `draw_project_tree.tile_background`; the chip beside the selector edits that tint.
The Claude Code tile is meltygui's `draw_claude_chat`, a plain function (one call of
`draw_chat_interface`, the only render boundary); `tile_views.py` names it `draw_chat` and
editor.py offers it in `multi_instance_renderers`. Its view, backends and detached chat service are
`meltygui.chat` (socket `$XDG_RUNTIME_DIR/meltygui-chat-<uid>/`; `MELTY_CHAT_SERVICE=name` isolates
a test run); a tile's new conversations start in the selected tab's project through
meltygui_pro's `chat_project` extension service (`integration.py`).
The Tasks tile is `tasks.py` (`draw_tasks`, `@render_func(multi_instance=True)`): the selected
project's `[tool.melty.tasks]` from its `pyproject.toml` (a name → command string, or a table with
`cmd` / `cwd` / `env`), read through `manifest_data`; one `TaskState` per tile; `start_task` runs the
command through the shell in the project's venv with a reader thread streaming into the tile,
`stop_task` ends the process group (the Stop button, and every live run at exit). Its project is
the nearest editor's, else the selected tab's. Run → Tasks ▸ lists the selected tab's project's
tasks and runs one in the first Tasks tile (`request_run`); Ctrl+Shift+R reruns the last.
The toolkit guide is `../meltygui/docs/APPS.md`; package ownership and migration
notes are `../meltygui-pro/docs/PACKAGE_SPLIT.md`.

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
