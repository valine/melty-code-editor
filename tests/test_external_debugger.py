import gc
import os
import shutil
import subprocess
import sys
import time
import weakref

import pytest
import tasks
from meltygui_pro.models import project_execution as execution
from meltygui.core.melty import Melty
from meltygui.model.source_snapshot_model import SourceSnapshot
from test_local_debugger import metadata, pump_until


def launch(tmp_path, source, line=None, python=sys.executable, env=None, name='main.py'):
    path = tmp_path / name
    path.write_text(source)
    snapshot = SourceSnapshot(str(path), source, path.stat().st_mtime)
    state = tasks.TaskState()
    request = dict(root=str(tmp_path), cwd=str(tmp_path), paths=[str(tmp_path)],
                   path=str(path), text=source, package=None, mtime=snapshot.disk_mtime)
    state.run_external(python, request, dict(os.environ, **(env or {})),
        file_metadata=metadata(source, path, line) if line else {}, source_snapshot=snapshot)
    return state, path, snapshot


def test_external_interpreter_and_real_stdout_stderr(tmp_path):
    import venv
    environment = tmp_path / '.venv'
    venv.EnvBuilder(with_pip=False).create(environment)
    python = str(environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))
    state, path, _ = launch(tmp_path,
        'import os, sys\nprint(sys.version_info[:2], os.getcwd(), os.environ["MY_SETTING"])\n'
        'os.write(2, b"native-stderr\\n")\n', python=python, env={'MY_SETTING': 'child-only'})
    try:
        pump_until(lambda: not state.running)
        pump_until(lambda: 'native-stderr' in state.output)
        assert state.error is None, state.error
        assert str(sys.version_info[:2]) in state.output and str(tmp_path) in state.output and 'child-only' in state.output
        assert state._external.capabilities['executable'] == python
        assert 'MY_SETTING' not in os.environ
    finally:
        state.shutdown()


def test_project_venv_dependencies_are_not_indexed(tmp_path):
    import venv
    environment = tmp_path / '.venv'
    venv.EnvBuilder(with_pip=False).create(environment)
    library = environment / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
    (library / 'installed_dependency.py').write_text('def value():\n    return 17\n')
    (tmp_path / 'helper.py').write_text('from installed_dependency import value\nresult = value()\n')
    state, path, _ = launch(tmp_path, 'from helper import result\nprint(result)\n', 2,
                            python=str(environment / 'bin' / 'python'))
    try:
        pump_until(lambda: state.paused or not state.running)
        assert state.paused, (state.error, state.output)
        assert state.selected_scope.locals['result'] == 17
        assert set(state._sources) == {str(path), str(tmp_path / 'helper.py')}
        state.resume()
        pump_until(lambda: not state.running)
        assert state.error is None
    finally:
        state.shutdown()


def test_implicit_melty_loop_breakpoint_and_step(tmp_path):
    source = ('from meltygui.core.runtime import app\n'
              'def render():\n    value = 10\n    value += 1\n    return value\n'
              'app.run = render\napp._hook_main_return()\n')
    state, _, _ = launch(tmp_path, source, 4, python=sys.executable)
    try:
        pump_until(lambda: state.paused or not state.running)
        assert state.paused, (state.error, state.output)
        assert state.selected_scope.name == 'render'
        assert state.selected_scope.locals['value'] == 10
        state.resume('step_over')
        pump_until(lambda: state.paused and state.selected_scope.line == 5)
        assert state.selected_scope.locals['value'] == 11
        state.resume()
        pump_until(lambda: not state.running)
        assert state.error is None
    finally:
        state.shutdown()


