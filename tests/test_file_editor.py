"""Run from the app with `.venv/bin/python -m pytest tests/test_file_editor.py`."""
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from file_editor import (
    FileEditorState, FileEditorComparisons,
    adopt_selection, comparison_pairs, version_value, _draw_ribbons)
from meltygui_pro.models.open_files import OpenFiles
from meltygui_pro.editor.git import proxies_for


@pytest.fixture
def repository(tmp_path):
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git is required")

    def git(*args):
        return subprocess.check_output(
            [executable, "-C", str(tmp_path), "-c", "user.name=Lukas Valine",
             "-c", "user.email=lukas@valine.io", *args], text=True,
            close_fds=False).strip()

    git("init", "-q")
    path = tmp_path / "original.py"
    path.write_text("value = 1\n")
    git("add", ".")
    git("commit", "-qm", "First")
    return tmp_path, path, git


def loaded_file(path, version):
    value = proxies_for(path)[0].file(path, version)
    done = threading.Event()
    value.subscribe(done.set)
    try:
        assert done.wait(5), "GitProxy did not publish its file value"
    finally:
        value.unsubscribe(done.set)
    return value


def test_versions_follow_renames_and_distinguish_disk_head_and_empty(repository):
    root, old, git = repository
    first = git("rev-parse", "HEAD")
    git("mv", old.name, "renamed.py")
    git("commit", "-qm", "Rename")
    path = root / "renamed.py"
    path.write_text("value = 2\n")
    value = loaded_file(path, first)
    assert first in value.versions.values() and value["value"] == "value = 1\n"
    assert loaded_file(path, "HEAD")["value"] == "value = 1\n"
    assert loaded_file(path, "filesystem")["value"] == "value = 2\n"
    path.write_text("")
    assert loaded_file(path, "filesystem")["value"] == ""
    path.write_bytes(b"\x00\xff")
    state = FileEditorState()
    state.selected_path, state.version = str(path), "filesystem"
    state._file = loaded_file(path, "filesystem")
    # The registered text codec owns decoding, including its Latin-1 fallback.
    assert version_value(state)[1:] == ("\x00ÿ", None)
    path.unlink()
    state._file = loaded_file(path, "filesystem")
    assert "absent" in version_value(state)[2]


def test_moving_head_refreshes_without_recreating_model(repository):
    _root, path, git = repository
    value = loaded_file(path, "HEAD")
    assert value["value"] == "value = 1\n"
    pinned = value.source
    path.write_text("value = 3\n")
    git("add", ".")
    git("commit", "-qm", "Second")
    refreshed = loaded_file(path, "HEAD")
    assert refreshed is value and value["value"] == "value = 3\n"
    assert pinned[path] == "value = 1\n"


def test_linked_worktree_versions(repository, tmp_path):
    root, _path, git = repository
    checkout = root.parent / (root.name + "-linked")
    git("worktree", "add", "-qb", "linked", str(checkout))
    assert loaded_file(checkout / "original.py", "HEAD")["value"] == "value = 1\n"


def test_selection_is_local_and_only_target_consumes_jump():
    files = OpenFiles()
    files.open_paths = ["/first.py", "/second.py"]
    a, b = FileEditorState(), FileEditorState()
    a.selected_path, b.selected_path = "/first.py", "/second.py"
    a.version, b.version = "HEAD", "filesystem"
    files.jump_to_instance, files.jump_to_path = "b", "/first.py"
    adopt_selection(files, a, "a")
    adopt_selection(files, b, "b")
    assert (a.selected_path, a.version) == ("/first.py", "HEAD")
    assert (b.selected_path, b.version) == ("/first.py", "current")
    files.close_file("/first.py")
    adopt_selection(files, a, "a")
    assert a.selected_path == "/second.py"


