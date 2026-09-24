"""One file/version per tile; siblings contribute only text and pane geometry.

The old Code Editor remains a separate renderer. Everything specific to this
tile lives here; GitProxy owns file values, loading and watching.
"""
import threading
from pathlib import Path

from meltygui import imgui, draw_text, render_func, DrawState
from meltygui.view.dropdown_view import fast_draw_dropdown
from project_tree import draw_project_tree, project_selection
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save
from meltygui.core.runtime.lifecycle import module_is_live
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.editor.diff import diff_opcodes
from meltygui_pro.models.tab_bar import TabBarState
from meltygui_pro.editor.code_editor import prepare_editor_tabs, _draw_tab_bar_rows, draw_editor_tab_icon
from meltygui_pro.models.open_files import OpenFiles


@no_save("version_ready", "_file", "_pane", "_text", "_line_numbers", "_comparison", "_diff_folds", "_tab_overlay")
class FileEditorState(DictConversion):
    def __init__(self):
        super().__init__()
        self.selected_path = None
        self.version = "current"
        self.version_ready = 0
        self._file = None
        self._pane = None
        self._text = None
        self._line_numbers = None
        self._comparison = None
        self._diff_folds = None
        self._tab_overlay = None

    def changed(self):
        self.version_ready += 1
        request_render()

    def bind(self, value):
        if self._file is value:
            return
        self.close()
        self._file = value
        if value is not None:
            value.subscribe(self.changed)

    def close(self):
        # A live state may still own the reader from before this refactor.
        # Retire its subscription when rebinding; the removed class is not kept.
        retired = vars(self).pop("_reader", None)
        if retired is not None:
            retired.close()
        if self._file is not None:
            self._file.unsubscribe(self.changed)
            self._file = None
        self._pane = self._text = None


def file_editor_endpoints(parent):
    """The repeated local shape: sibling renderer + injected state + tile ID."""
    if parent is None:
        return {}
    endpoints = {}
    for child in parent._view_children.values():
        if child is None or child._parent is not parent:
            continue
        if getattr(child._view_func, "__name__", None) != "draw_file_editor":
            continue
        state = child.misc.get("file_editor_state")
        instance = (child._kwargs or {}).get("instance")
        if state is not None and instance is not None:
            endpoints[instance] = child, state
    return endpoints


def adopt_selection(files, state, instance, project_source=None):
    link = project_source
    paths = files.paths_in(link.selected_project) if link is not None else files.open_paths
    paths = [path for path in paths if not path.startswith(OpenFiles.GIT_DIFF_PREFIX)]
    target = files.jump_to_instance or files.active_instance
    if target == instance and files.jump_to_path in files.open_paths:
        path = files.jump_to_path
        if not path.startswith(OpenFiles.GIT_DIFF_PREFIX):
            if link is not None and path not in paths:
                from meltygui_pro.models.projects import project_for
                link.set_project(str(project_for(path) or Path(path).parent))
                paths = [p for p in files.paths_in(link.selected_project)
                         if not p.startswith(OpenFiles.GIT_DIFF_PREFIX)]
            state.selected_path = path
            state.version = "current"
    if state.selected_path not in paths:
        state.selected_path = next(iter(paths), None)
    return paths


def version_value(state):
    """Presentation of the selected proxy, with no file-loading machinery."""
    ready = state.version_ready
    options = {"Current": "current", "Filesystem": "filesystem", "HEAD": "HEAD"}
    value = state._file
    if value is not None and str(value.repo.root / value.path) == state.selected_path:
        options.update(value.versions)
    if state.version not in options.values():
        options[state.version[:12]] = state.version
    if (value is None or str(value.repo.root / value.path) != state.selected_path
            or value.version != state.version):
        return options, None, "Loading version…"
    text = value.get("value")
    if value.error:
        return options, None, value.error
    if text is None:
        return options, None, "Loading version…" if value.loading else "File is absent or unavailable in this version."
    return options, text, None