def test_pause_alias_binding_resume_and_lifetime(tmp_path):
    state, path, snapshot = launch(tmp_path,
        'value = [1]\nalias = value\nvalue.append(2)\nvalue = [9]\nprint("finished")\n', 3)
    try:
        pump_until(lambda: state.paused or not state.running)
        assert state.paused, (state.error, state.output)
        value = state.selected_scope.locals['value']
        assert value is state.selected_scope.locals['alias']
        assert state.for_source(str(path), snapshot.text).live_store
        with pytest.raises(TypeError):
            state.selected_scope.locals['value'] = 0
        value.request()
        pump_until(lambda: not value.pending)
        assert dict(value.fields) == {'0': 1}
        state.resume('step_over')
        pump_until(lambda: state.paused and state.selected_scope.line == 4)
        assert state.selected_scope.locals['value'] is value
        value.request(refresh=True)
        pump_until(lambda: not value.pending)
        assert dict(value.fields) == {'0': 1, '1': 2}
        state.resume()
        pump_until(lambda: not state.running)
        assert state._external.process.poll() is None  # leases retain producer after execution
        state.replace_inspection(None)
        value.request(refresh=True)
        pump_until(lambda: not value.pending)
        assert dict(value.fields) == {'0': 1, '1': 2}
        del value
        gc.collect()
        pump_until(lambda: state._external.process.poll() is not None)
    finally:
        state.shutdown()


def test_import_breakpoint_step_in_out_and_dynamic_keys(tmp_path):
    helper = tmp_path / 'helper.py'
    helper_text = 'def compute(x):\n    y = x + 1\n    return y\n'
    helper.write_text(helper_text)
    state, path, snapshot = launch(tmp_path, 'from helper import compute\na = compute(4)\nb = a + 1\n', 2)
    state._breakpoint_metadata.update(metadata(helper_text, helper, 2))
    try:
        pump_until(lambda: state.paused or not state.running)
        assert state.paused, state.error
        state.resume('step_into')
        pump_until(lambda: state.paused and state.selected_scope.name == 'compute')
        assert state.selected_scope.locals['x'] == 4
        assert state.selected_scope.source.path == str(helper)
        state.resume('step_out')
        pump_until(lambda: state.paused and state.selected_scope.line == 3 and state.selected_scope.name == '<module>')
        assert state.selected_scope.locals['a'] == 5
        state.resume()
        pump_until(lambda: not state.running)
    finally:
        state.shutdown()


def test_cooperative_stop_unbroken_loop(tmp_path):
    state, _, _ = launch(tmp_path, 'x = 0\nwhile True:\n    x += 1\n')
    try:
        pump_until(lambda: bool(state._sources) or not state.running)
        state.stop()
        pump_until(lambda: not state.running)
        assert state.exit == -1 and state.error is None, state.error
    finally:
        state.shutdown()


def test_force_stop_invalidates_retained_values(tmp_path):
    state, _, _ = launch(tmp_path, 'value = []\nvalue.append(1)\n', 2)
    try:
        pump_until(lambda: state.paused or not state.running)
        value = state.selected_scope.locals['value']
        state.force_stop()
        pump_until(lambda: not value.owner.available and not state.running)
        assert value.error
        assert state.error is None
    finally:
        state.shutdown()


def test_old_stop_cannot_resume_new_pause(tmp_path):
    state, _, _ = launch(tmp_path, 'value = []\nvalue.append(1)\nvalue.append(2)\n', 2)
    try:
        pump_until(lambda: state.paused)
        old = state.inspection.stop
        state.resume('step_over')
        pump_until(lambda: state.paused and state.inspection.stop > old)
        state._external.send({'op': 'resume', 'stop': old, 'mode': 'continue'})
        time.sleep(.03)
        Melty._drain_render_tasks()
        assert state.paused and state.selected_scope.line == 3
    finally:
        state.shutdown()


def test_unsupported_python_reports_capability_error(tmp_path):
    python = os.environ.get('MELTY_TEST_OLD_PYTHON') or shutil.which('python3.11') or shutil.which('python3')
    if python is None:
        pytest.skip('Python older than 3.12 is not installed')
    version = subprocess.check_output([python, '-I', '-c',
        'import sys; print(sys.version_info >= (3, 12))'], text=True, close_fds=False).strip()
    if version == 'True':
        pytest.skip('Set MELTY_TEST_OLD_PYTHON to a Python older than 3.12')
    state, _, _ = launch(tmp_path, 'x = 1\n', python=python)
    try:
        pump_until(lambda: not state.running)
        assert 'Python 3.12' in state.error
    finally:
        state.shutdown()


