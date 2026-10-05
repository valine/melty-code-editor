"""Opt-in on-device package and real-editor smoke entry point.

Stage with --device-check and generate with --entry-module
device_dependency_check. All test state stays in Library/Caches.
"""
import importlib
import io
import json
from pathlib import Path
import ssl
import sys
import time


def create_app(config, host):
    assert sys.platform == 'ios', sys.platform
    sandbox = Path(config['cache']) / 'dependency-check'
    sandbox.mkdir(parents=True, exist_ok=True)
    # Isolate editor/session writes from the user's normal application state.
    Path.home = classmethod(lambda cls: sandbox)
    project = sandbox / 'Documents/Projects/DependencyCheck'
    project.mkdir(parents=True, exist_ok=True)
    source = project / 'main.py'
    source.write_text('def answer():\n    return 42\n\nvalue = answer()\nprint("on-device Python", value)\n')
    modules = ('numpy', 'PIL.Image', 'cffi', 'libcst', 'yaml_ft', 'jedi', 'pygments', 'freetype', 'rtree',
               'pydantic', 'jiter', 'rpds', 'cryptography', 'jwt', 'anthropic', 'httpx', 'httpx2', 'mcp',
               'rich', 'symspellpy', 'tomlkit', 'watchdog.observers', 'pyte', 'uvicorn', 'jsonschema')
    for module in modules:
        importlib.import_module(module)
        print(f'device dependency OK: {module}')
    import numpy as np
    assert np.linalg.det(np.eye(3)) == 1.0
    assert (np.arange(3) @ np.arange(3)) == 5
    from PIL import Image
    image = Image.new('RGB', (2, 2), (12, 34, 56))
    data = io.BytesIO()
    image.save(data, format='PNG')
    data.seek(0)
    assert Image.open(data).getpixel((0, 0)) == (12, 34, 56)
    from cffi import FFI
    assert FFI().sizeof('int') == 4
    import libcst
    assert libcst.parse_module(source.read_text()).code == source.read_text()
    import yaml_ft
    assert yaml_ft.safe_load('value: 42')['value'] == 42
    import freetype
    font = Path(importlib.util.find_spec('meltygui').origin).parent / 'resources/dejavu/DejaVuSans.ttf'
    face = freetype.Face(str(font))
    face.set_pixel_sizes(0, 16)
    face.load_char('A')
    assert face.glyph.bitmap.width > 0
    from rtree.index import Index
    index = Index()
    try:
        index.insert(7, (0, 0, 1, 1))
        assert list(index.intersection((0, 0, 2, 2))) == [7]
    finally:
        index.close()
    from pydantic import BaseModel
    class Value(BaseModel):
        value: int
    assert Value.model_validate_json('{"value":42}').value == 42
    import jiter
    assert jiter.from_json(b'{"value":42}') == {'value': 42}
    from rpds import HashTrieMap
    assert HashTrieMap({'value': 42})['value'] == 42
    import jwt
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    token = jwt.encode({'value': 42}, key, algorithm='EdDSA')
    assert jwt.decode(token, key.public_key(), algorithms=['EdDSA'])['value'] == 42
    from cryptography.fernet import Fernet
    cipher = Fernet(Fernet.generate_key())
    assert cipher.decrypt(cipher.encrypt(b'melty-ios')) == b'melty-ios'
    assert ssl.create_default_context().cert_store_stats()['x509_ca'] > 0
    namespace = {}
    exec(compile(source.read_text(), str(source), 'exec'), namespace)
    assert namespace['value'] == 42
    print('device dependency APIs and compile/exec OK')
    sys.argv[:] = ['melty-code-editor', str(source)]
    from melty_ios_app import create_app as create_editor
    application = create_editor(config, host)
    from meltygui.code import libcst_conversion as conversion
    completion = conversion._submit_interactive(conversion._jedi_complete_worker,
                                                 'text = "hello"\ntext.up', 2, 7, str(source))
    assert ('upper', 'function') in completion.result(timeout=15)
    print('device Jedi completion OK')
    return CheckedApplication(application, source, sandbox)


class CheckedApplication:
    def __init__(self, application, source, sandbox):
        self.application, self.source, self.sandbox = application, source, sandbox
        self.task = None
        self.started = time.monotonic()
        self.done = False

    def frame(self, info, events):
        more = self.application.frame(info, events)
        if self.task is None:
            import tasks
            self.task = tasks.TaskState()
            name = tasks.add_module_task(self.source, str(self.source.parent))
            self.task.run(str(self.source.parent), name)
        elif not self.done and not self.task.running and self.application.frames >= 4:
            assert self.task.exit == 0, self.task.output
            assert self.task.selected_scope.locals['value'] == 42
            assert self.application.cache._tiles, 'Editor did not create retained tiles'
            result = dict(python=sys.version, frames=self.application.frames, local_execution=True,
                          editor_source=str(self.source), status='passed')
            (self.sandbox / 'result.json').write_text(json.dumps(result, indent=2))
            print('DEVICE DEPENDENCY CHECK PASSED: native imports, real editor frames, local file execution')
            self.done = True
        if not self.done and time.monotonic() - self.started > 30:
            raise TimeoutError('Device editor/task smoke check did not finish')
        return more or not self.done

    def presented(self):
        self.application.presented()

    def suspend(self):
        self.application.suspend()

    def resume(self):
        self.application.resume()

    def close(self):
        if self.task:
            self.task.stop()
        self.application.close()