def cleanup_file_editor(draw_state):
    from meltygui_pro.editor.git import forget_consumer
    forget_consumer(draw_state)
    state = draw_state.misc.get("file_editor_state")
    if state is not None:
        state._tab_overlay = None
        state.close()


def draw_file_editor_overlay(draw_state, draw_list):
    """Paint prepared tabs at live bounds; all input stays in the normal body."""
    from meltygui.core.melty import Melty
    from meltygui.hdr_color import pack_color
    from meltygui.view.header_view import flat_button

    state = draw_state.misc.get("file_editor_state")
    if state is None or state._tab_overlay is None:
        return
    tabs, layout_tabs, button_height, row_height, swatch_width, background = state._tab_overlay
    height = layout_tabs(draw_state.width) if tabs else 0
    left, top = draw_state.abs_left, draw_state.abs_top + draw_state.height - height
    if state._pane is not None:
        from meltygui.core.rendering.overlay import place_overlay_view
        place_overlay_view(state._pane,
                           (left, draw_state.abs_top + 30, draw_state.width,
                            max(1, draw_state.height - height - 30)),
                           draw_state.abs_clip_rect)
        if getattr(draw_state, "_blit_served_frame", None) == Melty.frame_count:
            from meltygui.core.rendering.overlay import paint_cached_view
            paint_cached_view(state._pane, draw_list)
    if not tabs:
        return
    # Retain the body's resolved, tint-aware background when tabs cover frozen text.
    draw_list.add_rect_filled(left, top, left + draw_state.width, top + height,
                              pack_color(*background[:3], 1.0))
    mouse_x, mouse_y = imgui.get_mouse_pos()
    icon_size, close_size = Melty.px(18), Melty.px(16)
    for tab in tabs:
        style = tab.get("paint_style")
        if style is None:  # The dragged tab is painted by its floating ghost.
            continue
        x, y = left + tab["x"], top + tab["row"] * row_height
        hovered = (draw_state._bounding_hovered and not Melty.on_drag
                   and x <= mouse_x < x + tab["w"] and y <= mouse_y < y + button_height)
        draw_list.push_clip_rect(x, y, x + tab["w"], y + button_height, True)
        try:
            flat_button(tab["label"], draw_state, view_id=None,
                        width=tab["w"], height=button_height, pos=(x, y),
                        hovered=hovered, layout=False, draw_list=draw_list, **style)
            icon_x, icon_y = x + 2, y + (button_height - 18) * 0.5
            if hovered and icon_x <= mouse_x < icon_x + icon_size and icon_y <= mouse_y < icon_y + icon_size:
                draw_list.add_rect_filled(icon_x - 2, icon_y - 2,
                                          icon_x + icon_size + 2, icon_y + icon_size + 2,
                                          pack_color(1.0, 1.0, 1.0, 0.10), rounding=4.0)
            draw_editor_tab_icon(tab, draw_list, icon_x, icon_y,
                                 icon_size, style["text_color"])
            if hovered:
                close_x, close_y = x + tab["w"] - swatch_width, y + (button_height - close_size) * 0.5
                flat_button("×", draw_state, view_id=None, pos=(close_x, close_y),
                            width=close_size, height=close_size, color=style["color"],
                            factor=0.2, text_saturation=0.4, alpha=0.0, layout=False,
                            hovered=close_x <= mouse_x < close_x + close_size and close_y <= mouse_y < close_y + close_size,
                            draw_list=draw_list)
        finally:
            draw_list.pop_clip_rect()


@render_func(multi_instance=True, use_cache=True, disable_scroll=True,
             on_cleanup=cleanup_file_editor, draw_overlay=draw_file_editor_overlay,
             display_name="File Editor", icon=f"\uf15c", tint=(0.20, 0.30, 0.48))