def test_rejects_mismatched_source_before_launch(tmp_path):
    path = str(tmp_path / 'main.py')
    state = tasks.TaskState()
    with pytest.raises(ValueError, match='source version'):
        state.run_external(sys.executable, dict(path=path, text='x = 2\n'),
                           source_snapshot=SourceSnapshot(path, 'x = 1\n'))
    assert state.process is None


def test_task_honors_selected_interpreter_cwd_environment_and_pending_source(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import meltygui_pro.models.execution_targets as targets
    import meltygui_pro.models.project_analysis as analysis
    path = tmp_path / 'main.py'
    path.write_text('print("old disk source")\n')
    cwd = tmp_path / 'working'
    cwd.mkdir()
    source = 'import os,sys\nprint("pending source", sys.version_info[:2], os.getcwd(), os.environ["TASK_TEST"])\n'
    monkeypatch.setattr(targets, 'resolve_environment', lambda root, target=None: (sys.executable, None))
    monkeypatch.setattr(analysis, 'analysis_project', lambda **kw: SimpleNamespace(source_paths=[str(tmp_path)]))
    monkeypatch.setattr(execution, 'task_environment', lambda root, target=None: dict(os.environ))
    monkeypatch.setattr(tasks, 'read_tasks', lambda root: {'example': dict(module='main.py', cmd='main.py',
        cwd='working', env={'TASK_TEST': 'selected'})})
    state = tasks.TaskState()
    try:
        state.run(str(tmp_path), 'example', debug=True, source_snapshot=SourceSnapshot(str(path), source))
        pump_until(lambda: not state.running and 'pending source' in state.output)
        assert state.error is None, state.error
        assert str(sys.version_info[:2]) in state.output and str(cwd) in state.output and 'selected' in state.output
        assert 'old disk source' not in state.output
    finally:
        state.shutdown()


@pytest.mark.parametrize('pending', [False, True])
def test_task_debug_highlight_matches_editor_source_version(tmp_path, monkeypatch, pending):
    from types import SimpleNamespace
    from meltygui.code.fileref import Address
    from meltygui.code.new_codecs import TextFileCodec
    from meltygui.editor.pending_save import PendingSave
    import meltygui_pro.models.execution_targets as targets
    import meltygui_pro.models.project_analysis as analysis

    path = tmp_path / 'main.py'
    path.write_text('value = 1\nprint(value)\n')
    address = Address(path)
    original = TextFileCodec.load(address)
    if pending:
        PendingSave.mark_load(address, original, codec=TextFileCodec)
        PendingSave.queue_save(address, TextFileCodec, data='value = 2\nprint(value)\n', wake=False)
    editor_text = TextFileCodec.load(address)
    monkeypatch.setattr(targets, 'resolve_environment', lambda root, target=None: (sys.executable, None))
    monkeypatch.setattr(analysis, 'analysis_project', lambda **kw: SimpleNamespace(source_paths=[str(tmp_path)]))
    monkeypatch.setattr(execution, 'task_environment', lambda root, target=None: dict(os.environ))
    monkeypatch.setattr(tasks, 'read_tasks', lambda root: {'example': dict(module='main.py', cmd='main.py',
        cwd='.', env={})})
    state = tasks.TaskState()
    try:
        # The Tasks button supplies no editor-owned SourceSnapshot.
        state.run(str(tmp_path), 'example', debug=True, file_metadata=metadata(editor_text, path, 2))
        pump_until(lambda: state.paused or not state.running)
        assert state.paused, (state.error, state.output)
        inspection = state.for_source(str(path), editor_text)
        assert inspection is not None
        assert inspection.execution_key == state.selected_scope.execution_key
        assert state.selected_scope.source.index.sites[inspection.execution_key].start_line == 2
        assert state.selected_scope.locals['value'] == (2 if pending else 1)
        assert state.for_source(str(path), editor_text + '# later edit\n') is None
        assert path.read_text() == 'value = 1\nprint(value)\n'
    finally:
        state.shutdown()
        if pending:
            PendingSave.discard_entry_for(address)
