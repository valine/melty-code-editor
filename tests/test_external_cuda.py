"""Real driver IPC, no editor torch import; only rendered pixels leave the GPU."""
import gc
import os
import sys

import numpy as np
import pytest

from meltygui.core.graphics.cuda_context_core import using_device
from meltygui.model.cuda_buffer_model import CudaBuffer, empty_image_buffer, parameter_buffer
from meltygui.model.tensor_model import slice_volume_view
from meltygui.view.voxel_cuda_view import march
from meltygui_pro.models.reference_model import RemoteCudaBuffer
from test_external_debugger import launch
from test_local_debugger import pump_until


def image(buffer):
    """Read just the renderer's small output image for pixel comparisons."""
    import meltygui_pycuda.driver as cuda
    pixels = np.empty(buffer.shape, dtype=np.float16)
    with using_device(buffer.device.index):
        cuda.memcpy_dtoh(pixels, buffer.data_ptr())
    return pixels


@pytest.mark.parametrize('python', [sys.executable, *filter(None, os.environ.get('MELTY_TEST_PROJECT_PYTHONS', '').split(os.pathsep))])
def test_cuda_reference_renders_without_editor_torch(tmp_path, monkeypatch, python):
    # Loading a GUI module/renderer must not pull torch into this interpreter.
    import builtins
    original = builtins.__import__
    def no_torch(name, *args, **kwargs):
        if name == 'torch' or name.startswith('torch.'):
            raise AssertionError('editor imported torch for a shared buffer')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_torch)
    source = ('import torch\n'
        'value = torch.ones((8, 9, 10), device="cuda")\n'
        'alias = value; view_alias = value[:, :, ::2]\n'
        'value.add_(1)\n'
        'value = None\n')
    state, _, _ = launch(tmp_path, source, 4, python=python)
    try:
        pump_until(lambda: state.paused or not state.running, seconds=45)
        assert state.paused, state.error
        remote = state.selected_scope.locals['value']
        assert isinstance(remote, RemoteCudaBuffer)
        assert remote is state.selected_scope.locals['alias']
        with pytest.raises(RuntimeError, match='Opening'):
            remote.resolve()
        pump_until(lambda: remote._shared is not None or remote.error is not None, seconds=15)
        assert remote.error is None, remote.error
        buffer = remote.resolve()
        assert buffer.shape == (8, 9, 10)
        alias = state.selected_scope.locals['view_alias']
        with pytest.raises(RuntimeError, match='Opening'):
            alias.resolve()
        pump_until(lambda: alias._shared is not None or alias.error, seconds=15)
        assert not alias.error, alias.error
        assert alias.resolve().owner.memory.allocation is buffer.owner.memory.allocation
        del alias
        cv = slice_volume_view(buffer)
        out = empty_image_buffer((24, 32, 4), 'float16', buffer.device)
        lut = parameter_buffer([[0., 0., 1.], [1., 0., 0.]], buffer.device)
        march(cv.view, out, lut, display_shape=cv.shape)
        assert np.abs(image(out)).sum() > 0
        pointer = buffer.data_ptr()
        state.resume('step_over')
        pump_until(lambda: state.paused and state.selected_scope.line == 5)
        assert state.selected_scope.locals['alias'] is remote
        assert remote.resolve().data_ptr() == pointer
        state.resume()
        pump_until(lambda: not state.running)
        state.replace_inspection(None)
        # An independently retained slice keeps the allocation/producer alive.
        sliced = buffer[:, :, ::2]
        del remote, buffer, cv
        gc.collect()
        assert state._external.process.poll() is None
        march(sliced, out, lut, display_shape=sliced.shape)
        assert np.abs(image(out)).sum() > 0
        del sliced
        gc.collect()
        pump_until(lambda: state._external.process.poll() is not None, seconds=10)
    finally:
        state.shutdown()


def test_allocation_survives_producer_storage_resize(tmp_path):
    source = ('import torch\n'
        'value = torch.ones((8, 8, 8), device="cuda")\n'
        'value.untyped_storage().resize_(2000000)\n'
        'print("resized")\n')
    state, _, _ = launch(tmp_path, source, 3, python=sys.executable)
    try:
        pump_until(lambda: state.paused or not state.running, seconds=15)
        assert state.paused, state.error
        remote = state.selected_scope.locals['value']
        with pytest.raises(RuntimeError):
            remote.resolve()
        pump_until(lambda: remote._shared is not None or remote.error, seconds=15)
        assert not remote.error, remote.error
        buffer = remote.resolve()
        state.resume()
        pump_until(lambda: not state.running, seconds=15)
        assert state.error is None, state.error
        out = empty_image_buffer((16, 16, 4), 'float16', buffer.device)
        lut = parameter_buffer([[0., 0., 1.], [1., 0., 0.]], buffer.device)
        march(buffer, out, lut, display_shape=buffer.shape)
        assert np.abs(image(out)).sum() > 0
        sliced = buffer[:, :, ::2]
        state.force_stop()
        with pytest.raises(RuntimeError, match='unavailable'):
            sliced.data_ptr()
    finally:
        state.shutdown()
