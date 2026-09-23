"""One file/version per tile; siblings contribute only text and pane geometry.

The old Code Editor remains a separate renderer. Everything specific to this
tile lives here, including its short-lived Git reader and comparison scope.
"""
import threading
from pathlib import Path

from meltygui import imgui, draw_dropdown, draw_text, render_func
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save
from meltygui.core.runtime.lifecycle import module_is_live
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.editor.diff import diff_opcodes, _diff_disp_span
from meltygui.hdr_color import pack_color
from meltygui.view.header_view import flat_button
from meltygui_pro.models.open_files import OpenFiles


class FileVersions:
    """Selection adapter over the shared GitProxy's version-qualified values."""

    def __init__(self, path):
        from meltygui_pro.editor.git import proxies_for
        self.path = path
        self.repo = proxies_for(path)[0]

    def read(self, version):
        history = self.repo.file_history[self.path]
        options = {"Current": "current", "Filesystem": "filesystem", "HEAD": "HEAD"}
        options.update({meta.label: key for key, meta in history.log.items()})
        source = self.repo.files(version)
        value = source.get(self.path)
        if value is None:
            return options, None, "File is absent or unavailable in this version.", source
        if not isinstance(value, str):
            return options, None, "This version contains binary data.", source
        return options, value, None, source


class VersionReader:
    """Finite jobs on selection/watch events; the shared proxy owns watching."""

    def __init__(self, owner):
        self.owner = owner
        self.request = None
        self.result = None
        self._repo = None
        self._thread = None
        self._lock = threading.Lock()
        self._closed = False

    def changed(self, kind, path):
        # Pending edits don't change filesystem/commit text, but a filesystem
        # event can change the available history or the selected moving ref.
        if kind == "current" and self.owner.version != "current":
            return
        self.owner.source_revision += 1
        request_render()

    def select(self, path, version):
        request = path, version, self.owner.source_revision
        with self._lock:
            if self._closed or request == self.request:
                return
            self.request = request
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self.run, daemon=True,
                                            name="file-editor-version-read")
            self._thread.start()

    def run(self):
        served = None
        while module_is_live(globals()):
            with self._lock:
                request = self.request
                if self._closed or request == served:
                    self._thread = None
                    return
            path, version, _revision = request
            try:
                model = FileVersions(path)
                with self._lock:
                    if self._closed:
                        self._thread = None
                        return
                    if self._repo is not None:
                        self._repo.unsubscribe(self.changed)
                    self._repo = model.repo
                    self._repo.subscribe(self.changed, paths=(path,))
                options, text, error, source = model.read(version)
            except Exception as caught:
                options, text, error, source = {}, None, str(caught), None
            with self._lock:
                if not self._closed and request == self.request:
                    self.result = (path, version), options, text, error, source
                    self.owner.version_ready += 1
                    request_render()
                served = request

    def write(self, path, text):
        source = self.result[4]
        source[path] = text
        if source.writable:
            self.result = self.result[:2] + (text, None, source)

    def close(self):
        with self._lock:
            self._closed = True
            if self._repo is not None:
                self._repo.unsubscribe(self.changed)
                self._repo = None


@no_save("version_ready", "source_revision", "siblings", "_reader", "_pane", "_text", "_line_numbers")
class FileEditorState(DictConversion):
    def __init__(self):
        super().__init__()
        self.selected_path = None
        self.version = "current"
        self.sibling_tile_id = None
        self.version_ready = 0
        self.source_revision = 0
        self.siblings = ()
        self._reader = None
        self._pane = None
        self._text = None
        self._line_numbers = None

    def close(self):
        if self._reader is not None:
            self._reader.close()
            self._reader = None
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
    """Consume only the result for the selected source; retain no hidden host."""
    ready = state.version_ready
    revision = state.source_revision
    options = {"Current": "current", "Filesystem": "filesystem", "HEAD": "HEAD"}
    result = state._reader.result if state._reader is not None else None
    if result is not None and result[0][0] == state.selected_path:
        options.update(result[1])
    if state.version not in options.values():
        options[state.version[:12]] = state.version
    if result is not None and result[0] == (state.selected_path, state.version):
        return options, result[2], result[3]
    return options, None, "Loading version…"


def _tabs(draw_state, files, state, paths, left, top, width):
    """Wrapped file tabs; changing/closing a tab only selects this editor."""
    row_height, x, y = 26.0, 0.0, 0.0
    changed = False
    for path in paths:
        label = Path(path).name
        tab_width = min(width, max(90.0, imgui.calc_text_size(label)[0] + 38.0))
        if x and x + tab_width > width:
            x, y = 0.0, y + row_height
        imgui.set_cursor_screen_pos((left + x, top + y))
        if flat_button(label, draw_state, f"file-tab:{path}", width=max(1, tab_width - 22),
                       height=row_height, alpha=1.0 if state.selected_path == path else 0.0,
                       color=(0.20, 0.30, 0.48), layout=False):
            state.selected_path = path
            changed = True
        imgui.set_cursor_screen_pos((left + x + tab_width - 22, top + y))
        if flat_button(f"\uf00d", draw_state, f"close-file:{path}", width=22,
                       height=row_height, alpha=0.0, layout=False):
            files.close_file(path)
            changed = True
        x += tab_width + 3.0
    return changed, y + row_height if paths else 0.0


def cleanup_file_editor(draw_state):
    state = draw_state.misc.get("file_editor_state")
    if state is not None:
        state.close()


