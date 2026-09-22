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
The toolkit guide is `../meltygui/docs/APPS.md`; package ownership and migration
notes are `../meltygui-pro/docs/PACKAGE_SPLIT.md`.
