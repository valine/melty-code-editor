# melty_code_editor

A melty app: `editor.py` is the whole program (one `@glfw_window` function drawing the
studio's `draw_code_editor` through `root_view`, plus the RenderHost pump the studio's main
loop would otherwise run). Sibling of `melty_text_editor` (`draw_text`). Runs on the
latent-descent venv, not its own (see README "Setup"). melty's user guide is
`melty/README.md` in the latent-descent checkout.
Run: `./melty-code-editor FILE...`; smoke test: `MELTY_BENCH=1 PYTHONPATH=~/Desktop/latent-descent
~/Desktop/latent-descent/venv/bin/python editor.py FILE`. Read-only files / library installs are refused up front.
