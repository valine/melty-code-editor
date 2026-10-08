"""tasks.py without a window: `.venv/bin/python -m pytest tests/test_tasks.py`."""
import os
import pathlib
import sys
import time
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import meltygui_pro  # noqa: F401  registers its services before tasks imports project code
import tasks
from meltygui_pro.models import project_execution as execution


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
        tasks.Melty._drain_render_tasks()
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
        env = execution.task_environment(str(tmp_path))
    finally:
        del os.environ['PYTHONPATH']
    assert env['PATH'].split(os.pathsep)[0] == str(venv / 'bin')
    assert env['VIRTUAL_ENV'] == str(venv)
    assert 'PYTHONPATH' not in env and env['PYTHONUNBUFFERED'] == '1'


def test_start_streams_output_and_exit_code(tmp_path):
    root = project(tmp_path, 'hi = { cmd = "echo $GREETING; pwd; exit 3", cwd = "sub", env = { GREETING = "hello" } }\n')
    state = tasks.TaskState()
    state.run(root, 'hi')
    assert state.error is None and state.running
    assert wait(state)
    assert state.exit == 3
    assert state.output.splitlines() == ['$ echo $GREETING; pwd; exit 3', 'hello', str(pathlib.Path(root, 'sub').resolve())]
    assert tasks._last == (root, 'hi', '')


def test_stop_ends_the_process_group(tmp_path):
    root = project(tmp_path, 'wait = "sleep 30; echo never"\n')
    state = tasks.TaskState()
    state.run(root, 'wait')
    assert state.running
    state.stop()
    assert wait(state, 5.0)
    assert state.exit != 0 and state.output == '$ sleep 30; echo never\n'


def test_refusals_land_in_error(tmp_path):
    root = project(tmp_path, 'x = { cmd = "true", cwd = "missing" }\n')
    state = tasks.TaskState()
    state.run(root, 'nope')
    assert state.error.startswith('No task') and not state.running
    state.run(root, 'x')
    assert state.error.startswith('cwd is not a folder') and not state.running


def test_start_again_replaces_the_run(tmp_path):
    root = project(tmp_path, 'wait = "sleep 30"\nfast = "echo done"\n')
    state = tasks.TaskState()
    state.run(root, 'wait')
    state.run(root, 'fast')
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
    state.run(root, name)
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
    state, output = tasks.TaskViewState(), DrawState()
    state.running = True
    output._max_scroll_y = 100
    output.scroll_offset = (0, 0)
    tasks.follow_output(state, output, 2, running=state.running)
    assert output.scroll_offset == (0, 100)
    # New lines grow the range without moving the reader upward.
    output._max_scroll_y = 150
    tasks.follow_output(state, output, 2, running=state.running)
    assert output.scroll_offset == (0, 150)
    # Moving upward must win over automatic following, including with new output.
    output.scroll_offset = (0, 80)
    output._max_scroll_y = 200
    tasks.follow_output(state, output, 2, running=state.running)
    assert not state._follow
    assert output.scroll_offset == (0, 80)
    state.running = False
    tasks.follow_output(state, output, 2, running=state.running)  # formerly raised AttributeError: scrolled
    assert output.scroll_offset == (0, 80)


def test_output_overlay_tracks_live_tile_bounds(monkeypatch):
    from types import SimpleNamespace as NS
    from meltygui.core.rendering import overlay

    state = tasks.TaskViewState()
    pane = state._output_view = object()
    state._toolbar_height = 28
    parent = NS(misc={'task_state': tasks.TaskState(), '_task_view_state': state}, abs_left=100, abs_top=200,
                width=400, height=300, abs_clip_rect=(100, 200, 500, 500),
                _blit_served_frame=42)
    placed, painted = [], []
    monkeypatch.setattr(tasks.Melty, 'px', lambda value: value)
    monkeypatch.setattr(tasks.Melty, 'frame_count', 42)
    monkeypatch.setattr(overlay, 'place_overlay_view', lambda *args: placed.append(args))
    monkeypatch.setattr(overlay, 'paint_cached_view', painted.append)

    tasks.draw_tasks_overlay_background(parent, None)
    assert placed[-1] == (pane, (100, 224, 400, 248), parent.abs_clip_rect)
    assert painted == [pane]
    # Resize/move replay must not depend on the body's last layout.
    parent.abs_left, parent.abs_top = 50, 60
    parent.width, parent.height = 600, 60
    parent.abs_clip_rect = (50, 60, 650, 120)
    tasks.draw_tasks_overlay_background(parent, None)
    assert placed[-1] == (pane, (50, 84, 600, 8), parent.abs_clip_rect)
    parent.height = 40
    parent.abs_clip_rect = (50, 60, 650, 100)
    tasks.draw_tasks_overlay_background(parent, None)
    assert placed[-1] == (pane, (50, 84, 600, 0), parent.abs_clip_rect)
    parent.height = 500
    parent._blit_served_frame = 41
    tasks.draw_tasks_overlay_background(parent, None)
    assert placed[-1][1] == (50, 84, 600, 448)
    assert painted == [pane, pane, pane]  # normal body draws do not paint twice

    # Waiting stdin reserves its own row; replay keeps the editable field and
    # output separate at the new tile bounds.
    parent.misc['task_state'].waiting_for_input = True
    field = state._input_view = object()
    tasks.draw_tasks_overlay_background(parent, None)
    assert placed[-2] == (field, (50, 500, 492, 28), parent.abs_clip_rect)
    assert placed[-1] == (pane, (50, 84, 600, 416), parent.abs_clip_rect)

    # Debug controls reserve a row in both normal layout and cached replay.
    parent.misc['task_state'].debug_enabled = True
    tasks.draw_tasks_overlay_background(parent, None)
    assert placed[-2] == (field, (50, 500, 492, 28), parent.abs_clip_rect)
    assert placed[-1] == (pane, (50, 112, 600, 388), parent.abs_clip_rect)


