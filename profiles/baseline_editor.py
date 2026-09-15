#!/usr/bin/env python3
"""melty-code-editor — melty's code editor as a one-window app.

    melty-code-editor                    open an empty editor (no tabs)
    melty-code-editor FILE [FILE ...]    open them as tabs (the first is selected)

The window is one ``@glfw_window`` render func drawing melty's
``draw_code_editor`` with the app's ``OpenFiles`` (the tab list) as its
value. A tab per file along the bottom, the editor above it with Python
syntax analysis, folds, search and autocomplete, a Compare With dropdown
(git HEAD, the file on disk, any recent commit) and the nav back / forward
buttons. Search → Search… (Ctrl+Shift+F) is melty's global search, its
Code tab over the app's projects (`melty.global_search`): the folders
marked as projects (Projects → Add Folder…, or a right-click in the Open
dialog; the flag lives in melty's shared file-meta store, so every melty
app sees it) plus the implicit project of each open tab outside them.
melty draws the file hosts each frame (load, reparse) and writes
their queued saves when the window closes; this script only owns the list
of files to open, which persists between runs (`melty.persisted`): the
tabs of the last run come back, plus whatever the command line names.

The sibling melty_text_editor draws ``draw_text``, one file and nothing
else; this one draws ``draw_code_editor``, the model-backed editor around it.
"""
import pathlib
import sys

import glfw
import imgui

import melty
from melty import glfw_window, pressed
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.model.open_files import OpenFiles
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.view.core_conversion.address import writable_file_refusal
from src.lsd.gl_gui.view.core_views.headers import draw_header
from src.lsd.gl_gui.model.file_meta import mark_project, project_roots as marked_projects, project_for
from src.lsd.gl_gui.view.playground.open_files import draw_code_editor

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

# The tab list: one shared code_file_io host per file, kept between runs
# (melty.persisted: last run's tabs come back, minus deleted files; the
# selected tab is the window's own state and comes back with it). Files on
# the command line are added, and the first is posted as the pending jump,
# which selects its tab; with no files the editor draws last run's tabs.
open_files = melty.persisted('open_files', OpenFiles, app_id='melty-code-editor')
for path in paths:
    open_files.open_file(path)
if paths:
    open_files.jump_to_path = str(paths[0])



def real_open_paths():
    """The tabs that are files on disk (not git-diff tabs), in tab order."""
    return [path for path in open_files.open_paths
            if isinstance(path, str) and not path.startswith(OpenFiles.GIT_DIFF_PREFIX)]


def project_roots():
    """What global search's Code tab covers: every folder marked as a
    project (melty.mark_project, the shared file-meta store) plus the
    IMPLICIT project of every open tab outside them — its git repo or
    nearest project marker, else its directory — each once. Read on every
    search, so a project marked or a tab opened later joins."""
    roots = [str(root) for root in marked_projects()]
    for path in real_open_paths():
        root = project_for(path)
        root = str(root) if root else str(pathlib.Path(path).parent)
        if root not in roots:
            roots.append(root)
    return roots


def implicit_projects():
    """The projects of the open tabs that are NOT marked yet: {name: root}."""
    marked = set(marked_projects())
    out = {}
    for path in real_open_paths():
        root = project_for(path)
        if root and root not in marked:
            out.setdefault(root.name, root)
    return out


# Global search (Search → Search…, Ctrl+Shift+F): melty draws the studio's
# search window over this window; picks open in the editor through
# open_files. Code only: the app has no studio windows, toggles or actions.
search = melty.global_search(categories=('Code',), roots=project_roots, open_files=open_files)

open_requested = False
add_project_requested = False
browse = None            # the Open dialog's pending (directory, token), see the body
open_error = None
# The New… path field's draft, or None while it is closed; new_opened is
# True for the one frame after File → New… / Ctrl+N so the field takes focus.
new_draft = None
new_opened = False
NEW_FIELD_HEIGHT = 30.0


def request_open():
    global open_requested
    open_requested = True


def request_add_project():
    """Projects → Add Folder…: the folder picker (a child OS window)."""
    global add_project_requested
    add_project_requested = True


def mark_folder(path):
    """Mark `path` (a folder, or a file's folder) as a project: the store's
    flag, shared with every melty app, and editable source at once."""
    p = pathlib.Path(path).expanduser()
    mark_project(p if p.is_dir() else p.parent)


def unmark_folder(path):
    mark_project(path, on=False)


def projects_menu():
    """Projects: add a folder, mark an open tab's project, unmark one.
    Rebuilt each frame from the store and the open tabs."""
    menu = {'Add Folder…': request_add_project}
    pending = implicit_projects()
    if pending:
        menu['Mark'] = {name: (lambda _root=root: mark_project(_root))
                        for name, root in pending.items()}
    marked = marked_projects()
    if marked:
        menu['Unmark'] = {root.name: (lambda _root=root: unmark_project(_root))
                          for root in marked}
    return menu


