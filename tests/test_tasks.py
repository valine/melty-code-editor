"""tasks.py without a window: `.venv/bin/python -m pytest tests/test_tasks.py`."""
import os
import pathlib
import sys
import time
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import meltygui_pro  # noqa: F401  registers its services before tasks imports project code
import tasks


@pytest.fixture(autouse=True)
def isolated_task_list(monkeypatch):
    monkeypatch.setattr(tasks, 'project_tasks', tasks.ProjectTasks())
    monkeypatch.setattr(tasks, '_pending', None)
    monkeypatch.setattr(tasks, '_pending_error', None)


def project(tmp_path, table):
    (tmp_path / 'pyproject.toml').write_text('[tool.melty.tasks]\n' + table)
    (tmp_path / 'sub').mkdir()
    return str(tmp_path)


def wait(state, seconds=10.0):
    deadline = time.monotonic() + seconds
    while state.running and time.monotonic() < deadline:
        time.sleep(0.02)
    return not state.running


def test_read_tasks_short_and_long_form(tmp_path):
    root = project(tmp_path, 'test = "echo hi"\n'
                             'serve = { cmd = "python -m app", cwd = "sub", env = { PORT = 8000 } }\n'
                             'bad = 3\n'
                             'worse = { cwd = "x" }\n')
    assert tasks.read_tasks(root) == {
        'test': {'cmd': 'echo hi', 'cwd': '.', 'env': {}},
        'serve': {'cmd': 'python -m app', 'cwd': 'sub', 'env': {'PORT': '8000'}}}


def test_read_tasks_without_table_or_file(tmp_path):
    assert tasks.read_tasks(str(tmp_path)) == {}
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "x"\n')
    assert tasks.read_tasks(str(tmp_path)) == {}


def test_environment_prefers_the_project_venv(tmp_path):
    venv = tmp_path / '.venv'
    (venv / 'bin').mkdir(parents=True)
    (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')
    python = venv / 'bin' / 'python'
    python.write_text('#!/bin/sh\n')
    python.chmod(0o755)
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "x"\n')
    os.environ['PYTHONPATH'] = '/nowhere'
    try:
        env = tasks.task_environment(str(tmp_path))
    finally:
        del os.environ['PYTHONPATH']
    assert env['PATH'].split(os.pathsep)[0] == str(venv / 'bin')
    assert env['VIRTUAL_ENV'] == str(venv)
    assert 'PYTHONPATH' not in env and env['PYTHONUNBUFFERED'] == '1'


def test_start_streams_output_and_exit_code(tmp_path):
    root = project(tmp_path, 'hi = { cmd = "echo $GREETING; pwd; exit 3", cwd = "sub", env = { GREETING = "hello" } }\n')
    state = tasks.TaskState()
    tasks.start_task(state, root, 'hi')
    assert state.error is None and state.running
    assert wait(state)
    assert state.exit == 3
    assert state.output.splitlines() == ['$ echo $GREETING; pwd; exit 3', 'hello', str(pathlib.Path(root, 'sub').resolve())]
    assert tasks._last == (root, 'hi')


def test_stop_ends_the_process_group(tmp_path):
    root = project(tmp_path, 'wait = "sleep 30; echo never"\n')
    state = tasks.TaskState()
    tasks.start_task(state, root, 'wait')
    assert state.running
    tasks.stop_task(state)
    assert wait(state, 5.0)
    assert state.exit != 0 and state.output == '$ sleep 30; echo never\n'


def test_refusals_land_in_error(tmp_path):
    root = project(tmp_path, 'x = { cmd = "true", cwd = "missing" }\n')
    state = tasks.TaskState()
    tasks.start_task(state, root, 'nope')
    assert state.error.startswith('No task') and not state.running
    tasks.start_task(state, root, 'x')
    assert state.error.startswith('cwd is not a folder') and not state.running


def test_start_again_replaces_the_run(tmp_path):
    root = project(tmp_path, 'wait = "sleep 30"\nfast = "echo done"\n')
    state = tasks.TaskState()
    tasks.start_task(state, root, 'wait')
    tasks.start_task(state, root, 'fast')
    assert wait(state) and state.exit == 0 and 'done' in state.output


def test_module_tasks_persist_with_session_and_do_not_edit_manifest(tmp_path):
    from meltygui.core.conversion.load_save_v2 import save, load
    root = project(tmp_path, '"Run main.py" = "echo user command"\n')
    path = tmp_path / 'main.py'
    path.write_text('print("hello")\n')
    before = (tmp_path / 'pyproject.toml').read_text()
    name = tasks.add_module_task(path, root)
    assert name == 'Run main.py (2)'
    assert tasks.add_module_task(path, root) == name
    assert len(tasks.read_tasks(root)) == 2
    session = tmp_path / 'tasks.pkl'
    save(tasks.project_tasks, session)
    tasks.project_tasks = load(session, run_on_load=False)
    assert tasks.read_tasks(root)[name]['module'] == 'main.py'
    assert (tmp_path / 'pyproject.toml').read_text() == before


def test_module_run_uses_pending_text_and_package_imports(tmp_path, monkeypatch):
    from meltygui.editor.pending_save import PendingSave
    root = project(tmp_path, '')
    package = tmp_path / 'src' / 'example'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'helper.py').write_text('answer = 42\n')
    path = package / 'main.py'
    path.write_text('raise RuntimeError("stale disk text")\n')
    pending = 'from .helper import answer\nprint(__name__, answer)\n'
    monkeypatch.setattr(PendingSave, 'current_file_text', lambda path: pending)
    name = tasks.add_module_task(path, root)
    state = tasks.TaskState()
    tasks.start_task(state, root, name)
    assert state.error is None
    assert wait(state) and state.exit == 0
    assert '__main__ 42' in state.output
    assert state.project == root
    tasks.request_rerun()
    assert tasks.pending_run() == (root, name)


