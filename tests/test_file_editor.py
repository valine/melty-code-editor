"""Run from the app with `.venv/bin/python -m pytest tests/test_file_editor.py`."""
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from file_editor import (
    FileEditorState, FileEditorComparisons, FileVersions, VersionReader,
    adopt_selection, comparison_pairs, version_value, _line_band)
from meltygui_pro.models.open_files import OpenFiles


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


def test_versions_follow_renames_and_distinguish_disk_head_and_empty(repository):
    root, old, git = repository
    first = git("rev-parse", "HEAD")
    git("mv", old.name, "renamed.py")
    git("commit", "-qm", "Rename")
    path = root / "renamed.py"
    path.write_text("value = 2\n")
    versions = FileVersions(str(path))
    options, value, error, source = versions.read(first)
    assert first in options.values() and value == "value = 1\n" and error is None
    assert versions.read("HEAD")[1] == "value = 1\n"
    assert versions.read("filesystem")[1] == "value = 2\n"
    path.write_text("")
    assert versions.read("filesystem")[1:3] == ("", None)
    path.write_bytes(b"\x00\xff")
    assert "binary" in versions.read("filesystem")[2]
    path.unlink()
    assert "absent" in versions.read("filesystem")[2]


def test_moving_head_refreshes_without_recreating_model(repository):
    _root, path, git = repository
    versions = FileVersions(str(path))
    assert versions.read("HEAD")[1] == "value = 1\n"
    path.write_text("value = 3\n")
    git("add", ".")
    git("commit", "-qm", "Second")
    assert versions.read("HEAD")[1] == "value = 3\n"


def test_linked_worktree_versions(repository, tmp_path):
    root, _path, git = repository
    checkout = root.parent / (root.name + "-linked")
    git("worktree", "add", "-qb", "linked", str(checkout))
    versions = FileVersions(str(checkout / "original.py"))
    assert versions.read("HEAD")[1] == "value = 1\n"


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
    state._reader = SimpleNamespace(result=(("/a.py", "filesystem"), {}, "wrong", None))
    assert version_value(state)[1] is None
    state._reader.result = (("/b.py", "HEAD"), {}, "wrong", None)
    assert version_value(state)[1] is None
    state._reader.result = (("/a.py", "HEAD"), {}, "", None)
    assert version_value(state)[1:] == ("", None)


def test_comparisons_deduplicate_reciprocal_links_and_retire_readers():
    a, b = FileEditorState(), FileEditorState()
    a.selected_path = b.selected_path = "/a.py"
    a.sibling_tile_id, b.sibling_tile_id = "b", "a"
    calls = []
    b._reader = SimpleNamespace(close=lambda: calls.append("closed"))
    endpoints = {"a": (None, a), "b": (None, b)}
    group = FileEditorComparisons()
    group.reconcile(endpoints)
    assert comparison_pairs(endpoints) == [("a", "b")]
    assert a.siblings[0][0] == "b"
    group.reconcile({"a": (None, a)})
    assert calls == ["closed"] and a.siblings == ()
    assert a.sibling_tile_id == "b"  # Stable identity survives temporary removal.
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


def test_folded_scrolled_line_bands_use_live_pane_geometry():
    pane = SimpleNamespace(abs_top=100, height=200, _diff_top_inset=10,
                           _diff_clip_off=(10, 5), _diff_line_px=20,
                           scroll_offset=(0, 20), _diff_d2b=[0, 1, 8, 9])
    assert _line_band(pane, 8, 9) == (130, 150)
    pane.abs_top = 300
    assert _line_band(pane, 8, 9) == (330, 350)


def test_saved_state_keeps_choices_and_excludes_runtime(tmp_path):
    from meltygui.core.conversion.load_save_v2 import load, save
    state = FileEditorState()
    state.selected_path, state.version, state.sibling_tile_id = "/a.py", "HEAD", "second"
    state._reader = object()
    path = tmp_path / "file-editor.pkl"
    save(state, path)
    restored = load(path, run_on_load=False)
    assert (restored.selected_path, restored.version, restored.sibling_tile_id) == ("/a.py", "HEAD", "second")
    assert restored._reader is None and restored._pane is None


def test_reader_stops_when_closed(repository, monkeypatch):
    import file_editor
    monkeypatch.setattr(file_editor, "request_render", lambda: None)
    _root, path, _git = repository
    state = FileEditorState()
    reader = VersionReader(state)
    try:
        reader.select(str(path), "HEAD")
        deadline = time.monotonic() + 8
        while reader.result is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert reader.result[2:4] == ("value = 1\n", None)
    finally:
        reader.close()
        thread = reader._thread
        if thread is not None:
            thread.join(1)
    assert reader._thread is None or not reader._thread.is_alive()


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
