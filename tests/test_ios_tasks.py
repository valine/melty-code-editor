"""Embedded execution behavior on the host; these do not claim device coverage."""
import os
import sys
import threading
import time

import pytest

import meltygui_pro  # Registers project services before importing the app.
import tasks
from meltygui.core.melty import Melty
from meltygui.model.source_snapshot_model import SourceSnapshot


def pump_until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        Melty._drain_render_tasks()
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('Timed out waiting for local task event')


@pytest.fixture(autouse=True)
def ios_tasks(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'platform', 'ios')
    monkeypatch.setattr(tasks, 'project_tasks', tasks.ProjectTasks())
    monkeypatch.setattr(tasks, '_last', None)
    monkeypatch.setattr(tasks, '_pending', None)
    sessions = []
    yield sessions
    for state in sessions:
        state.stop()
    pump_until(lambda: all(not state.running for state in sessions))
    for name, module in tuple(sys.modules.items()):
        if str(getattr(module, '__file__', '')).startswith(str(tmp_path) + os.sep):
            del sys.modules[name]


def session(sessions):
    state = tasks.TaskState()
    sessions.append(state)
    return state


def module_task(root, source='value = 1\n', name='main.py'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path, tasks.add_module_task(path, str(root))


def test_ios_run_pending_source_with_package_imports_and_real_values(tmp_path, monkeypatch, ios_tasks):
    package = tmp_path / 'src' / 'ios_task_package'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'helper.py').write_text('answer = 42\nfrom . import peer\nprint("helper output")\n')
    (package / 'peer.py').write_text('from .helper import answer\nvalue = answer + 1\n')
    path, name = module_task(tmp_path, 'raise RuntimeError("stale disk")\n',
                             'src/ios_task_package/main.py')
    pending = ('from . import helper\nimport sys\n'
               'print(__name__, helper.answer, helper.peer.value)\n'
               'sys.stdout.write("stdout output\\n")\n'
               'sys.stderr.write("stderr output\\n")\n'
               'value = [helper.answer]\n')
    from meltygui.editor.pending_save import PendingSave
    monkeypatch.setattr(PendingSave, 'current_file_text', lambda filename: pending)
    cwd, environment, paths = os.getcwd(), dict(os.environ), list(sys.path)
    streams, finders = (sys.stdout, sys.stderr, sys.stdin), list(sys.meta_path)
    state = session(ios_tasks)
    state.run(str(tmp_path), name)
    pump_until(lambda: not state.running)
    assert state.exit == 0, state.output
    assert not state.debug_enabled and state._local_execution
    assert 'helper output' in state.output and '__main__ 42 43' in state.output
    assert 'stdout output' in state.output and 'stderr output' in state.output
    assert 'stale disk' not in state.output
    assert state.selected_scope.locals['value'] == [42]
    assert state.selected_scope.locals['helper'] is sys.modules['ios_task_package.helper']
    assert not state.inspection.current
    assert (os.getcwd(), dict(os.environ), sys.path) == (cwd, environment, paths)
    assert (sys.stdout, sys.stderr, sys.stdin) == streams
    assert sys.meta_path == finders and state._monitoring_tool is None
    tasks.request_rerun()
    assert tasks.pending_run() == (str(tmp_path), name)


def test_ios_debug_uses_exact_snapshot_and_retains_references(tmp_path, ios_tasks):
    from test_local_debugger import metadata
    path, name = module_task(tmp_path, 'raise RuntimeError("stale disk")\n')
    text = 'value = []\nvalue.append(3)\nvalue = [4]\n'
    snapshot = SourceSnapshot(str(path), text)
    state = session(ios_tasks)
    state.run(str(tmp_path), name, debug=True,
              file_metadata=metadata(text, path, 2), source_snapshot=snapshot)
    pump_until(lambda: state.paused or not state.running)
    assert state.paused, state.output
    assert state._debug_source is snapshot
    value = state.selected_scope.locals['value']
    state.resume('step_over')
    pump_until(lambda: state.paused and state.selected_scope.line == 3)
    assert state.selected_scope.locals['value'] is value and value == [3]
    state.resume()
    pump_until(lambda: not state.running)
    assert state.exit == 0 and value == [3]


def test_run_ignores_breakpoints_and_reads_console_input(tmp_path, ios_tasks):
    from test_local_debugger import metadata
    text = 'name = input("Name: ")\nprint("Hello", name)\n'
    path, name = module_task(tmp_path, text)
    state = session(ios_tasks)
    state.run(str(tmp_path), name, file_metadata=metadata(text, path, 2))
    pump_until(lambda: state.waiting_for_input or not state.running)
    assert state.waiting_for_input, state.output
    assert 'Name: ' in state.output
    state.submit_input('Melty')
    pump_until(lambda: not state.running)
    assert state.exit == 0 and 'Hello Melty' in state.output
    assert state.selected_scope.locals['name'] == 'Melty'
    assert not state.paused and not state.waiting_for_input


def test_console_eof_and_stop_release_waiting_task(tmp_path, ios_tasks):
    state = session(ios_tasks)
    state.run_local('import sys\nvalue = sys.stdin.read()\n', str(tmp_path / 'input.py'), debug=False)
    pump_until(lambda: state.waiting_for_input)
    state.submit_input('one')
    state.submit_input('two')
    state.close_input()
    pump_until(lambda: not state.running)
    assert state.exit == 0 and state.selected_scope.locals['value'] == 'one\ntwo\n'
    state.run_local('input("waiting")\n', str(tmp_path / 'input.py'), debug=False)
    pump_until(lambda: state.waiting_for_input)
    state.stop()
    pump_until(lambda: not state.running)
    assert state.exit < 0 and not state.waiting_for_input, state.output


