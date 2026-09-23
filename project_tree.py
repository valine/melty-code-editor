"""draw_project_tree — the app's Files tile: a project selector over the
selected project's files as a collapsible tree, in the file browser's look and
with its keyboard.

A tile editor (``@render_func(multi_instance=True)``: the tile picker offers
it, the tile view instantiates it and its injected ``ProjectPanelState``).
The header stays put: the project selector (`draw_project_selector`: the saved
projects, a divider, the filesystem shortcuts, Choose Folder… — a project is
any folder). The rows under it (`draw_project_files`) are that folder's tree; a
folder row toggles on a click, a file row opens on a double-click (or Enter).

The tree owns the selected project. Tasks and editor tiles receive it through
DrawState[draw_project_tree]; draw_tiles supplies their per-parameter link pickers.

The rows are the file browser's listing (`draw_file_listing`): names straight
to the draw list, viewport-culled, each wearing its file-meta tint and icon,
the same selection / hover recipe, a listing per folder memoized on the
folder's mtime and refreshed by a FileWatch emitter per folder shown.

Type to search, the browser's way (`search_keys` / `search_typed` /
`search_match`, the same pill, highlight and flash) — but over every file of
the projects, collapsed folders included: while there is a query the tree
shows only the matches under their folders, the best one selected. Up / Down /
Tab step through the matches, Enter opens (and reveals the file in the tree),
Esc clears. Without a query Up / Down walk the rows, Right / Left expand and
collapse. The tree takes the keyboard on a click inside it and leaves it to
whatever text view claims it next.

Right-click a row (or the empty space: the project folder) for Rename…,
New → File / Folder, Add to Projects and Move to Trash (`gio trash`) (`file_menu`, the wrapper's
`context_menu=`). A new file / folder is created under a free name and named
in place like a rename: Enter commits, Esc or a click elsewhere leaves the
name. Open tabs follow a renamed file or folder (`follow_rename`).

The whole tile's background uses the browser's folder wash and file-meta tint
(`tile_fill`, painted by the tile manager through
`draw_project_tree.tile_background`); the chip beside the selector edits it.

A file opens in the active editor linked to this Files view, else the first
linked editor. Links are consumer-owned DrawState[draw_project_tree] parameters.
The shared OpenFiles model carries the open request into the editor's body.
"""
import os
from pathlib import Path

from meltygui import imgui
from meltygui import window_api as glfw
from meltygui.core.core_render import render_func
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.melty import Melty, FileWatch
from meltygui.code.fileref import writable_file_refusal
from meltygui.hdr_color import pack_color
from meltygui_pro.models.editor_project import ProjectSelection
from meltygui_pro.models.open_files import OpenFiles
from meltygui_pro.models.projects import project_roots, project_for
from editor_settings import settings

# Never listed: not something to pick.
TREE_HIDE = frozenset({"__pycache__"})
# Folders the search never descends into (the tree still shows them).
SEARCH_SKIP = frozenset({"__pycache__", "node_modules", ".git", ".venv", "venv"})
# The search index stops growing here: a home folder marked as a project
# must not stall the first keystroke.
SEARCH_LIMIT = 60000


class ProjectTreeState(DictConversion):
    """The rows' injected state. Persists which folders are open and the
    selection; paths are strings. The underscore fields are one session's memos."""
    _owner_ds = None

    def __init__(self):
        super().__init__()
        self.expanded = set()       # str paths of the open folders
        self.selected = None        # str path of the selected row
        self._listings = {}         # str dir -> (mtime_ns, show_hidden, rows)
        self._watches = {}          # str dir -> the watch_directory holder
        self._armed = False         # a click inside took the keyboard
        self._search = ""           # the type-to-search query
        self._search_for = None     # the query the selection was landed for
        self._index = None          # (roots, show_hidden, entries) while searching
        self._hits = None           # (query, [(entry index, rank, spans)])
        self._reveal = None         # str path to scroll to on the next run
        self._error = None          # why the last open / file operation was refused
        self._naming = None         # {'path', 'draft', 'opened'}: the row being named in place


class _Watch:
    """One folder's slot for `watch_directory` (it keeps the watched dir on
    the state object it is handed; the tree has one per folder shown)."""
    _watched = None


def open_path(open_files, path, instance):
    """Open `path` as the selected tab of editor `instance`, by the app's
    editability rules. Returns the refusal (a short reason) or None."""
    path = Path(path).expanduser().resolve()
    try:
        refusal = writable_file_refusal(path)
        if not path.is_file():
            refusal = 'not an existing file'
        if refusal:
            return f'Cannot open {path}: {refusal}'
        open_files.open_file(path)
    except OSError as error:
        return f'Cannot open {path}: {error}'
    open_files.jump_to_path = str(path)
    open_files.jump_to_instance = instance
    return None


# ── the rows ────────────────────────────────────────────────────────────────

def folder_rows(state, directory, meta, show_hidden):
    """`directory`'s rows in the browser's order, memoized on its mtime."""
    from meltygui.model.file_model import _dir_mtime_ns
    from meltygui.model.file_model import list_directory
    from meltygui.model.file_metadata_model import ordered_rows
    key = str(directory)
    mtime = _dir_mtime_ns(directory)
    memo = state._listings.get(key)
    if memo is None or memo[:2] != (mtime, show_hidden):
        memo = state._listings[key] = (
            mtime, show_hidden, [row for row in list_directory(directory, show_hidden)
                                 if row[0].name not in TREE_HIDE])
    return ordered_rows(memo[2], meta)