def test_version_switch_never_serves_old_file_or_revision():
    state = FileEditorState()
    state.selected_path, state.version = "/a.py", "HEAD"
    state._file = SimpleNamespace(repo=SimpleNamespace(root=Path("/")),
                                 path="a.py", version="filesystem", versions={},
                                 error=None, loading=False, get=lambda key: "")
    assert version_value(state)[1] is None
    state._file.path, state._file.version = "b.py", "HEAD"
    assert version_value(state)[1] is None
    state._file.path = "a.py"
    assert version_value(state)[1:] == ("", None)


def test_current_navigation_address_resolves_same_and_other_file(tmp_path):
    from file_editor import navigation_address
    from meltygui.editor.roster_tints import ctrl_b_lookup

    target = tmp_path / "target.py"
    target.write_text("def destination():\n    return 42\n")
    path = tmp_path / "main.py"
    text = "from target import destination\ndef local():\n    return destination()\nlocal()\n"
    path.write_text("# The editor's imports and functions are not saved yet.\n")
    address = navigation_address(str(path), "current")
    assert address.path == path and address.pending_coords
    project = str(tmp_path)
    local = ctrl_b_lookup(text, text.rindex("local"), address.path, project=project)
    remote = ctrl_b_lookup(text, text.rindex("destination"), address.path, project=project)
    assert [(ref.path, ref.line) for ref in local[4]] == [(path, 2)]
    assert [(ref.path, ref.line) for ref in remote[4]] == [(target, 1)]
    # Historic buffers must not replace the live roster until lookup accepts
    # their own source world.
    assert navigation_address(str(path), "HEAD") is None


def test_navigation_waits_for_loaded_buffer_and_reveals_folded_target(monkeypatch):
    from file_editor import consume_navigation
    from meltygui.editor.text_editor import _fold_build
    from meltygui.core.melty import Melty
    from meltygui.state.core_undo import NavUndo

    text = "def before():\n    pass\n    pass\ndef destination():\n    pass\n    pass\n"
    ranges = ((0, 2), (3, 5))
    collapsed = set(ranges)
    events = []
    pane = SimpleNamespace(
        _diff_line_px=20, height=40, scroll_offset=(3, 0),
        text_cursor_pos=0, _fold_collapsed=collapsed,
        _fold_cache=(text, (ranges, frozenset(collapsed)), _fold_build(text, ranges, collapsed)),
        invalidate=lambda: events.append("invalidate"))
    monkeypatch.setattr(Melty, "text_focused_ds", None)
    monkeypatch.setattr(Melty, "_text_focus_grant_frame", None, raising=False)
    monkeypatch.setattr(NavUndo, "quiet_caret", lambda: events.append("quiet"))
    files = OpenFiles()
    files.active_instance = "other"
    files.jump_to_instance, files.jump_to_path = "owner", "/main.py"
    files.jump_to_line, files.jump_to_token = 4, "destination"
    state = FileEditorState()
    state.selected_path, state._file = "/main.py", object()
    consume_navigation(files, state, "other", pane, text)
    consume_navigation(files, state, "owner", pane, None)
    assert files.jump_to_line == 4 and pane.text_cursor_pos == 0
    consume_navigation(files, state, "owner", pane, text)
    display = pane._fold_cache[2][0]
    assert pane.text_cursor_pos == display.index("destination")
    assert pane.text_selection_start == pane.text_selection_end == pane.text_cursor_pos
    assert pane._fold_collapsed == {(0, 2)}
    assert pane.scroll_offset == (3, 4)
    assert Melty.text_focused_ds is pane
    assert files.jump_to_path is None and files.jump_to_line is None
    assert "quiet" in events and "invalidate" in events


