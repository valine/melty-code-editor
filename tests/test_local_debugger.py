"""Real monitoring, shallow reference ownership and isolated session events."""
import time
import meltygui_pro
import tasks
from meltygui.core.melty import Melty
from meltygui.code.melty_scan import scan, SourceSiteIndex


def pump_until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        Melty._drain_render_tasks()
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('Timed out waiting for session event')


def metadata(source, path, line):
    annotations = {}
    tree, _, _ = scan(source, site_metadata=annotations)
    index = SourceSiteIndex(source, tree, annotations)
    return {str(path): {'breakpoints': {index.at_line(line)[0].path: {'enabled': True}}}}


def test_breakpoint_steps_and_retains_binding(tmp_path):
    path = tmp_path / 'capture.py'
    source = 'value = [1]\nvalue.append(2)\nvalue = [9]\nprint(value)\n'
    state = tasks.TaskState()
    state.run_local(source, str(path), file_metadata=metadata(source, path, 2))
    try:
        pump_until(lambda: state.paused)
        retained = state.selected_scope.locals['value']
        assert retained == [1]
        assert state.for_source(str(path), source).live_store
        state.resume('step_over')
        pump_until(lambda: state.paused and state.selected_scope.line == 3)
        assert retained == [1, 2]
        state.resume()
        pump_until(lambda: not state.running)
        assert retained == [1, 2]
        assert '[9]' in state.output
        assert state._monitoring_tool is None
    finally:
        state.stop()


def test_step_into_over_out_recursion(tmp_path):
    path = tmp_path / 'calls.py'
    source = 'def f(n):\n    value = n\n    return value\nx = f(3)\ny = x + 1\n'
    state = tasks.TaskState()
    state.run_local(source, str(path), file_metadata=metadata(source, path, 4))
    try:
        pump_until(lambda: state.paused)
        state.resume('step_into')
        pump_until(lambda: state.paused and state.selected_scope.name == 'f')
        assert len(state.inspection.scopes) == 2
        state.resume('step_out')
        pump_until(lambda: state.paused and state.selected_scope.line == 5)
        assert state.selected_scope.locals['x'] == 3
        state.resume()
        pump_until(lambda: not state.running)
    finally:
        state.stop()


def test_stop_unbroken_loop_and_stale_output(tmp_path):
    state = tasks.TaskState()
    state.run_local('while True:\n    value = 1\n', str(tmp_path / 'loop.py'))
    state.stop()
    pump_until(lambda: not state.running)
    assert state.exit < 0
    state._apply_event(object(), 'output', 'stale')
    assert 'stale' not in state.output


def test_two_sessions_do_not_stop_each_other(tmp_path):
    source = 'value = []\nvalue.append(1)\n'
    a, b = tasks.TaskState(), tasks.TaskState()
    for state, name in ((a, 'a'), (b, 'b')):
        path = tmp_path / f'{name}.py'
        state.run_local(source, str(path), file_metadata=metadata(source, path, 2))
    try:
        pump_until(lambda: a.paused and b.paused)
        assert a._monitoring_tool != b._monitoring_tool
        a.resume()
        pump_until(lambda: not a.running)
        assert b.paused
        b.stop()
        pump_until(lambda: not b.running)
    finally:
        a.stop()
        b.stop()


def test_source_provenance_and_dynamic_breakpoints(tmp_path):
    from meltygui.code.chain_converters import DiskSpanText
    path = tmp_path / 'versions.py'
    source = 'value = 1\nvalue += 2\nvalue += 3\n'
    state = tasks.TaskState()
    state.run_local(source, str(path), file_metadata=metadata(source, path, 2), disk_mtime=12.5)
    try:
        pump_until(lambda: state.paused)
        rendered = DiskSpanText(source)
        rendered._disk_span = (str(path), None, None)
        rendered._disk_mtime = 12.5
        assert state.for_source(str(path), rendered) is state.selected_scope
        rendered._disk_mtime = 13.0
        assert state.for_source(str(path), rendered) is None
        rendered._disk_mtime = 12.5
        index = state.selected_scope.source_index
        state.set_breakpoints(str(path), rendered, index,
                              {index.at_line(3)[0].path: {'enabled': True}})
        state.resume()
        pump_until(lambda: state.paused and state.selected_scope.line == 3)
        assert state.selected_scope.locals['value'] == 3
        state.resume()
        pump_until(lambda: not state.running)
    finally:
        state.stop()


def test_live_state_migration_preserves_output():
    state = tasks.TaskState()
    state.output = 'existing output'
    del state.inspection
    del state._resume_event
    state.ensure_runtime()
    assert state.output == 'existing output'
    assert state.inspection is None
    assert state._resume_event is not None


def test_file_editor_prepares_key_index_without_debugger(tmp_path):
    from file_editor import FileEditorState
    from meltygui.model.breakpoint_model import current_breakpoint_index
    state = FileEditorState()
    source = 'def f():\n    value = 2\n    return value\n'
    path = str(tmp_path / 'source.py')
    invalidations = []
    state.prepare_source(source, path, lambda **kwargs: invalidations.append(kwargs))
    pump_until(lambda: state.source_tree(source, path) is not None)
    tree = state.source_tree(source, path)
    index = current_breakpoint_index(tree, source)
    assert ('f', 'locals', 'value') in index.sites
    assert invalidations == [{'frame_delta': 1}]
    assert state.source_tree(source + '\n', path) is None
    state.prepare_source(source, path, lambda **kwargs: invalidations.append(kwargs))
    assert state._source_worker is None


def test_local_failure_and_exit_release_monitoring(tmp_path):
    state = tasks.TaskState()
    state.run_local('raise ValueError("expected probe error")\n', str(tmp_path / 'failure.py'))
    pump_until(lambda: not state.running)
    assert state.exit == 1 and 'expected probe error' in state.output
    assert state._monitoring_tool is None
    state.run_local('raise SystemExit(7)\n', str(tmp_path / 'exit.py'))
    pump_until(lambda: not state.running)
    assert state.exit == 7 and state._monitoring_tool is None