def visible_rows(state, roots, meta, show_hidden):
    """The tree as drawn without a query: [(Path, is_dir, depth)], the project
    folder's rows (the folder itself is the selector's, not a row) with the
    open folders' rows under them; plus the folders listed (the ones to watch)."""
    rows, shown = [], set()

    def add(directory, depth):
        shown.add(str(directory))
        for path, is_dir in folder_rows(state, directory, meta, show_hidden):
            rows.append((path, is_dir, depth))
            if is_dir and str(path) in state.expanded:
                add(path, depth + 1)

    for root in roots:
        add(root, 0)
    return rows, shown


def search_index(roots, show_hidden):
    """Every file and folder under `roots`, pre-order in the tree's natural
    order (folders first, case-insensitive): [(Path, is_dir, depth)], the
    roots included. Symlinked folders are listed, not entered."""
    entries = []

    def add(directory, depth):
        try:
            found = list(os.scandir(directory))
        except OSError:
            return
        folders, files = [], []
        for entry in found:
            if entry.name in TREE_HIDE or (not show_hidden and entry.name.startswith(".")):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            (folders if is_dir else files).append(entry.name)
        for name in sorted(folders, key=str.casefold):
            if len(entries) >= SEARCH_LIMIT:
                return
            entries.append((Path(directory) / name, True, depth))
            if name not in SEARCH_SKIP:
                add(Path(directory) / name, depth + 1)
        for name in sorted(files, key=str.casefold):
            if len(entries) >= SEARCH_LIMIT:
                return
            entries.append((Path(directory) / name, False, depth))

    for root in roots:
        entries.append((Path(root), True, 0))
        add(root, 1)
    return entries


def search_rows(state, roots, show_hidden, query):
    """The tree under `query`: the matches and the folders leading to them.
    Returns ``(rows, hit_spans, positions, best)`` — rows as `visible_rows`,
    `hit_spans` {row index: spans}, `positions` the hit rows in order and
    `best` the position to land on (files before folders at a rank, the
    shorter name first). A query that extends the last one re-ranks only
    that one's matches."""
    from meltygui.files.fast_file_explorer import search_match
    root_key = tuple(str(root) for root in roots)
    index = state._index
    if index is None or index[:2] != (root_key, show_hidden):
        index = state._index = (root_key, show_hidden, search_index(roots, show_hidden))
        state._hits = None
    entries = index[2]
    last = state._hits
    pool = ([hit[0] for hit in last[1]] if last is not None and query.startswith(last[0])
            else range(len(entries)))
    hits = []
    for at in pool:
        path, _is_dir, depth = entries[at]
        match = search_match(path.name, query) if depth else None   # roots are not hits
        if match is not None:
            hits.append((at, match[0], match[1]))
    state._hits = (query, hits)
    # The rows: every hit, and the chain of folders above each (pre-order,
    # so a hit's ancestors are the last entries of each lower depth before it).
    keep = set()
    chain = {}
    hit_at = {at for at, _rank, _spans in hits}
    for at, (path, _is_dir, depth) in enumerate(entries):
        chain[depth] = at
        if at in hit_at:
            keep.update(chain[level] for level in range(depth + 1))
    rows, hit_spans, positions = [], {}, []
    spans_of = {at: spans for at, _rank, spans in hits}
    rank_of = {at: rank for at, rank, _spans in hits}
    best, best_key = None, None
    for at in sorted(keep):
        path, is_dir, depth = entries[at]
        if not depth:                                # the project folder: not a row
            continue
        depth -= 1
        if at in spans_of:
            hit_spans[len(rows)] = spans_of[at]
            key = (rank_of[at], is_dir, len(path.name), len(positions))
            if best_key is None or key < best_key:
                best, best_key = len(positions), key
            positions.append(len(rows))
        rows.append((path, is_dir, depth))
    return rows, hit_spans, positions, best


def sync_watches(draw_state, state, shown):
    """One FileWatch emitter per folder listed (the listing's
    `watch_directory`, a holder each), retired when the folder leaves the
    tree: the inotify instance cap is per user."""
    from meltygui.core.files import file_explorer_core as explorer
    for key in shown - state._watches.keys():
        holder = state._watches[key] = _Watch()
        explorer.watch_directory(draw_state, holder, key)
    for key in state._watches.keys() - shown:
        del state._watches[key]
        state._listings.pop(key, None)
        holders = explorer._WATCHERS.get(key)
        if holders is not None:
            holders.discard(draw_state)
            if not holders:
                del explorer._WATCHERS[key]
                FileWatch.unwatch_dir(key)


def reveal(state, path, roots):
    """Open the folders above `path` (up to its root) and scroll to it on
    the next run."""
    path = Path(path)
    for root in roots:
        if path == root or path.is_relative_to(root):
            state.expanded.add(str(root))
            for parent in path.parents:
                if parent == root or not parent.is_relative_to(root):
                    break
                state.expanded.add(str(parent))
            break
    state._reveal = str(path)


# ── the right-click menu: Rename, New → File / Folder, Add to Projects, Trash ──
# The menu is the wrapper's (`context_menu=` on the draw_project_files call,
# the file browser's pattern): its callables are built by the tile and get no
# arguments, so they post a request on `menu_target`, the dict the rows write
# the right-clicked path into. The rows pick the request up on their next run.
NEW_FILE_NAME = "untitled.py"
NEW_FOLDER_NAME = "New Folder"