def test_save_module_task_preserves_manifest_and_pending_edits(tmp_path):
    from meltygui.code.project_code import project_code
    root = project(tmp_path, 'test = "echo keep" # keep comment\n')
    manifest = tmp_path / 'pyproject.toml'
    path = tmp_path / 'main.py'
    path.write_text('print("hello")\n')
    source = project_code[manifest]
    before = source.text() + '\n[tool.other]\nvalue = "pending"\n'
    assert source.write_text(before)
    name = tasks.add_module_task(path, root, save=True)
    updated = source.text()
    assert '# keep comment' in updated and 'pending' in updated
    tasks.project_tasks = tasks.ProjectTasks()
    assert tasks.read_tasks(root)[name]['module'] == 'main.py'
    assert tasks.read_tasks(root)['test']['cmd'] == 'echo keep'


def test_module_requests_keep_project_lists_separate(tmp_path, monkeypatch):
    import editor_settings
    monkeypatch.setattr(editor_settings, 'settings', {'Tasks': {'save_tasks': False}})
    for folder in ('one', 'two'):
        root = tmp_path / folder
        root.mkdir()
        path = root / 'main.py'
        path.write_text('print("hello")\n')
        tasks.request_module_run(str(path), str(root))
        assert tasks.pending_run() == (str(root), 'Run main.py')
    assert len(tasks.project_tasks.projects) == 2


def test_task_request_opens_one_tasks_tile():
    from app_model import EditorAppModel
    from meltygui_pro.models.open_files import OpenFiles
    from meltygui.core.layout.tile_manager_core import walk
    app, files = EditorAppModel(), OpenFiles()
    assert app.ensure_tasks(files)
    assert not app.ensure_tasks(files)
    tiles = [tile for _, tile in walk(app.tiles) if getattr(tile, 'render_func', None) is tasks.draw_tasks]
    assert len(tiles) == 1 and tiles[0].input_value is files


