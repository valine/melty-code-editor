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
The toolkit guide is `../meltygui/docs/APPS.md`; package ownership and migration
notes are `../meltygui-pro/docs/PACKAGE_SPLIT.md`.