def draw_file_editor(input_value: OpenFiles, draw_state=None,
                     diff_with: "DrawState[draw_file_editor]" = None, instance=0,
                     layout_frame=None, tab_bar_state: TabBarState = None,
                     files_view: DrawState[draw_project_tree] = None):
    from meltygui.core.rendering.injected_state import owned_state
    state = owned_state(draw_state, "file_editor_state", FileEditorState)
    files = input_value
    if not isinstance(files, OpenFiles):
        state._tab_overlay = None
        return False, input_value
    from meltygui_pro.editor.git import note_consumer
    note_consumer(draw_state)
    project_source = project_selection(files_view)
    paths = adopt_selection(files, state, instance, project_source)
    left, top = draw_state.abs_left, draw_state.abs_top
    width, height = draw_state.width, draw_state.height
    (tabs, tab_button_height, tab_row_height, swatch_width,
     layout_tabs, hold_close) = prepare_editor_tabs(
        paths, paths, [Path(path).name for path in paths], draw_state, tab_bar_state)
    tab_height = layout_tabs(width) if paths else 0
    changed = False
    if state.selected_path not in files.open_paths:
        state.selected_path = next(iter(p for p in paths if p in files.open_paths), None)
    path = state.selected_path
    options, text, status = version_value(state)
    version_label = next((label for label, value in options.items() if value == state.version), state.version)
    # Keep these controls anchored to content; frozen resize need not repaint them.
    version_width = min(max(1, width - (94 if state._comparison is not None else 0)),
                        imgui.calc_text_size(f"\uf078 {version_label}").x + 60)
    imgui.set_cursor_screen_pos((left, top))
    picked, version = fast_draw_dropdown(
        state.version, collection=options, name="Version", show_name=False,
        display_label=version_label,
        width=version_width, height=28, show_header=False)
    if picked:
        state.version = version
        options, text, status = version_value(state)
        changed = True
    if state._comparison is not None:
        from meltygui_pro.editor.comparison import draw_comparison_controls
        imgui.set_cursor_screen_pos((left + version_width + 8, top))
        draw_comparison_controls(draw_state, state._comparison)
    # One codec-selected view, including loading/empty/error states. Its identity
    # includes the version, so historic cursors/folds never replace the working ones.
    imgui.set_cursor_screen_pos((left, top + 30))
    file_value = state._file if text is not None else None
    editable = file_value is not None and file_value.writable
    is_text = isinstance(text, str)
    displayed = text if text is not None else (status if path else "Open a file from File → Open or a Files tile.")
    if is_text and (state._line_numbers is None or state._line_numbers[0] is not text):
        from meltygui.editor.text_editor import _line_starts
        state._line_numbers = text, range(1, len(_line_starts(text)) + 1)
    context_menu = {}
    if editable and is_text and Path(path).suffix.lower() == ".py":
        from tasks import request_module_run
        link = project_source
        root = link.selected_project if link is not None else None
        context_menu["Run"] = lambda path=path, root=root: request_module_run(path, root)
    from meltygui.code.new_converters import _codec_view
    view = _codec_view(file_value.codec if file_value is not None else None, displayed, draw_text)
    edited, replacement, pane = view(
        displayed, name=f"file:{path}:{state.version}",
        file_key=file_value.display_path if file_value is not None else path,
        source_context=file_value, context_menu=context_menu,
        line_numbers=state._line_numbers[1] if is_text else None,
        width=width, height=max(1, height - tab_height - 30), return_extras=True,
        editable=editable, syntax_highlight=is_text,
        syntax_language="python" if path and path.endswith(".py") else "text",
        show_header=False, show_file_header=False, gutter_indent=True, freeze_resize=True,
        roster_live_hold=editable and state.version == "current", autocomplete=editable, shadow=False,
        diff_fold_ranges=state._diff_folds if is_text else None,
        expand_diff=state._comparison.expand_diff if state._comparison is not None else None,
        use_cache=True)
    if edited and editable:
        file_value["value"] = replacement
        text = replacement
        changed = True
    state._pane, state._text = pane, text if isinstance(text, str) else None
    if draw_state._bounding_hovered:
        files.active_instance = instance
    if files.active_instance == instance:
        files.active_path = path
    if path is not None and files.jump_to_path == path and (
            files.jump_to_instance or files.active_instance) == instance and file_value is not None:
        if files.jump_to_line is not None and is_text:
            from meltygui.editor.text_editor import _line_starts
            starts = _line_starts(text)
            line = max(0, min(files.jump_to_line - 1, len(starts) - 1))
            position = starts[line]
            if files.jump_to_token:
                end = starts[line + 1] if line + 1 < len(starts) else len(text)
                found = text.find(files.jump_to_token, position, end)
                if found >= 0:
                    position = found
            pane.text_cursor_pos = pane.text_selection_start = pane.text_selection_end = position
            pane.scroll_offset = (pane.scroll_offset[0], max(0, line * pane._diff_line_px - pane.height * 0.4))
        files.jump_to_path = files.jump_to_line = files.jump_to_token = None
    def select_tab(selected_path):
        nonlocal changed
        state.selected_path = selected_path
        changed = True

    imgui.set_cursor_screen_pos((left, top + height - tab_height))
    bar_left, bar_top = imgui.get_cursor_pos()
    closed = _draw_tab_bar_rows(
        draw_state, files, tabs, paths, state.selected_path, instance,
        bar_left, bar_top, tab_button_height, tab_row_height, swatch_width,
        on_select=select_tab, paint=False)
    from meltygui.core.melty import Melty
    state._tab_overlay = (tabs, layout_tabs, tab_button_height, tab_row_height,
                          swatch_width, Melty.bg_color_stack[-1])
    if closed is not None:
        hold_close(closed)
        if state.selected_path == closed:
            remaining = [other for other in paths if other != closed]
            state.selected_path = (remaining[min(paths.index(closed), len(remaining) - 1)]
                                   if remaining else None)
        files.close_file(closed)
        draw_state.invalidate()
        request_render()
        changed = True
    # Dispatch only after rendering; the next render consumes the completed value.
    if path is not None:
        from meltygui_pro.editor.git import proxies_for
        state.bind(proxies_for(path)[0].file(path, state.version))
    else:
        state.bind(None)
    return changed, input_value


