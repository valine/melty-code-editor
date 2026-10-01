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
        assert state.for_source(str(path), rendered) is state.selected_scope.source_inspection
        rendered._disk_mtime = 13.0
        assert state.for_source(str(path), rendered) is None
        rendered._disk_mtime = 12.5
        index = state.selected_scope.source.index
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
    from meltygui_pro.editor.git import GitFile
    from types import SimpleNamespace
    owner = GitFile(SimpleNamespace(root=tmp_path), 'source.py', 'current')
    owner._values['value'] = source
    state._file = owner
    invalidations = []
    state.prepare_source(source, path, lambda **kwargs: invalidations.append(kwargs))
    pump_until(lambda: state.source_tree(source, path) is not None)
    tree = state.source_tree(source, path)
    index = current_breakpoint_index(tree, source)
    assert ('f', 'locals', 'value') in index.sites
    assert invalidations == [{'frame_delta': 1}]
    assert state.source_tree(source + '\n', path) is None
    state.prepare_source(source, path, lambda **kwargs: invalidations.append(kwargs))
    assert owner.source_snapshot()._index_worker is None


def test_local_failure_and_exit_release_monitoring(tmp_path):
    state = tasks.TaskState()
    state.run_local('raise ValueError("expected probe error")\n', str(tmp_path / 'failure.py'))
    pump_until(lambda: not state.running)
    assert state.exit == 1 and 'expected probe error' in state.output
    assert state._monitoring_tool is None
    state.run_local('raise SystemExit(7)\n', str(tmp_path / 'exit.py'))
    pump_until(lambda: not state.running)
    assert state.exit == 7 and state._monitoring_tool is None


def test_metadata_updates_two_sessions_before_first_pause(tmp_path):
    import threading
    from meltygui.models.file_meta import FileMetaProxy
    from meltygui.code.chain_converters import DiskSpanText
    path = tmp_path / 'observe.py'
    source = 'gate.wait()\nvalue = 1\nvalue = 2\n'
    annotations = metadata(source, path, 2)
    store = FileMetaProxy(tmp_path / 'meta.pkl')
    gate = threading.Event()
    sessions = [tasks.TaskState(), tasks.TaskState()]
    try:
        for state in sessions:
            state.run_local(source, str(path), file_metadata=store,
                            namespace={'gate': gate}, disk_mtime=12.5)
        pump_until(lambda: all(s._monitoring_tool is not None for s in sessions))
        assert all(s.inspection is None for s in sessions)
        rendered = DiskSpanText(source)
        rendered._disk_span = (str(path), None, None)
        rendered._disk_mtime = 12.5
        first = sessions[0]
        first.set_breakpoints(str(path), rendered, first._debug_source.index,
                              annotations[str(path)]['breakpoints'])
        assert first._breakpoint_lines == {2}
        store[str(path)] = annotations[str(path)]
        pump_until(lambda: all(s._breakpoint_lines == {2} for s in sessions))
        gate.set()
        pump_until(lambda: all(s.paused for s in sessions))
        for state in sessions:
            state.resume()
        pump_until(lambda: all(not s.running for s in sessions))
        assert all(s._breakpoint_metadata is None for s in sessions)
    finally:
        gate.set()
        for state in sessions:
            state.stop()
        store.flush()


