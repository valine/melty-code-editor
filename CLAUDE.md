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

Run: `./melty-code-editor FILE...`.
Smoke test: `MELTY_BENCH=1 .venv/bin/python editor.py FILE`.
Read-only files / library installs are refused up front.
Tabs are scoped: `OpenFiles.project_of(path)` is the deepest marked project or None, and
the tab bar shows the scope of the editor's selected project (`EditorProjectState`, in
meltygui_pro's `draw_code_editor`); `open_paths` is still the one flat persisted list.
File → New Project… is `new_project.py` over `project_templates/`: one sub-folder per
template, each a `create(name, ...)` function returning `{path: str | bytes}`; its
parameters are the window's inputs. A view returns its input's type (a `str` there).
Search → Search… / Ctrl+Shift+F uses `meltygui_pro.global_search`, over the app's
project roots (`project_roots()` in editor.py).
The Files tile is `project_tree.py` (`draw_project_tree`, just `@render_func(multi_instance=True)`;
`draw_file_tree` is meltygui's name and the registry is keyed by `__name__`). It finds the
editor to open in by the sibling draw_state walk (`target_editor`), posts `jump_to_path` +
`jump_to_instance`, and forces that editor past its blit cache so the jump is adopted.
The toolkit guide is `../meltygui/docs/APPS.md`; package ownership and migration
notes are `../meltygui_pro/docs/PACKAGE_SPLIT.md`.
