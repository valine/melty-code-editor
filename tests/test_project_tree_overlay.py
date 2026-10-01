"""Files row decoration follows live tile bounds rather than cached body pixels."""
import ctypes
import inspect
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest
from meltygui import imgui
from meltygui.core.melty import Melty
from meltygui.core.rendering import overlay
from meltygui.core.rendering.injected_state import state_parameters
from project_tree import FileRowsOverlayState
import project_tree


@pytest.fixture
def frame(monkeypatch):
    previous = imgui.get_current_context()
    context = imgui.create_context()
    io = imgui.get_io()
    io.display_size = (800, 600)
    io.delta_time = 1 / 60
    io.fonts.get_tex_data_as_rgba32()
    monkeypatch.setattr(Melty, 'cache', None)
    monkeypatch.setattr(Melty, 'text_focused_ds', None)
    monkeypatch.setattr(Melty, 'px', staticmethod(lambda value: value))
    monkeypatch.setattr(project_tree, 'sync_watches', lambda owner, state, directories: None)
    try:
        yield
    finally:
        imgui.destroy_context(context)
        if previous is not None:
            imgui.set_current_context(previous)


def test_tree_selection_and_hover_emit_no_cached_row_pixels(frame, tmp_path, monkeypatch):
    for name in ('first.txt', 'second.txt'):
        (tmp_path / name).write_text('text\n')
    state = project_tree.ProjectTreeState()
    row_overlay = FileRowsOverlayState()
    monkeypatch.setattr(state._folder_icons, 'dispatch', lambda notify, *, check_stale: None)
    outputs = []
    try:
        for selected, hovered in ((None, False), (str(tmp_path / 'first.txt'), True)):
            state.selected = selected
            imgui.new_frame()
            imgui.set_next_window_position(0, 0)
            imgui.set_next_window_size(500, 400)
            imgui.begin('Files row pixels')
            x, y = imgui.get_cursor_screen_pos()
            # Hover in the row's label area; its icon has separate local input.
            monkeypatch.setattr(imgui, 'get_mouse_pos', lambda: (x + 200, y + 10))
            ds = SimpleNamespace(
                width=400, height=200, content_width=388, abs_left=x, abs_top=y,
                abs_clip_rect=(x, y, x + 400, y + 200), scroll_offset=(0, 0),
                _bounding_hovered=hovered, _tile_id='tree', closable=False,
                parent_window=None, on_action=lambda *args, **kwargs: None,
                invalidate=lambda *args, **kwargs: None, current_tint=(.3, .4, .5),
                misc={'_row_overlay': row_overlay})
            dl = imgui.get_window_draw_list()
            start = dl.vtx_buffer_size
            try:
                result = inspect.unwrap(project_tree.draw_project_files)(
                    None, draw_state=ds, tree_state=state, root=str(tmp_path), file_metadata={},
                    _row_overlay=row_overlay)
                assert result == (False, None)
                outputs.append(ctypes.string_at(dl.vtx_buffer_data + start * imgui.VERTEX_SIZE,
                                                (dl.vtx_buffer_size - start) * imgui.VERTEX_SIZE))
                assert row_overlay.layout['selected_index'] == (0 if selected else None)
                assert row_overlay.layout['width_reserve'] == 12
                if selected:
                    # Explicit tree_state is not in misc: the per-view overlay
                    # must still paint the selected row after the body capture.
                    overlay_start = dl.vtx_buffer_size
                    project_tree.draw_file_rows_overlay(ds, dl)
                    assert dl.vtx_buffer_size > overlay_start
            finally:
                imgui.end()
                imgui.end_frame()
        assert outputs[0] and outputs[0] == outputs[1]
    finally:
        state._folder_icons.close()