def test_save_setting_creates_manifest_for_module_request(tmp_path, monkeypatch):
    import editor_settings
    from meltygui_pro.models.project_kind import manifest_data
    monkeypatch.setattr(editor_settings, 'settings', {'Tasks': {'save_tasks': True}})
    path = tmp_path / 'main.py'
    path.write_text('print("hello")\n')
    tasks.request_module_run(str(path), str(tmp_path))
    assert tasks._pending_error is None
    assert manifest_data(tmp_path)['tool']['melty']['tasks']['Run main.py']['module'] == 'main.py'


def test_invalid_manifest_is_not_overwritten_when_saving(tmp_path, monkeypatch):
    import editor_settings
    monkeypatch.setattr(editor_settings, 'settings', {'Tasks': {'save_tasks': True}})
    path = tmp_path / 'main.py'
    path.write_text('print("hello")\n')
    manifest = tmp_path / 'pyproject.toml'
    manifest.write_text('[unfinished\n')
    tasks.request_module_run(str(path), str(tmp_path))
    assert tasks._pending_error
    assert manifest.read_text() == '[unfinished\n'


def test_project_selection_restores_while_another_task_runs(tmp_path):
    from meltygui.core.conversion.load_save_v2 import save, load
    state = tasks.TaskState()
    state.project, state.task, state.running = '/a', 'second', True
    assert tasks.selected_task(state, '/a', {'first': {}, 'second': {}}) == 'second'
    assert tasks.selected_task(state, '/b', {'other': {}}) == 'other'
    assert (state.project, state.task, state.running) == ('/a', 'second', True)
    assert tasks.selected_task(state, '/a', {'first': {}, 'second': {}}) == 'second'
    assert tasks.selected_task(state, '/empty', {}) is None
    assert tasks.selected_task(state, '/a', {'first': {}}) == 'first'
    session = tmp_path / 'selection.pkl'
    save(state, session)
    restored = load(session, run_on_load=False)
    assert restored.selected_tasks == state.selected_tasks


def test_tasks_follow_injected_files_selection():
    from types import SimpleNamespace as NS
    selection = NS(selected_project='/chosen')
    source = NS(misc={'panel_state': selection})
    assert tasks.tile_project(source, None) == '/chosen'
    selection.selected_project = '/changed'
    assert tasks.tile_project(source, None) == '/changed'
    selection.selected_project = None
    assert tasks.tile_project(source, None) is None


def test_output_follow_scroll_and_completion():
    from meltygui.state.new_core_model import DrawState
    state, output = tasks.TaskState(), DrawState()
    state.running = True
    output._max_scroll_y = 100
    output.scroll_offset = (0, 0)
    tasks.follow_output(state, output, 2)
    assert output.scroll_offset == (0, 100)
    # New lines grow the range without moving the reader upward.
    output._max_scroll_y = 150
    tasks.follow_output(state, output, 2)
    assert output.scroll_offset == (0, 150)
    # Moving upward must win over automatic following, including with new output.
    output.scroll_offset = (0, 80)
    output._max_scroll_y = 200
    tasks.follow_output(state, output, 2)
    assert not state._follow
    assert output.scroll_offset == (0, 80)
    state.running = False
    tasks.follow_output(state, output, 2)  # formerly raised AttributeError: scrolled
    assert output.scroll_offset == (0, 80)


def test_output_follow_range_clamp_and_unmeasured_content():
    from meltygui.state.new_core_model import DrawState
    state, output = tasks.TaskState(), DrawState()
    state.running = True
    tasks.follow_output(state, output, 2)
    assert state._output_scroll_y is None
    output._max_scroll_y = 100
    tasks.follow_output(state, output, 2)
    # A larger viewport or trimmed output clamps the offset; keep following.
    output._max_scroll_y = 50
    output.scroll_offset = (0, 50)
    tasks.follow_output(state, output, 2)
    assert state._follow
    output._max_scroll_y = 120
    tasks.follow_output(state, output, 2)
    assert output.scroll_offset == (0, 120)