def test_navigation_no_focus_and_first_layout(monkeypatch):
    from file_editor import consume_navigation
    from meltygui.core.melty import Melty
    from meltygui.state.core_undo import NavUndo

    previous_focus = object()
    monkeypatch.setattr(Melty, "text_focused_ds", previous_focus)
    monkeypatch.setattr(NavUndo, "quiet_caret", lambda: None)
    pane = SimpleNamespace(_diff_line_px=None, height=100, scroll_offset=(0, 0),
                           text_cursor_pos=0, invalidate=lambda: None)
    files = OpenFiles()
    files.jump_to_path, files.jump_to_line, files.jump_no_focus = "/main.py", 2, True
    state = FileEditorState()
    state.selected_path, state._file = "/main.py", object()
    consume_navigation(files, state, 0, pane, "first\nsecond\n")
    assert files.jump_to_line == 2
    pane._diff_line_px = 20
    consume_navigation(files, state, 0, pane, "first\nsecond\n")
    assert pane.text_cursor_pos == 6 and Melty.text_focused_ds is previous_focus
    assert files.jump_no_focus is False


def test_file_editor_navigation_routes_and_records_origin(monkeypatch):
    from file_editor import draw_file_editor
    from meltygui.core.melty import Melty
    from meltygui.state.core_undo import NavUndo
    from meltygui_pro.editor import code_editor
    from meltygui_pro.navigation import enclosing_window, remember_editor, forget_editor, apply_location

    monkeypatch.setattr(code_editor, "_active_editors", {})
    owner = SimpleNamespace(_view_func=draw_file_editor.__wrapped__, instance="file-tile",
                            _tile_id=23, closed=False, name="file-editor")
    pane = SimpleNamespace(_parent=owner, text_cursor_pos=6)
    remember_editor(owner.instance, "/original.py", pane, "first\nsecond\n")
    assert enclosing_window(pane) is owner
    monkeypatch.setattr(Melty, "find_window", lambda name: None)
    assert code_editor.editor_window_draw_state(owner.instance) is owner
    assert code_editor._nav_location(owner.instance) == ("/original.py", 2, owner.instance)
    files = OpenFiles()
    files.active_instance = "other-tile"
    opened, recorded, invalidated, raised = [], [], [], []
    monkeypatch.setattr(OpenFiles, "open_file", lambda self, path: opened.append(path))
    monkeypatch.setattr(Melty, "vis", SimpleNamespace(root=SimpleNamespace(open_files=files)))
    monkeypatch.setattr(Melty, "cache", SimpleNamespace(invalidate_up=lambda *a, **k: invalidated.append(a)))
    monkeypatch.setattr(Melty, "move_window_to_front", lambda win: raised.append(win))
    monkeypatch.setattr(NavUndo, "record_location", lambda *locs: recorded.append(locs))
    code_editor.open_in_editor("/target.py", line_number=7, token="destination", editor_window=owner)
    assert files.jump_to_instance == owner.instance
    assert (files.jump_to_path, files.jump_to_line, files.jump_to_token) == ("/target.py", 7, "destination")
    assert recorded[-1] == (("/original.py", 2, owner.instance), ("/target.py", 7, owner.instance))
    assert invalidated[-1] == (23,)
    apply_location(("/original.py", 2, owner.instance))
    assert opened[-1] == "/original.py" and raised[-1] is owner
    replacement = SimpleNamespace(_parent=owner)
    remember_editor(owner.instance, "/target.py", replacement, "target")
    forget_editor(owner.instance, pane)
    assert code_editor._active_editors[owner.instance][1] is replacement
    forget_editor(owner.instance, replacement)
    assert owner.instance not in code_editor._active_editors


def test_navigation_replay_targets_primary_even_when_another_tile_is_active(monkeypatch):
    from meltygui.core.melty import Melty
    from meltygui.core.runtime import extensions
    from meltygui_pro.navigation import apply_location

    owner = SimpleNamespace(closed=True)
    opened = []
    monkeypatch.setattr(extensions, "source_window", lambda instance: owner if instance == 0 else None)
    monkeypatch.setattr(extensions, "open_source", lambda *a, **kw: opened.append((a, kw)))
    monkeypatch.setattr(Melty, "move_window_to_front", lambda win: None)
    apply_location(("/main.py", 3, 0))
    assert opened == [(("/main.py",), {"line_number": 3, "editor_window": owner})]
    assert not owner.closed