def test_tree_overlay_uses_live_width_scroll_and_shared_selection(monkeypatch):
    state = FileRowsOverlayState()
    state.layout = dict(left_offset=2, top_offset=6, width_reserve=12,
                              row_height=20, row_count=10, selected_index=3,
                              hover_alpha=.05, rounding=3)
    ds = SimpleNamespace(misc={'_row_overlay': state}, abs_left=10, abs_top=30,
                         width=300, height=180, abs_clip_rect=(10, 30, 310, 210),
                         scroll_offset=(0, 20), _bounding_hovered=False)
    selection = create_autospec(Melty.paint_selection)
    monkeypatch.setattr(Melty, 'paint_selection', selection)
    dl = Mock()
    project_tree.draw_file_rows_overlay(ds, dl)
    assert selection.call_args.args == (ds, dl, (12, 76, 288, 20))
    ds.width, ds.abs_left, ds.abs_top, ds.scroll_offset = 480, 40, 60, (0, 40)
    ds.abs_clip_rect = (40, 60, 520, 240)
    project_tree.draw_file_rows_overlay(ds, dl)
    assert selection.call_args.args == (ds, dl, (42, 86, 468, 20))


def test_shared_tree_state_keeps_each_views_row_layout(frame, tmp_path, monkeypatch):
    path = tmp_path / 'selected.txt'
    path.write_text('text\n')
    tree = project_tree.ProjectTreeState()
    tree.selected = str(path)
    monkeypatch.setattr(tree._folder_icons, 'dispatch', lambda notify, *, check_stale: None)
    views = []
    try:
        for width, row_height in ((240, 20), (480, 28)):
            rows = FileRowsOverlayState()
            imgui.new_frame()
            imgui.set_next_window_position(0, 0)
            imgui.set_next_window_size(600, 400)
            imgui.begin('Borrowed tree state')
            x, y = imgui.get_cursor_screen_pos()
            ds = SimpleNamespace(
                width=width, height=200, content_width=width - 12, abs_left=x, abs_top=y,
                abs_clip_rect=(x, y, x + width, y + 200), scroll_offset=(0, 0),
                _bounding_hovered=False, _tile_id=f'tree-{width}', closable=False,
                parent_window=None, on_action=lambda *args, **kwargs: None,
                invalidate=lambda *args, **kwargs: None, misc={'_row_overlay': rows})
            try:
                inspect.unwrap(project_tree.draw_project_files)(
                    None, draw_state=ds, tree_state=tree, root=str(tmp_path), file_metadata={},
                    row_height=row_height, _row_overlay=rows)
                views.append((ds, dict(rows.layout)))
            finally:
                imgui.end()
                imgui.end_frame()
        assert not hasattr(tree, '_row_overlay')
        assert '_row_overlay' not in state_parameters(project_tree.draw_project_files)
        selection = create_autospec(Melty.paint_selection)
        monkeypatch.setattr(Melty, 'paint_selection', selection)
        dl = Mock()
        for (ds, expected), (width, height) in zip(views, ((228, 20), (468, 28))):
            assert ds.misc['_row_overlay'].layout == expected
            project_tree.draw_file_rows_overlay(ds, dl)
            assert selection.call_args.args == (ds, dl, (ds.abs_left, ds.abs_top, width, height))
    finally:
        tree._folder_icons.close()


def test_files_background_places_nested_view_at_live_tile_bounds(monkeypatch):
    state = project_tree.ProjectPanelState()
    state._files_view = object()
    state._files_insets = (2, 34, 6, 4)
    ds = SimpleNamespace(misc={'panel_state': project_tree.ProjectPanelState()},
                         _kwargs={'panel_state': state}, abs_left=20, abs_top=40,
                         width=400, height=300, abs_clip_rect=(20, 40, 420, 340))
    place = create_autospec(overlay.place_overlay_view)
    monkeypatch.setattr(overlay, 'place_overlay_view', place)
    project_tree.draw_project_tree_overlay_background(ds, object())
    place.assert_called_once_with(state._files_view, (22, 74, 392, 262), ds.abs_clip_rect)
    ds.abs_left, ds.abs_top, ds.width, ds.height = 50, 70, 600, 220
    ds.abs_clip_rect = (50, 70, 650, 290)
    project_tree.draw_project_tree_overlay_background(ds, object())
    assert place.call_args.args == (state._files_view, (52, 104, 592, 182), ds.abs_clip_rect)


def test_tree_overlay_state_does_not_persist_view_references():
    panel = project_tree.ProjectPanelState()
    panel._files_view, panel._files_insets = object(), (2, 34, 6, 4)
    saved = panel.to_dict()
    assert '_files_view' not in saved and '_files_insets' not in saved
    row_overlay = FileRowsOverlayState()
    row_overlay.layout = {'selected_index': 3}
    assert 'layout' not in row_overlay.to_dict()
