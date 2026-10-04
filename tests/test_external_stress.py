"""Backpressure, retained runs and independent views under repeated debugger events."""
import gc
import sys
import time

from meltygui.core.melty import Melty
from test_external_debugger import launch
from test_local_debugger import pump_until


def test_high_output_is_bounded_before_render(tmp_path):
    state, _, _ = launch(tmp_path, 'import os\nfor i in range(2000):\n    os.write(1, b"x" * 8192)\nprint("done")\n')
    try:
        pump_until(lambda: bool(state._sources))
        # Deliberately let the producer outrun the UI for a while.
        time.sleep(.2)
        assert len(state._external._output_pending) <= 200000
        pump_until(lambda: not state.running)
        pump_until(lambda: 'done' in state.output)
        assert len(state.output) <= 200000 and state.error is None
    finally:
        state.shutdown()


def test_repeated_stops_share_references_and_hidden_tile_keeps_execution(tmp_path):
    source = 'value = []\nfor i in range(40):\n    value.append(i)\nprint("done")\n'
    state, _, _ = launch(tmp_path, source, 3)
    try:
        pump_until(lambda: state.paused)
        value = state.selected_scope.locals['value']
        state.on_tile_layout_event('removed')
        for stop in range(40):
            assert state.selected_scope.locals['value'] is value
            assert state.selected_scope.locals['i'] == stop
            previous = state.inspection.stop
            state.resume()
            pump_until(lambda: not state.running or state.paused and state.inspection.stop > previous)
        assert not state.running
        value.request()
        pump_until(lambda: not value.pending)
        assert list(value.fields.values()) == list(range(40))
    finally:
        state.shutdown()


def test_replacement_run_cannot_adopt_old_values_or_output(tmp_path):
    state, _, _ = launch(tmp_path, 'value = [1]\nvalue.append(2)\n', 2)
    try:
        pump_until(lambda: state.paused)
        old = state._external
        retained = state.selected_scope.locals['value']
        state.resume()
        pump_until(lambda: not state.running)
        path = tmp_path / 'second.py'
        source = 'value = [9]\nvalue.append(10)\n'
        path.write_text(source)
        from test_local_debugger import metadata
        state.run_external(sys.executable, dict(root=str(tmp_path), cwd=str(tmp_path),
            paths=[str(tmp_path)], path=str(path), text=source), file_metadata=metadata(source, path, 2))
        pump_until(lambda: state.paused)
        new_value = state.selected_scope.locals['value']
        assert new_value is not retained and new_value.owner.identity != retained.owner.identity
        old.post('output', 'stale run output')
        retained.request()
        new_value.request()
        pump_until(lambda: not retained.pending and not new_value.pending)
        assert list(retained.fields.values()) == [1, 2]
        assert list(new_value.fields.values()) == [9]
        assert 'stale run' not in state.output
        del retained
        gc.collect()
        pump_until(lambda: old.process.poll() is not None)
    finally:
        state.shutdown()