def file_menu(menu_target):
    """{label: callable | {label: callable}} for `context_menu=`."""
    def post(kind):
        return lambda: menu_target.update(request=(kind, menu_target.get("path")))
    # [tint=(0.95, 0.78, 0.35)]
    rename_icon = f"\uf303"
    # [tint=(0.45, 0.85, 0.55)]
    new_icon = f"\uf067"
    # [tint=(0.55, 0.72, 0.95)]
    file_icon = f"\uf15b"
    # [tint=(0.55, 0.72, 0.95)]
    folder_icon = f"\uf07b"
    # [tint=(0.62, 0.78, 0.98)]
    project_icon = f"\uf02e"
    # [tint=(0.95, 0.45, 0.4)]
    trash_icon = f"\uf2ed"
    return {f"{rename_icon}  Rename…": post("rename"),
            f"{new_icon}  New": {f"{file_icon}  File": post("file"),
                                f"{folder_icon}  Folder": post("folder")},
            f"{project_icon}  Add to Projects": post("project"),
            f"{trash_icon}  Move to Trash": post("trash")}


def move_to_trash(target, open_files):
    """`gio trash` (the file browser's): the desktop's trash, so it can be
    restored. The tabs of what went are closed. Returns the refusal or None."""
    import shutil
    import subprocess
    gio = shutil.which("gio")
    if gio is None:
        return "Cannot move to the trash: gio is not on PATH."
    done = subprocess.run([gio, "trash", str(target)], capture_output=True, text=True)
    if done.returncode:
        return (done.stderr.strip().splitlines() or [f"Cannot trash {target.name}."])[-1]
    if open_files is not None:
        for path in list(open_files.open_paths):
            real = Path(path.removeprefix(OpenFiles.GIT_DIFF_PREFIX)) if isinstance(path, str) else None
            if real is not None and (real == target or target in real.parents):
                open_files.close_file(path)
    return None


def free_name(directory, name):
    """`directory/name`, numbered ("name (2).ext") until nothing is there."""
    target = Path(directory) / name
    stem, suffix = target.stem, target.suffix
    number = 2
    while target.exists():
        target = Path(directory) / f"{stem} ({number}){suffix}"
        number += 1
    return target


def begin_request(state, request, roots, open_files=None):
    """Start what the menu asked for. A new file / folder is created under a
    free name at once and then named in place, as a rename. Returns the
    refusal (a short reason) or None."""
    kind, target = request
    target = Path(target) if target else roots[0]
    if kind == "project":
        from meltygui_pro.models.projects import mark_project
        mark_project(target if target.is_dir() else target.parent)
        return None
    if kind == "trash":
        if target in roots:
            return "The project folder is not trashed from its own tree."
        refusal = move_to_trash(target, open_files)
        if refusal is None and state.selected == str(target):
            state.selected = None
        return refusal
    if kind == "rename":
        if target in roots:
            return "The project folder is renamed outside the tree."
    else:
        directory = target if target.is_dir() else target.parent
        target = free_name(directory, NEW_FOLDER_NAME if kind == "folder" else NEW_FILE_NAME)
        try:
            target.mkdir() if kind == "folder" else target.touch(exist_ok=False)
        except OSError as error:
            return f"Cannot create {target.name}: {error.strerror or error}"
        reveal(state, target, roots)
    state.selected = str(target)
    state._naming = {"path": str(target), "draft": target.name, "opened": True}
    return None


def commit_name(state, open_files):
    """Rename the row being named to its draft; open tabs follow the file.
    Returns (refusal | None, the new path | None)."""
    naming, state._naming = state._naming, None
    old = Path(naming["path"])
    name = naming["draft"].strip()
    if not name or name == old.name:
        return None, None
    if "/" in name or name in (".", ".."):
        return f"'{name}' is not a file name.", None
    new = old.with_name(name)
    if new.exists():
        return f"{name} already exists.", None
    try:
        old.rename(new)
    except OSError as error:
        return f"Cannot rename {old.name}: {error.strerror or error}", None
    if str(old) in state.expanded:
        state.expanded.discard(str(old))
        state.expanded.add(str(new))
    state.selected = str(new)
    if open_files is not None:
        follow_rename(open_files, old, new)
    return None, new


def follow_rename(open_files, old, new):
    """The tabs of `old` (a file, or the files under a folder) become `new`'s:
    same slots, hosts reopened from the new path on their next draw."""
    def moved(path):
        real = Path(path.removeprefix(OpenFiles.GIT_DIFF_PREFIX))
        if real != old and old not in real.parents:
            return None
        prefix = OpenFiles.GIT_DIFF_PREFIX if path.startswith(OpenFiles.GIT_DIFF_PREFIX) else ""
        return prefix + str(new / real.relative_to(old) if real != old else new)

    for index, path in enumerate(list(open_files.open_paths)):
        target = moved(path) if isinstance(path, str) else None
        if target is None:
            continue
        open_files.open_paths[index] = target
        open_files.files.pop(path, None)
        if open_files.active_path == path:
            open_files.jump_to_path = target
            open_files.jump_to_instance = open_files.active_instance
            open_files.jump_no_focus = True


