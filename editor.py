#!/usr/bin/env python3
"""melty-code-editor — melty's code editor as a one-window app.

    melty-code-editor                    open an empty editor (no tabs)
    melty-code-editor FILE [FILE ...]    open them as tabs (the first is selected)

The window is one ``@glfw_window`` hosting a persisted tiled workspace.
Drag a tile corner inward to split it; each tile's dropdown selects an
editor, the Files tree (project_tree.py: the projects as collapsible trees
with the file browser's type-to-search; a double-click opens the file in the
editor tile to the tree's left, else the closest one) or a Claude Code chat
(``meltygui.draw_claude_chat`` as a tile; new conversations start in the selected
tab's project, and the conversations live in meltygui's detached chat
service, shared with every app that draws the chat). Editors share ``OpenFiles``
with independent view state. A tab per file along the bottom, the editor above it with Python
syntax analysis, folds, search and autocomplete, a Compare With dropdown
(git HEAD, the file on disk, any recent commit) and the nav back / forward
buttons. Search → Search… (Ctrl+Shift+F) is melty's global search, its
Code tab over the app's projects (`meltygui_pro.global_search`): the folders
marked as projects (Projects → Add Folder…, or a right-click in the Open
dialog; the flag lives in melty's shared file-meta store, so every melty
app sees it). Open tabs do not add unmarked projects to search.
melty draws the file hosts each frame (load, reparse) and writes
their queued saves when the window closes; this script only owns the list
of files to open, which persists between runs (`meltygui.persisted`): the
tabs of the last run come back, plus whatever the command line names.

The sibling melty_text_editor draws ``draw_text``, one file and nothing
else; this one draws ``draw_code_editor``, the model-backed editor around it.
"""
import pathlib
import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(2)

import meltygui
import meltygui_pro
meltygui.boot(app_id='melty-code-editor')
# Resolve the view through the package APIs: it waits for the import worker, whose work
# overlaps the fresh EGL context above, before importing the code-editor views.
from meltygui import glfw_window, pressed, imgui, window_api as glfw
# Register tensor views for inline captures, including project-process arrays.
from meltygui import draw_voxels, draw_line_graph
from app_model import EditorAppModel
from tile_views import draw_main_editor, draw_chat  # noqa: F401  tile_views registers the Tasks tile
from file_editor import (FileEditorComparisons, draw_file_editor_comparisons,
                         cleanup_file_editor_comparisons)
from project_tree import draw_project_tree, open_path  # noqa: F401  draw_project_tree registers the Files tile
from editor_settings import settings
from new_project import draw_new_project, main_files
import tasks
from meltygui.core.layout.tile_manager_core import TileManagerState, draw_tiles
from meltygui.core.core_render import render_func
from meltygui_pro.models.open_files import OpenFiles
from meltygui.core.melty import Melty
from meltygui.code.fileref import writable_file_refusal
from meltygui.view.header_view import draw_header
from meltygui_pro.models.projects import mark_project, project_roots as marked_projects, project_for

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
# (meltygui.persisted: last run's tabs come back, minus deleted files; the
# selected tab is the window's own state and comes back with it). Files on
# the command line are added, and the first is posted as the pending jump,
# which selects its tab; with no files the editor draws last run's tabs.
open_files = meltygui.persisted('open_files', OpenFiles, app_id='melty-code-editor')
tasks.project_tasks = meltygui.persisted('project_tasks', tasks.ProjectTasks,
                                       app_id='melty-code-editor')
app_model = meltygui.persisted('tile_layout', EditorAppModel, app_id='melty-code-editor')
app_model.bind_open_files(open_files)
for path in paths:
    open_files.open_file(path)
if paths:
    open_files.jump_to_path = str(paths[0])



def real_open_paths():
    """The tabs that are files on disk (not git-diff tabs), in tab order."""
    return [path for path in open_files.open_paths
            if isinstance(path, str) and not path.startswith(OpenFiles.GIT_DIFF_PREFIX)]


def project_roots():
    """Search only explicitly marked projects that still exist on disk.

    Open tabs remain editable after unmarking their project, but no longer
    silently put that project back into global search.
    """
    return [str(root) for root in marked_projects()]


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
search = meltygui_pro.global_search(categories=('Code',), roots=project_roots, open_files=open_files)

open_requested = False
add_project_requested = False
new_project_requested = False
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


def request_new_project():
    """File → New Project…: the template window (a child OS window)."""
    global new_project_requested
    new_project_requested = True


