#!/usr/bin/env python3
"""melty-code-editor — melty's code editor as a one-window app.

    melty-code-editor                    open an empty editor (no tabs)
    melty-code-editor FILE [FILE ...]    open them as tabs (the first is selected)

The window is one ``@glfw_window`` render func drawing melty's
``draw_code_editor`` with the app's ``OpenFiles`` (the tab list) as its
value. A tab per file along the bottom, the editor above it with Python
syntax analysis, folds, search and autocomplete, a Compare With dropdown
(git HEAD, the file on disk, any recent commit) and the nav back / forward
buttons. melty draws the file hosts each frame (load, reparse) and writes
their queued saves when the window closes; this script only owns the list
of files to open.

The sibling melty_text_editor draws ``draw_text``, one file and nothing
else; this one draws ``draw_code_editor``, the model-backed editor around it.
"""
import pathlib
import sys

import melty
from melty import glfw_window
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.model.open_files import OpenFiles
from src.lsd.gl_gui.view.core_conversion.address import writable_file_refusal
from src.lsd.gl_gui.view.core_views.headers import draw_header

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(2)

paths = [pathlib.Path(arg).expanduser().resolve() for arg in sys.argv[1:]]
for path in paths:
    if not path.exists():
        path.touch()          # a new file: the editor needs something on disk to host
    # Refuse up front what the studio's plain-file codec will not edit (a
    # library install, no write permission): the reason beats a dead tab.
    refusal = writable_file_refusal(path)
    if refusal:
        sys.exit(f'melty-code-editor: {path}: not editable — {refusal}')

# The tab list: one shared code_file_io host per file. The first file is
# posted as the pending jump, which selects its tab; with no files the
# editor draws its controls over an empty main cell.
open_files = OpenFiles()
for path in paths:
    open_files.open_file(path)
if paths:
    open_files.jump_to_path = str(paths[0])


@glfw_window(name=paths[0].name if len(paths) == 1 else 'Code Editor', app_id='melty-code-editor',
             with_header=draw_header, size=(1280, 800))
@render_func()
def editor(_, draw_state):
    melty.draw_code_editor(open_files, name='code-editor',
                           width=draw_state.width - 10, height=draw_state.height - 10)
    return False, None
