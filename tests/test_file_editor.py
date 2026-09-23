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
    assert kwargs == {"label_left": "", "label_right": ""}


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