def test_comparisons_deduplicate_reciprocal_links_and_release_subscriptions():
    a, b = FileEditorState(), FileEditorState()
    a.selected_path = b.selected_path = "/a.py"
    view_a, view_b = SimpleNamespace(_kwargs={}), SimpleNamespace(_kwargs={})
    view_a._kwargs["diff_with"], view_b._kwargs["diff_with"] = view_b, view_a
    calls = []
    b._file = SimpleNamespace(unsubscribe=lambda callback: calls.append("closed"))
    endpoints = {"a": (view_a, a), "b": (view_b, b)}
    group = FileEditorComparisons()
    group.reconcile(endpoints)
    assert comparison_pairs(endpoints) == [("a", "b")]
    group.reconcile({"a": (view_a, a)})
    assert calls == ["closed"]
    assert view_a._kwargs["diff_with"] is view_b
    assert comparison_pairs(group._endpoints) == []


def test_diff_results_keep_texts_alive_and_do_not_recompute_identical_inputs(monkeypatch):
    group = FileEditorComparisons()
    import file_editor
    monkeypatch.setattr(file_editor, "request_render", lambda: None)
    a, b = "first\nsecond", "first\nchanged"
    token = id(a), id(b)
    request = {("a", "b"): (token, a, b)}
    group.compute(request)
    assert group._results[("a", "b")][1] == [("replace", 1, 2, 1, 2)]
    monkeypatch.setattr(file_editor, "diff_opcodes", lambda *args: pytest.fail("recomputed"))
    group.compute(request)
    assert group._results[("a", "b")][2:] == (a, b)
    group.close()
    assert group._results == {}


def test_comparison_dispatch_runs_worker_and_reuses_finished_result(monkeypatch):
    import file_editor
    monkeypatch.setattr(file_editor, 'request_render', lambda: None)
    group = FileEditorComparisons()
    a, b = 'first\nsecond', 'first\nchanged'
    request = {('a', 'b'): ((id(a), id(b)), a, b)}
    group.dispatch(request)
    worker = group._thread
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert group._results[('a', 'b')][1] == [('replace', 1, 2, 1, 2)]
    group.dispatch(request)
    assert group._thread is worker
    group.close()


def test_full_line_ribbons_use_existing_renderer_and_reverse_swapped_panes(monkeypatch):
    from meltygui_pro.editor import code_editor
    calls = []
    monkeypatch.setattr(code_editor, "_draw_compare_ribbons", lambda *args, **kwargs: calls.append((args, kwargs)))
    left = SimpleNamespace(_abs_left=lambda: 10, _abs_top=lambda: 100)
    right = SimpleNamespace(_abs_left=lambda: 510, _abs_top=lambda: 100)
    window = object()
    _draw_ribbons(window, right, left, [("insert", 2, 2, 3, 6)])
    args, kwargs = calls[0]
    assert args == (window, left, right, [(3, 6, 2, 2, "delete")])
    assert kwargs == {"label_left": "", "label_right": "", "draw_list": None}


def test_comparison_change_invalidates_baked_washes_once(monkeypatch):
    import file_editor
    from unittest.mock import Mock
    monkeypatch.setattr(file_editor, "request_render", lambda: None)
    group, state = FileEditorComparisons(), FileEditorState()
    pane = SimpleNamespace(_parent=None, invalidate_up=Mock())
    state._pane = pane
    group._endpoints = {"one": (None, state)}
    group._requests = {("one", "two"): ((1, 2), "a", "b")}
    group.dispatch({})
    pane.invalidate_up.assert_called_once_with(max_depth=6)
    assert pane._external_change
    group.dispatch({})
    assert pane.invalidate_up.call_count == 1