@pytest.mark.parametrize('command', ['continue', 'step_into', 'step_over', 'step_out', 'force_stop'])
def test_debug_controls_only_dispatch_from_enabled_task_body(monkeypatch, command):
    from types import SimpleNamespace as NS
    from meltygui.core.cache import tile_marks

    state = tasks.TaskState()
    state.debug_enabled = state.running = state.paused = True
    state._external = object()
    parent = NS(abs_clip_rect=(10, 20, 410, 320))
    calls, shadows = [], []
    monkeypatch.setattr(tasks.Melty, 'px', lambda value: value)
    monkeypatch.setattr(tasks.imgui, 'set_cursor_screen_pos', lambda pos: None)
    monkeypatch.setattr(tile_marks, 'add_shadow', lambda rect, **kw: shadows.append(rect))

    def click(label, owner, view_id, **kwargs):
        calls.append((owner, view_id, kwargs))
        return kwargs['layout'] and view_id == f'tasks-debug-{command}'

    monkeypatch.setattr(tasks, 'flat_button', click)
    assert tasks.draw_task_debug_controls(state, parent, 10, 20) == command
    body = calls[:]
    assert all(owner is parent and kw['layout'] and not kw['paint'] for owner, _, kw in body)
    calls.clear()
    assert tasks.draw_task_debug_controls(state, parent, 10, 20, paint=True, draw_list=object()) is None
    assert [kw['pos'] for _, _, kw in calls] == [kw['pos'] for _, _, kw in body]
    assert shadows == [(*kw['pos'], kw['width'], kw['height']) for _, _, kw in body]
    assert all(view_id is None and not kw['layout'] for _, view_id, kw in calls)

    # Resume actions are disabled while running; force stop remains available.
    state.paused = False
    calls.clear()
    assert tasks.draw_task_debug_controls(state, parent, 10, 20) == ('force_stop' if command == 'force_stop' else None)
    assert all(owner is None and kw['hovered'] is False for owner, _, kw in calls[:4])
    state.running = False
    assert tasks.draw_task_debug_controls(state, parent, 10, 20) is None
    state.debug_enabled = False
    calls.clear()
    assert tasks.draw_task_debug_controls(state, parent, 10, 20) is None
    assert calls == []


def test_toolbar_overlay_moves_buttons_and_shadows_together(monkeypatch):
    from types import SimpleNamespace as NS
    from meltygui.core.cache import tile_marks
    from meltygui_pro.editor import code_editor

    state = tasks.TaskViewState()
    state._toolbar_height = 28
    state._toolbar = {'left': 100, 'picker_widths': (180, 140), 'has_tasks': True}
    parent = NS(misc={'task_state': tasks.TaskState(), '_task_view_state': state}, abs_left=10, abs_top=20,
                width=600, height=300, abs_clip_rect=(10, 20, 610, 320))
    buttons, shadows, cleared, navigation = [], [], [], []
    monkeypatch.setattr(tasks.Melty, 'px', lambda value: value)
    monkeypatch.setattr(tasks, 'flat_button', lambda *args, **kw: buttons.append(kw))
    monkeypatch.setattr(tile_marks, 'clear_shadows', lambda *args: cleared.append(args))
    monkeypatch.setattr(tile_marks, 'add_shadow', lambda rect, **kw: shadows.append((rect, kw)))
    monkeypatch.setattr(code_editor, '_draw_nav_buttons', lambda *args, **kw: navigation.append(kw))

    for width, height in ((600, 300), (420, 160), (800, 500)):
        parent.width, parent.height = width, height
        tasks.draw_tasks_overlay(parent, object())
        assert len(buttons) == len(shadows)
        for button, (rect, shadow) in zip(buttons[-3:], shadows[-3:]):
            assert (*button['pos'], button['width'], button['height']) == rect
            assert rect[1] == parent.abs_top + height - 26
            assert button['layout'] is False
            assert shadow['draw_state'] is parent
            assert shadow['group'] == 'task_buttons'
            assert shadow['clip'] == parent.abs_clip_rect
        assert navigation == []
    assert cleared == [(parent, 'task_buttons')] * 3
    assert buttons[0]['pos'][0] > buttons[3]['pos'][0]  # narrow tile reflows