def test_shared_preparation_is_off_thread_and_stop_prevents_execution(tmp_path, monkeypatch):
    import threading
    from meltygui.model.source_snapshot_model import SourceSnapshot
    from meltygui_pro.editor.git import GitFile
    from types import SimpleNamespace
    from file_editor import FileEditorState
    owner = GitFile(SimpleNamespace(root=tmp_path), 'shared.py', 'current')
    owner._values['value'] = 'executed.append(True)\n'
    snapshot = owner.source_snapshot()
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = SourceSnapshot.prepare_index
    def prepare(self):
        calls.append(threading.get_ident())
        entered.set()
        release.wait(5)
        return original(self)
    monkeypatch.setattr(SourceSnapshot, 'prepare_index', prepare)
    view = FileEditorState()
    view._file = owner
    view.prepare_source(snapshot.text, snapshot.path, lambda **kw: None)
    assert entered.wait(2)
    executed = []
    state = tasks.TaskState()
    state.run_local(snapshot.text, snapshot.path, source_snapshot=snapshot,
                    namespace={'executed': executed})
    state.stop()
    release.set()
    pump_until(lambda: not state.running and snapshot._index_worker is None)
    assert executed == []
    assert state._debug_source is snapshot is owner.source_snapshot()
    assert view.source_tree(snapshot.text, snapshot.path) is snapshot.code_tree
    assert all(thread != threading.get_ident() for thread in calls)


def test_preparation_failure_surfaces_and_releases_state(tmp_path):
    state = tasks.TaskState()
    state.run_local('def broken(:\n', str(tmp_path / 'bad.py'))
    pump_until(lambda: not state.running)
    assert state.error
    assert state._monitoring_tool is None
    assert state.exit == 1


def test_captured_scope_reuses_occurrence_maps(tmp_path, monkeypatch):
    import ast
    source = 'value = []\nvalue.append(1)\n'
    state = tasks.TaskState()
    state.run_local(source, str(tmp_path / 'refs.py'),
                    file_metadata=metadata(source, tmp_path / 'refs.py', 2))
    try:
        pump_until(lambda: state.paused)
        scope = state.selected_scope
        def forbidden(*args, **kwargs):
            raise AssertionError('capture traversed the AST')
        monkeypatch.setattr(ast, 'walk', forbidden)
        monkeypatch.setattr(ast, 'iter_child_nodes', forbidden)
        recaptured = tasks.CapturedScope(state._paused_frame, state._debug_source)
        assert recaptured.source_inspection.bindings
        assert any(binding[3] is scope.locals['value']
                   for binding in recaptured.source_inspection.bindings)
        state.resume()
        pump_until(lambda: not state.running)
    finally:
        state.stop()