@render_func(multi_instance=True, use_cache=True, disable_scroll=True,
             on_cleanup=cleanup_file_editor,
             display_name="File Editor", icon=f"\uf15c", tint=(0.20, 0.30, 0.48))
def draw_file_editor(input_value: OpenFiles, draw_state=None,
                     file_editor_state: FileEditorState = None, instance=0,
                     layout_frame=None):
    state, files = file_editor_state, input_value
    if not isinstance(files, OpenFiles):
        return False, input_value
    paths = adopt_selection(files, state, instance)
    left, top = draw_state.abs_left, draw_state.abs_top
    width, height = draw_state.width, draw_state.height
    changed, tab_height = _tabs(draw_state, files, state, paths, left, top, width)
    if state.selected_path not in files.open_paths:
        state.selected_path = next(iter(p for p in paths if p in files.open_paths), None)
    path = state.selected_path
    options, text, status = version_value(state)
    imgui.set_cursor_screen_pos((left, top + tab_height))
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
    imgui.set_cursor_screen_pos((left + width * 0.5 + 2, top + tab_height))
    picked, sibling = draw_dropdown(
        state.sibling_tile_id, collection=siblings, name="Compare with", show_name=False,
        display_label=next(label for label, value in siblings.items() if value == state.sibling_tile_id),
        width=max(1, width * 0.5 - 2), height=28, show_header=False)
    if picked:
        state.sibling_tile_id = sibling
        changed = True
    # Exactly one text view, including loading/empty/error states. Its identity
    # includes the version, so historic cursors/folds never replace the working ones.
    imgui.set_cursor_screen_pos((left, top + tab_height + 30))
    editable = path is not None and state.version in ("current", "filesystem") and text is not None
    source = (state._reader.result[4] if text is not None and state._reader is not None else None)
    file_value = source.file(path) if source is not None else None
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
        state._reader.write(path, replacement)
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
    # Dispatch only after rendering; the next render consumes the completed value.
    if path is not None:
        if state._reader is None:
            state._reader = VersionReader(state)
        state._reader.select(path, state.version)
    elif state._reader is not None:
        state.close()
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

    def dispatch(self, requests):
        if self._closed:
            return
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
        request_render()


def comparison_pairs(endpoints):
    pairs = set()
    for key, (_view, state) in endpoints.items():
        sibling = state.sibling_tile_id
        if sibling != key and sibling in endpoints:
            pairs.add(tuple(sorted((key, sibling))))
    return sorted(pairs)


def _line_band(pane, start, end):
    if not hasattr(pane, "_diff_top_inset"):
        return None
    d0, d1 = _diff_disp_span(pane, start, end)
    top = pane.abs_top
    clip_top, clip_bottom = pane._diff_clip_off
    lo, hi = top + clip_top, top + pane.height - clip_bottom
    origin = top + pane._diff_top_inset - pane.scroll_offset[1]
    y0 = min(hi, max(lo, origin + d0 * pane._diff_line_px))
    y1 = min(hi, max(lo, origin + d1 * pane._diff_line_px))
    return y0, max(y0 + 2.0, y1)


def _draw_ribbons(draw_state, pane_a, pane_b, blocks):
    """Only paint the space between panes, so pane cache captures stay clean."""
    a_left, b_left = pane_a.abs_left, pane_b.abs_left
    if a_left > b_left:
        pane_a, pane_b = pane_b, pane_a
        reverse = {"insert": "delete", "delete": "insert", "replace": "replace"}
        blocks = [(reverse[tag], j0, j1, i0, i1) for tag, i0, i1, j0, j1 in blocks]
    x0, x1 = pane_a.abs_left + pane_a.width, pane_b.abs_left
    # Stacked panes connect in their shared right margin. The same line bands
    # remain meaningful when there isn't a left-to-right seam.
    stacked = x1 <= x0
    if stacked:
        x0 = max(pane_a.abs_left + pane_a.width, pane_b.abs_left + pane_b.width)
        x1 = min(draw_state.abs_left + draw_state.width, x0 + 5.0)
    if x1 <= x0:
        return
    colors = {"insert": Toggles.CodeEditor.ribbon_insert_tint,
              "delete": Toggles.CodeEditor.ribbon_delete_tint,
              "replace": Toggles.CodeEditor.ribbon_replace_tint}
    draw_list = imgui.get_window_draw_list()
    steps = max(2, int(Toggles.CodeEditor.ribbon_curve_steps))
    for tag, i0, i1, j0, j1 in blocks:
        a, b = _line_band(pane_a, i0, i1), _line_band(pane_b, j0, j1)
        if a is None or b is None:
            continue
        color = colors[tag]
        fill = pack_color(*color[:3], Toggles.CodeEditor.ribbon_fill_alpha)
        flags = draw_list.flags
        # Shared tessellation edges must not get antialiased individually.
        draw_list.flags = flags & ~imgui.DRAW_LIST_ANTI_ALIASED_FILL
        try:
            for step in range(steps):
                t0, t1 = step / steps, (step + 1) / steps
                s0, s1 = t0 * t0 * (3 - 2 * t0), t1 * t1 * (3 - 2 * t1)
                left, right = x0 + (x1 - x0) * t0, x0 + (x1 - x0) * t1
                draw_list.add_quad_filled(left, a[0] + (b[0] - a[0]) * s0,
                                          right, a[0] + (b[0] - a[0]) * s1,
                                          right, a[1] + (b[1] - a[1]) * s1,
                                          left, a[1] + (b[1] - a[1]) * s0, fill)
        finally:
            draw_list.flags = flags


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
