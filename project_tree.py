"""draw_project_tree — the app's file selector: the projects as collapsible
trees, in the file browser's look and with its keyboard.

A tile editor (``@render_func(multi_instance=True)``: the tile picker offers
it, the tile view instantiates it and its injected ``ProjectTreeState``). The
roots are the marked projects plus the projects of the open tabs; a folder
row toggles on a click, a file row opens on a double-click (or Enter).

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

A file opens in the code-editor tile ADJACENT TO THE TREE'S LEFT, else the
closest one: `target_editor` finds them by the sibling draw_state walk (the
dim picker's `_sibling_dim_keys`) — the tiles of a host are all children of
the host's draw_state, so the editors are the tree's siblings rendering
`draw_code_editor`, each carrying its tile's ``instance`` and its live box.
The open itself is the model's: ``jump_to_path`` + ``jump_to_instance`` on
the shared ``OpenFiles``, adopted by that editor on its next frame.
"""
import math
import os
from pathlib import Path

from meltygui import imgui
from meltygui import window_api as glfw
from meltygui.core.core_render import render_func
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.melty import Melty, FileWatch
from meltygui.code.fileref import writable_file_refusal
from meltygui.hdr_color import pack_color
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
EDITOR_VIEW = "draw_code_editor"


class ProjectTreeState(DictConversion):
    """The tree's injected state. Persists which folders are open, the
    selection and the roots already seen (a NEW root starts expanded); paths
    are strings. The underscore fields are one session's memos."""
    _owner_ds = None

    def __init__(self):
        super().__init__()
        self.expanded = set()       # str paths of the open folders
        self.known_roots = []       # str roots that have been shown once
        self.selected = None        # str path of the selected row
        self._listings = {}         # str dir -> (mtime_ns, show_hidden, rows)
        self._watches = {}          # str dir -> the watch_directory holder
        self._armed = False         # a click inside took the keyboard
        self._search = ""           # the type-to-search query
        self._search_for = None     # the query the selection was landed for
        self._index = None          # (roots, show_hidden, entries) while searching
        self._hits = None           # (query, [(entry index, rank, spans)])
        self._reveal = None         # str path to scroll to on the next run
        self._error = None          # why the last open was refused


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


# ── the sibling draw_state walk ─────────────────────────────────────────────

def sibling_editors(draw_state):
    """The code-editor tiles rendering beside this tree: [(draw_state, box)],
    box = (x0, y0, x1, y1) absolute. Found by walking the render tree (the
    parent's _view_children index, where every rendered child self-registers)
    and recognizing the renderer by its stable name; nothing is written on
    a sibling. A tile switched to another editor leaves its old draw_state
    registered, so only the ones seen this frame or the last count."""
    parent = draw_state._parent if draw_state is not None else None
    if parent is None or parent is draw_state:      # a root ds parents itself
        return []
    editors = []
    for sib in parent._view_children.values():
        if sib is None or sib is draw_state or sib._parent is not parent:
            continue
        if getattr(sib._view_func, "__name__", None) != EDITOR_VIEW:
            continue
        if sib.last_seen is None or Melty.frame_count - sib.last_seen > 1:
            continue
        instance = (sib._kwargs or {}).get("instance")
        if instance is None or not sib.width or not sib.height:
            continue
        editors.append((sib, (sib.abs_left, sib.abs_top,
                                   sib.abs_left + sib.width, sib.abs_top + sib.height)))
    return editors


def target_editor(draw_state, slack=24.0):
    """The editor a file opens in (its draw_state): the one whose right edge
    meets this tile's left edge (within `slack`: the divider, the grips) over
    a shared span of rows — the nearest, then the longest shared span — else the
    editor closest to this tile, box to box. None without an editor tile."""
    editors = sibling_editors(draw_state)
    if not editors:
        return None
    x0, y0 = draw_state.abs_left, draw_state.abs_top
    x1, y1 = x0 + (draw_state.width or 0), y0 + (draw_state.height or 0)
    left = []
    for editor, (ex0, ey0, ex1, ey1) in editors:
        shared = min(y1, ey1) - max(y0, ey0)
        if ex1 <= x0 + slack and shared > 0:
            left.append((x0 - ex1, -shared, editor))
    if left:
        return min(left, key=lambda item: item[:2])[2]

    def distance(box):
        ex0, ey0, ex1, ey1 = box
        return math.hypot(max(0.0, ex0 - x1, x0 - ex1), max(0.0, ey0 - y1, y0 - ey1))
    return min(editors, key=lambda item: distance(item[1]))[0]


# ── the rows ────────────────────────────────────────────────────────────────