def draw_file_badge(draw_list, x, y, size, color, badge, alpha=1.0):
    """Tinted page with a small codec accent; geometry stays inside the tint chip."""
    label, accent, mark = badge
    unit = size / 28.0
    # Change these proportions to adjust the page/badge balance.
    left, top = x + 5 * unit, y + unit
    right, bottom = x + 23 * unit, y + 26 * unit
    fold = 5 * unit
    ink = pack_color(0.08, 0.10, 0.13, alpha)
    if mark == "image":
        # The same white-to-metadata-tint blend as the other file bases.
        # A dark inset makes the landscape distinct even with no assigned tint.
        draw_list.add_rect_filled(x + unit, y + 2 * unit, x + 27 * unit,
                                  y + 19 * unit, color, rounding=unit)
        draw_list.add_rect_filled(x + 3 * unit, y + 4 * unit, x + 25 * unit,
                                  y + 17 * unit, ink)
        draw_list.add_triangle_filled(x + 4 * unit, y + 16 * unit,
                                      x + 10 * unit, y + 9 * unit,
                                      x + 16 * unit, y + 16 * unit, color)
        draw_list.add_triangle_filled(x + 12 * unit, y + 16 * unit,
                                      x + 18 * unit, y + 11 * unit,
                                      x + 24 * unit, y + 16 * unit, color)
        draw_list.add_circle_filled(x + 20 * unit, y + 7 * unit, 1.5 * unit, color)
    else:
        draw_list.add_rect_filled(left, top, right - fold, bottom, color, rounding=unit)
        draw_list.add_rect_filled(right - fold, top + fold, right, bottom, color)
        draw_list.add_triangle_filled(right - fold, top, right, top + fold,
                                      right - fold, top + fold, color)
        draw_list.add_line(right - fold, top, right - fold, top + fold, ink, unit)
        draw_list.add_line(right - fold, top + fold, right, top + fold, ink, unit)
    if mark == "python":
        # Interlocking blue/yellow snakes, drawn as vectors so no brand font is needed.
        blue = pack_color(0.18, 0.43, 0.65, alpha)
        yellow = pack_color(1.0, 0.82, 0.28, alpha)
        sx, sy = x + 8 * unit, y + 4 * unit
        for dx, dy, width, height, paint in (
                (3, 0, 7, 5, blue), (0, 4, 7, 5, blue),
                (5, 5, 7, 5, yellow), (2, 9, 7, 4, yellow)):
            draw_list.add_rect_filled(sx + dx * unit, sy + dy * unit,
                                      sx + (dx + width) * unit, sy + (dy + height) * unit,
                                      paint, rounding=2 * unit)
        draw_list.add_circle_filled(sx + 5 * unit, sy + 2 * unit, 0.7 * unit, ink)
        draw_list.add_circle_filled(sx + 7 * unit, sy + 11 * unit, 0.7 * unit, ink)
    # A narrow colored footer leaves the page itself predominantly file-tinted.
    draw_list.add_rect_filled(x, y + 18 * unit, x + size, y + 27 * unit,
                              pack_color(*accent, alpha), rounding=unit)
    imgui.set_window_font_scale(0.36)
    try:
        extent = imgui.calc_text_size(label)
        # Dark red needs light lettering; the brighter badges use dark lettering.
        lettering = pack_color(1.0, 0.93, 0.93, alpha) if label == "MD" else ink
        draw_list.add_text(x + (size - extent.x) * 0.5,
                           y + 22.5 * unit - extent.y * 0.5, lettering, label)
    finally:
        imgui.set_window_font_scale(1.0)


@render_func(tint=(0.32, 0.42, 0.54), selectable=False, disable_scroll=False, show_add_delete=False,
             is_tree=False, show_bg=False, shadow=False, show_header=False)
