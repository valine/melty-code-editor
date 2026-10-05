"""Full native Python editor lifecycle on the real offscreen host Metal encoder."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from meltygui.platforms.ios import build_directory
EDITOR = Path(__file__).resolve().parents[2]

PROGRAM = r'''
import importlib.abc
import os
from pathlib import Path
import sys
import time

sandbox, artifacts, editor_root = map(Path, sys.argv[1:])
os.chdir(editor_root)
Path.home = classmethod(lambda cls: sandbox)
attempts = []
class NoDesktop(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('OpenGL', 'glfw'):
            attempts.append(name)
            raise AssertionError('Desktop graphics import: ' + name)
sys.meta_path.insert(0, NoDesktop())
import _melty_metal as gpu
gpu._test_initialize(str(artifacts / 'Melty.metallib'), str(artifacts / 'metal-programs.json'))

class Host:
    def request_frame(self): pass
    def set_keyboard_visible(self, visible): pass
    def set_safe_zone(self, inset): pass
    def get_clipboard_text(self): return ''
    def set_clipboard_text(self, text): pass

from meltygui.core.runtime import app
from meltygui.core.runtime.native_app import NativeApplication
from meltygui.core.graphics.metal_renderer import MetalRenderer
native = NativeApplication({'app_id': 'melty-code-editor'}, Host(), lambda: MetalRenderer(gpu))
app.install_native_host(native)
project = sandbox / 'Documents/Projects/Demo'
project.mkdir(parents=True)
source_file = project / 'main.py'
source_file.write_text('def greet(name):\n    return f"Hello, {name}!"\n\nmessage = greet("iOS")\nprint(message)\n')
compile(source_file.read_text(), str(source_file), 'exec')
sys.platform = 'ios'
sys.argv[:] = ['melty-code-editor', str(source_file)]
import editor
app.run()
for index in range(16):
    native.frame({'now': index / 60, 'presentation_time': (index + 1) / 60,
                  'deadline': (index + 1) / 60, 'width': 1024, 'height': 768, 'scale': 2}, [])
    gpu._test_flush()
    native.presented()
    time.sleep(.01)  # let real parser/watcher completions publish between frames
assert not attempts, attempts
assert native.cache.enabled
assert str(source_file.resolve()) in editor.open_files.open_paths
assert native.cache._tiles, 'The editor never populated the retained tile cache'
import numpy as np
pixels = np.frombuffer(gpu._test_pixels(native.renderer.scene_framebuffer), dtype=np.float16)
assert pixels.size == 2048 * 1536 * 4 and np.isfinite(pixels).all()
assert pixels.max() > pixels.min(), 'The scene is uniformly blank'
native.suspend()
assert (sandbox / 'Library/Application Support/melty-code-editor/session.pkl').exists()
native.resume()
native.close()
assert native.closed
'''


@unittest.skipUnless(os.environ.get('MELTY_METAL_TEST') == '1', 'requires compiled host Metal extension')
class EditorMetal(unittest.TestCase):
    def test_editor_code_file_cache_and_checkpoint_without_desktop_graphics(self):
        artifacts = build_directory() / 'metal-test'
        with tempfile.TemporaryDirectory(prefix='melty-ios-editor-') as sandbox:
            env = dict(os.environ)
            env['PYTHONPATH'] = str(artifacts)
            result = subprocess.run([sys.executable, '-c', PROGRAM, sandbox, str(artifacts), str(EDITOR)],
                                    capture_output=True, text=True, close_fds=False, env=env, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