@no_save("ready", "_endpoints", "_requests", "_results", "_thread", "_closed", "_paint", "_paint_commands")
class FileEditorComparisons(DictConversion):
    """The workspace's local pairs; neither editor owns its sibling's state."""

    def __init__(self):
        super().__init__()
        self.ready = 0
        self.interactions = {}
        self._endpoints = {}
        self._requests = {}
        self._results = {}
        self._thread = None
        self._closed = False
        self._paint = ()
        self._paint_commands = None

    def reconcile(self, endpoints):
        for key, (_view, state) in self._endpoints.items():
            if key not in endpoints:
                state.close()
        self._endpoints = endpoints

    def close(self):
        self._closed = True
        for _view, state in self._endpoints.values():
            state.close()
        self._endpoints = self._requests = self._results = {}
        self._paint = ()
        self._paint_commands = None

    def invalidate_panes(self):
        """Changed overlays must replace, rather than stack on, baked washes."""
        from meltygui_pro.editor.git import _flag_external_change
        for view, state in tuple(self._endpoints.values()):
            if state._pane is not None:
                _flag_external_change(state._pane)
                state._pane.invalidate_up(max_depth=6)
            if view is not None:
                _flag_external_change(view)
                view.invalidate_up(max_depth=6)
        request_render()

    def dispatch(self, requests):
        if self._closed:
            return
        before = {key: value[0] for key, value in self._requests.items()}
        after = {key: value[0] for key, value in requests.items()}
        if before != after:
            self.invalidate_panes()
        self._requests = requests
        if self._thread is not None and self._thread.is_alive():
            return
        if all(key in self._results and self._results[key][0] == token
               for key, (token, _a, _b) in requests.items()):
            return
        self._thread = threading.Thread(target=self.compute, args=(requests,),
                                        daemon=True, name="file-editor-diffs")
        self._thread.start()

    def compute(self, requests):
        results = {}
        for pair, (token, a, b) in requests.items():
            if self._closed:
                return
            held = self._results.get(pair)
            blocks = held[1] if held is not None and held[0] == token else diff_opcodes(a.split("\n"), b.split("\n"))
            results[pair] = token, blocks, a, b  # Keep identities alive until replacement.
        if self._closed:
            return
        self._results = results
        self.ready += 1
        self.invalidate_panes()