def test_saved_state_keeps_choices_and_excludes_runtime(tmp_path):
    from meltygui.core.conversion.load_save_v2 import load, save
    state = FileEditorState()
    state.selected_path, state.version = "/a.py", "HEAD"
    state._file = object()
    path = tmp_path / "file-editor.pkl"
    save(state, path)
    restored = load(path, run_on_load=False)
    assert (restored.selected_path, restored.version) == ("/a.py", "HEAD")
    assert restored._file is None and restored._pane is None


def test_tiles_share_proxy_and_release_only_their_own_subscription(repository):
    _root, path, _git = repository
    a, b = FileEditorState(), FileEditorState()
    value = proxies_for(path)[0].file(path, "HEAD")
    a.bind(value)
    b.bind(value)
    assert a._file is b._file
    a.close()
    assert b.changed in value._listeners and a.changed not in value._listeners
    b.close()
    assert not value._listeners
    thread = value._thread
    if thread is not None:
        thread.join(5)
        assert not thread.is_alive()


def test_both_editor_types_are_recognized_by_workspace():
    from app_model import EditorAppModel
    from file_editor import draw_file_editor
    from meltygui.model.tile_model import Tile
    files, app = OpenFiles(), EditorAppModel()
    old = app.tiles.children[0]
    new = Tile("File Editor", render_func=draw_file_editor, input_value=files)
    app.tiles.children.append(new)
    files.active_instance = new.id
    app.reconcile_editors(files)
    assert files.active_instance == new.id and files.primary_instance == old.id
    assert app.file_editor_ids() == {new.id}


def test_rebinding_retires_a_pre_refactor_reader():
    calls = []
    state = FileEditorState()
    state._reader = SimpleNamespace(close=lambda: calls.append("retired"))
    value = SimpleNamespace(subscribe=lambda callback: calls.append("subscribed"))
    state.bind(value)
    assert calls == ["retired", "subscribed"]
    assert "_reader" not in vars(state)


def test_tab_overlay_reflows_at_live_bottom_without_registering_inputs(monkeypatch):
    from unittest.mock import Mock
    import file_editor
    from meltygui.core.melty import Melty
    from meltygui.view import header_view
    from meltygui_pro.editor.code_editor import prepare_editor_tabs
    from meltygui_pro.models.tab_bar import TabBarState

    monkeypatch.setattr(file_editor.imgui, 'calc_text_size', lambda text: SimpleNamespace(x=50, y=16))
    monkeypatch.setattr(file_editor.imgui, 'set_window_font_scale', lambda scale: None)
    monkeypatch.setattr(file_editor.imgui, 'get_mouse_pos', lambda: (-100, -100))
    monkeypatch.setattr(Melty, 'on_drag', False)
    ds = SimpleNamespace(misc={}, _bounding_hovered=False, width=400, height=300,
                         abs_left=20, abs_top=30, bg_color=(.1, .1, .1, 1),
                         on_action=Mock(side_effect=AssertionError('overlay registered input')))
    state = FileEditorState()
    ds.misc['file_editor_state'] = state
    paths = ['/tmp/overlay-first.txt', '/tmp/overlay-second.txt']
    tabs, button_height, row_height, swatch_width, layout, _ = prepare_editor_tabs(
        paths, paths, ['first', 'second'], ds, TabBarState())
    assert all(tab['badge'][0] == 'TXT' for tab in tabs)
    for tab in tabs:
        tab['paint_style'] = {'color': (.2, .3, .4), 'text_color': (1, 1, 1)}
    state._tab_overlay = tabs, layout, button_height, row_height, swatch_width, ds.bg_color
    state._pane = object()
    ds.abs_clip_rect = (20, 30, 420, 330)
    from meltygui.core.rendering import overlay
    place = Mock()
    monkeypatch.setattr(overlay, 'place_overlay_view', place)
    paint = Mock()
    monkeypatch.setattr(header_view, 'flat_button', paint)
    dl = Mock()
    backing = Mock()
    monkeypatch.setattr(file_editor.imgui, 'get_window_draw_list', lambda: backing)
    file_editor.draw_file_editor_overlay_background(ds, dl)
    file_editor.draw_file_editor_overlay(ds, dl)
    backing.add_rect_filled.assert_called_once()
    backing_rect = backing.add_rect_filled.call_args.args[:4]
    assert all(call.args[:4] != backing_rect for call in dl.add_rect_filled.call_args_list)
    assert [call.kwargs['pos'][1] for call in paint.call_args_list] == [283, 283]
    place.assert_called_once_with(state._pane, (20, 60, 400, 223), ds.abs_clip_rect)
    assert all(call.kwargs['layout'] is False and call.kwargs['draw_list'] is dl
               for call in paint.call_args_list)
    # The same prepared data reflows on a frozen frame without running the body.
    paint.reset_mock()
    from unittest.mock import create_autospec
    child_paint = create_autospec(overlay.paint_cached_view)
    monkeypatch.setattr(overlay, 'paint_cached_view', child_paint)
    ds._blit_served_frame = Melty.frame_count
    ds.width, ds.height = tabs[0]['w'] + 14, 200
    file_editor.draw_file_editor_overlay_background(ds, dl)
    file_editor.draw_file_editor_overlay(ds, dl)
    assert [call.kwargs['pos'][1] for call in paint.call_args_list] == [139, 183]
    assert place.call_args.args == (state._pane, (20, 60, ds.width, 79), ds.abs_clip_rect)
    assert dl.push_clip_rect.call_count == dl.pop_clip_rect.call_count == 4
    child_paint.assert_called_once_with(state._pane)
    ds.on_action.assert_not_called()


