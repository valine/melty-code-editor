"""One file/version per tile; siblings contribute only text and pane geometry.

The old Code Editor remains a separate renderer. Everything specific to this
tile lives here; GitProxy owns file values, loading and watching.
"""
import threading
from pathlib import Path

from meltygui import imgui, draw_dropdown, draw_text, render_func
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save
from meltygui.core.runtime.lifecycle import module_is_live
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.editor.diff import diff_opcodes
from meltygui_pro.models.tab_bar import TabBarState
from meltygui_pro.editor.code_editor import prepare_editor_tabs, _draw_tab_bar_rows
from meltygui_pro.models.open_files import OpenFiles


@no_save("version_ready", "siblings", "_file", "_pane", "_text", "_line_numbers")
class FileEditorState(DictConversion):
    def __init__(self):
        super().__init__()
        self.selected_path = None
        self.version = "current"
        self.sibling_tile_id = None
        self.version_ready = 0
        self.siblings = ()
        self._file = None
        self._pane = None
        self._text = None
        self._line_numbers = None

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


def adopt_selection(files, state, instance):
    link = files.project_link(instance)
    paths = files.paths_in(link.folder) if link is not None else files.open_paths
    paths = [path for path in paths if not path.startswith(OpenFiles.GIT_DIFF_PREFIX)]
    target = files.jump_to_instance or files.active_instance
    if target == instance and files.jump_to_path in files.open_paths:
        path = files.jump_to_path
        if not path.startswith(OpenFiles.GIT_DIFF_PREFIX):
            if link is not None and path not in paths:
                from meltygui_pro.models.projects import project_for
                link.select(str(project_for(path) or Path(path).parent))
                paths = [p for p in files.paths_in(link.folder)
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
    if not isinstance(text, str):
        return options, None, "This version contains binary data."
    return options, text, None


def cleanup_file_editor(draw_state):
    from meltygui_pro.editor.git import forget_consumer
    forget_consumer(draw_state)
    state = draw_state.misc.get("file_editor_state")
    if state is not None:
        state.close()


@render_func(multi_instance=True, use_cache=True, disable_scroll=True,
             on_cleanup=cleanup_file_editor,
             display_name="File Editor", icon=f"\uf15c", tint=(0.20, 0.30, 0.48))
def draw_file_editor(input_value: OpenFiles, draw_state=None,
                     file_editor_state: FileEditorState = None, instance=0,
                     layout_frame=None, tab_bar_state: TabBarState = None):
    state, files = file_editor_state, input_value
    if not isinstance(files, OpenFiles):
        return False, input_value
    from meltygui_pro.editor.git import note_consumer
    note_consumer(draw_state)
    paths = adopt_selection(files, state, instance)
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
    imgui.set_cursor_screen_pos((left, top))
    picked, version = draw_dropdown(
        state.version, collection=options, name="Version", show_name=False,
        display_label=next((label for label, value in options.items() if value == state.version), state.version),
        width=max(1, width * 0.5 - 2), height=28, show_header=False)
    if picked:
        state.version = version
        options, text, status = version_value(state)
        changed = True
    siblings = {"No comparison": None}
    for other_id, label in state.siblings:
        siblings[label] = other_id
    if state.sibling_tile_id is not None and state.sibling_tile_id not in siblings.values():
        siblings["Unavailable editor"] = state.sibling_tile_id
    imgui.set_cursor_screen_pos((left + width * 0.5 + 2, top))
    picked, sibling = draw_dropdown(
        state.sibling_tile_id, collection=siblings, name="Compare with", show_name=False,
        display_label=next(label for label, value in siblings.items() if value == state.sibling_tile_id),
        width=max(1, width * 0.5 - 2), height=28, show_header=False)
    if picked:
        state.sibling_tile_id = sibling
        changed = True
    # Exactly one text view, including loading/empty/error states. Its identity
    # includes the version, so historic cursors/folds never replace the working ones.
    imgui.set_cursor_screen_pos((left, top + 30))
    editable = path is not None and state.version in ("current", "filesystem") and text is not None
    file_value = state._file if text is not None else None
    displayed = text if text is not None else (status if path else "Open a file from File → Open or a Files tile.")
    if text is not None and (state._line_numbers is None or state._line_numbers[0] is not text):
        from meltygui.editor.text_editor import _line_starts
        state._line_numbers = text, range(1, len(_line_starts(text)) + 1)
    edited, replacement, pane = draw_text(
        displayed, name=f"file:{path}:{state.version}",
        file_key=file_value.display_path if file_value is not None else path,
        source_context=file_value,
        line_numbers=state._line_numbers[1] if text is not None else None,
        width=width, height=max(1, height - tab_height - 30), return_extras=True,
        editable=editable, syntax_highlight=text is not None,
        syntax_language="python" if path and path.endswith(".py") else "text",
        show_header=False, show_file_header=False, gutter_indent=True,
        roster_live_hold=editable and state.version == "current", autocomplete=editable, shadow=False,
        use_cache=True)
    if edited and editable and replacement != text:
        file_value["value"] = replacement
        text = replacement
        changed = True
    state._pane, state._text = pane, text
    if draw_state._bounding_hovered:
        files.active_instance = instance
    if files.active_instance == instance:
        files.active_path = path
    if path is not None and files.jump_to_path == path and (
            files.jump_to_instance or files.active_instance) == instance and editable:
        if files.jump_to_line is not None:
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
        on_select=select_tab)
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