def comparison_pairs(endpoints):
    """Compare only injected view references; each editor retains its own state."""
    by_view = {id(view): key for key, (view, _state) in endpoints.items()}
    pairs = set()
    for key, (view, _state) in endpoints.items():
        target = (view._kwargs or {}).get('diff_with') if view is not None else None
        other = by_view.get(id(target)) if target is not None else None
        if other is not None and other != key:
            pairs.add(tuple(sorted((key, other))))
    return sorted(pairs)


def migrate_comparison_link(tile):
    """Translate an old saved comparison once; discard whole-editor sharing."""
    from meltygui.core.layout.tile_links import prepare_endpoint
    from meltygui.state.view_reference import view_identifier
    endpoint = prepare_endpoint(tile)
    state = endpoint.draw_state.misc.get('file_editor_state')
    key = view_identifier(draw_file_editor)
    bindings = dict(tile.links.get(key, {}))
    changed = 'file_editor_state' in bindings
    bindings.pop('file_editor_state', None)
    if state is not None:
        old_target = vars(state).pop('sibling_tile_id', None)
        vars(state).pop('siblings', None)
        if old_target is not None and 'diff_with' not in bindings:
            bindings['diff_with'] = (old_target, key, None)
            changed = True
    if changed:
        tile.links = {**tile.links, key: bindings}


def _draw_ribbons(draw_state, pane_a, pane_b, blocks, draw_list=None):
    """Use the established full-line washes and their connected seam ribbon."""
    from meltygui_pro.editor.code_editor import _draw_compare_ribbons, _pane_pos
    if _pane_pos(pane_a)[0] > _pane_pos(pane_b)[0]:
        pane_a, pane_b = pane_b, pane_a
        reverse = {"insert": "delete", "delete": "insert", "replace": "replace"}
        blocks = [(reverse[tag], j0, j1, i0, i1) for tag, i0, i1, j0, j1 in blocks]
    bands = [(i0, i1, j0, j1, tag) for tag, i0, i1, j0, j1 in blocks]
    _draw_compare_ribbons(draw_state, pane_a, pane_b, bands,
                          label_left="", label_right="", draw_list=draw_list)