def test_editor_source_completion_never_replaces_newer_value(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    from meltygui.model.source_snapshot_model import SourceSnapshot
    from meltygui_pro.editor.git import GitFile
    from file_editor import FileEditorState
    owner = GitFile(SimpleNamespace(root=tmp_path), 'edit.py', 'current')
    owner._values['value'] = 'old = 1\n'
    old = owner.source_snapshot()
    entered, release = threading.Event(), threading.Event()
    original = SourceSnapshot.prepare_index
    def prepare(self):
        if self is old:
            entered.set()
            release.wait(5)
        return original(self)
    monkeypatch.setattr(SourceSnapshot, 'prepare_index', prepare)
    state = FileEditorState()
    state._file = owner
    invalidations = []
    state.prepare_source(old.text, old.path, lambda **kw: invalidations.append(kw))
    assert entered.wait(2)
    owner._values['value'] = 'new = 2\n'
    new = owner.source_snapshot()
    release.set()
    pump_until(lambda: old._index_worker is None)
    assert owner.source_snapshot() is new
    assert state.source_tree(new.text, new.path) is None
    assert invalidations  # wake the pane so it can request the new version
    state.prepare_source(new.text, new.path, lambda **kw: None)
    pump_until(lambda: state.source_tree(new.text, new.path) is not None)
    assert state.source_tree(new.text, new.path) is new.code_tree


def test_module_action_targets_its_session_and_private_state_stays_private(tmp_path, monkeypatch):
    from meltygui.core.rendering.injected_state import state_parameters
    path = tmp_path / 'module.py'
    path.write_text('value = 1\n')
    first, selected = tasks.TaskState(), tasks.TaskState()
    launches = []
    monkeypatch.setattr(tasks.TaskState, 'run', lambda self, *args: launches.append((self, args)))
    monkeypatch.setattr(tasks, '_pending', None)
    selected.run_module(str(path), str(tmp_path), debug=True)
    assert len(launches) == 1 and launches[0][0] is selected
    assert first is not selected and tasks._pending is None
    assert 'task_state' in state_parameters(tasks.draw_tasks)
    assert '_task_view_state' not in state_parameters(tasks.draw_tasks)
    assert '_console_view_state' not in state_parameters(tasks.draw_console)


def test_monitoring_restart_preserves_other_tool_registration():
    import sys
    monitoring = sys.monitoring
    tools = []
    try:
        for tool in range(6):
            try:
                monitoring.use_tool_id(tool, 'coexistence-probe')
                tools.append(tool)
                break
            except ValueError:
                pass
        assert tools
        tool = tools[0]
        calls = []
        def other_callback(code, line):
            calls.append(line)
            return monitoring.DISABLE
        def other_work():
            return 1
        monitoring.register_callback(tool, monitoring.events.LINE, other_callback)
        monitoring.set_local_events(tool, other_work.__code__, monitoring.events.LINE)
        other_work()
        count = len(calls)
        other_work()
        assert len(calls) == count
        state = tasks.TaskState()
        state._install_monitoring()
        try:
            state._disabled_lines = True
            state._update_monitoring()
            other_work()
            assert len(calls) > count  # documented CPython cross-tool restart cost
            assert monitoring.get_tool(tool) == 'coexistence-probe'
        finally:
            state._remove_monitoring()
        before = len(calls)
        monitoring.restart_events()
        other_work()
        assert len(calls) > before  # removing our tool preserved the other callback
    finally:
        for tool in tools:
            monitoring.set_local_events(tool, other_work.__code__, 0)
            monitoring.register_callback(tool, monitoring.events.LINE, None)
            monitoring.free_tool_id(tool)


def test_non_debug_inspection_adapts_keyed_references_and_disk_reload(tmp_path):
    from meltygui.code.chain_converters import DiskSpanText
    from meltygui.model.source_snapshot_model import SourceSnapshot, SourceInspection
    from meltygui.model.breakpoint_model import current_breakpoint_index
    path = str(tmp_path / 'producer.py')
    text = DiskSpanText('value = 1\n')
    text._disk_span = (path, None, None)
    text._disk_mtime = 1.0
    snapshot = SourceSnapshot(path, text).prepare()
    occurrence = snapshot.occurrences[snapshot.compiled][1][0]
    key, name, occurrence_id = occurrence
    value = []
    inspection = SourceInspection(snapshot, [('remote-reference', key, name, value, occurrence_id)])
    assert inspection.live_store.__live_values__[('line:1#value',)] is value
    value.append(5)
    assert inspection.live_store.__live_values__[('line:1#value',)] == [5]
    equivalent = DiskSpanText(str(text))
    equivalent._disk_span, equivalent._disk_mtime = text._disk_span, text._disk_mtime
    assert current_breakpoint_index(snapshot.code_tree, equivalent) is snapshot.index
    equivalent._disk_mtime = 2.0
    assert current_breakpoint_index(snapshot.code_tree, equivalent) is None


def test_live_tuple_source_adoption_preserves_monitored_code_and_references(tmp_path):
    import ast
    source = 'value = []\nvalue.append(1)\nvalue.append(2)\n'
    path = str(tmp_path / 'adopt.py')
    state = tasks.TaskState()
    state.run_local(source, path, file_metadata=metadata(source, path, 2))
    try:
        pump_until(lambda: state.paused)
        scope = state.selected_scope
        before = state._debug_source
        codes = tuple(state._debug_codes)
        retained = scope.locals['value']
        store = scope.source_inspection.live_store
        # Recreate the held fields that existed before this definition update.
        scope.source, scope.path = source, path
        scope.live_store = store
        scope.source_index, scope.code_tree = before.index, before.code_tree
        del scope.source_inspection
        state._debug_source = (path, source, before.index, ast.parse(source), before.code_tree, None)
        state.ensure_runtime()
        assert state._debug_source.index is before.index
        assert state._debug_source.compiled is before.compiled
        assert tuple(state._debug_codes) == codes
        assert state.selected_scope is scope
        assert state.for_source(path, source).live_store.__live_values__[('line:2#value',)] is retained
        state.resume('step_over')
        pump_until(lambda: state.paused and state.selected_scope.line == 3)
        assert retained == [1]
        state.resume()
        pump_until(lambda: not state.running)
        assert retained == [1, 2]
    finally:
        state.stop()


def test_popup_action_survives_creating_draw(tmp_path):
    from file_editor import FileEditorState
    state = FileEditorState()
    session = tasks.TaskState()
    action = (session, str(tmp_path / 'file.py'), str(tmp_path), None, object())
    invalidations = []
    callback = lambda: state.request_debug(action, lambda **kw: invalidations.append(kw))
    # A retained popup invokes this only after its creating draw returned.
    assert state._execution_action is None
    callback()
    assert state._execution_action is action
    assert invalidations == [{'frame_delta': 1}]


def test_run_local_rejects_unrelated_prepared_source(tmp_path):
    import pytest
    from meltygui.model.source_snapshot_model import SourceSnapshot
    path = str(tmp_path / 'requested.py')
    source = 'value = 1\n'
    wrong_path = SourceSnapshot(str(tmp_path / 'other.py'), source)
    wrong_text = SourceSnapshot(path, 'unrelated = 2\n')
    state = tasks.TaskState()
    for snapshot in (wrong_path, wrong_text):
        with pytest.raises(ValueError, match='source version'):
            state.run_local(source, path, source_snapshot=snapshot)
        assert not state.running and state.process is None
    snapshot = SourceSnapshot(path, source, disk_mtime=8.0)
    equal_disk_text = source.encode().decode()
    assert equal_disk_text is not source
    state.run_local(equal_disk_text, path, source_snapshot=snapshot, disk_mtime=8.0)
    pump_until(lambda: not state.running)
    assert state.exit == 0 and state._debug_source is snapshot


def test_same_line_lambdas_have_their_own_occurrences(tmp_path):
    import types
    from meltygui.model.source_snapshot_model import SourceSnapshot
    source = 'first, second = lambda left: left + 1, lambda right: right + 2\n'
    snapshot = SourceSnapshot(str(tmp_path / 'lambdas.py'), source).prepare()
    lambdas = [code for code in snapshot.compiled.co_consts
               if isinstance(code, types.CodeType) and code.co_name == '<lambda>']
    assert len(lambdas) == 2
    assert {name for key, name, occurrence in snapshot.occurrences[lambdas[0]][1]} == {'left'}
    assert {name for key, name, occurrence in snapshot.occurrences[lambdas[1]][1]} == {'right'}


def test_nested_same_line_lambda_uses_its_body_span(tmp_path):
    import types
    from meltygui.model.source_snapshot_model import SourceSnapshot
    snapshot = SourceSnapshot(str(tmp_path / 'nested.py'),
                              'nested = lambda outer: lambda inner: inner + outer\n').prepare()
    outer = next(code for code in snapshot.compiled.co_consts if isinstance(code, types.CodeType))
    inner = next(code for code in outer.co_consts if isinstance(code, types.CodeType))
    assert {name for key, name, occurrence in snapshot.occurrences[outer][1]} == {'outer'}
    assert {name for key, name, occurrence in snapshot.occurrences[inner][1]} == {'inner', 'outer'}


def test_switch_file_does_not_wait_for_previous_index(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    from file_editor import FileEditorState
    from meltygui.model.source_snapshot_model import SourceSnapshot
    a = SourceSnapshot(str(tmp_path / 'a.py'), 'a = 1\n')
    b = SourceSnapshot(str(tmp_path / 'b.py'), 'b = 2\n')
    entered, release = threading.Event(), threading.Event()
    original = SourceSnapshot.prepare_index
    def prepare(self):
        if self is a:
            entered.set()
            release.wait(3)
        return original(self)
    monkeypatch.setattr(SourceSnapshot, 'prepare_index', prepare)
    state = FileEditorState()
    notices = []
    state._file = SimpleNamespace(source_snapshot=lambda: a)
    state.prepare_source(a.text, a.path, lambda **kw: notices.append('a'))
    assert entered.wait(2)
    try:
        state._file = SimpleNamespace(source_snapshot=lambda: b)
        state.prepare_source(b.text, b.path, lambda **kw: notices.append('b'))
        pump_until(lambda: 'b' in notices)
        assert not a.index_ready
        assert state.source_tree(b.text, b.path) is b.code_tree
        assert b.compiled is None and not b.ready
    finally:
        release.set()
        pump_until(lambda: a._index_worker is None)


def test_local_launch_uses_supplied_snapshot_without_module_request(tmp_path, monkeypatch):
    import sys
    from meltygui.model.source_snapshot_model import SourceSnapshot
    from meltygui_pro.models import project_function_runner
    source = 'value = 42\n'
    snapshot = SourceSnapshot(str(tmp_path / 'pending.py'), source)
    monkeypatch.setattr(project_function_runner, 'project_python', lambda root: sys.executable)
    monkeypatch.setattr(tasks, 'module_request', lambda *a: (_ for _ in ()).throw(AssertionError('source reread')))
    state = tasks.TaskState()
    state._start_local_debug(str(tmp_path), {'module':'pending.py','cwd':'.','env':{}}, None, snapshot)
    pump_until(lambda: not state.running)
    assert state.exit == 0 and state._debug_source is snapshot


def test_imported_project_breakpoints_steps_and_metadata(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from meltygui.models.file_meta import FileMetaProxy
    package = tmp_path / 'review_package'
    package.mkdir()
    init = package / '__init__.py'
    init.write_text('')
    helper = package / 'helper.py'
    helper_text = 'def compute():\n    value = []\n    value.append(1)\n    value.append(2)\n    return value\n'
    helper.write_text(helper_text)
    spec = importlib.util.spec_from_file_location('review_package', init)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'review_package', module)
    # Imports use the package's ordinary search path, without a global import hook.
    monkeypatch.delitem(sys.modules, 'review_package.helper', raising=False)
    store = FileMetaProxy(tmp_path / 'metadata.pkl')
    store[str(helper)] = metadata(helper_text, helper, 3)[str(helper)]
    state = tasks.TaskState()
    root_path = str(tmp_path / 'main.py')
    source = 'from review_package.helper import compute\nresult = compute()\n'
    state.run_local(source, root_path, file_metadata=store, project_root=str(tmp_path))
    try:
        pump_until(lambda: state.paused)
        assert state.selected_scope.source.path == str(helper)
        assert state.selected_scope.line == 3
        assert {scope.source.path for scope in state.inspection.scopes.values()} == {root_path, str(helper)}
        assert state.for_source(str(helper), state.selected_scope.source.text) is not None
        retained = state.selected_scope.locals['value']
        import pytest
        with pytest.raises(TypeError):
            state.selected_scope.locals['value'] = []
        state.resume('step_over')
        pump_until(lambda: state.paused and state.selected_scope.line == 4)
        assert retained == [1]
        store[str(helper)] = metadata(helper_text, helper, 5)[str(helper)]
        pump_until(lambda: state._breakpoints_by_path[str(helper)] == {5})
        state.resume()
        pump_until(lambda: state.paused and state.selected_scope.line == 5)
        assert retained == [1, 2]
        state.resume('step_out')
        pump_until(lambda: not state.running)
        assert state.exit == 0
        assert not store._path_listeners
    finally:
        state.stop()
        pump_until(lambda: not state.running)
        sys.modules.pop('review_package.helper', None)
        store.flush()


def test_debugger_cleanup_is_not_discovered_as_project_code():
    # Even when the project contains the debugger's own implementation,
    # discovery must not instrument its cleanup or output-posting machinery.
    state = tasks.TaskState()
    state.run_local('print(42)\n', tasks.__file__)
    pump_until(lambda: not state.running)
    assert state.exit == 0 and state.output.endswith('42\n')
    assert state._monitoring_tool is None


def test_unchanged_breakpoints_do_not_restart_monitoring(tmp_path, monkeypatch):
    source = 'value = 1\nvalue = 2\n'
    path = str(tmp_path / 'unchanged.py')
    values = metadata(source, path, 2)
    state = tasks.TaskState()
    state.run_local(source, path, file_metadata=values)
    try:
        pump_until(lambda: state.paused)
        updates = []
        original = state._update_monitoring
        monkeypatch.setattr(state, '_update_monitoring', lambda *a: updates.append(a))
        state._breakpoints_changed()
        assert updates == []
        monkeypatch.setattr(state, '_update_monitoring', original)
        state.resume()
        pump_until(lambda: not state.running)
    finally:
        state.stop()


def test_stale_loaded_helper_is_not_attached_to_new_source(tmp_path):
    helper = tmp_path / 'helper.py'
    original = 'def helper():\n    return 1\n'
    namespace = {}
    exec(compile(original, str(helper), 'exec'), namespace)
    helper.write_text('def helper():\n    return 2\n')
    state = tasks.TaskState()
    state.run_local('print(helper())\n', str(tmp_path / 'main.py'), namespace=namespace)
    pump_until(lambda: not state.running)
    assert state.exit == 0
    assert 'Loaded code does not match source' in state.output
    assert state.output.endswith('1\n')
    assert str(helper) not in state._sources


def test_equal_code_objects_in_different_files_have_independent_breakpoints(tmp_path):
    source = 'def compute():\n    value = 1\n    return value\n'
    namespace = {}
    functions = []
    for name in ('first.py', 'second.py'):
        path = tmp_path / name
        path.write_text(source)
        loaded = {}
        exec(compile(source, str(path), 'exec'), loaded)
        functions.append(loaded['compute'])
    first, second = functions
    assert first.__code__ == second.__code__ and first.__code__ is not second.__code__
    state = tasks.TaskState()
    second_path = tmp_path / 'second.py'
    state.run_local('first()\nsecond()\n', str(tmp_path / 'main.py'),
                    namespace={'first': first, 'second': second},
                    file_metadata=metadata(source, second_path, 2))
    try:
        pump_until(lambda: state.paused)
        assert state.selected_scope.source.path == str(second_path)
        assert state._code_sources[first.__code__].path == str(tmp_path / 'first.py')
        assert state._code_sources[second.__code__].path == str(second_path)
        state.resume()
        pump_until(lambda: not state.running)
        assert state.exit == 0
    finally:
        state.stop()
        pump_until(lambda: not state.running)


def test_adopt_pre_multifile_session_preserves_pause_and_references(tmp_path):
    source = 'value = []\nvalue.append(1)\nvalue.append(2)\n'
    path = str(tmp_path / 'live.py')
    state = tasks.TaskState()
    state.run_local(source, path, file_metadata=metadata(source, path, 2))
    try:
        pump_until(lambda: state.paused)
        retained = state.selected_scope.locals['value']
        execution = state.process
        # The previous implementation had code-keyed dicts and no source registry.
        state._debug_codes = dict(state._debug_codes)
        for name in ('_sources', '_code_sources', '_line_codes', '_breakpoints_by_path',
                     '_source_callbacks', '_project_root', '_executing'):
            delattr(state, name)
        state.ensure_runtime()
        pump_until(lambda: path in state._line_codes)
        assert state.paused and state.process is execution
        assert state.selected_scope.locals['value'] is retained
        state.resume('step_over')
        pump_until(lambda: state.paused and state.selected_scope.line == 3)
        assert retained == [1]
        state.resume()
        pump_until(lambda: not state.running)
    finally:
        state.stop()
        pump_until(lambda: not state.running)
