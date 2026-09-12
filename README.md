# melty_code_editor

The sibling of `melty_text_editor`, built on the studio's **Code Editor**
instead of the bare text view: `editor.py` draws `draw_code_editor` where the
text editor draws `draw_text`.

```python
from melty import glfw_window, pressed, root_view
from src.lsd.gl_gui.view.playground.open_files import draw_code_editor

@glfw_window(title='Code Editor', app_id='melty-code-editor', size=(1280, 800))
def editor():
    ensure_root()                            # Melty.vis.root with the open files
    root_view(draw_code_editor, name='code-editor')
    pump_hosts()                             # load / reparse / auto-save the files
```

    ./melty-code-editor FILE [FILE ...]      # each file is a tab; the first is selected

What you get is the studio's editor window as an OS window: a tab per file
along the bottom (close buttons, drag to reorder), the editor above it with
Python highlighting, folds, search, autocomplete and analysis, the **Compare
With** dropdown (git HEAD, the file on disk, any recent commit, rendered as
an editable side-by-side diff) and the nav back / forward buttons. Edits
**auto-save** to disk through melty's file hosts; there is no Ctrl+S. Ctrl+Q
quits.

## What draw_code_editor needs that draw_text does not

`draw_text` takes a string. `draw_code_editor` takes nothing and reads the
studio's app model, so the app fakes the two pieces of it the editor uses
(`ensure_root` in `editor.py`):

* `Melty.vis.root.open_files`, an `OpenFiles` (the tab list, one
  `code_file_io` RenderHost per file from the shared code-host cache), with
  `jump_to_path` posted for the first file so its tab is selected; and
  `root.file_meta_collection` (per-file tints); `root.draw_state_registry`
  points at melty's own registry.
* The studio's main loop draws every registered RenderHost once a frame,
  and that is what loads a file, reparses it after an edit and auto-saves.
  `pump_hosts` does that here, and keeps frames coming while a file is
  still loading (the app's loop only renders on request; the file loads on
  a worker thread).
* The studio's plain-file codec refuses library installs (venv,
  site-packages, node_modules) and files the process cannot write
  (`address.writable_file_refusal`, which used to require `$HOME` and refuse
  silently); a refused tab now shows the reason, and the app refuses such a
  file up front with the same reason.

Files outside `melty`'s public surface: `draw_code_editor` itself is not on
melty's lazy view list yet, hence the `src.lsd.gl_gui...` import.

## Setup

`draw_code_editor` pulls in the studio's app model (`app_model.py`: torch,
transformers, peft, moderngl, ...), far beyond melty's own dependency list,
so unlike the text editor this app runs on the **latent-descent venv itself**
(`~/Desktop/latent-descent/venv`), with the checkout on `PYTHONPATH` for
`import melty`. The launcher does both; `MELTY_CHECKOUT` / `MELTY_PYTHON`
override the paths. In IntelliJ / PyCharm set the interpreter to that venv
and mark the checkout as a source root (or `pip install -e` melty into it).

    ./melty-code-editor ~/some/file.py ~/some/notes.txt
    MELTY_BENCH=1 PYTHONPATH=~/Desktop/latent-descent ~/Desktop/latent-descent/venv/bin/python editor.py FILE   # smoke test
    MELTY_CODE_DEBUG=1 ./melty-code-editor FILE    # melty's perf trace → /tmp/lsd_symbol_perf.log

`melty-code-editor.desktop` registers it in the app menu (copy to
`~/.local/share/applications/`). First frame is ~1.6 s (the studio model
imports); the text editor's is 0.4 s.

## Known gaps

* Read-only files and library installs are refused (studio rule, see above).
* GLFW binds only the first `wl_seat`, so an agent seat cannot click or type
  into this app; input has to be checked by a person. Screenshots of the
  agent desktop work (`Pictures/Screenshots/melty-code-editor-*.png`).
