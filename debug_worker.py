"""GUI-free debugger inside the project's interpreter. Launched with -I."""
import ast
import importlib.util
import json
import os
import socket
import sys
import sysconfig
import threading
import tokenize
import traceback
import types


class Cancelled(BaseException):
    pass


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Execution:
    def __init__(self, request, stream, wire):
        self.request, self.stream, self.wire = request, stream, wire
        cuda = load_file('_melty_cuda_shared_memory', request['cuda_sharing'])
        self.exports = wire.Exports(buffer_export=cuda.TensorExport)
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.resume_event = threading.Event()
        self.source_events = {}
        self.sources, self.codes = {}, {}  # code id -> strong code/source record
        self.line_codes = {}
        self.breakpoints = {}
        self.source_root = os.path.realpath(request['root']) + os.sep
        # A project's environment often lives under its source root. Installed
        # dependencies are not project code: importing torch must not compile,
        # index and acknowledge every file in site-packages before it can run.
        self.library_roots = tuple({os.path.realpath(sysconfig.get_path(name)) + os.sep
                                    for name in ('purelib', 'platlib', 'stdlib', 'platstdlib')})
        self.thread_id = threading.get_ident()
        self.tool = None
        self.mode, self.step_frame = 'continue', None
        self.stop_id, self.paused = 0, False
        self.cancelled = self.finished = self.closed = self.disabled = False

    def send(self, kind, **message):
        with self.send_lock:
            self.wire.send_packet(self.stream, dict(kind=kind, run=self.request['run'], **message))

    def commands(self):
        try:
            while not self.closed:
                command = self.wire.receive_packet(self.stream)
                if command.get('run') != self.request['run']:
                    continue
                op = command['op']
                if op == 'breakpoints':
                    path = command['path']
                    previous = self.breakpoints.get(path, set())
                    lines = self.breakpoints[path] = set(command['lines'])
                    affected = {identity for line in previous ^ lines
                                for identity in self.line_codes.get(path, {}).get(line, ())}
                    if affected:
                        self.update(identities=affected)
                    event = self.source_events.get(path)
                    if event:
                        event.set()
                elif op == 'resume':
                    if self.paused and command['stop'] == self.stop_id:
                        self.mode = command['mode']
                        self.resume_event.set()
                elif op == 'stop':
                    self.cancelled = True
                    self.update()
                    self.resume_event.set()
                    for event in self.source_events.values():
                        event.set()
                elif op == 'expand':
                    try:
                        with self.lock:
                            result = self.exports.expand(command['ref'], command['offset'])
                        self.send('fields', ref=command['ref'], generation=command['generation'],
                                  offset=command['offset'], **result)
                    except Exception as error:
                        self.send('fields', ref=command['ref'], generation=command['generation'],
                                  error=f'{type(error).__name__}: {error}')
                elif op == 'release':
                    with self.lock:
                        self.exports.release(command['tokens'])
                    if self.finished and not self.exports.entries:
                        self.closed = True
                elif op == 'buffer':
                    try:
                        with self.lock:
                            result = self.exports.buffer(command['ref'])
                        self.send('buffer', ref=command['ref'], generation=command['generation'], **result)
                    except Exception as error:
                        self.send('buffer', ref=command['ref'], generation=command['generation'],
                                  error=f'{type(error).__name__}: {error}; the tensor was not copied')
                elif op == 'close':
                    self.closed = self.cancelled = True
                    self.resume_event.set()
                    for event in self.source_events.values():
                        event.set()
                if self.closed:
                    break
        except (EOFError, OSError, ValueError):
            self.closed = self.cancelled = True
            self.resume_event.set()
            for event in self.source_events.values():
                event.set()

    def register(self, code, text=None, mtime=None):
        path = os.path.realpath(code.co_filename)
        if path in self.sources:
            return
        if text is None:
            before = os.stat(path).st_mtime_ns
            with tokenize.open(path) as source_file:
                text = source_file.read()
            if before != os.stat(path).st_mtime_ns:
                raise ValueError('Source changed while loading it')
            # Loaded bytecode can predate the source on disk. Never place a
            # breakpoint on a different version of an imported module.
            expected = compile(text, path, 'exec', dont_inherit=True)
            candidates = []
            def collect(current):
                candidates.append(current)
                for child in current.co_consts:
                    if isinstance(child, types.CodeType):
                        collect(child)
            collect(expected)
            if not any(code == candidate for candidate in candidates):
                raise ValueError('Loaded bytecode does not match source on disk')
            mtime = os.stat(path).st_mtime
        tree = ast.parse(text, filename=path)
        scope_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                       ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        nodes = [tree] + [node for node in ast.walk(tree) if isinstance(node, scope_types)]
        nodes_by_location = {}
        anonymous = {'Lambda': '<lambda>', 'ListComp': '<listcomp>', 'SetComp': '<setcomp>',
                     'DictComp': '<dictcomp>', 'GeneratorExp': '<genexpr>'}
        for node in nodes:
            name = getattr(node, 'name', anonymous.get(type(node).__name__, '<module>'))
            first_line = min([getattr(node, 'lineno', 1)] +
                             [d.lineno for d in getattr(node, 'decorator_list', ())])
            nodes_by_location.setdefault((name, first_line), []).append(node)
        line_codes = self.line_codes[path] = {}
        records = []
        def register_code(current):
            candidates = nodes_by_location.get((current.co_name, current.co_firstlineno), [])
            if current.co_name == '<module>':
                candidates = [tree]
            # Distinguish multiple lambdas/comprehensions on the same line.
            positions = [(line, col) for line, _, col, end in current.co_positions()
                         if line is not None and col is not None and end != col]
            node = next((n for n in candidates if isinstance(n, ast.Module) or all(
                (n.lineno, n.col_offset) <= p <= (n.end_lineno, n.end_col_offset)
                for p in positions)), candidates[0] if candidates else None)
            occurrences = []
            def visit(item):
                if item is not node and isinstance(item, scope_types):
                    return
                variable = item.id if isinstance(item, ast.Name) else item.arg if isinstance(item, ast.arg) else None
                if variable is not None:
                    occurrences.append([item.lineno, variable])
                for child in ast.iter_child_nodes(item):
                    visit(child)
            if node is not None:
                visit(node)
            record = dict(code=id(current), name=current.co_name,
                          def_line=getattr(node, 'lineno', None), occurrences=occurrences,
                          lines=sorted({line for _, _, line in current.co_lines() if line is not None}))
            self.codes[id(current)] = (current, path, record)
            for line in record['lines']:
                line_codes.setdefault(line, []).append(id(current))
            records.append(record)
            for child in current.co_consts:
                if isinstance(child, types.CodeType):
                    register_code(child)
        register_code(code)
        self.sources[path] = text
        event = self.source_events[path] = threading.Event()
        self.send('source', path=path, text=text, mtime=mtime, codes=records)
        event.wait()
        if self.cancelled:
            raise Cancelled()
        self.update(path=path)

    def on_start(self, code, offset):
        if threading.get_ident() != self.thread_id:
            return
        if id(code) not in self.codes:
            if code.co_filename.startswith('<'):
                return sys.monitoring.DISABLE
            path = os.path.realpath(code.co_filename)
            if (path == __file__ or not path.startswith(self.source_root)
                    or path.startswith(self.library_roots)):
                return sys.monitoring.DISABLE
            try:
                self.register(code)
            except (OSError, ValueError, SyntaxError) as error:
                self.send('warning', text=f'Debug source unavailable: {path}: {error}\n')
                return sys.monitoring.DISABLE
        if self.cancelled:
            raise Cancelled()
        return sys.monitoring.DISABLE

    def update(self, path=None, identities=None):
        if self.tool is None:
            return
        monitoring = sys.monitoring
        events = monitoring.events
        monitoring.set_events(self.tool, events.PY_START | (events.PY_UNWIND if self.mode != 'continue' else 0))
        records = (tuple(self.codes[identity] for identity in identities)
                   if identities is not None else tuple(self.codes.values()))
        for code, filename, record in records:
            if path is not None and filename != path:
                continue
            mask = events.PY_RETURN if self.mode != 'continue' else 0
            if self.cancelled or self.mode != 'continue' or self.breakpoints.get(filename, set()).intersection(record['lines']):
                mask |= events.LINE
            monitoring.set_local_events(self.tool, code, mask)
        if self.disabled:
            monitoring.restart_events()
            self.disabled = False

    def on_line(self, code, line):
        if threading.get_ident() != self.thread_id:
            return
        if self.cancelled:
            raise Cancelled()
        _, path, _ = self.codes[id(code)]
        hit = line in self.breakpoints.get(path, ())
        if not hit and self.mode == 'continue':
            self.disabled = True
            return sys.monitoring.DISABLE
        frame = sys._getframe(1)
        if hit or self.mode == 'step_into' or self.mode in ('step_over', 'step_out') and frame is self.step_frame:
            self.pause(frame)

    def on_return(self, code, offset, value):
        if threading.get_ident() == self.thread_id and sys._getframe(1) is self.step_frame:
            self.step_frame = self.step_frame.f_back

    def pause(self, frame):
        self.resume_event.clear()
        self.stop_id += 1
        self.paused = True
        scopes = []
        cursor = frame
        with self.lock:
            while cursor is not None:
                if id(cursor.f_code) in self.codes:
                    _, path, record = self.codes[id(cursor.f_code)]
                    scopes.append(dict(identity=id(cursor), code=id(cursor.f_code), path=path,
                        name=cursor.f_code.co_name, line=cursor.f_lineno,
                        locals={name: self.exports.encode(value) for name, value in cursor.f_locals.copy().items()
                                if name != '__builtins__'}))
                cursor = cursor.f_back
        self.send('paused', stop=self.stop_id, scopes=scopes)
        self.resume_event.wait()
        if self.cancelled:
            raise Cancelled()
        self.step_frame = frame.f_back if self.mode == 'step_out' else frame
        self.paused = False
        self.update()
        self.send('resumed', stop=self.stop_id)

    def run(self):
        if not hasattr(sys, 'monitoring'):
            raise RuntimeError('Debug requires Python 3.12 or newer (sys.monitoring); no tracing fallback is enabled')
        monitoring = sys.monitoring
        for tool in range(5, -1, -1):
            try:
                monitoring.use_tool_id(tool, 'melty-debug')
                self.tool = tool
                break
            except ValueError:
                continue
        if self.tool is None:
            raise RuntimeError('No free monitoring tool slot')
        try:
            request = self.request
            code = compile(request['text'], request['path'], 'exec', dont_inherit=True)
            self.register(code, request['text'], request.get('mtime'))
            for event, callback in ((monitoring.events.PY_START, self.on_start),
                    (monitoring.events.LINE, self.on_line), (monitoring.events.PY_RETURN, self.on_return),
                    (monitoring.events.PY_UNWIND, self.on_return)):
                monitoring.register_callback(self.tool, event, callback)
            self.update()
            module = types.ModuleType('__main__')
            module.__file__, module.__package__ = request['path'], request.get('package') or None
            sys.modules['__main__'] = module
            sys.argv = [request['path'], *request.get('args', [])]
            sys.path[:0] = request['paths']  # target program's normal project import paths
            exec(code, vars(module))
        finally:
            monitoring.set_events(self.tool, 0)
            for code, _, _ in self.codes.values():
                monitoring.set_local_events(self.tool, code, 0)
            monitoring.free_tool_id(self.tool)
            self.tool = None
            self.step_frame = None


def main():
    request = json.load(sys.stdin)
    if os.name != 'nt':
        os.setsid()
    os.chdir(request['cwd'])
    wire = load_file('_melty_reference_wire', request['wire'])
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stream.connect(request['address'])
    execution = Execution(request, stream, wire)
    execution.send('hello', version=wire.VERSION, python=sys.version, executable=sys.executable,
                   monitoring=hasattr(sys, 'monitoring'))
    reader = threading.Thread(target=execution.commands, name='melty-reference-commands', daemon=True)
    reader.start()
    status = 0
    try:
        execution.run()
    except Cancelled:
        status = -1
    except SystemExit as error:
        status = error.code if isinstance(error.code, int) else 0 if error.code is None else 1
    except BaseException:
        status = 1
        execution.send('error', text=traceback.format_exc())
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        execution.finished = True
        execution.send('exited', status=status)
    # The execution has ended. Its referenced objects still belong to this
    # interpreter until the final lease is released by a view/inspection.
    while not execution.closed:
        with execution.lock:
            if not execution.exports.entries:
                break
        reader.join(.1)
    stream.close()


if __name__ == '__main__':
    main()