def draw_project_files(input_value: object, draw_state, tree_state: ProjectTreeState = None,
                       root=None, file_metadata=None, left_mouse_down=False, left_mouse_double_clicked=False,
                       right_mouse_down=False, menu_target=None,
                      row_height=20.0, left_pad=6.0, indent=12.0, glyph_width=24.0,
                      chevron_width=13.0, default_tint=(0.32, 0.42, 0.54, 1.0),
                      select_boost=0.22, plain_select_boost=0.06, select_shadow=2.0,
                      select_rounding=3.0, hover_boost=0.06, hover_alpha=0.05,
                      text_mix=0.5, icon_mix=0.9, search_tint=(1.0, 0.82, 0.3), search_dim=0.45,
                      search_flash_frames=36, **kwargs):
    """The files of the project folder `root` as a collapsible tree (see the
    module docstring): the rows under `draw_project_tree`'s header.
    `input_value` is the tile's: the app's ``OpenFiles``, where a picked file
    opens; it is returned unchanged. The look parameters are
    `draw_file_listing`'s; `indent` is one nesting level, `chevron_width`
    the folder arrow's column before the icon."""
    from meltygui.files.fast_file_explorer import _scroll_row_into_view
    from meltygui.files.fast_file_explorer import claim_keyboard
    from meltygui.files.fast_file_explorer import row_icon
    from meltygui.code.codec_registry import file_badge_for_path
    from meltygui.files.fast_file_explorer import row_tint_bg
    from meltygui.files.fast_file_explorer import search_keys
    from meltygui.files.fast_file_explorer import search_typed
    from meltygui.files.fast_file_explorer import tinted_text
    from meltygui.models.file_meta import FileMeta
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.core.cache.tile_marks import add_shadow
    from meltygui.core.cache.tile_marks import clear_glows

    # [tint=(0.55, 0.72, 0.95)]
    folder_icon = f"\uf07b"
    # [tint=(0.55, 0.72, 0.95)]
    file_icon = f"\uf15b"
    text_rgba = (0.867, 0.858, 0.874, 1.0)
    text_col = pack_color(*text_rgba)
    dim_col = pack_color(0.6, 0.63, 0.68, 1.0)
    folder_rgba = (0.78, 0.84, 0.92, 1.0)
    folder_col = pack_color(*folder_rgba)
    search_wash = pack_color(search_tint[0], search_tint[1], search_tint[2], 0.30)
    search_col = pack_color(search_tint[0], search_tint[1], search_tint[2], 1.0)
    no_match_col = pack_color(0.95, 0.55, 0.5, 1.0)
    hover_wash = pack_color(1.0, 1.0, 1.0, hover_alpha)

    state = tree_state
    open_files = input_value if isinstance(input_value, OpenFiles) else None
    show_hidden = settings["FileTree"]["show_hidden"]
    # The selected row's add_shadow is retained under this draw_state until
    # its group opens again (the listing's rule).
    clear_glows(draw_state)
    px = Melty.px
    row_h, pad, glyph_w = px(row_height), px(left_pad), px(glyph_width)
    step_w, chevron_w = px(indent), px(chevron_width)
    draw_list = imgui.get_window_draw_list()
    content_w = draw_state.content_width or (draw_state.width or 240)
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    click = ((left_mouse_down.x, left_mouse_down.y)
             if (left_mouse_down and hasattr(left_mouse_down, "x")) else None)
    double_click = ((left_mouse_double_clicked.x, left_mouse_double_clicked.y)
                    if (left_mouse_double_clicked and hasattr(left_mouse_double_clicked, "x")) else None)
    right_press = ((right_mouse_down.x, right_mouse_down.y)
                   if (right_mouse_down and hasattr(right_mouse_down, "x")) else None)
    meta = file_metadata
    row_bg = row_tint_bg()

    roots = [Path(root)] if root and Path(root).is_dir() else []
    if not roots:
        imgui.text_wrapped("Select a project above.")
        return False, input_value

    # ── the menu's pick (before the rows are listed: a new file is one) ──
    request = menu_target.pop("request", None) if menu_target is not None else None
    if request is not None:
        state._error = begin_request(state, request, roots, open_files)
        if request[0] == "trash" and state._error is None:
            tile_ds = draw_state._parent             # a tab may have closed
            wake_editors(tile_ds, {instance for instance, _editor in ordered_editors(tile_ds)})
        request_render()
    naming = state._naming
    if naming is not None and not Path(naming["path"]).exists():
        naming = state._naming = None
    if naming is not None:
        state._armed = False                         # the name field has the keyboard

    # ── the keyboard: taken on a click inside, left to the next text view ──
    if click is not None:
        state._armed = True
        state._error = None
    holder = Melty.text_focused_ds
    if holder is not None and getattr(holder, "_tile_id", None) != draw_state._tile_id:
        state._armed = False
    owns_keys = state._armed and claim_keyboard(draw_state)
    query, step, activate, escape, side = state._search, 0, False, False, 0
    if owns_keys:
        keys = search_keys()
        query, step, activate, _parent, escape = search_typed(query, keys)
        if not query:
            side = sum((key == glfw.KEY_RIGHT) - (key == glfw.KEY_LEFT) for key, _mods in keys)
        if keys:
            draw_state.invalidate()
            request_render()
    if query != state._search:
        state._search = query
        if not query:
            state._index = state._hits = None        # the next search rescans

    # ── the rows: the matches under their folders, else the open tree ──
    hit_spans, positions, best = {}, [], None
    if query:
        rows, hit_spans, positions, best = search_rows(state, roots, show_hidden, query)
        if not positions:                            # no match: the tree, dimmed by nothing
            rows, shown = visible_rows(state, roots, meta, show_hidden)
            sync_watches(draw_state, state, shown)
    else:
        rows, shown = visible_rows(state, roots, meta, show_hidden)
        sync_watches(draw_state, state, shown)
    searching = bool(query and positions)

    rows_x, rows_y = imgui.get_cursor_screen_pos()
    top_inset = (rows_y + draw_state.scroll_offset[1]) - draw_state.abs_top
    content_h = len(rows) * row_h + max(0.0, top_inset)
    imgui.dummy(content_w, max(1.0, content_h))
    clip = getattr(draw_state, "abs_clip_rect", None)
    rows_top = rows_y - draw_state.abs_top + draw_state.scroll_offset[1]
    view_rect = clip if clip is not None else (
        draw_state.abs_left, draw_state.abs_top,
        draw_state.abs_left + (draw_state.width or 0), draw_state.abs_top + (draw_state.height or 0))

    def row_at(point):
        if point is None or not (rows_x <= point[0] <= rows_x + content_w):
            return None
        index = int((point[1] - rows_y) // row_h)
        return index if 0 <= index < len(rows) else None

    def index_of(path):
        for i, (row_path, _is_dir, _depth) in enumerate(rows):
            if str(row_path) == path:
                return i
        return None

    selected_index = index_of(state.selected) if state.selected is not None else None

    def flash_row(index):
        """Melty.emphasize on row `index` (the listing's search flash)."""
        top = rows_top + index * row_h

        def rect(ds=draw_state, top=top, h=row_h, x0=rows_x, w=content_w):
            y = ds.abs_top + top - ds.scroll_offset[1]
            return (x0, y, x0 + w, y + h)

        def clip_rect(ds=draw_state, fallback=view_rect):
            return getattr(ds, "abs_clip_rect", None) or fallback

        Melty.emphasize(f"project tree {draw_state._tile_id} {rows[index][0]}", rect,
                        clip=clip_rect, rounding=px(select_rounding),
                        fade_frames=search_flash_frames)

    def select_row(index, centre=False, flash=False):
        moved = index != selected_index
        state.selected = str(rows[index][0])
        _scroll_row_into_view(draw_state, index, row_h, rows_top, centre=centre,
                              content_h=content_h)
        if flash and moved:
            flash_row(index)
        draw_state.invalidate()
        request_render()
        return index

    def toggle(path):
        state.expanded.symmetric_difference_update({str(path)})
        draw_state.invalidate()
        request_render()

    def open_row(index):
        """A file: open it in a linked editor (and leave the search,
        the file shown in the tree). A folder: toggle it, or leave the
        search with it open."""
        path, is_dir, _depth = rows[index]
        if query:
            state._search, state._search_for = "", None
            state._index = state._hits = None
            reveal(state, path, roots)
            if is_dir:
                state.expanded.add(str(path))
        elif is_dir:
            toggle(path)
        if not is_dir and open_files is not None:
            editors = project_editors(draw_state._parent)
            editor = next((item for item in editors
                           if item._kwargs.get("instance") == open_files.active_instance),
                          next(iter(editors), None))
            instance = (open_files.active_instance if editor is None
                        else editor._kwargs["instance"])
            state._error = open_path(open_files, path, instance)
            if state._error is None:
                # The jump is adopted INSIDE the editor's body: force it past
                # its blit cache (open_in_editor's wake).
                if editor is not None and Melty.cache is not None and editor._tile_id is not None:
                    Melty.cache.invalidate_up(editor._tile_id, force=True, max_depth=4)
                # Every other editor shows the same OpenFiles: its tab bar has a new tab.
                for linked_editor in editors:
                    linked_editor.invalidate()
                open_files.jump_no_focus = False
                state._armed = False                 # the editor takes the keyboard
                if Melty.text_focused_ds is draw_state:
                    Melty.text_focused_ds = None
        draw_state.invalidate()
        request_render()

    # A file opened from the search (or restored): scroll to it once shown.
    if state._reveal is not None and not query:
        at = index_of(state._reveal)
        state._reveal = None
        if at is not None:
            selected_index = select_row(at, centre=True, flash=True)

    # ── input on rows ──
    if naming is not None and (click is not None or right_press is not None):
        naming = state._naming = None                # a click elsewhere leaves the name as it was
    hit = row_at(right_press)
    if hit is not None:                              # the menu acts on the row under it
        state.selected = str(rows[hit][0])
        selected_index = hit
        draw_state.invalidate()
        request_render()
    elif right_press is not None and state.selected is not None:
        state.selected = None                        # empty space: the project folder
        selected_index = None
        draw_state.invalidate()
        request_render()
    if menu_target is not None:
        menu_target["path"] = state.selected if selected_index is not None else str(roots[0])
    hit = row_at(double_click)
    if hit is not None:
        state.selected = str(rows[hit][0])
        if not rows[hit][1]:
            open_row(hit)
            return True, input_value
        if not query:
            # The double arrives on the second release, after both presses
            # toggled the folder: a double-click is ONE toggle.
            toggle(rows[hit][0])
            return False, input_value
    hit = row_at(click)
    if hit is not None:
        state.selected = str(rows[hit][0])
        selected_index = hit
        if rows[hit][1] and not query:               # a folder toggles on the press
            toggle(rows[hit][0])
            return False, input_value
        draw_state.invalidate()
        request_render()
    if activate and selected_index is not None:
        open_row(selected_index)
        return True, input_value
    if escape and state.selected is not None:
        state.selected = None
        selected_index = None
        draw_state.invalidate()
        request_render()

    # ── the search lands on the best match; the steps walk matches / rows ──
    if searching:
        if state._search_for != query or selected_index not in hit_spans:
            selected_index = select_row(positions[best], centre=True, flash=True)
        elif step:
            at = positions.index(selected_index)
            selected_index = select_row(positions[(at + step) % len(positions)],
                                        centre=True, flash=True)
    elif step and not query:
        selected_index = select_row(
            0 if selected_index is None and step > 0
            else len(rows) - 1 if selected_index is None
            else max(0, min(len(rows) - 1, selected_index + step)))
    elif side and selected_index is not None:
        path, is_dir, depth = rows[selected_index]
        is_open = is_dir and str(path) in state.expanded
        if side > 0 and is_dir and not is_open:
            toggle(path)
        elif side < 0 and is_open:
            toggle(path)
        elif side < 0 and depth:                     # to the folder above
            for at in range(selected_index - 1, -1, -1):
                if rows[at][2] < depth:
                    selected_index = select_row(at)
                    break
    state._search_for = query if query else None
    if query and not positions and step:
        request_render()

    # ── rows: viewport-culled, straight to the draw list ──
    from meltygui.view.collection_view import draw_tuple_fast
    # Keep the icon and its tint hit target inside the original 20px file row.
    chip_size = px(18)
    name_field = None
    text_y_pad = (row_h - imgui.get_font_size()) * 0.5
    for i, (path, is_dir, depth) in enumerate(rows):
        ry0 = rows_y + i * row_h
        ry1 = ry0 + row_h
        if clip is not None and (ry1 < clip[1] or ry0 > clip[3]):
            continue
        entry = meta.get(str(path)) if meta is not None else None
        tint = FileMeta.painted_tint(entry)
        icon = row_icon(path, is_dir, entry, folder_icon, file_icon)
        badge = (file_badge_for_path(path) if not is_dir
                 and not (isinstance(entry, dict) and entry.get("icon")) else None)
        spans = hit_spans.get(i)
        name_rgba, glyph_rgba = (folder_rgba if is_dir else text_rgba), text_rgba
        faded = searching and spans is None          # a folder leading to a match
        if faded:
            name_rgba = name_rgba[:3] + (name_rgba[3] * search_dim,)
            glyph_rgba = glyph_rgba[:3] + (glyph_rgba[3] * search_dim,)
        if tint:
            name_col = tinted_text(name_rgba, tint, text_mix)
            icon_col = tinted_text(glyph_rgba, tint, icon_mix)
        elif faded:
            name_col = pack_color(*name_rgba)
            icon_col = pack_color(*glyph_rgba)
        else:
            name_col = icon_col = folder_col if is_dir else text_col
        row_hovered = hover_ok and rows_x <= mouse_x <= rows_x + content_w and ry0 <= mouse_y < ry1
        boost = (select_boost if tint else plain_select_boost) if i == selected_index else 0.0
        if i == selected_index and select_shadow:
            add_shadow((rows_x, ry0, content_w, row_h), offset=select_shadow,
                       corner_radius=px(select_rounding), clip=clip, draw_state=draw_state)
        if boost:
            draw_list.add_rect_filled(rows_x, ry0, rows_x + content_w, ry1,
                                      row_bg(tint or default_tint,
                                             boost + (hover_boost if row_hovered else 0.0)),
                                      rounding=px(select_rounding))
        elif row_hovered:
            draw_list.add_rect_filled(rows_x, ry0, rows_x + content_w, ry1, hover_wash,
                                      rounding=px(select_rounding))
        x = rows_x + pad + depth * step_w
        if is_dir:
            # The folder arrow: right = closed, down = open (every folder
            # above a match is open while searching).
            is_open = searching or str(path) in state.expanded
            cx, cy, radius = x + chevron_w * 0.5 - px(1), ry0 + row_h * 0.5, px(3.5)
            tri = ((cx - radius, cy - radius + px(1), cx + radius, cy - radius + px(1), cx, cy + radius - px(1))
                   if is_open else
                   (cx - radius + px(1), cy - radius, cx + radius - px(1), cy, cx - radius + px(1), cy + radius))
            draw_list.add_triangle_filled(*tri, icon_col)
        x += chevron_w
        chip_cursor = imgui.get_cursor_screen_pos()
        tint_changed, new_tint = draw_tuple_fast(
            tuple(tint) if tint is not None else FileMeta.tint, draw_state,
            view_id=f"tree_tint_{path}", x=x,
            y=ry0 + (row_h - chip_size) * 0.5, size=chip_size,
            empty_tint_icon=True, icon="" if badge else icon, icon_color=icon_col,
            hovered=row_hovered and x <= mouse_x < x + chip_size,
            setter=lambda value, _path=str(path): set_folder_tint(_path, value))
        imgui.set_cursor_screen_pos(chip_cursor)
        if badge:
            draw_file_badge(draw_list, x, ry0 + (row_h - chip_size) * 0.5,
                            chip_size, icon_col, badge, glyph_rgba[3])
        if tint_changed:
            set_folder_tint(str(path), new_tint)
            draw_state.invalidate()
            request_render()
        name_x = x + glyph_w
        name = path.name or str(path)
        if spans:
            for start, end in spans:
                sx0 = name_x + imgui.calc_text_size(name[:start]).x
                sx1 = sx0 + imgui.calc_text_size(name[start:end]).x
                draw_list.add_rect_filled(sx0 - px(1), ry0 + px(2), sx1 + px(1), ry1 - px(2),
                                          search_wash, rounding=px(2))
        if naming is not None and naming["path"] == str(path):
            name_field = (name_x, ry0)               # the field is drawn over the row, below
            continue
        draw_list.add_text(name_x, ry0 + text_y_pad, name_col, name)

    # ── the name field: Enter renames, Esc (or a click elsewhere) leaves it ──
    if naming is not None and name_field is not None:
        import meltygui
        opened, naming["opened"] = naming["opened"], False
        cursor = imgui.get_cursor_screen_pos()
        imgui.set_cursor_screen_pos((name_field[0] - px(3), name_field[1]))
        edited, draft = meltygui.draw_text(
            naming["draft"], name="row-name", single_line=True, syntax_highlight=False,
            autocomplete=False, wrap=False, request_focus=opened, select_all_on_focus=opened,
            width=max(px(80), rows_x + content_w - name_field[0]), height=row_h,
            show_header=False, disable_scroll=True)
        imgui.set_cursor_screen_pos(cursor)
        keys = {key for key, _mods in Melty.frame_key_events}
        entered = bool(keys & {glfw.KEY_ENTER, glfw.KEY_KP_ENTER}) or (edited and "\n" in draft)
        if edited:
            naming["draft"] = draft.replace("\n", "")
        if glfw.KEY_ESCAPE in keys:
            state._naming = None
        elif entered:
            state._error, renamed = commit_name(state, open_files)
            if renamed is not None:
                for linked_editor in editors:
                    linked_editor.invalidate()
        if state._naming is None:
            draw_state.invalidate()
            request_render()

    # ── the search pill: the query and "n of m", bottom right of the view ──
    if query:
        icon_search = ""
        count = (f"{positions.index(selected_index) + 1} of {len(positions)}"
                 if searching and selected_index in hit_spans else "no match")
        pill_h = px(26)
        pad_x, gap = px(11), px(12)
        query_w = imgui.calc_text_size(query).x
        icon_w = imgui.calc_text_size(icon_search).x
        count_w = imgui.calc_text_size(count).x
        pill_w = pad_x + icon_w + px(8) + query_w + gap + count_w + pad_x
        px1 = view_rect[2] - px(14)
        py1 = view_rect[3] - px(12)
        px0 = max(view_rect[0] + px(6), px1 - pill_w)
        py0 = py1 - pill_h
        draw_list.add_rect_filled(px0 + px(1), py0 + px(2), px1 + px(1), py1 + px(2),
                                  pack_color(0.0, 0.0, 0.0, 0.35), rounding=pill_h * 0.5)
        draw_list.add_rect_filled(px0, py0, px1, py1,
                                  pack_color(*row_bg.rgb(default_tint, -0.02), 0.97),
                                  rounding=pill_h * 0.5)
        draw_list.add_rect(px0, py0, px1, py1, pack_color(1.0, 1.0, 1.0, 0.14),
                           rounding=pill_h * 0.5)
        pill_ty = py0 + (pill_h - imgui.get_font_size()) * 0.5
        draw_list.add_text(px0 + pad_x, pill_ty, search_col, icon_search)
        draw_list.add_text(px0 + pad_x + icon_w + px(8), pill_ty, text_col, query)
        draw_list.add_text(px1 - pad_x - count_w, pill_ty,
                           dim_col if searching else no_match_col, count)
    elif state._error:
        draw_list.add_text(view_rect[0] + px(8), view_rect[3] - px(12) - imgui.get_font_size(),
                           no_match_col, state._error)

    return False, input_value


# ── the tile: the selector and the rows ─────────────────────


class ProjectPanelState(ProjectSelection):
    """The Files tile's selected project and row menu state."""

    def __init__(self):
        super().__init__()
        self.selected_project = None
        self._menu = {}             # the rows' right-click target + the menu's pending request


def set_folder_tint(folder, tint):
    """Set a file/folder tint in shared metadata; None removes the tint."""
    from meltygui.models.file_meta import file_meta_store
    from meltygui.model.file_metadata_model import set_row_tint
    set_row_tint(file_meta_store(), folder, tint)


def folder_tint(folder):
    """The project folder's file-meta tint (rgba; FileMeta's default, alpha 0, when unpainted)."""
    from meltygui.models.file_meta import FileMeta, file_meta_store
    return tuple(FileMeta.painted_tint(file_meta_store().get(folder)) or FileMeta.tint)


def tile_fill(tint, fallback, folder_bg_boost=-0.23):
    """Use the browser's folder wash; adjust its boost alongside browser()."""
    from meltygui.files.fast_file_explorer import row_tint_bg
    painted = len(tint) < 4 or bool(tint[3])
    return row_tint_bg()(tint if painted else fallback, folder_bg_boost)


# tile id -> packed fill, written by each Files tile's body and read by the
# tile manager through `draw_project_tree.tile_background` (the whole tile,
# grips and picker included: the view is clipped to the box inside them).
_TILE_FILLS = {}


def default_project(open_files):
    """A fresh tree's project: the selected tab's, else the first saved one, else home."""
    path = open_files.active_path if open_files is not None else None
    if isinstance(path, str) and not path.startswith(OpenFiles.GIT_DIFF_PREFIX):
        root = project_for(path)
        if root:
            return str(root)
    saved = project_roots()
    return str(saved[0] if saved else Path.home())


@render_func(multi_instance=True, tint=(0.32, 0.42, 0.54), icon=f"\uf07c", display_name="Files",
             selectable=False, disable_scroll=True, show_add_delete=False, is_tree=False,
             show_bg=False, shadow=False, show_header=False, use_cache=False)
def draw_project_tree(input_value: object, draw_state, panel_state: ProjectPanelState = None,
                      header_height=30.0, **kwargs):
    """The Files tile (see the module docstring). `input_value` is the tile's:
    the app's ``OpenFiles``; it is returned unchanged."""
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui_pro.editor.project_selector import draw_project_selector
    state = panel_state
    open_files = input_value if isinstance(input_value, OpenFiles) else None
    initialized = not state.selected_project or not Path(state.selected_project).is_dir()
    if initialized:
        state.set_project(default_project(open_files))

    px = Melty.px
    left, top = imgui.get_cursor_screen_pos()
    width = draw_state.content_width or (draw_state.width or 240)
    gap, header_h = px(4), px(header_height)
    tint = folder_tint(state.selected_project)
    _TILE_FILLS[kwargs.get("instance")] = tile_fill(tint, draw_state.current_tint)
    # The project folder's colour (its file-meta tint: the tile's background,
    # the folder's row in every tree): the tab bar's chip, picker and undo.
    from meltygui.view.collection_view import draw_tuple_fast
    chip, chip_w = px(17), px(24)
    project = state.selected_project
    tint_changed, new_tint = draw_tuple_fast(
        tint, draw_state, view_id=f"project_tint_{project}",
        x=left + (chip_w - chip) * 0.5, y=top + (header_h - chip) * 0.5,
        empty_tint_icon=True,
        setter=lambda value, _folder=project: set_folder_tint(_folder, value))
    if tint_changed:
        set_folder_tint(project, new_tint)
        draw_state.invalidate()
        request_render()
    imgui.set_cursor_screen_pos((left + chip_w + gap, top))
    changed, folder = draw_project_selector(state.selected_project, name="project-selector",
                                            width=max(px(60), width - chip_w - gap),
                                            trigger_height=header_height)
    if changed:
        state.set_project(folder)
        draw_state.invalidate()
        request_render()
    project_path = Path(state.selected_project)
    draw_state.nickname = project_path.name or str(project_path)
    # Expose the same project colour the instance paints, for source menus.
    project_tint = folder_tint(state.selected_project)
    if len(project_tint) < 4 or project_tint[3]:
        draw_state.current_tint = tuple(project_tint[:3])
    imgui.set_cursor_screen_pos((left, top + header_h + gap))
    rows_height = max(0.0, (draw_state.height or 0) - header_h - gap - px(4))
    opened, _ = draw_project_files(input_value, name="files", root=state.selected_project,
                                   width=width, height=rows_height,
                                   context_menu=file_menu(state._menu), menu_target=state._menu)
    return initialized or changed or tint_changed or opened, input_value


draw_project_tree.tile_background = lambda tile: _TILE_FILLS.get(tile.id)


def project_selection(files_view):
    """The selection owned by an injected Files view, or no shared project."""
    return files_view.misc.get("panel_state") if files_view is not None else None


def project_editors(files_view):
    """Editors subscribed through framework injection, used for file-open routing."""
    if Melty.cache is None:
        return []
    return sorted((consumer for consumer in Melty.cache.parameter_dependencies.subscribers(files_view)
                   if getattr(consumer._view_func, "__name__", None)
                   in {"draw_main_editor", "draw_code_editor", "draw_file_editor"}),
                  key=lambda consumer: str(consumer._kwargs.get("instance", "")))
