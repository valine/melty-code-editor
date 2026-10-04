"""Process resources and immutable inspection bindings for an external task run."""
import codecs
import atexit
import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import tempfile
import threading
from types import MappingProxyType
import uuid
import weakref

from meltygui.core.melty import Melty
from meltygui.model.source_snapshot_model import SourceSnapshot, SourceInspection
from meltygui_pro.models.reference_model import ReferenceOwner
from meltygui_pro.models import reference_wire
from meltygui_pro.models import cuda_shared_memory
from meltygui_pro.views.reference_view import draw_reference


class RemoteScope:
    def __init__(self, owner, source, record, scope):
        self.identity = (owner.identity, scope['identity'])
        self.name, self.line, self.source = scope['name'], scope['line'], source
        self.locals = MappingProxyType({name: owner.decode(value) for name, value in scope['locals'].items()})
        sites = source.index.at_line(self.line)
        self.execution_key = sites[0].path if sites else None
        bindings = []
        for occurrence_id, (line, name) in enumerate(record['occurrences']):
            sites = source.index.at_line(line)
            if name not in self.locals or not sites:
                continue
            key = sites[0].path
            # Offsets belong to this source version, keyed by remote code + occurrence.
            occurrence = (scope['code'], occurrence_id)
            source.occurrence_offsets[occurrence] = line - sites[0].start_line
            bindings.append(((self.identity, occurrence), key, name, self.locals[name], occurrence))
        self.source_inspection = SourceInspection(source, bindings,
            execution_key=self.execution_key, name=self.name, def_line=record['def_line'])

    def for_source(self, path, source=None):
        return self.source_inspection if self.source.matches(path, source) else None


class RemoteInspection:
    def __init__(self, execution, message):
        self.current = True
        self.scopes = {}
        self.stop = message['stop']
        for record in message['scopes']:
            source, codes = execution.sources[record['path']]
            scope = RemoteScope(execution.references, source, codes[record['code']], record)
            self.scopes[scope.identity] = scope


