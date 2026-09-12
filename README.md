# melty_code_editor

The sibling of `melty_text_editor`, built on the studio's **Code Editor**
instead of the bare text view: `editor.py` draws `draw_code_editor` where the
text editor draws `draw_text`.

```python
from melty import glfw_window, pressed, root_view
from src.lsd.gl_gui.model.open_files import OpenFiles
from src.lsd.gl_gui.view.playground.open_files import draw_code_editor

open_files = OpenFiles()                     # the tab list, owned by the app
for path in paths:
    open_files.open_file(path)

@glfw_window(title='Code Editor', app_id='melty-code-editor', size=(1280, 800))
def editor():
    root_view(draw_code_editor, name='code-editor', value=open_files)
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

`draw_text` takes a string. `draw_code_editor` takes an **`OpenFiles`**: the
tab list, one `code_file_io` RenderHost per file from the shared code-host
cache (`src/lsd/gl_gui/model/open_files.py`, a light module with no studio
imports behind it). The app builds one, opens its files into it and posts
`jump_to_path` for the first file so its tab is selected. Passed `None`, as
the studio's `@window` registration does, the editor falls back to the app
model's `Melty.vis.root.open_files`.

File tints (and icons, folder order) come from the **shared file-meta
store**, `file_meta_store()` in `src/lsd/gl_gui/model/file_meta.py`: a
`FileMetaProxy` dict backed by `~/.melty/file_meta.pkl` that the studio and
this app both read and write. Paint a file in the studio and the tab here
takes the colour within half a second (a poller thread watches the file;
writes are debounced and atomic, concurrent edits from two apps merge per
path). `MELTY_FILE_META=/path.pkl` points a process at another store.

Two more things the app does that the studio's main loop would otherwise do:

* The studio draws every registered RenderHost once a frame, and that is
  what loads a file, reparses it after an edit and auto-saves. `pump_hosts`
  does that here, and keeps frames coming while a file is still loading
  (the app's loop only renders on request; the file loads on a worker
  thread).
* The studio's plain-file codec refuses library installs (venv,
  site-packages, node_modules) and files the process cannot write
  (`address.writable_file_refusal`, which used to require `$HOME` and refuse
  silently); a refused tab now shows the reason, and the app refuses such a
  file up front with the same reason.

`draw_code_editor` itself is not on melty's lazy view list yet, hence the
`src.lsd.gl_gui...` import.

## Setup

Like the text editor: melty installed **editable** into the project's own
small venv from the `latent-descent` checkout (its `pyproject.toml`). Since
`draw_code_editor` takes an `OpenFiles` from the light `model/open_files`
module, nothing of the studio's app model (torch, transformers, peft) loads.

    uv venv .venv --python 3.12
    uv pip install --python .venv/bin/python -e ~/Desktop/latent-descent
    ./melty-code-editor ~/some/file.py ~/some/notes.txt

    MELTY_BENCH=1 .venv/bin/python editor.py FILE     # smoke test: exit 0 after the first frame
    MELTY_CODE_DEBUG=1 ./melty-code-editor FILE        # melty's perf trace → /tmp/lsd_symbol_perf.log

In IntelliJ / PyCharm set the project interpreter to `.venv/bin/python`.
`melty-code-editor.desktop` registers it in the app menu (copy to
`~/.local/share/applications/`). First frame is ~0.4 s, the same as the
text editor (it was 1.6 s while the app model loaded).

## Known gaps

* Read-only files and library installs are refused (studio rule, see above).
* GLFW binds only the first `wl_seat`, so an agent seat cannot click or type
  into this app; input has to be checked by a person. Screenshots of the
  agent desktop work (`Pictures/Screenshots/melty-code-editor-*.png`).
