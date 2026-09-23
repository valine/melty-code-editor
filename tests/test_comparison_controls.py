from types import SimpleNamespace

from meltygui.core.melty import Melty
from meltygui_pro.editor import code_editor
from meltygui_pro.editor.comparison import (
    ComparisonState, navigate_comparison, synchronize_comparison)


def pane():
    return SimpleNamespace(_diff_line_px=10, height=100, width=200,
                           scroll_offset=(7, 0), _max_scroll_y=1000,
                           abs_left=0, abs_top=0, invalidate=lambda: None)


def test_navigation_wraps_and_centers_both_versions(monkeypatch):
    monkeypatch.setattr(Melty, 'emphasize', lambda *a, **kw: None)
    monkeypatch.setattr(code_editor, '_diff_disp_line', lambda pane, line: line)
    a, b = pane(), pane()
    blocks = [(20, 24, 30, 32, 'replace'), (80, 82, 90, 92, 'replace')]
    memory = {'_cmp_nav_req': -1}
    navigate_comparison(memory, a, b, blocks, 'pair')
    assert memory['_cmp_nav_idx'] == 1
    assert a.scroll_offset == (7, 765)
    assert b.scroll_offset == (7, 865)
    memory['_cmp_nav_req'] = 1
    navigate_comparison(memory, a, b, blocks, 'pair')
    assert memory['_cmp_nav_idx'] == 0
    assert a.scroll_offset == (7, 175)
    assert b.scroll_offset == (7, 265)
    assert '_cmp_nav_req' not in memory


def test_manual_fold_switch_is_pair_local_and_rebinding_resets_memory(monkeypatch):
    for name in ('_sync_diff_folds', '_sync_scope_fold_all', '_sync_scope_folds', '_link_compare_panes'):
        monkeypatch.setattr(code_editor, name, lambda *args: None)
    a, b = pane(), pane()
    first, other = ComparisonState(), ComparisonState()
    synchronize_comparison(first, a, b, [], 'a', 'b', 'first')
    a._diff_manual_gen = 1
    synchronize_comparison(first, a, b, [], 'a', 'b', 'first')
    assert first.expand_diff is None
    assert other.expand_diff is True
    first.misc['_cmp_nav_idx'] = 4
    synchronize_comparison(first, a, b, [], 'c', 'd', 'new')
    assert '_cmp_nav_idx' not in first.misc


def test_navigation_without_changes_consumes_request():
    memory = {'_cmp_nav_req': 1}
    navigate_comparison(memory, pane(), pane(), [], 'empty')
    assert memory == {}


def test_partner_scroll_invalidates_its_cached_tile(monkeypatch):
    for name in ('_sync_diff_folds', '_sync_scope_fold_all', '_sync_scope_folds'):
        monkeypatch.setattr(code_editor, name, lambda *args: None)
    from meltygui_pro.editor import git
    monkeypatch.setattr(git, '_flag_external_change', lambda pane: None)
    calls = []
    a, b = pane(), pane()
    b.invalidate_up = lambda **kwargs: calls.append(kwargs)
    def scroll(state, first, second, blocks):
        second.scroll_offset = (0, 40)
    monkeypatch.setattr(code_editor, '_link_compare_panes', scroll)
    synchronize_comparison(ComparisonState(), a, b, [], 'a', 'b', 'pair')
    assert calls == [{'max_depth': 6}]


def test_unlink_removes_controls_and_diff_layout(monkeypatch):
    import file_editor
    editor = file_editor.FileEditorState()
    editor._comparison = ComparisonState()
    editor._diff_folds = [(2, 10)]
    calls = []
    view = SimpleNamespace(_kwargs={}, invalidate_up=lambda **kw: calls.append(kw))
    monkeypatch.setattr(file_editor, 'file_editor_endpoints', lambda owner: {'a': (view, editor)})
    group = file_editor.FileEditorComparisons()
    file_editor.draw_file_editor_comparisons(None, group, {'a'})
    assert editor._comparison is None and editor._diff_folds is None
    assert calls
