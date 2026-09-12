#!/usr/bin/env python3
"""melty-code-editor — a one-window code editor built on melty.

    melty-code-editor FILE [FILE ...]    open them as tabs (the first is selected)

The window is the studio's Code Editor (``draw_code_editor``) as an OS window:
a tab per file along the bottom, the editor above it with Python syntax
analysis, folds, search and autocomplete, a Compare With dropdown (git HEAD,
the file on disk, any recent commit) and the nav back / forward buttons.
Edits auto-save to disk through melty's file hosts; this script only owns the
list of files to open.

The sibling melty_text_editor draws ``draw_text``, one file and nothing
else; this one draws ``draw_code_editor``, the model-backed editor around it.
"""
import os
import pathlib
import sys
import types

from melty import glfw_window, pressed, root_view

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(2)

paths = [pathlib.Path(arg).expanduser().resolve() for arg in sys.argv[1:]]
for path in paths:
    if not path.exists():
        path.touch()          # a new file: the editor needs something on disk to host
    # The studio's plain-file codec edits only under $HOME and never inside a
    # venv / site-packages (address.is_writable_file); a refused file would
    # sit on a tab that never loads, so refuse it here with a reason.
    parts = set(path.parts)
    if (not path.is_relative_to(pathlib.Path.home())
            or parts & {'site-packages', 'dist-packages', 'venv', '.venv', 'node_modules'}):
        sys.exit(f'melty-code-editor: {path}: the editor only opens files under '
                 f'{pathlib.Path.home()} (and not inside a venv / site-packages)')
state = {}


def ensure_root():
    """draw_code_editor reads its tabs from ``Melty.vis.root.open_files``,
    the studio's app model. A bare melty app has no ``vis``; give it the two
    collections the editor reads (the tab list and the per-file tints) and
    post the first file as the pending jump, which selects its tab."""
    if 'root' in state:
        return
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.model.app_model import OpenFiles, FileMetaCollection
    root = types.SimpleNamespace(open_files=OpenFiles(),
                                 file_meta_collection=FileMetaCollection(),
                                 draw_state_registry=Melty.draw_state_registry)
    for path in paths:
        root.open_files.open_file(path)
    root.open_files.jump_to_path = str(paths[0])
    Melty.vis = types.SimpleNamespace(root=root, window=Melty.glfw_window,
                                      invalidate_all=lambda *a, **k: None)
    # Not on melty's public list yet: the studio's Code Editor window body.
    from src.lsd.gl_gui.view.playground.open_files import draw_code_editor
    state['root'], state['view'] = root, draw_code_editor
    if os.environ.get('MELTY_CODE_DEBUG'):
        from src.lsd.gl_gui.toggles import Toggles
        Toggles.symbol_perf_log = True


def pump_hosts():
    """The studio's main loop draws every registered RenderHost once a
    frame; that is what loads a tab's file (lazily, on the host's first
    draw), reparses it after an edit and auto-saves it. A bare melty app has
    no such loop, so run it here."""
    import imgui
    from src.lsd.gl_gui.utils.glfw_utils import request_render
    from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
    if imgui.is_mouse_down(0) or imgui.is_mouse_down(1) or imgui.is_mouse_down(2):
        return          # as the studio: no host work under a drag
    hosts = RenderHost.all()
    drew = False
    for host in hosts:
        if host.draw_needed():
            host.draw()
            drew = True
    loading = any(host.get(host.value_key) is None
                  for host in state['root'].open_files.files.values())
    if drew or loading:
        # The loop only renders on request and a file loads on a worker
        # thread: keep frames coming until every host holds its value, and
        # one more after a host drew (its result lands in the blit cache).
        request_render()


@glfw_window(title=paths[0].name if len(paths) == 1 else 'Code Editor',
             app_id='melty-code-editor', size=(1280, 800))
def editor():
    ensure_root()
    root_view(state['view'], name='code-editor')
    pump_hosts()
    if pressed('ctrl+q'):
        sys.exit(0)