def unmark_project(root):
    mark_project(root, on=False)


FOLDER_MENU = {'Mark Folder as Project': mark_folder,
               'Unmark Project': unmark_folder}


def default_new_dir():
    """Where a new file goes by default: beside the last opened tab."""
    real = real_open_paths()
    return pathlib.Path(real[-1]).parent if real else pathlib.Path.cwd()


def request_new():
    """Open the path field under the menu bar (drawn by the window body)."""
    global new_draft, new_opened
    new_draft = str(default_new_dir() / 'untitled.py')
    new_opened = True


def new_file_commit():
    """Create the file named in the field (an existing one is just opened)
    and open it as the selected tab; the field closes on success."""
    global new_draft, open_error
    target = new_draft.strip()
    if not target:
        return
    path = pathlib.Path(target).expanduser().resolve()
    try:
        if not path.exists():
            path.touch()
    except OSError as error:
        open_error = f'Cannot create {path}: {error}'
        return
    open_file(path)
    if open_error is None:
        new_draft = None


def open_file(filename):
    """Open/select a tab using the same editability rules as command-line files."""
    global open_error
    path = pathlib.Path(filename).expanduser().resolve()
    try:
        refusal = writable_file_refusal(path)
        if not path.is_file():
            refusal = 'not an existing file'
        if refusal:
            open_error = f'Cannot open {path}: {refusal}'
            return
        open_files.open_file(path)
    except OSError as error:
        open_error = f'Cannot open {path}: {error}'
        return
    open_files.jump_to_path = str(path)
    open_error = None


def quit_window():
    glfw.set_window_should_close(Melty.glfw_window, True)


def draw_new_field(width):
    """The one-line path field: Enter creates + opens, Esc closes. Returns its height."""
    global new_draft, new_opened
    opened, new_opened = new_opened, False
    changed, draft = melty.draw_text(new_draft, name='new-file', single_line=True,
                                     syntax_highlight=False, autocomplete=False, wrap=False,
                                     request_focus=opened, select_all_on_focus=opened,
                                     width=width - 10, height=NEW_FIELD_HEIGHT, show_header=False,
                                     disable_scroll=True)
    if changed:
        new_draft = draft.replace('\n', '')
    if pressed('escape'):
        new_draft = None
    elif pressed('enter') or pressed('kp_enter'):
        new_file_commit()
    return NEW_FIELD_HEIGHT


@glfw_window(name=paths[0].name if len(paths) == 1 else 'Code Editor', app_id='melty-code-editor',
             with_header=draw_header, bg_offset=-2, tint=(0.15, 0.16, 0.19))
@render_func()
def editor(_, draw_state):
    global open_requested, add_project_requested, browse
    menu_height = 25.0
    # The editor's shortcuts column was clicked: open the Open dialog there.
    # The request is kept (with a fresh token) until the next one — the
    # dialog is an OS child whose body runs on its own frames.
    if open_files.browse_request:
        browse = (open_files.browse_request, (browse[1] + 1) if browse else 0)
        open_files.browse_request = None
        open_requested = True
    melty.draw_menu_bar({'File': {'New…': request_new, 'Open…': request_open},
                         'Projects': projects_menu(),
                         'Search': {'Search…': search.open}},
                        name='menu', bar_height=menu_height)
    if new_draft is None and pressed('ctrl+n'):
        request_new()
    changed, path = melty.draw_file_selector(
        str(paths[0].parent) if paths else None, name='Open file', glfw_window=True,
        open_requested=open_requested or pressed('ctrl+o'), context_menu=FOLDER_MENU,
        window_size=(720, 640), browse=browse)
    open_requested = False
    if changed:
        open_file(path)
    # Projects → Add Folder…: the same explorer as a folder picker; the
    # chosen folder (or a right-clicked one) is marked in the shared store.
    changed, folder = melty.draw_file_selector(
        str(default_new_dir()), name='Add project folder', glfw_window=True,
        open_requested=add_project_requested, choose_folder=True, context_menu=FOLDER_MENU,
        window_size=(720, 640))
    add_project_requested = False
    if changed:
        mark_project(folder)
    if new_draft is not None:
        menu_height += draw_new_field(draw_state.width)
    if open_error:
        imgui.text_wrapped(open_error)
        menu_height += imgui.get_item_rect_size()[1]
    draw_code_editor(open_files, name='code-editor', disable_scroll=True, use_cache=False,
                     show_shortcuts=True,
                     width=draw_state.width - 10, height=draw_state.height - draw_state.header_height - menu_height - 1)
    return False, None
