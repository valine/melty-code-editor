# melty_code_editor

The sibling of `melty_text_editor`, built on the studio's **Code Editor**
instead of the bare text view: `editor.py` draws `draw_code_editor` where the
text editor draws `draw_text`.

```python
import melty
from melty import glfw_window
from src.lsd.gl_gui.model.open_files import OpenFiles

open_files = melty.persisted('open_files', OpenFiles, app_id='melty-code-editor')   # the tab list, kept between runs
for path in paths:
    open_files.open_file(path)

@glfw_window(title='Code Editor', app_id='melty-code-editor', with_header=draw_header, size=(1280, 800))
@render_func()
def editor(_, draw_state):
    melty.draw_code_editor(open_files, name='code-editor',
                           width=draw_state.width - 10, height=draw_state.height - 10)
    return False, None
```

    ./melty-code-editor FILE [FILE ...]      # each file is a tab; the first is selected

What you get is the studio's editor window as an OS window: a tab per file
along the bottom (close buttons, drag to reorder), the editor above it with
Python highlighting, folds, search, autocomplete and analysis, the **Compare
With** dropdown (git HEAD, the file on disk, any recent commit, rendered as
an editable side-by-side diff) and the nav back / forward buttons.
**Search → Search…** (Ctrl+Shift+F) is melty's global search, Code tab
only: files, classes, defs and their call sites across the projects of the
open tabs (the git root of each, or its directory), a hit opening in the
editor at the definition. `melty.global_search(categories=('Code',),
roots=project_roots, open_files=open_files)` in `editor.py` is the whole of
it; the query and the pick counts persist with the session. Edits go
to melty's file hosts (the studio's deferred-save model: queued in memory,
written to disk when the window closes); there is no Ctrl+S.

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

The file hosts need nothing from the app. melty draws every registered
RenderHost once a frame (`RenderHost.draw_all`, the studio's `draw_main` host
loop, run from `Surface.frame` in a melty app) — that is what loads a file,
reparses it after an edit and queues its save — and flushes the queued saves
to disk when the loop exits (`app.run`). The studio's plain-file codec refuses
library installs (venv, site-packages, node_modules) and files the process
cannot write (`address.writable_file_refusal`); a refused tab shows the
reason, and the app refuses such a file up front with the same reason.


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
* Started through the desktop tooling's `launch` (the seat's own Wayland
  socket) an agent seat can click and type into the app; a plain start binds
  the first `wl_seat` only.

## File menu

**File → New…** or **Ctrl+N** opens a one-line path field under the menu
bar, prefilled with `untitled.py` beside the last opened tab. Edit the path
and press Enter: the file is created (an existing path is simply opened) and
becomes the selected tab. Escape closes the field. A path that cannot be
created shows its error under the bar, the way a refused open does.

**File → Open…** or **Ctrl+O** opens melty's file selector. Double-click a
file or select it and press Enter to open and select its tab. Choosing an
already-open file selects that tab; existing tabs and edits stay intact.
Cancel/Escape leaves the editor unchanged. Read-only files and library
installs show the same refusal as command-line opens.

**File → Quit** closes the window and uses melty's normal save-on-exit flow.
