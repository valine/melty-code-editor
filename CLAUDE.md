# melty_code_editor

A melty app: `editor.py` is the whole program (one `@glfw_window` render func drawing
`melty.draw_code_editor` with the app's own `OpenFiles` as its value; melty draws the file hosts each frame
and flushes their saves at exit). Sibling of
`melty_text_editor` (`draw_text`). Own venv with melty editable (README "Setup"); melty's
user guide is `melty/README.md` in the latent-descent checkout.
Run: `./melty-code-editor FILE...`; smoke test: `MELTY_BENCH=1 .venv/bin/python editor.py FILE`.
Read-only files / library installs are refused up front.
Search → Search… / Ctrl+Shift+F is `melty.global_search` (src/lsd/gl_gui/app_search.py in melty), Code tab over
the open tabs' project roots (`project_roots()` in editor.py).