class ExternalExecution:
    """An owned process/connection, not a controller. TaskState issues all commands."""
    def __init__(self, state, python, request, environment, source_snapshot=None, output_limit=200000):
        self.identity = uuid.uuid4().hex
        self.state = weakref.ref(state)
        self.initial_source = source_snapshot
        self.sources = {}
        self.line_codes = {}
        self.stop_id = 0
        self.finished = False
        self._termination_reason = None
        self.closed = False
        self._outgoing = queue.SimpleQueue()
        self._output_lock = threading.Lock()
        self._output_pending = ''
        self._output_scheduled = False
        self._output_limit = output_limit
        self._directory = tempfile.TemporaryDirectory(prefix='melty-debug-')
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self._directory.name + '/control')
        self._listener.listen(1)
        self._listener.settimeout(.2)
        self.stream = None
        self.references = ReferenceOwner(self.identity, self.send)
        self.capabilities = None
        self._on_exit = lambda ref=weakref.ref(self): ref() is not None and ref().terminate()
        request = dict(request, run=self.identity, address=self._directory.name + '/control',
                       wire=reference_wire.__file__, cuda_sharing=cuda_shared_memory.__file__)
        try:
            with tempfile.TemporaryFile() as payload:
                payload.write(json.dumps(request).encode('utf8'))
                payload.seek(0)
                self.process = subprocess.Popen([os.path.abspath(python), '-I', '-u',
                    str(Path(__file__).with_name('debug_worker.py'))], stdin=payload,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment, close_fds=False)
        except BaseException:
            self._listener.close()
            self._directory.cleanup()
            raise

    def start(self):
        atexit.register(self._on_exit)
        self.reader = threading.Thread(target=self._read, name='melty-debug-events', daemon=True)
        self.reader.start()
        threading.Thread(target=self._read_output, name='melty-debug-output', daemon=True).start()

    def send(self, message):
        if not self.closed:
            self._outgoing.put(dict(message, run=self.identity))

    def _write(self):
        try:
            while not self.closed:
                message = self._outgoing.get()
                if message is None:
                    break
                if message['op'] == 'release':
                    # Batch leases released together during a snapshot replacement.
                    while not self._outgoing.empty():
                        following = self._outgoing.get()
                        if following is None:
                            return
                        if following['op'] != 'release':
                            reference_wire.send_packet(self.stream, message)
                            message = following
                            break
                        message['tokens'].extend(following['tokens'])
                reference_wire.send_packet(self.stream, message)
        except (OSError, ValueError):
            pass

    def post(self, kind, value=None):
        if kind == 'output':
            with self._output_lock:
                self._output_pending = (self._output_pending + value)[-self._output_limit:]
                if not self._output_scheduled:
                    self._output_scheduled = True
                    Melty.post_to_render(self._flush_output)
            return
        state = self.state()
        if state is not None:
            state._post(self.process, kind, value)

    def _flush_output(self):
        with self._output_lock:
            output, self._output_pending = self._output_pending, ''
            self._output_scheduled = False
        state = self.state()
        if state is not None:
            state._apply_event(self.process, 'output', output)

    def _source(self, message):
        path = message['path']
        source = self.initial_source if self.initial_source is not None and self.initial_source.path == path else None
        if source is None:
            source = SourceSnapshot(path, message['text'], message['mtime'])
        source.prepare_index()
        codes = {record['code']: record for record in message['codes']}
        lines = self.line_codes[path] = {}
        for code, record in codes.items():
            for line in record['lines']:
                lines.setdefault(line, []).append(code)
        self.sources[path] = source, codes
        self.post('external_source', source)

    def _read(self):
        reason = None
        try:
            while not self.closed:
                try:
                    self.stream, _ = self._listener.accept()
                    break
                except socket.timeout:
                    if self.process.poll() is not None:
                        raise RuntimeError('Debug worker exited before connecting')
            if self.closed:
                return
            hello = reference_wire.receive_packet(self.stream)
            if hello.get('run') != self.identity or hello.get('version') != reference_wire.VERSION:
                raise RuntimeError('Incompatible debugger protocol')
            self.capabilities = hello
            threading.Thread(target=self._write, name='melty-debug-commands', daemon=True).start()
            while not self.closed:
                message = reference_wire.receive_packet(self.stream)
                if message.get('run') != self.identity:
                    continue
                kind = message['kind']
                if kind == 'source':
                    self._source(message)
                elif kind == 'paused':
                    self.stop_id = message['stop']
                    Melty.post_to_render(lambda message=message: self._publish_pause(message))
                elif kind == 'resumed':
                    self.post('external_resumed', message['stop'])
                elif kind in ('fields', 'buffer'):
                    Melty.post_to_render(lambda message=message: self.references.accept(message))
                elif kind == 'warning':
                    self.post('output', message['text'])
                elif kind == 'error':
                    self.post('error', message['text'])
                elif kind == 'exited':
                    self.finished = True
                    self.post('exited', message['status'])
        except Exception as error:
            reason = str(error)
            if not self.finished and not self.closed:
                if self._termination_reason is None:
                    self.post('error', reason)
                self.post('exited', -1)
        finally:
            self.closed = True
            self._outgoing.put(None)
            if self.stream is not None:
                self.stream.close()
            self._listener.close()
            self._directory.cleanup()
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            atexit.unregister(self._on_exit)
            Melty.post_to_render(lambda: self.references.disconnected(
                self._termination_reason or reason or 'The process owning this value exited'))

    def _publish_pause(self, message):
        # Intern and update value models only between frames. Decode even a
        # retired run, so dropping its event balances all incoming leases.
        inspection = RemoteInspection(self, message)
        state = self.state()
        if state is not None:
            state._apply_event(self.process, 'paused', inspection)

    def _read_output(self):
        decoder = codecs.getincrementaldecoder('utf8')(errors='replace')
        try:
            while True:
                chunk = os.read(self.process.stdout.fileno(), 65536)
                output = decoder.decode(chunk, final=not chunk)
                if output:
                    self.post('output', output)
                if not chunk:
                    break
        finally:
            self.process.stdout.close()

    def terminate(self):
        """Force means references become unavailable; graceful stop is a command."""
        self._termination_reason = 'This value belongs to a force-stopped process.'
        if self.references.available:
            self.references.ensure_runtime()
            self.references.available = False
            self.references.error = self._termination_reason
            # Finish this editor's outstanding reads before allowing the producer
            # to free imported allocations. An unexpected producer crash cannot
            # provide this guarantee and is reported as a disconnected value.
            import ctypes
            from meltygui_pro.models.cuda_shared_memory import _driver, _check
            contexts = {}
            for lease in tuple(self.references.buffer_leases):
                memory = lease.memory
                if memory is not None:
                    contexts[memory.allocation.context.value] = memory.allocation.context
            for context in contexts.values():
                driver = _driver()
                driver.cuCtxPushCurrent_v2(context)
                try:
                    _check(driver.cuCtxSynchronize(), 'cuCtxSynchronize')
                finally:
                    driver.cuCtxPopCurrent_v2(ctypes.byref(ctypes.c_void_p()))
            Melty.post_to_render(lambda: self.references.disconnected(self._termination_reason))
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # An early interpreter exit can precede setsid; macOS may
                # return EPERM for that missing group. Target our child only.
                self.process.kill()