def test_output_follow_range_clamp_and_unmeasured_content():
    from meltygui.state.new_core_model import DrawState
    state, output = tasks.TaskViewState(), DrawState()
    state.running = True
    tasks.follow_output(state, output, 2, running=state.running)
    assert state._output_scroll_y is None
    output._max_scroll_y = 100
    tasks.follow_output(state, output, 2, running=state.running)
    # A larger viewport or trimmed output clamps the offset; keep following.
    output._max_scroll_y = 50
    output.scroll_offset = (0, 50)
    tasks.follow_output(state, output, 2, running=state.running)
    assert state._follow
    output._max_scroll_y = 120
    tasks.follow_output(state, output, 2, running=state.running)
    assert output.scroll_offset == (0, 120)


def test_current_file_default_is_not_a_persisted_definition(tmp_path):
    state = tasks.TaskState()
    choices = tasks.task_choices(str(tmp_path))
    assert tasks.selected_task(state, str(tmp_path), choices) == tasks.RUN_CURRENT_FILE
    assert tasks.selected_task(state, None, tasks.task_choices(None)) == tasks.RUN_CURRENT_FILE
    assert tasks.read_tasks(tmp_path) == {}


def test_current_file_tracks_linked_editor_and_creates_reusable_tasks(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from file_editor import FileEditorState
    import editor_settings
    monkeypatch.setattr(editor_settings, 'settings', {'Tasks': {'save_tasks': False}})
    monkeypatch.setattr(tasks, 'project_for', lambda path: tmp_path)
    editor = FileEditorState()
    view = NS(misc={'file_editor_state': editor}, _kwargs={})
    state = tasks.TaskState()
    runs = []
    monkeypatch.setattr(state, 'execute', lambda *args: runs.append(args))
    for filename in ('first.py', 'second file.py', 'second file.py'):
        path = tmp_path / filename
        path.write_text('print("hello")\n')
        editor.selected_path = str(path)
        state.run_current_file(view, debug=True, file_metadata='metadata')
        assert runs[-1][0] == str(tmp_path)
        assert runs[-1][1]['module'] == filename
        assert runs[-1][2:5] == ('Run ' + filename, True, 'metadata')
        assert state.selected_tasks[str(tmp_path)] == tasks.RUN_CURRENT_FILE
    assert len(tasks.read_tasks(tmp_path)) == 2
    assert tasks.read_tasks(tmp_path)['Run second file.py']['cmd'] == "python 'second file.py'"
    # A concrete task keeps its file when the linked editor changes tabs.
    state.run(str(tmp_path), 'Run first.py')
    assert runs[-1][1]['module'] == 'first.py'


def test_current_file_missing_or_historic_editor_does_not_run(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from file_editor import FileEditorState
    state = tasks.TaskState()
    monkeypatch.setattr(state, 'run_module', lambda *a, **kw: pytest.fail('unexpected run'))
    editor = FileEditorState()
    for view in (None, NS(misc={}, _kwargs={}), NS(misc={'file_editor_state': editor}, _kwargs={})):
        state.run_current_file(view)
        assert 'linked File Editor' in state.error
    editor.selected_path, editor.version = str(tmp_path / 'main.py'), 'HEAD'
    state.run_current_file(NS(misc={'file_editor_state': editor}, _kwargs={}))
    assert 'current Python file' in state.error


def test_generated_commands_keep_script_extension_and_package_context(tmp_path):
    script = tmp_path / 'my.script.py'
    script.write_text('')
    name = tasks.add_module_task(script, tmp_path)
    assert tasks.read_tasks(tmp_path)[name]['cmd'] == 'python my.script.py'
    package = tmp_path / 'pkg'
    package.mkdir()
    (package / '__init__.py').write_text('')
    module = package / 'main.py'
    module.write_text('')
    name = tasks.add_module_task(module, tmp_path)
    assert tasks.read_tasks(tmp_path)[name]['cmd'] == 'python -m pkg.main'
    assert tasks.read_tasks(tmp_path)[name]['module'] == 'pkg/main.py'


def test_current_file_runs_pending_source_in_linked_project(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from file_editor import FileEditorState
    from meltygui.editor.pending_save import PendingSave
    import editor_settings
    monkeypatch.setattr(editor_settings, 'settings', {'Tasks': {'save_tasks': False}})
    root = project(tmp_path, '')
    path = tmp_path / 'sub' / 'main.py'
    path.write_text('raise RuntimeError("stale disk text")\n')
    monkeypatch.setattr(PendingSave, 'current_file_text', lambda path: 'print("linked pending source")\n')
    editor = FileEditorState()
    editor.selected_path = str(path)
    source = NS(misc={'panel_state': NS(selected_project=root)})
    view = NS(misc={'file_editor_state': editor}, _kwargs={'files_view': source})
    assert tasks.current_file_project(view) == root
    state = tasks.TaskState()
    state.run_current_file(view)
    assert state.error is None
    assert wait(state) and state.exit == 0
    assert 'linked pending source' in state.output
    assert state.task == 'Run sub/main.py'
    assert state.selected_tasks[root] == tasks.RUN_CURRENT_FILE
    assert tasks.read_tasks(root)[state.task]['module'] == 'sub/main.py'
