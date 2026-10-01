"""Selection waits for a click; a right press/drag cannot select a file."""
import inspect
from types import SimpleNamespace

import pytest
from meltygui import imgui
from meltygui.state.file_state import FileExplorerState
from meltygui.state.path_icon_state import PathIconState
from meltygui.view.file_view import draw_file_listing
import project_tree
from test_project_tree_overlay import frame


@pytest.mark.parametrize('selected', [None, 'first.txt'])
@pytest.mark.parametrize('kind', ['browser', 'tree'])
@pytest.mark.parametrize('event,expected', [('right_mouse_down', 'first.txt'),
                                         ('right_mouse_drag', 'first.txt'),
                                         ('right_mouse_clicked', 'second.txt')])
def test_right_selection(frame, tmp_path, monkeypatch, kind, event, expected, selected):
    from meltygui.core.files import file_explorer_core
    monkeypatch.setattr(file_explorer_core, 'watch_directory', lambda *args: None)
    for name in ('first.txt', 'second.txt'):
        (tmp_path / name).write_text('text')
    state = project_tree.ProjectTreeState() if kind == 'tree' else FileExplorerState()
    state.selected = str(tmp_path / selected) if selected else None
    state._last_dir = str(tmp_path)
    icons = PathIconState()
    row_overlay = project_tree.FileRowsOverlayState()
    imgui.new_frame()
    imgui.set_next_window_position(0, 0)
    imgui.set_next_window_size(500, 400)
    imgui.begin('right selection')
    x, y = imgui.get_cursor_screen_pos()
    ds = SimpleNamespace(width=400, height=200, content_width=388, abs_left=x, abs_top=y,
        abs_clip_rect=(x, y, x + 400, y + 200), scroll_offset=(0, 0),
        _bounding_hovered=True, _tile_id='files', closable=False, parent_window=None,
        on_action=lambda *args, **kwargs: None, invalidate=lambda *args, **kwargs: None,
        current_tint=(.3, .4, .5), misc={'_row_overlay': row_overlay})
    target = {}
    kwargs = dict(draw_state=ds, file_metadata={}, menu_target=target,
                  **{event: SimpleNamespace(x=x + 200, y=y + 30)})
    try:
        if kind == 'tree':
            inspect.unwrap(project_tree.draw_project_files)(None, tree_state=state,
                root=str(tmp_path), _row_overlay=row_overlay, **kwargs)
        else:
            inspect.unwrap(draw_file_listing)(str(tmp_path), explorer_state=state,
                icon_state=icons, show_crumbs=False, drag_rows=False,
                show_tint_chips=False, type_to_search=False, **kwargs)
        expected_path = str(tmp_path / expected) if event == 'right_mouse_clicked' or selected else None
        assert state.selected == expected_path
        assert target['path'] == (expected_path or str(tmp_path))
    finally:
        imgui.end()
        imgui.end_frame()
        icons.folders.close()
        if kind == 'tree':
            state._folder_icons.close()
