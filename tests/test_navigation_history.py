"""File opens record once, before loading; replay retains the source caret/tile."""
from types import SimpleNamespace

import pytest

from file_editor import FileEditorState, adopt_selection, consume_navigation, draw_file_editor
from project_tree import open_path
from meltygui.core.melty import Melty
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.runtime.toggles import Toggles
from meltygui.editor.text_editor import _fold_build
from meltygui.state.core_undo import NavUndo, UndoManager, UndoStack
from meltygui_pro.editor import code_editor
from meltygui_pro.models.open_files import OpenFiles
from meltygui_pro.navigation import remember_editor


@pytest.fixture
def navigation(monkeypatch, tmp_path):
    monkeypatch.setattr(Core, 'melty', Melty)
    monkeypatch.setattr(Melty, 'frame_count', 100)
    monkeypatch.setattr(Melty, 'cache', None)
    monkeypatch.setattr(Melty, 'text_focused_ds', None)
    monkeypatch.setattr(Melty, '_text_focus_grant_frame', None, raising=False)
    monkeypatch.setattr(Melty, 'find_window', lambda name: None)
    monkeypatch.setattr(Melty, 'move_window_to_front', lambda owner: None)
    monkeypatch.setattr(Toggles.CodeEditor, 'undo_navigation', True)
    monkeypatch.setattr(UndoManager, 'settle_for', -1)
    monkeypatch.setattr(NavUndo, 'stack', UndoStack('navigation-test'))
    monkeypatch.setattr(NavUndo, '_restoring', False)
    monkeypatch.setattr(NavUndo, '_caret_quiet_until', -1)
    monkeypatch.setattr(code_editor, '_active_editors', {})
    files = OpenFiles()
    # A Files tile can open into an editor other than the last hovered one.
    files.active_instance = 'other-editor'
    monkeypatch.setattr(Melty, 'vis', SimpleNamespace(root=SimpleNamespace(open_files=files)))

    def open_file(self, path):
        if str(path) not in self.open_paths:
            self.open_paths.append(str(path))

    monkeypatch.setattr(OpenFiles, 'open_file', open_file)
    owner = SimpleNamespace(_view_func=draw_file_editor.__wrapped__, instance='file-editor',
                            _tile_id=None, closed=False, name='file-editor')
    before, after = tmp_path / 'before.py', tmp_path / 'after.py'
    text = 'def folded():\n    pass\n    pass\n\nsource = 1\n'
    before.write_text(text)
    after.write_text('destination = 2\n')
    ranges = ((0, 2),)
    collapsed = set(ranges)
    built = _fold_build(text, ranges, collapsed)
    pane = SimpleNamespace(_parent=owner, _diff_line_px=20, height=100,
                           scroll_offset=(0, 0), text_cursor_pos=built[0].index('source'),
                           _fold_collapsed=collapsed,
                           _fold_cache=(text, (ranges, frozenset(collapsed)), built),
                           invalidate=lambda: None)
    state = FileEditorState()
    state.selected_path, state._file = str(before), object()
    files.open_paths = [str(before)]
    remember_editor(owner.instance, str(before), pane, text)
    return files, state, owner, pane, text, before, after


def test_file_open_records_source_caret_and_replays_once(navigation):
    files, state, owner, pane, text, before, after = navigation
    files.jump_to_line, files.jump_to_token, files.jump_no_focus = 99, 'stale', True
    assert open_path(files, after, owner.instance) is None
    assert len(NavUndo.stack.history) == 1
    change = NavUndo.stack.history[0]
    assert change.old == (str(before), 5, owner.instance)
    assert change.new == (str(after), None, owner.instance)
    assert files.jump_to_line is None and files.jump_to_token is None
    assert files.jump_no_focus is False
    assert files.jump_to_instance == owner.instance

    adopt_selection(files, state, owner.instance)
    consume_navigation(files, state, owner.instance, pane, None)
    assert files.jump_to_path == str(after)  # Loading does not consume or record.
    target = SimpleNamespace(_parent=owner, text_cursor_pos=0)
    consume_navigation(files, state, owner.instance, target, after.read_text())
    remember_editor(owner.instance, str(after), target, after.read_text())
    assert len(NavUndo.stack.history) == 1

    # A second request for the same tab is a no-op in navigation history.
    assert open_path(files, after, owner.instance) is None
    assert len(NavUndo.stack.history) == 1
    NavUndo.undo()
    assert not NavUndo.stack.history and len(NavUndo.stack.redo_stack) == 1
    assert (files.jump_to_path, files.jump_to_line, files.jump_to_instance) == (str(before), 5, owner.instance)
    adopt_selection(files, state, owner.instance)
    consume_navigation(files, state, owner.instance, pane, text)
    remember_editor(owner.instance, str(before), pane, text)
    assert not NavUndo.stack.history
    assert pane._fold_cache[2][0][pane.text_cursor_pos:].startswith('source')

    NavUndo.redo()
    assert len(NavUndo.stack.history) == 1 and not NavUndo.stack.redo_stack
    assert (files.jump_to_path, files.jump_to_line, files.jump_to_instance) == (str(after), None, owner.instance)
    adopt_selection(files, state, owner.instance)
    consume_navigation(files, state, owner.instance, target, after.read_text())
    assert state.selected_path == str(after) and len(NavUndo.stack.history) == 1


def test_refused_file_open_does_not_record_or_replace_pending_jump(navigation, monkeypatch):
    files, _state, owner, _pane, _text, before, after = navigation
    files.jump_to_path, files.jump_to_line = str(before), 5
    monkeypatch.setattr('project_tree.writable_file_refusal', lambda path: 'read-only')
    assert 'read-only' in open_path(files, after, owner.instance)
    assert not NavUndo.stack.history
    assert (files.jump_to_path, files.jump_to_line) == (str(before), 5)


@pytest.mark.parametrize('restoring', [False, True])
def test_file_open_respects_navigation_history_gate(navigation, monkeypatch, restoring):
    files, _state, owner, *_rest, after = navigation
    monkeypatch.setattr(NavUndo, '_restoring', restoring)
    monkeypatch.setattr(Toggles.CodeEditor, 'undo_navigation', restoring)
    assert open_path(files, after, owner.instance) is None
    assert not NavUndo.stack.history
    assert files.jump_to_path == str(after)