def default_project_location():
    """Where a new project goes by default: beside the selected tab's project."""
    selected = open_files.active_path
    if isinstance(selected, str) and not selected.startswith(OpenFiles.GIT_DIFF_PREFIX):
        root = project_for(selected)
        if root:
            return pathlib.Path(root).parent
    return pathlib.Path.home()


def project_created(root):
    """A template's folder was written: it is a project (search, the
    shortcuts column, every melty app), and its main file is the selected tab."""
    mark_project(root)
    main_file = main_files.pop(root, None)
    if main_file:
        open_file(main_file)


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
        open_files.jump_to_line = 1
        open_files.jump_no_focus = False


def open_file(filename):
    """Open/select a tab using the same editability rules as command-line files."""
    global open_error
    open_error = open_path(open_files, filename, open_files.active_instance)


def selected_tab_project():
    """The selected tab's project root (str), or None without a file tab."""
    selected = open_files.active_path
    if isinstance(selected, str) and not selected.startswith(OpenFiles.GIT_DIFF_PREFIX):
        root = project_for(selected)
        return str(root) if root else None
    return None


def run_menu():
    """The selected tab's project tasks, rebuilt each frame."""
    menu = {'Rerun last task (Ctrl+Shift+R)': tasks.request_rerun}
    root = selected_tab_project()
    names = list(tasks.read_tasks(root)) if root else []
    if names:
        menu['Tasks'] = {name: (lambda _name=name, _root=root: tasks.request_run(_root, _name))
                         for name in names}
    return menu


def quit_window():
    glfw.set_window_should_close(Melty.glfw_window, True)


def draw_new_field(width):
    """The one-line path field: Enter creates + opens, Esc closes. Returns its height."""
    global new_draft, new_opened
    opened, new_opened = new_opened, False
    changed, draft = meltygui.draw_text(new_draft, name='new-file', single_line=True,
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
             with_header=draw_header, bg_offset=-3, tint=(0.39, 0.60, 0.86), settings=settings)
@render_func(use_cache=True, on_cleanup=cleanup_file_editor_comparisons)
def editor(input_value: object, draw_state,
           tile_state: TileManagerState = None, multi_instance_renderers=(),
           file_comparisons: FileEditorComparisons = None):
    global open_requested, add_project_requested, new_project_requested
    menu_height = 25.0
    meltygui.draw_menu_bar({'File': {'New…': request_new, 'New Project…': request_new_project,
                                  'Open…': request_open},
                         'Run': run_menu(),
                         'Projects': projects_menu(),
                         'Search': {'Search…': search.open}},
                        name='menu', bar_height=menu_height)
    if new_draft is None and pressed('ctrl+n'):
        request_new()
    if pressed('ctrl+shift+r'):
        tasks.request_rerun()
    changed, path = meltygui.draw_file_selector(
        str(paths[0].parent) if paths else None, name='Open file', glfw_window=True,
        open_requested=open_requested or pressed('ctrl+o'), context_menu=FOLDER_MENU,
        window_size=(720, 640))
    open_requested = False
    if changed:
        open_file(path)
    # Projects → Add Folder…: the same explorer as a folder picker; the
    # chosen folder (or a right-clicked one) is marked in the shared store.
    changed, folder = meltygui.draw_file_selector(
        str(default_new_dir()), name='Add project folder', glfw_window=True,
        open_requested=add_project_requested, choose_folder=True, context_menu=FOLDER_MENU,
        window_size=(720, 640))
    add_project_requested = False
    if changed:
        mark_project(folder)
    # File → New Project…: the templates of project_templates/ as a form.
    created, project = draw_new_project(
        str(default_project_location()), name='New Project', glfw_window=True,
        open_requested=new_project_requested, window_size=(560, 720))
    new_project_requested = False
    if created:
        project_created(project)
    if new_draft is not None:
        draw_new_field(draw_state.width)
    if open_error:
        imgui.text_wrapped(open_error)
    app_model.reconcile_editors(open_files)
    added_tasks = app_model.ensure_tasks(open_files) if tasks.pending_run() else False
    layout_changed = draw_tiles(
        app_model.tiles, draw_state, tile_state=tile_state,
        multi_instance_renderers=(draw_main_editor, draw_chat, *multi_instance_renderers),
        content_top=imgui.get_cursor_screen_pos()[1])
    draw_file_editor_comparisons(draw_state, file_comparisons, app_model.file_editor_ids())
    app_model.reconcile_editors(open_files)
    return layout_changed or added_tasks, input_value