def test_stop_executing_python_loop_and_rerun_pending_source(tmp_path, ios_tasks):
    path, name = module_task(tmp_path, 'while True: pass\n')
    state = session(ios_tasks)
    state.run(str(tmp_path), name)
    pump_until(lambda: state._executing or not state.running)
    assert state.running, state.output
    old = state.process
    updated = SourceSnapshot(str(path), 'print("new version")\nvalue = 7\n')
    state.run(str(tmp_path), name, source_snapshot=updated)
    pump_until(lambda: not state.running)
    assert state.process is not old and state.exit == 0, state.output
    assert state._debug_source is updated
    assert state.selected_scope.locals['value'] == 7
    state._apply_event(old, 'output', 'old output')
    state._apply_event(old, 'exited', 99)
    assert 'old output' not in state.output and state.exit == 0


def test_blocking_native_call_must_return_before_stop(tmp_path, ios_tasks):
    gate, entered = threading.Event(), threading.Event()
    state = session(ios_tasks)
    state.run_local('entered.set()\ngate.wait()\nvalue = 1\n', str(tmp_path / 'native.py'),
                    namespace={'gate': gate, 'entered': entered}, debug=False)
    try:
        pump_until(entered.is_set)
        state.stop()
        Melty._drain_render_tasks()
        assert state.running
    finally:
        gate.set()
    pump_until(lambda: not state.running)
    assert state.exit < 0, state.output


@pytest.mark.parametrize('debug', [False, True])
def test_stop_allows_python_finally_to_release_resources(tmp_path, ios_tasks, debug):
    entered, cleaned = threading.Event(), []
    state = session(ios_tasks)
    state.run_local('try:\n    entered.set()\n    while True: pass\nfinally:\n    cleaned.append(True)\n',
                    str(tmp_path / 'cleanup.py'), namespace={'entered': entered, 'cleaned': cleaned}, debug=debug)
    pump_until(entered.is_set)
    state.stop()
    pump_until(lambda: not state.running)
    assert state.exit < 0 and cleaned == [True], state.output


@pytest.mark.parametrize('source, code, expected', [
    ('def broken(:\n', 1, 'invalid syntax'),
    ('raise ValueError("runtime failure")\n', 1, 'runtime failure'),
    ('raise SystemExit(7)\n', 7, None),
])
def test_source_errors_and_exit_cleanup(tmp_path, ios_tasks, source, code, expected):
    path, name = module_task(tmp_path, source)
    state = session(ios_tasks)
    state.run(str(tmp_path), name)
    pump_until(lambda: not state.running)
    assert state.exit == code
    if expected is not None:
        assert expected in state.output and str(path) in state.output
    assert state._monitoring_tool is None and not tasks._LOCAL_CONTEXT_USERS
    assert tasks._LOCAL_FINDER not in sys.meta_path


@pytest.mark.parametrize('entry, expected', [
    ({'cmd': 'echo unavailable', 'cwd': '.', 'env': {}}, 'shell commands'),
    ({'cmd': 'python main.py', 'module': 'main.py', 'cwd': '.', 'env': {'X': '1'}}, 'environment'),
    ({'cmd': 'python main.py', 'module': 'main.py', 'cwd': 'other', 'env': {}}, 'working directory'),
])
def test_unsupported_task_capabilities_refused(tmp_path, monkeypatch, ios_tasks, entry, expected):
    (tmp_path / 'main.py').write_text('print("must not run")\n')
    (tmp_path / 'other').mkdir()
    tasks.project_tasks.projects[str(tmp_path)] = {'probe': entry}
    def forbidden(*args, **kwargs):
        raise AssertionError('iOS attempted desktop execution')
    monkeypatch.setattr(tasks.TaskState, '_start_external_debug', forbidden)
    monkeypatch.setattr(tasks, 'module_request', forbidden)
    monkeypatch.setattr(tasks, 'task_environment', forbidden)
    state = session(ios_tasks)
    state.run(str(tmp_path), 'probe', debug=True)
    assert not state.running and expected in state.error
    assert state.process is None


def test_imported_modules_keep_identity_and_conflicting_project_is_refused(tmp_path, ios_tasks):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    (first / 'ios_shared_helper.py').write_text('value = []\n')
    (second / 'ios_shared_helper.py').write_text('value = ["wrong project"]\n')
    paths = [module_task(root, 'import ios_shared_helper\nios_shared_helper.value.append(1)\n')
             for root in (first, second)]
    state = session(ios_tasks)
    state.run(str(first), paths[0][1])
    pump_until(lambda: not state.running)
    assert state.exit == 0, state.output
    module = sys.modules['ios_shared_helper']
    state.run(str(first), paths[0][1])
    pump_until(lambda: not state.running)
    assert state.exit == 0 and module is sys.modules['ios_shared_helper'] and module.value == [1, 1]
    state.run(str(second), paths[1][1])
    pump_until(lambda: not state.running)
    assert state.exit == 1 and 'already loaded' in state.error
    assert module.value == [1, 1]


def test_task_text_output_is_thread_scoped(tmp_path, ios_tasks):
    state = session(ios_tasks)
    gate, entered = threading.Event(), threading.Event()
    state.run_local('print("task only")\nentered.set()\ngate.wait()\n',
                    str(tmp_path / 'output.py'), namespace={'gate': gate, 'entered': entered}, debug=False)
    try:
        pump_until(entered.is_set)
        print('outside task')
    finally:
        gate.set()
    pump_until(lambda: not state.running)
    assert state.exit == 0 and 'task only' in state.output
    assert 'outside task' not in state.output