def draw_file_editor_comparisons(draw_state, state, live_ids):
    endpoints = {key: value for key, value in file_editor_endpoints(draw_state).items()
                 if key in live_ids}
    state.reconcile(endpoints)
    ready = state.ready  # Track async diff completion in the containing view.
    from meltygui_pro.editor.comparison import ComparisonState, synchronize_comparison
    from meltygui_pro.editor.code_editor import _diff_gap_folds
    from meltygui.core.runtime.toggles import Toggles
    requests = {}
    presentations = {}
    paint = []
    for pair in comparison_pairs(endpoints):
        a, b = (endpoints[key][1] for key in pair)
        if a._text is None or b._text is None or a._pane is None or b._pane is None:
            continue
        token = (a.selected_path, a.version, id(a._text), b.selected_path, b.version, id(b._text))
        requests[pair] = token, a._text, b._text
        result = state._results.get(pair)
        if result is not None and result[0] == token:
            interaction = state.interactions.get(pair)
            if interaction is None:
                interaction = state.interactions[pair] = ComparisonState()
            bands = [(i0, i1, j0, j1, tag) for tag, i0, i1, j0, j1 in result[1]]
            synchronize_comparison(interaction, a._pane, b._pane, bands,
                                   a._text, b._text, (pair, token[:2], token[3:5]))
            fold_settings = (Toggles.CodeEditor.diff_fold_context,
                             Toggles.TextEditor.diff_preview_lines_below,
                             Toggles.TextEditor.diff_preview_lines_above)
            cached = interaction.misc.get('fold_ranges')
            if cached is None or cached[0] is not result or cached[1] != fold_settings:
                ranges = tuple(_diff_gap_folds(
                    [(band[side * 2], band[side * 2 + 1]) for band in bands],
                    text.count("\n") + 1, *fold_settings)
                    for side, text in enumerate((a._text, b._text)))
                cached = result, fold_settings, ranges
                interaction.misc['fold_ranges'] = cached
            for side, key in enumerate(pair):
                folds = cached[2][side]
                # A tile can participate in several pairs; its outgoing link
                # owns the fold layout and toolbar, otherwise the first incoming.
                view = endpoints[key][0]
                outgoing = (view._kwargs or {}).get('diff_with') is endpoints[pair[1-side]][0]
                if key not in presentations or outgoing:
                    presentations[key] = interaction, folds
            if a._pane.abs_left <= b._pane.abs_left:
                paint.append((a._pane, b._pane, bands))
            else:
                reverse = {"insert": "delete", "delete": "insert", "replace": "replace"}
                paint.append((b._pane, a._pane,
                              [(j0, j1, i0, i1, reverse[tag])
                               for i0, i1, j0, j1, tag in bands]))
    for key, (view, editor) in endpoints.items():
        interaction, folds = presentations.get(key, (None, None))
        if editor._comparison is not interaction or editor._diff_folds != folds:
            editor._comparison, editor._diff_folds = interaction, folds
            view.invalidate_up(max_depth=6)
            request_render()
    for pair, interaction in state.interactions.items():
        if pair not in requests:
            interaction.misc.clear()  # Retain preferences, release old text/layouts.
    state._results = {key: value for key, value in state._results.items() if key in requests}
    state._paint = tuple(paint)
    state._paint_commands = prepare_comparison_overlay(draw_state, paint)
    state.dispatch(requests)


def prepare_comparison_overlay(draw_state, paint):
    """Resolve ribbons and shadow marks in the body; retain only paint commands.

    Cached root movement translates these commands; resize and scrolling run
    the body after child placement and prepare fresh geometry.
    """
    from types import SimpleNamespace
    from meltygui_pro.editor.code_editor import _draw_compare_ribbons
    commands = []
    recorder = SimpleNamespace(flags=imgui.get_overlay_draw_list().flags)
    def record(method):
        def append(*args, **kwargs):
            commands.append((recorder.flags, method, args, kwargs))
        return append
    for method in ('add_rect_filled', 'add_triangle_filled', 'add_polyline'):
        setattr(recorder, method, record(method))
    for pane_a, pane_b, bands in paint:
        _draw_compare_ribbons(draw_state, pane_a, pane_b, bands,
                              label_left="", label_right="", draw_list=recorder)
    return (draw_state.abs_left, draw_state.abs_top), tuple(commands)


def draw_file_editor_comparison_overlay(draw_state, draw_list):
    """Submit prepared ribbon primitives; no diff/layout/shadow work here."""
    state = draw_state.misc.get("file_comparisons")
    if state is None or state._paint_commands is None:
        return
    origin, commands = state._paint_commands
    dx, dy = draw_state.abs_left - origin[0], draw_state.abs_top - origin[1]
    original_flags = flags = draw_list.flags
    try:
        for next_flags, method, args, kwargs in commands:
            if flags != next_flags:
                draw_list.flags = flags = next_flags
            if dx or dy:
                if method == 'add_polyline':
                    args = ([(x + dx, y + dy) for x, y in args[0]], *args[1:])
                else:
                    count = 6 if method == 'add_triangle_filled' else 4
                    args = (*(value + (dx if i % 2 == 0 else dy)
                              for i, value in enumerate(args[:count])), *args[count:])
            getattr(draw_list, method)(*args, **kwargs)
    finally:
        draw_list.flags = original_flags


def cleanup_file_editor_comparisons(draw_state):
    state = draw_state.misc.get("file_comparisons")
    if state is not None:
        state.close()
