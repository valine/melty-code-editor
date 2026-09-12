# melty_code_editor

A melty app: `editor.py` is the whole program (one `@glfw_window` function drawing the
studio's `draw_code_editor` through `root_view` with the app's own `OpenFiles` as the value,
plus the RenderHost pump the studio's main loop would otherwise run). Sibling of
`melty_text_editor` (`draw_text`). Own venv with melty editable (README "Setup"); melty's
user guide is `melty/README.md` in the latent-descent checkout.
Run: `./melty-code-editor FILE...`; smoke test: `MELTY_BENCH=1 .venv/bin/python editor.py FILE`.
Read-only files / library installs are refused up front.