def test_files_settings_overlay_tracks_live_right_edge(monkeypatch):
    from unittest.mock import Mock
    import project_tree
    from meltygui.core.melty import Melty
    from meltygui_pro.editor import project_selector
    paint = Mock()
    monkeypatch.setattr(project_selector, 'draw_project_settings_overlay', paint)
    monkeypatch.setattr(Melty, 'px', lambda value: value)
    ds = SimpleNamespace(misc={'panel_state': SimpleNamespace(selected_project=None)},
                         _kwargs={'header_height': 30,
                                  'panel_state': SimpleNamespace(selected_project='/tmp/project')},
                         abs_left=20, abs_top=40, width=400)
    dl = object()
    project_tree.draw_project_tree_overlay(ds, dl)
    assert paint.call_args.args == (ds, dl, 390, 40, 30)
    ds.width = 250
    project_tree.draw_project_tree_overlay(ds, dl)
    assert paint.call_args.args == (ds, dl, 240, 40, 30)


def test_comparison_overlay_uses_prepared_data_and_translates_cached_root(monkeypatch):
    from unittest.mock import Mock
    import file_editor
    from meltygui_pro.editor import code_editor
    state = FileEditorComparisons()
    ds = SimpleNamespace(misc={'file_comparisons': state}, abs_left=10, abs_top=20)
    dl = Mock(flags=7)
    monkeypatch.setattr(file_editor.imgui, 'get_overlay_draw_list', lambda: dl)
    def prepare(owner, a, b, bands, **kwargs):
        target = kwargs['draw_list']
        target.add_rect_filled(12, 23, 40, 50, 123)
        target.flags = 0
        target.add_polyline([(12, 23), (40, 50)], 456, thickness=2)
    build = Mock(side_effect=prepare)
    monkeypatch.setattr(code_editor, '_draw_compare_ribbons', build)
    state._paint_commands = file_editor.prepare_comparison_overlay(ds, [(object(), object(), [])])
    dl.add_rect_filled.assert_not_called()
    build.reset_mock()
    ds.abs_left, ds.abs_top = 30, 50
    file_editor.draw_file_editor_comparison_overlay(ds, dl)
    build.assert_not_called()
    dl.add_rect_filled.assert_called_once_with(32, 53, 60, 80, 123)
    dl.add_polyline.assert_called_once_with([(32, 53), (60, 80)], 456, thickness=2)
    assert dl.flags == 7