@no_save("ready", "_endpoints", "_requests", "_results", "_thread", "_closed")
class FileEditorComparisons(DictConversion):
    """The workspace's local pairs; neither editor owns its sibling's state."""

    def __init__(self):
        super().__init__()
        self.ready = 0
        self._endpoints = {}
        self._requests = {}
        self._results = {}
        self._thread = None
        self._closed = False

    def reconcile(self, endpoints):
        for key, (_view, state) in self._endpoints.items():
            if key not in endpoints:
                state.close()
        self._endpoints = endpoints
        for key, (_view, state) in endpoints.items():
            state.siblings = tuple(
                (other_id, f"{Path(other.selected_path).name if other.selected_path else 'Empty'}"
                 f" · {other.version[:12]} · {other_id}")
                for other_id, (_other_view, other) in endpoints.items() if other_id != key)

    def close(self):
        self._closed = True
        for _view, state in self._endpoints.values():
            state.close()
        self._endpoints = self._requests = self._results = {}

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
    pairs = set()
    for key, (_view, state) in endpoints.items():
        sibling = state.sibling_tile_id
        if sibling != key and sibling in endpoints:
            pairs.add(tuple(sorted((key, sibling))))
    return sorted(pairs)


def _draw_ribbons(draw_state, pane_a, pane_b, blocks):
    """Use the established full-line washes and their connected seam ribbon."""
    from meltygui_pro.editor.code_editor import _draw_compare_ribbons, _pane_pos
    if _pane_pos(pane_a)[0] > _pane_pos(pane_b)[0]:
        pane_a, pane_b = pane_b, pane_a
        reverse = {"insert": "delete", "delete": "insert", "replace": "replace"}
        blocks = [(reverse[tag], j0, j1, i0, i1) for tag, i0, i1, j0, j1 in blocks]
    bands = [(i0, i1, j0, j1, tag) for tag, i0, i1, j0, j1 in blocks]
    _draw_compare_ribbons(draw_state, pane_a, pane_b, bands,
                          label_left="", label_right="")


def draw_file_editor_comparisons(draw_state, state, live_ids):
    endpoints = {key: value for key, value in file_editor_endpoints(draw_state).items()
                 if key in live_ids}
    state.reconcile(endpoints)
    ready = state.ready  # Track async diff completion in the containing view.
    requests = {}
    for pair in comparison_pairs(endpoints):
        a, b = (endpoints[key][1] for key in pair)
        if a._text is None or b._text is None or a._pane is None or b._pane is None:
            continue
        token = (a.selected_path, a.version, id(a._text), b.selected_path, b.version, id(b._text))
        requests[pair] = token, a._text, b._text
        result = state._results.get(pair)
        if result is not None and result[0] == token:
            _draw_ribbons(draw_state, a._pane, b._pane, result[1])
    state._results = {key: value for key, value in state._results.items() if key in requests}
    state.dispatch(requests)


def cleanup_file_editor_comparisons(draw_state):
    state = draw_state.misc.get("file_comparisons")
    if state is not None:
        state.close()
