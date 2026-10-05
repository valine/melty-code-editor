"""Import the real editor through the native owner without desktop graphics.

This checks the Python import/lifecycle boundary on the build host. It does not
substitute for importing the cross-compiled extension wheels on a device.
"""
from pathlib import Path
import subprocess
import sys


def test_editor_registers_native_root_without_desktop_graphics(tmp_path):
    source = Path(__file__).resolve().parents[1]
    program = f'''
import importlib.abc
import os
from pathlib import Path
import sys
os.chdir({str(source)!r})
Path.home = classmethod(lambda cls: Path({str(tmp_path)!r}))
attempts = []
class NoDesktop(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('OpenGL', 'glfw'):
            attempts.append(name)
            raise AssertionError('Desktop graphics import: ' + name)
sys.meta_path.insert(0, NoDesktop())
from meltygui.core.runtime import app
from meltygui.core.runtime.native_app import NativeApplication
class Host:
    def request_frame(self): pass
class Renderer: pass
native = NativeApplication({{'app_id': 'melty-code-editor'}}, Host(), Renderer)
app.install_native_host(native)
# Use host-compatible binary extensions while selecting device Python paths.
sys.platform = 'ios'
sys.argv[:] = ['melty-code-editor']
import editor
assert app._state['native_host'] is native
assert native.ctx is not None
assert len(app._ROOTS) == 1
assert not app._state['ran']  # UIKit, rather than the desktop trace hook, starts frames.
assert not attempts, attempts
assert 'OpenGL.GL' not in sys.modules and 'glfw' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', program], text=True,
                            capture_output=True, close_fds=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