def tree_roots(open_files):
    """The roots: the marked projects, then the (unmarked) projects of the
    open tabs, in tab order — except one inside a root already shown (a
    template's pyproject.toml makes its folder a project of its own)."""
    roots = list(project_roots())
    if isinstance(open_files, OpenFiles):
        for path in open_files.open_paths:
            if not isinstance(path, str) or path.startswith(OpenFiles.GIT_DIFF_PREFIX):
                continue
            root = project_for(path)
            if root and not any(root == shown or root.is_relative_to(shown) for shown in roots):
                roots.append(root)
    return roots


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
    """The tree as drawn without a query: [(Path, is_dir, depth)], the open
    folders' rows under them; plus the folders listed (the ones to watch)."""
    rows, shown = [], set()

    def add(directory, depth):
        shown.add(str(directory))
        for path, is_dir in folder_rows(state, directory, meta, show_hidden):
            rows.append((path, is_dir, depth))
            if is_dir and str(path) in state.expanded:
                add(path, depth + 1)

    for root in roots:
        rows.append((root, True, 0))
        if str(root) in state.expanded:
            add(root, 1)
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


@render_func(multi_instance=True, tint=(0.32, 0.42, 0.54), icon=f"\uf07c", display_name="Files",
             selectable=False, disable_scroll=False, show_add_delete=False, is_tree=False,
             show_bg=False, shadow=False, show_header=False)
def draw_project_tree(input_value: object, draw_state, tree_state: ProjectTreeState = None,
                      file_metadata=None, left_mouse_down=False, left_mouse_double_clicked=False,
                      row_height=20.0, left_pad=6.0, indent=12.0, glyph_width=18.0,
                      chevron_width=13.0, default_tint=(0.32, 0.42, 0.54, 1.0),
                      select_boost=0.22, plain_select_boost=0.06, select_shadow=2.0,
                      select_rounding=3.0, hover_boost=0.06, hover_alpha=0.05,
                      text_mix=0.5, icon_mix=0.9, search_tint=(1.0, 0.82, 0.3), search_dim=0.45,
                      search_flash_frames=36, **kwargs):
    """The projects' files as collapsible trees (see the module docstring).
    `input_value` is the tile's: the app's ``OpenFiles``, where a picked file
    opens; it is returned unchanged. The look parameters are
    `draw_file_listing`'s; `indent` is one nesting level, `chevron_width`
    the folder arrow's column before the icon."""
    from meltygui.files.fast_file_explorer import _scroll_row_into_view
    from meltygui.files.fast_file_explorer import claim_keyboard
    from meltygui.files.fast_file_explorer import row_icon
    from meltygui.files.fast_file_explorer import row_tint_bg
    from meltygui.files.fast_file_explorer import search_keys
    from meltygui.files.fast_file_explorer import search_typed
    from meltygui.files.fast_file_explorer import tinted_text
    from meltygui.models.file_meta import FileMeta
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.core.cache.tile_cache import add_shadow
    from meltygui.core.cache.tile_cache import clear_glows

    # [tint=(0.55, 0.72, 0.95)]
    folder_icon = f"\uf07b"
    # [tint=(0.55, 0.72, 0.95)]
    file_icon = f"\uf15b"
    text_rgba = (0.92, 0.92, 0.92, 1.0)
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
    meta = file_metadata
    row_bg = row_tint_bg()

    roots = tree_roots(open_files)
    if not roots:
        imgui.text_wrapped("No project yet: Projects → Add Folder…, or open a file.")
        return False, input_value
    for root in roots:
        if str(root) not in state.known_roots:       # a new root starts open
            state.known_roots.append(str(root))
            state.expanded.add(str(root))

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
        """A file: open it in the editor to the left (and leave the search,
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
            editor = target_editor(draw_state)
            instance = (open_files.active_instance if editor is None
                        else editor._kwargs["instance"])
            state._error = open_path(open_files, path, instance)
            if state._error is None:
                # The jump is adopted INSIDE the editor's body: force it past
                # its blit cache (open_in_editor's wake).
                if editor is not None and Melty.cache is not None and editor._tile_id is not None:
                    Melty.cache.invalidate_up(editor._tile_id, force=True, max_depth=4)
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
    text_y_pad = (row_h - imgui.get_font_size()) * 0.5
    for i, (path, is_dir, depth) in enumerate(rows):
        ry0 = rows_y + i * row_h
        ry1 = ry0 + row_h
        if clip is not None and (ry1 < clip[1] or ry0 > clip[3]):
            continue
        entry = meta.get(str(path)) if meta is not None else None
        tint = FileMeta.painted_tint(entry)
        icon = row_icon(path, is_dir, entry, folder_icon, file_icon)
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
        draw_list.add_text(x, ry0 + text_y_pad, icon_col, icon)
        name_x = x + glyph_w
        name = path.name or str(path)
        if spans:
            for start, end in spans:
                sx0 = name_x + imgui.calc_text_size(name[:start]).x
                sx1 = sx0 + imgui.calc_text_size(name[start:end]).x
                draw_list.add_rect_filled(sx0 - px(1), ry0 + px(2), sx1 + px(1), ry1 - px(2),
                                          search_wash, rounding=px(2))
        draw_list.add_text(name_x, ry0 + text_y_pad, name_col, name)
        if not depth:
            # A root says where it lives, after its name.
            where = str(path.parent).replace(str(Path.home()), "~", 1)
            draw_list.add_text(name_x + imgui.calc_text_size(name).x + px(8), ry0 + text_y_pad,
                               dim_col, where)

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
