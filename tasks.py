"""The Tasks tile: project commands and Python modules, with streamed output.

Right-click Run adds/reuses a module in ProjectTasks.projects[root]. The app
persists that object beside OpenFiles; Tasks.save_tasks additionally writes
the entry to pyproject.toml through the normal pending-file save lifecycle.
Module tasks execute the current editor text, with package-relative imports,
in the project's interpreter. Their `module` field is a root-relative path.

The tasks are the project's own: ``[tool.melty.tasks]`` in its
``pyproject.toml``, read through meltygui_pro's `manifest_data` (the editor's
pending text, so an unsaved edit already counts)::

    [tool.melty.tasks]
    test  = "pytest -q"
    lint  = "ruff check ."
    serve = { cmd = "python -m app", cwd = "src", env = { PORT = "8000" } }

A value is the command string, or a table with ``cmd`` and optionally ``cwd``
(relative to the project root) and ``env``. Commands run through the shell in
the project root with the project's environment (`analysis_project`) first on
PATH, so ``pytest`` is the project's pytest.

One injected `TaskState` per tile: selected task names persist per project; the
process, its reader thread and the output are the session's. `TaskState.run`
spawns the command in its own process group and a daemon thread that appends
the output and wakes the window; `TaskState.stop` ends the group (the Stop button,
and every live run at app exit). Closing the tile does not stop a run: the
state, and the process with it, come back with the tile.

The picker follows its injected Files view's selected project, falling
back to the selected tab or saved project when unlinked. Run identity and
output stay separate from the per-project picker selection.
The Run menu (editor.py) lists the same tasks and runs one in the first Tasks
tile through `request_run`.

On iOS both Run and Debug execute Python modules in the embedded interpreter.
Project imports and text console I/O are scoped to the execution thread, while
the module cache, app cwd and environment are shared. Shell tasks are refused.
"""
from meltygui.model.source_snapshot_model import CodeIdentityMap

import atexit
import codecs
import importlib.machinery
import json
import os
import shlex
import signal
import sys
import threading
import time
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from meltygui import imgui
from meltygui.core.core_render import render_func
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.melty import Melty
from meltygui.core.rendering.core_decoration import no_save
from meltygui.view.header_view import flat_button
from meltygui.view.dropdown_view import draw_dropdown
from meltygui_pro.models.open_files import OpenFiles
from meltygui_pro.models.projects import project_for, project_roots
from project_tree import draw_project_tree, project_selection
from meltygui import DrawState

OUTPUT_LIMIT = 200_000       # chars of output kept, the console's limit
KILL_AFTER = 3.0             # seconds between SIGTERM and SIGKILL
NO_TASKS = 'No tasks — right-click a Python module to Run'
OUTPUT_TOP = 24.0           # status line (20) and gap (4), logical pixels


class ProjectTasks(DictConversion):
    """Project root -> named task definitions, saved with the app's open files."""
    def __init__(self):
        super().__init__()
        self.projects = {}


# editor.py replaces this with the app session's persisted instance at startup.
project_tasks = globals().get("project_tasks", ProjectTasks())


class _LocalDebugStopped(BaseException):
    """Cooperative cancellation of the session's local execution thread."""


_LOCAL_CONTEXT = globals().get('_LOCAL_CONTEXT', threading.local())
_LOCAL_CONTEXT_LOCK = globals().get('_LOCAL_CONTEXT_LOCK', threading.RLock())
_LOCAL_CONTEXT_USERS = globals().get('_LOCAL_CONTEXT_USERS', 0)
_LOCAL_PROJECT = globals().get('_LOCAL_PROJECT')
_LOCAL_STREAMS = globals().get('_LOCAL_STREAMS', {})


class _LocalProjectFinder:
    """Normal imports on the task thread, without changing the app's sys.path.

    Modules keep their canonical sys.modules identity, including circular
    imports and live edits. This is a shared interpreter, not a separate venv.
    Threads started by user code do not inherit this task's import/output scope.
    """
    def find_spec(self, fullname, path=None, target=None):
        roots = getattr(_LOCAL_CONTEXT, 'paths', ())
        if roots and path is None:
            return importlib.machinery.PathFinder.find_spec(fullname, roots, target)
        return None


_LOCAL_FINDER = globals().get('_LOCAL_FINDER', _LocalProjectFinder())


class _LocalStream:
    """Route Python text I/O on the execution thread; leave other threads alone."""
    def __init__(self, original, name):
        self.original, self.name = original, name

    def __getattr__(self, name):
        return getattr(self.original, name)

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError('write() argument must be str')
        context = getattr(_LOCAL_CONTEXT, 'execution', None)
        if context is None or self.name == 'stdin':
            return self.original.write(text)
        state, execution = context
        if text:
            state._post(execution, 'output', text)
        return len(text)

    def flush(self):
        if getattr(_LOCAL_CONTEXT, 'execution', None) is None:
            return self.original.flush()

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def isatty(self):
        return False if getattr(_LOCAL_CONTEXT, 'execution', None) else self.original.isatty()

    def readline(self, size=-1):
        context = getattr(_LOCAL_CONTEXT, 'execution', None)
        if context is None or self.name != 'stdin':
            return self.original.readline(size)
        return context[0]._read_local_input(size)

    def read(self, size=-1):
        if getattr(_LOCAL_CONTEXT, 'execution', None) is None or self.name != 'stdin':
            return self.original.read(size)
        result = ''
        while size < 0 or len(result) < size:
            line = self.readline(-1 if size < 0 else size - len(result))
            if not line:
                break
            result += line
        return result

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line


def _check_project_imports(paths):
    """Refuse conflicting cached names instead of running another project's code."""
    names = set()
    for root in paths:
        for entry in Path(root).iterdir():
            if entry.suffix == '.py' and entry.stem.isidentifier():
                names.add(entry.stem)
            elif entry.is_dir() and entry.name.isidentifier():
                names.add(entry.name)
    for name in names.intersection(sys.modules):
        module = sys.modules[name]
        if module is None:
            continue
        spec = importlib.machinery.PathFinder.find_spec(name, paths)
        if spec is None:
            continue
        if spec.origin is not None:
            loaded = getattr(module, '__file__', None)
            matches = loaded is not None and os.path.realpath(loaded) == os.path.realpath(spec.origin)
        else:
            matches = tuple(getattr(module, '__path__', ())) == tuple(spec.submodule_search_locations or ())
        if not matches:
            raise ImportError(f'Local execution shares imported modules: {name!r} is already loaded '
                              'from another location. Use a unique package name or restart the app.')


@contextmanager
def _local_execution_context(state, execution, paths):
    """Own temporary I/O routing and the finder across concurrent local tasks."""
    global _LOCAL_CONTEXT_USERS, _LOCAL_PROJECT
    with _LOCAL_CONTEXT_LOCK:
        if _LOCAL_CONTEXT_USERS and _LOCAL_PROJECT != state._project_root:
            raise RuntimeError('Local execution shares one project import context. '
                               'Stop the other project before running this one.')
        _check_project_imports(paths)
        if not _LOCAL_CONTEXT_USERS:
            _LOCAL_PROJECT = state._project_root
            for name in ('stdin', 'stdout', 'stderr'):
                original = getattr(sys, name)
                wrapper = _LocalStream(original, name)
                _LOCAL_STREAMS[name] = wrapper
                setattr(sys, name, wrapper)
            # Respect other import hooks, builtins and frozen modules.
            index = next((i for i, finder in enumerate(sys.meta_path)
                          if finder is importlib.machinery.PathFinder), len(sys.meta_path))
            sys.meta_path.insert(index, _LOCAL_FINDER)
        _LOCAL_CONTEXT_USERS += 1
    _LOCAL_CONTEXT.execution = (state, execution)
    _LOCAL_CONTEXT.paths = paths
    try:
        yield
    finally:
        del _LOCAL_CONTEXT.execution, _LOCAL_CONTEXT.paths
        with _LOCAL_CONTEXT_LOCK:
            _LOCAL_CONTEXT_USERS -= 1
            if not _LOCAL_CONTEXT_USERS:
                _LOCAL_PROJECT = None
                if _LOCAL_FINDER in sys.meta_path:
                    sys.meta_path.remove(_LOCAL_FINDER)
                for name, wrapper in _LOCAL_STREAMS.items():
                    if getattr(sys, name) is wrapper:
                        setattr(sys, name, wrapper.original)
                _LOCAL_STREAMS.clear()


class CapturedScope:
    """Shallow bindings at a stop; source analysis belongs to the snapshot."""
    def __init__(self, frame, source_version):
        from meltygui.model.source_snapshot_model import SourceInspection
        self.identity = (threading.get_ident(), id(frame))
        self.name = frame.f_code.co_qualname
        from types import MappingProxyType
        self.locals = MappingProxyType(dict(frame.f_locals))
        self.source = source_version
        self.line = frame.f_lineno
        sites = source_version.index.at_line(self.line)
        self.execution_key = sites[0].path if sites else None
        def_line, occurrences = source_version.occurrences[frame.f_code]
        bindings = [( (self.identity, key, name, occurrence_id), key, name, self.locals[name], occurrence_id)
                    for key, name, occurrence_id in occurrences if name in self.locals]
        self.source_inspection = SourceInspection(source_version, bindings,
            execution_key=self.execution_key, name=self.name, def_line=def_line)

    def for_source(self, path, source=None):
        return self.source_inspection if self.source.matches(path, source) else None


class LocalFrameInspection:
    def __init__(self, frame, source_version, codes):
        from meltygui.model.source_snapshot_model import SourceSnapshot
        self.scopes = {}
        self.current = True
        while frame is not None:
            if frame.f_code in codes:
                scope = CapturedScope(frame, codes[frame.f_code] if isinstance(codes[frame.f_code], SourceSnapshot) else source_version)
                self.scopes[scope.identity] = scope
            frame = frame.f_back


@no_save('_follow', '_output_scroll_y', '_output_view', '_toolbar_height',
         '_toolbar', '_selector_view', '_execution', '_input_view')
class TaskViewState(DictConversion):
    """Presentation owned by one Tasks view, independent of its shared session."""
    def __init__(self):
        super().__init__()
        self.input_text = ''
        self._follow = True
        self._output_scroll_y = None
        self._output_view = None
        self._toolbar_height = 0
        self._toolbar = None
        self._selector_view = None
        self._execution = None
        self._input_view = None


@no_save('process', 'thread', 'output', 'running', 'exit', 'started', 'ended', 'error',
         'debug_enabled', 'paused', 'inspection', 'selected_scope_id', '_pending_start',
         '_monitoring_tool', '_debug_thread_id', '_debug_codes', '_debug_source',
         '_stop_requested', '_resume_event', '_step_mode', '_step_frame', '_paused_frame',
         '_breakpoint_lines', 'unresolved_breakpoints', '_disabled_lines', '_breakpoint_metadata',
         '_sources', '_code_sources', '_line_codes', '_breakpoints_by_path', '_source_callbacks', '_project_root', '_executing', '_external',
         '_local_execution', 'waiting_for_input', '_input_condition', '_input_lines', '_input_eof')
class TaskState(DictConversion):
    _disabled_lines = False

    def __init__(self):
        super().__init__()
        self.selected_tasks = {}  # project root -> picker selection (persists)
        self.task = None          # last run task name (persists)
        self.project = None       # last run project (persists)
        self.process = None       # subprocess.Popen while running
        self.thread = None        # the reader thread
        self.output = ''          # what the tile shows, the last OUTPUT_LIMIT chars
        self.running = False
        self.exit = None          # exit code of the last run
        self.started = 0.0        # time.monotonic() of the last start
        self.ended = 0.0
        self.error = None         # why the last start was refused
        self.debug_enabled = False
        self.paused = False
        self.inspection = None
        self.selected_scope_id = None
        self._pending_start = None
        self._monitoring_tool = None
        self._debug_thread_id = None
        self._debug_codes = CodeIdentityMap()
        self._debug_source = None
        self._stop_requested = threading.Event()
        self._resume_event = threading.Event()
        self._step_mode = 'continue'
        self._step_frame = None
        self._paused_frame = None
        self._breakpoint_lines = set()
        self.unresolved_breakpoints = []
        self._disabled_lines = False
        self._breakpoint_metadata = None
        self._sources = {}
        self._code_sources = CodeIdentityMap()
        self._line_codes = {}
        self._breakpoints_by_path = {}
        self._source_callbacks = {}
        self._project_root = None
        self._executing = False
        self._external = None
        self._local_execution = False
        self.waiting_for_input = False
        self._input_condition = threading.Condition()
        self._input_lines = []
        self._input_eof = False

    def ensure_runtime(self):
        """Add session fields to pre-feature live states without replacing them."""
        if any(name not in vars(self) for name in ('inspection', '_breakpoint_metadata', '_sources', '_executing', '_external', '_local_execution')):
            local_execution = isinstance(self.process, threading.Thread)
            defaults = TaskState()
            for name, value in vars(defaults).items():
                if name not in vars(self):
                    setattr(self, name, value)
            self._local_execution = local_execution
            self._executing = bool(self.running and local_execution and self._monitoring_tool is not None)
            if self._monitoring_tool is not None and local_execution:
                sys.monitoring.register_callback(self._monitoring_tool, sys.monitoring.events.INSTRUCTION,
                                                 self._on_local_instruction)
        migrated_codes = not isinstance(self._debug_codes, CodeIdentityMap)
        if migrated_codes:
            self._debug_codes = CodeIdentityMap(self._debug_codes)
            self._code_sources = CodeIdentityMap(self._code_sources)
            self._disabled_lines = True
        self._adopt_running_source()
        if self._debug_source is not None and self._debug_codes and not self._sources:
            source = self._debug_source
            self._sources[source.path] = source
            self._code_sources = CodeIdentityMap((code, source) for code in self._debug_codes)
            self._project_root = os.path.realpath(os.path.dirname(source.path))
            self._breakpoints_by_path[source.path] = self._breakpoint_lines
            self._source_callbacks[source.path] = self._breakpoints_changed
            execution = self.process
            def prepare():
                try:
                    source.prepare()
                    self._post(execution, 'adopted', source)
                except Exception as error:
                    self._post(execution, 'error', str(error))
            threading.Thread(target=prepare, name='debug-source-registry', daemon=True).start()
            if self._monitoring_tool is not None:
                sys.monitoring.register_callback(self._monitoring_tool, sys.monitoring.events.PY_START, self._on_debug_start)
                self._update_monitoring()

        if migrated_codes and self._monitoring_tool is not None:
            self._update_monitoring()

    def _adopt_running_source(self):
        from meltygui.model.source_snapshot_model import SourceSnapshot, SourceInspection
        legacy = self._debug_source
        if not isinstance(legacy, tuple):
            return
        snapshot = SourceSnapshot.adopt_running(legacy, self._debug_codes)
        self._debug_source = snapshot
        # Existing captured values already have shallow-reference ownership.
        # Translate the retired overlay addresses once, without walking an AST.
        if self.inspection is not None:
            for scope in self.inspection.scopes.values():
                bindings = []
                for old_key, value in scope.live_store.__live_values__.items():
                    line_name = old_key[0]
                    line, name = line_name[5:].split('#', 1)
                    line = int(line)
                    sites = snapshot.index.at_line(line)
                    if not sites:
                        continue
                    occurrence_id = len(snapshot.occurrence_offsets)
                    snapshot.occurrence_offsets[occurrence_id] = line - sites[0].start_line
                    key = sites[0].path
                    bindings.append(((scope.identity, key, name, occurrence_id),
                                     key, name, value, occurrence_id))
                scope.source = snapshot
                scope.source_inspection = SourceInspection(snapshot, bindings,
                    execution_key=scope.execution_key if self.inspection.current else None,
                    name=scope.name, def_line=getattr(scope.live_store, '__def_line__', None))
                for retired in ('live_store', 'path', 'source_index', 'code_tree', 'disk_mtime'):
                    vars(scope).pop(retired, None)
        execution = self.process
        def prepare():
            try:
                snapshot.prepare()
            except Exception as error:
                self._post(execution, 'error', str(error))
        threading.Thread(target=prepare, name='debug-source-adoption', daemon=True).start()

    @property
    def status(self):
        self.ensure_runtime()
        if self.paused:
            return 'paused'
        return 'running' if self.running else ('finished' if self.exit is not None else 'idle')

    @property
    def selected_scope(self):
        self.ensure_runtime()
        return self.inspection.scopes.get(self.selected_scope_id) if self.inspection else None

    def select_scope(self, scope_id):
        if self.inspection and scope_id in self.inspection.scopes:
            self.selected_scope_id = scope_id
            self._changed()

    def for_source(self, path, source=None):
        scope = self.selected_scope
        inspection = scope.for_source(path, source) if scope is not None else None
        if inspection is not None:
            inspection.execution_key = scope.execution_key if self.inspection.current else None
        return inspection

    def replace_inspection(self, inspection):
        follow_top = (self.inspection is None or self.selected_scope_id ==
                      next(iter(self.inspection.scopes), None))
        self.inspection = inspection
        if inspection is None:
            self.selected_scope_id = None
        elif follow_top or self.selected_scope_id not in inspection.scopes:
            self.selected_scope_id = next(iter(inspection.scopes), None)
        self._changed()

    def on_tile_layout_event(self, event):
        """Layout policy: a hidden/replaced tile keeps its execution alive."""

    def _changed(self):
        from meltygui.core.windowing.glfw_utils import request_render
        if Melty.cache is not None:
            # Events apply between frames; keep the invalidation through the
            # next paint even when this frame already rendered a consumer.
            Melty.cache.invalidate_up_by_obj(self, frame_delta=1)
        request_render()

    def _post(self, execution, kind, value=None):
        Melty.post_to_render(lambda: self._apply_event(execution, kind, value))

    def _apply_event(self, execution, kind, value=None):
        if execution is not self.process:
            return
        if kind == 'output':
            self.output = (self.output + value)[-OUTPUT_LIMIT:]
        elif kind == 'error':
            self.error = value
        elif kind == 'adopted':
            self._accept_source(value)
        elif kind == 'external_source':
            if self._debug_source is None:
                self._debug_source = value
            self._sources[value.path] = value
            self._line_codes[value.path] = self._external.line_codes[value.path]
            if value.path not in self._source_callbacks:
                callback = lambda path=value.path: self._breakpoints_changed(path)
                self._source_callbacks[value.path] = callback
                if self._breakpoint_metadata is not None and hasattr(self._breakpoint_metadata, 'subscribe'):
                    self._breakpoint_metadata.subscribe(value.path, callback)
            self._breakpoints_changed(value.path)
        elif kind in ('prepared', 'source'):
            self._accept_source(value)
            if self.unresolved_breakpoints:
                self.output += f'Unresolved breakpoint keys: {self.unresolved_breakpoints!r}\n'
            self._resume_event.set()
        elif kind == 'paused':
            self.paused = True
            self.replace_inspection(value)
        elif kind == 'inspection':
            self.replace_inspection(value)
            self.inspection.current = False
        elif kind == 'input':
            self.waiting_for_input = value
        elif kind == 'resumed':
            self.paused = False
            if self.inspection:
                self.inspection.current = False
        elif kind == 'external_resumed':
            if self.inspection is not None and value == self.inspection.stop:
                self.paused = False
                self.inspection.current = False
        elif kind == 'exited':
            self._detach_breakpoints()
            self.running, self.paused = False, False
            self.waiting_for_input = False
            self.exit, self.ended = value, time.monotonic()
            if self.inspection:
                self.inspection.current = False
            _LIVE.discard(self)
            pending, self._pending_start = self._pending_start, None
            if pending is not None:
                self.run(*pending)
        self._changed()

    def run_module(self, path, root=None, *, debug=False, file_metadata=None, source_snapshot=None):
        from editor_settings import settings
        root = str(Path(root or project_for(path) or Path(path).parent).resolve())
        try:
            name = add_module_task(path, root, save=settings['Tasks']['save_tasks'])
            self.run(root, name, debug, file_metadata, source_snapshot)
        except (OSError, ValueError, TypeError) as error:
            self.error = str(error)
            self._changed()

    def run(self, root, name, debug=False, file_metadata=None, source_snapshot=None):
        self.ensure_runtime()
        if self.running:
            self._pending_start = (root, name, debug, file_metadata, source_snapshot)
            self.stop()
            return
        task = read_tasks(root).get(name)
        self.selected_tasks[str(root)] = name
        self.task, self.project, self.error = name, str(root), None
        if task is None:
            self.error = f'No task {name!r} in {Path(root).name}'
            self._changed()
            return
        cwd = Path(root) / task['cwd']
        if not cwd.is_dir():
            self.error = f'cwd is not a folder: {cwd}'
            self._changed()
            return
        if sys.platform == 'ios':
            self._start_ios_module(root, task, debug, file_metadata, source_snapshot)
            return
        if debug:
            self._start_external_debug(root, task, file_metadata, source_snapshot)
            return
        self._external = None
        self._local_execution = False
        self.debug_enabled, self.paused = False, False
        self.replace_inspection(None)
        self.output = f'$ {task["cmd"]}\n'
        self.exit = None
        self.started, self.ended = time.monotonic(), 0.0
        try:
            import shutil
            import subprocess
            env = task_environment(root)
            env.update(task['env'])
            with ExitStack() as stack:
                shell = shutil.which('cmd' if sys.platform == 'win32' else 'sh')
                command = [shell, '/c' if sys.platform == 'win32' else '-c', task['cmd']]
                stdin = subprocess.DEVNULL
                if 'module' in task:
                    command, request = module_request(root, task['module'])
                    stdin = stack.enter_context(tempfile.TemporaryFile())
                    stdin.write(json.dumps(request).encode('utf-8'))
                    stdin.seek(0)
                # Session/cwd setup executes in the child; Popen remains on
                # its posix_spawn path even with the editor's live threads.
                options = {}
                setup = 'import os,sys; '
                if sys.platform == 'win32':
                    options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    # macOS has setsid(2), but no Linux `setsid` executable.
                    setup += 'os.setsid(); '
                command = [sys.executable, '-I', '-c', setup +
                    'os.chdir(sys.argv[1]); os.execvpe(sys.argv[2],sys.argv[2:],os.environ)',
                    str(cwd.resolve()), *command]
                process = subprocess.Popen(command, env=env, stdin=stdin,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=False, **options)
        except (OSError, ValueError, TypeError) as error:
            self.error = str(error)
            self._changed()
            return
        global _last
        _last = (str(root), name)
        self.process, self.running = process, True
        _LIVE.add(self)
        self.thread = threading.Thread(target=self._read_output, args=(process,),
                                       name='melty-task', daemon=True)
        self.thread.start()
        self._changed()

    def _read_output(self, process):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            while True:
                chunk = os.read(process.stdout.fileno(), 4096)
                text = decoder.decode(chunk, final=not chunk)
                if text:
                    self._post(process, 'output', text)
                if not chunk:
                    break
        finally:
            process.stdout.close()
            self._post(process, 'exited', process.wait())

    def stop(self):
        self.ensure_runtime()
        process = self.process
        if self._external is not None:
            if self.running:
                self._external.send({'op': 'stop'})
            return
        if self._local_execution:
            if self.running:
                self._stop_requested.set()
                self._update_monitoring()
                self._resume_event.set()
                with self._input_condition:
                    self._input_condition.notify_all()
            return
        if process is None or process.poll() is not None:
            return
        try:
            if sys.platform == 'win32':
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            # setsid may not have run yet; terminate its launcher directly.
            process.terminate()
        timer = threading.Timer(KILL_AFTER, self._kill_process, args=(process,))
        timer.daemon = True
        timer.start()

    def _kill_process(self, process):
        if process.poll() is None:
            try:
                if sys.platform == 'win32':
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def shutdown(self):
        self._pending_start = None
        self.stop()
        if self._external is not None:
            self._external.terminate()

    def force_stop(self):
        """Explicitly sacrifice remote references when cooperative stop cannot return."""
        if self._external is not None:
            self._external.terminate()
        elif not self._local_execution and self.process is not None:
            self._kill_process(self.process)

    def resume(self, mode='continue'):
        if mode not in ('continue', 'step_into', 'step_over', 'step_out'):
            raise ValueError(f'Unknown step mode: {mode}')
        if not self.paused:
            return
        if self._external is not None:
            self._external.send({'op': 'resume', 'stop': self.inspection.stop, 'mode': mode})
            return
        self._step_mode = mode
        self._resume_event.set()

    def _start_external_debug(self, root, task, file_metadata, source_snapshot=None):
        from meltygui_pro.models.project_function_runner import project_python
        from meltygui_pro.models.project_analysis import analysis_project
        try:
            if 'module' not in task:
                raise ValueError('Debug supports Python module tasks. Use Run for shell commands.')
            path = (Path(root) / task['module']).resolve()
            python = project_python(root) or sys.executable
            if source_snapshot is None:
                from meltygui.code.fileref import Address
                from meltygui.code.new_codecs import TextFileCodec
                from meltygui.model.source_snapshot_model import SourceSnapshot
                # Use the editor's codec path: pending text keeps its identity,
                # and disk text carries its file/version provenance. A plain
                # file read loses that contract and hides the paused line.
                text = TextFileCodec.load(Address(path))
                if not isinstance(text, str):
                    raise ValueError(f'Cannot read Python source: {path}')
                source_snapshot = SourceSnapshot(str(path), text)
            package, parent = [], path.parent
            while (parent / '__init__.py').is_file():
                package.insert(0, parent.name)
                parent = parent.parent
            request = dict(path=str(path), text=source_snapshot.text, package='.'.join(package),
                           paths=list(analysis_project(path=root).source_paths))
            request.update(root=str(Path(root).resolve()), cwd=str((Path(root) / task['cwd']).resolve()),
                           mtime=source_snapshot.disk_mtime)
            request['paths'] = list(dict.fromkeys([str(path.parent), str(root), *request['paths']]))
            environment = task_environment(root)
            environment.update(task['env'])
            self.run_external(python, request, environment, file_metadata=file_metadata,
                              source_snapshot=source_snapshot)
        except (OSError, ValueError, TypeError) as error:
            self.error = str(error)
            self._changed()

    def run_external(self, python, request, environment=None, *, file_metadata=None, source_snapshot=None):
        if sys.platform == 'ios':
            raise RuntimeError('iOS runs Python in the embedded interpreter; subprocess debugging is unavailable.')
        from external_debugger import ExternalExecution
        if self.running:
            raise RuntimeError('Stop the current execution before starting Debug')
        if source_snapshot is not None and not source_snapshot.matches(request['path'], request['text']):
            raise ValueError('Prepared source does not match the requested execution source version')
        execution = ExternalExecution(self, python, request,
            dict(os.environ) if environment is None else environment, source_snapshot, OUTPUT_LIMIT)
        self._detach_breakpoints()
        self._external = execution
        self._local_execution = False
        self._sources, self._line_codes, self._breakpoints_by_path = {}, {}, {}
        self._breakpoint_metadata = file_metadata
        self._debug_source = source_snapshot
        self.unresolved_breakpoints = []
        self.debug_enabled, self.paused, self.running = True, False, True
        self.error, self.exit = None, None
        self.output = f'Debug: {request["path"]}\n'
        self.started, self.ended = time.monotonic(), 0.0
        self.replace_inspection(None)
        self.process = execution.process
        _LIVE.add(self)
        execution.start()
        self.thread = execution.reader
        self._changed()

    def _start_local_debug(self, root, task, file_metadata, source_snapshot=None):
        """Explicit in-process mode for callers that want native object references."""
        if sys.platform == 'ios':
            self._start_ios_module(root, task, True, file_metadata, source_snapshot)
            return
        from meltygui_pro.models.project_function_runner import project_python
        try:
            selected = project_python(root) or sys.executable
        except ValueError as error:
            self.error = str(error)
            self._changed()
            return
        if os.path.abspath(selected) != os.path.abspath(sys.executable):
            self.error = 'Local Debug requires the editor interpreter; this project uses another interpreter.'
        elif 'module' not in task:
            self.error = 'Local Debug supports Python module tasks only.'
        elif task['env'] or task['cwd'] != '.':
            self.error = 'Local Debug cannot change the editor process environment or working directory.'
        else:
            try:
                from meltygui.model.source_snapshot_model import SourceSnapshot
                path = (Path(root) / task['module']).resolve()
                # The editor already owns this exact version. Do not re-read it
                # through the subprocess request/pending-save composition path.
                snapshot = source_snapshot or SourceSnapshot(str(path), None)
                self.run_local(snapshot.text, str(path), file_metadata=file_metadata,
                               source_snapshot=snapshot, project_root=root)
                return
            except (OSError, ValueError, SyntaxError, RuntimeError) as error:
                self.error = str(error)
        self._changed()

    def _start_ios_module(self, root, task, debug, file_metadata, source_snapshot=None):
        """iOS has one embedded interpreter, with no shell or project processes."""
        try:
            if 'module' not in task:
                raise ValueError('iOS supports Python module tasks; shell commands and subprocesses are unavailable.')
            path = (Path(root) / task['module']).resolve()
            if task['env']:
                raise ValueError('Local execution cannot change the app process environment.')
            package_root = path.parent
            while (package_root / '__init__.py').is_file():
                package_root = package_root.parent
            # Generated module entries use the package parent as their desktop
            # cwd. Here it is an import root; arbitrary cwd overrides are refused.
            cwd = (Path(root) / task['cwd']).resolve()
            if cwd not in (Path(root).resolve(), package_root):
                raise ValueError('Local execution cannot change the app working directory. '
                                 'Use paths relative to __file__ for project resources.')
            from meltygui.model.source_snapshot_model import SourceSnapshot
            snapshot = source_snapshot or SourceSnapshot(str(path), None)
            self.run_local(snapshot.text, str(path), debug=debug,
                           file_metadata=file_metadata, source_snapshot=snapshot, project_root=root)
            global _last
            if self.task is not None:
                _last = (str(root), self.task)
        except (OSError, ValueError, SyntaxError, RuntimeError) as error:
            self.error = str(error)
            self._changed()

    def run_local(self, source, path, *, debug=True, file_metadata=None, namespace=None,
                  package=None, disk_mtime=None, source_snapshot=None, project_root=None):
        """Run source in this interpreter, retaining actual local objects.

        This explicit local context shares installed imports, cwd and environment
        with the editor. It does not impersonate a project subprocess. Project
        imports use normal canonical modules and stay cached across runs; live
        edits patch those same objects. Stop is cooperative at Python monitoring
        events; native blocking calls must return. Child threads are user-owned
        and do not inherit task I/O or imports. Direct native/fd output is not captured.
        """
        self.ensure_runtime()
        if self.running:
            raise RuntimeError('Stop the current execution before starting local execution')
        from meltygui.model.source_snapshot_model import SourceSnapshot
        if not hasattr(sys, 'monitoring'):
            raise RuntimeError('Local execution requires Python 3.12 or newer')
        path = os.path.abspath(path)
        if source_snapshot is not None:
            same_path = source_snapshot.path == path
            same_version = (source_snapshot.matches(path, source) or
                            (not hasattr(source, '_disk_span') and not hasattr(source, '_disk_mtime')
                             and disk_mtime is not None and disk_mtime == source_snapshot.disk_mtime))
            if not same_path or not same_version:
                raise ValueError('Prepared source does not match the requested execution source version')
        self._detach_breakpoints()
        self._external = None
        self._local_execution = True
        self._debug_source = source_snapshot or SourceSnapshot(path, source, disk_mtime)
        self._debug_codes = CodeIdentityMap()
        self._sources, self._code_sources, self._line_codes = {}, CodeIdentityMap(), {}
        self._breakpoints_by_path, self._source_callbacks = {}, {}
        self._project_root = os.path.realpath(project_root or os.path.dirname(path))
        self._breakpoint_lines = set()
        self.unresolved_breakpoints = []
        self._breakpoint_metadata = file_metadata if debug else None
        self._stop_requested.clear()
        self._resume_event.clear()
        self.waiting_for_input = False
        self._input_lines, self._input_eof = [], False
        self._step_mode, self._step_frame = 'continue', None
        self._disabled_lines = False
        self.debug_enabled, self.paused = debug, False
        self.error, self.exit = None, None
        self.output = f'Local {"Debug" if debug else "Run"}: {path}\n'
        if sys.platform == 'ios':
            self.output += f'Shared app working directory: {os.getcwd()}\n'
        self.started, self.ended = time.monotonic(), 0.0
        self.replace_inspection(None)
        execution = threading.Thread(target=self._execute_local,
            args=(dict(namespace or {}), package), name='melty-local-python', daemon=True)
        self.process = self.thread = execution
        self.running = True
        _LIVE.add(self)
        execution.start()
        self._changed()

    def submit_input(self, text):
        """Send a line to the current local task's input()/sys.stdin."""
        if not self.running or not self._local_execution:
            raise RuntimeError('No local Python task is running')
        if not isinstance(text, str):
            raise TypeError('Console input must be text')
        with self._input_condition:
            if self._input_eof:
                raise ValueError('Console input is closed for this run')
            self._input_lines.append(text if text.endswith('\n') else text + '\n')
            self._input_condition.notify_all()

    def close_input(self):
        with self._input_condition:
            self._input_eof = True
            self._input_condition.notify_all()

    def _read_local_input(self, size=-1):
        if size == 0:
            return ''
        with self._input_condition:
            if not self._input_lines and not self._input_eof:
                self._post(self.process, 'input', True)
            self._input_condition.wait_for(lambda: self._input_lines or self._input_eof
                                           or self._stop_requested.is_set())
            self._post(self.process, 'input', False)
            if self._stop_requested.is_set():
                self._cancel_local()
            if not self._input_lines:
                return ''
            text = self._input_lines.pop(0)
            if size >= 0 and len(text) > size:
                self._input_lines.insert(0, text[size:])
                text = text[:size]
            return text

    def _detach_breakpoints(self):
        metadata = getattr(self, '_breakpoint_metadata', None)
        if metadata is not None and hasattr(metadata, 'unsubscribe'):
            for path, callback in getattr(self, '_source_callbacks', {}).items():
                metadata.unsubscribe(path, callback)
            # A live pre-multifile state used this subscription.
            if self._debug_source is not None:
                metadata.unsubscribe(self._debug_source.path, self._breakpoints_changed)
        self._source_callbacks = {}
        self._breakpoint_metadata = None

    def _accept_source(self, source):
        """Render-thread publication of worker-prepared source/code indexes."""
        path = source.path
        self._sources[path] = source
        self._line_codes[path] = source.line_codes
        added = []
        for code, lines in source.code_lines.items():
            if code in self._code_sources:
                continue
            added.append(code)
            self._debug_codes[code] = lines
            self._code_sources[code] = source
        if path not in self._source_callbacks:
            callback = lambda path=path: self._breakpoints_changed(path)
            self._source_callbacks[path] = callback
            metadata = self._breakpoint_metadata
            if metadata is not None and hasattr(metadata, 'subscribe'):
                metadata.subscribe(path, callback)
        self._breakpoints_changed(path)
        self._update_monitoring(added)

    def _breakpoints_changed(self, path=None):
        if not self.debug_enabled:
            return
        from meltygui.model.breakpoint_model import file_breakpoints
        source = self._sources.get(path) if path is not None else self._debug_source
        if source is None or source.path not in self._line_codes:
            return
        self.set_breakpoints(source.path, source.text, source.index,
                             file_breakpoints(self._breakpoint_metadata, source.path))

    def set_breakpoints(self, path, source, index, breakpoints):
        """Resolve only this file's keys; update only affected code objects."""
        if not self.debug_enabled:
            return
        snapshot = self._sources.get(path)
        if snapshot is None or not snapshot.matches(path, source):
            return
        index = snapshot.index
        executable = self._line_codes[path]
        lines, unresolved = set(), []
        for key, value in breakpoints.items():
            if not value.get('enabled', True):
                continue
            site = index.sites.get(key)
            if (site is not None and site.start_line in executable
                    and index.at_line(site.start_line)[0].path == key):
                lines.add(site.start_line)
            else:
                unresolved.append(key)
        previous = self._breakpoints_by_path.get(path, set())
        self._breakpoints_by_path[path] = lines
        if snapshot is self._debug_source:
            self._breakpoint_lines = lines
            self.unresolved_breakpoints = unresolved
        if self._external is not None:
            self._external.send({'op': 'breakpoints', 'path': path, 'lines': sorted(lines)})
            if previous != lines or unresolved:
                self._changed()
            return
        affected = {}
        for line in previous ^ lines:
            affected.update((id(code), code) for code in executable.get(line, ()))
        if affected:
            self._update_monitoring(affected.values())
        if previous != lines or unresolved:
            self._changed()

    def _on_debug_start(self, code, instruction_offset):
        """Discover project code on this execution thread, without import hooks."""
        if threading.get_ident() != self._debug_thread_id:
            return
        if not self._executing:
            return
        path = os.path.realpath(code.co_filename)
        if path == os.path.realpath(__file__) and code not in self._code_sources:
            return sys.monitoring.DISABLE
        root = self._project_root
        if root is None or not path.endswith('.py') or not path.startswith(root + os.sep):
            return sys.monitoring.DISABLE
        if self._stop_requested.is_set():
            self._cancel_local()
        if code not in self._code_sources:
            from meltygui.model.source_snapshot_model import SourceSnapshot
            try:
                source = self._sources.get(path)
                if source is None:
                    # Imported code executes its loaded disk version, not editor
                    # pending text. Existing Python import semantics remain intact.
                    from meltygui.code.chain_converters import DiskSpanText
                    with open(path, encoding='utf-8') as stream:
                        text = DiskSpanText(stream.read())
                        text._disk_mtime = os.fstat(stream.fileno()).st_mtime
                    text._disk_span = (path, None, None)
                    source = SourceSnapshot(path, text)
                source.prepare(actual_code=code)
            except (OSError, UnicodeError, SyntaxError, ValueError) as error:
                self._post(self.process, 'output', f'Cannot inspect {path}: {error}\n')
                return sys.monitoring.DISABLE
            self._resume_event.clear()
            self._post(self.process, 'source', source)
            self._resume_event.wait()
            if self._stop_requested.is_set():
                self._cancel_local()
            self._resume_event.clear()
        # Discovery is per code object, not per call. LINE remains local.
        return sys.monitoring.DISABLE

    def _install_monitoring(self):
        monitoring = sys.monitoring
        # Python reserves slots 0..5; claim an unused slot rather than replacing
        # debugger/coverage/profiler callbacks already installed in this interpreter.
        for tool in range(5, -1, -1):
            try:
                monitoring.use_tool_id(tool, 'melty-local-debug')
                self._monitoring_tool = tool
                break
            except ValueError:
                continue
        else:
            raise RuntimeError('No free Python monitoring tool slot')
        try:
            monitoring.register_callback(tool, monitoring.events.LINE, self._on_debug_line)
            monitoring.register_callback(tool, monitoring.events.PY_RETURN, self._on_debug_return)
            monitoring.register_callback(tool, monitoring.events.PY_UNWIND, self._on_debug_return)
            monitoring.register_callback(tool, monitoring.events.PY_START, self._on_debug_start)
            monitoring.register_callback(tool, monitoring.events.INSTRUCTION, self._on_local_instruction)
            self._update_monitoring()
        except BaseException:
            self._remove_monitoring()
            raise

    def _update_monitoring(self, codes=None):
        if self._monitoring_tool is None or codes is not None and not codes:
            return
        events = sys.monitoring.events
        sys.monitoring.set_events(self._monitoring_tool,
            events.PY_START | (events.PY_UNWIND if self._step_mode != 'continue' else 0))
        for code in tuple(self._debug_codes) if codes is None else codes:
            lines = self._debug_codes[code]
            source = self._code_sources.get(code, self._debug_source)
            breakpoints = self._breakpoints_by_path.get(source.path, set())
            mask = events.PY_RETURN if self._step_mode != 'continue' else 0
            if not self.debug_enabled and code is self._debug_source.compiled:
                # Keep ordinary Run's resulting live values available to Locals.
                mask |= events.PY_RETURN
            if (lines.intersection(breakpoints) or self._step_mode != 'continue'
                    or self._stop_requested.is_set()):
                mask |= events.LINE
            if self._stop_requested.is_set():
                mask |= events.INSTRUCTION
            sys.monitoring.set_local_events(self._monitoring_tool, code, mask)
        if self._disabled_lines:
            # CPython's API reactivates disabled locations across tools. It
            # leaves registrations intact; each tool can disable its own
            # locations again. Required when stepping or changing breakpoints.
            sys.monitoring.restart_events()
            self._disabled_lines = False

    def _remove_monitoring(self):
        tool, self._monitoring_tool = self._monitoring_tool, None
        if tool is None:
            return
        sys.monitoring.set_events(tool, 0)
        for code in self._debug_codes:
            sys.monitoring.set_local_events(tool, code, 0)
        for event in (sys.monitoring.events.LINE, sys.monitoring.events.PY_RETURN,
                      sys.monitoring.events.PY_UNWIND, sys.monitoring.events.PY_START,
                      sys.monitoring.events.INSTRUCTION):
            sys.monitoring.register_callback(tool, event, None)
        sys.monitoring.free_tool_id(tool)

    def _on_local_instruction(self, code, instruction_offset):
        # LINE alone cannot stop a tight single-line loop. INSTRUCTION runs
        # before the opcode, preserving finally handlers (JUMP exceptions can
        # skip them on Python 3.12). Enable only while stopping tracked project
        # code, after library/native cleanup has returned.
        if (threading.get_ident() == self._debug_thread_id and self._executing
                and self._stop_requested.is_set()):
            self._cancel_local()

    def _cancel_local(self):
        # Inject once, allowing user finally/context-manager cleanup to run.
        # A task that deliberately catches BaseException can ignore Stop; this
        # remains cooperative cancellation, not a force-kill of app code.
        self._executing = False
        raise _LocalDebugStopped()

    def _execute_local(self, namespace, package):
        import builtins
        import traceback
        self._debug_thread_id = threading.get_ident()
        execution = threading.current_thread()
        local_builtins = dict(vars(builtins))
        result = 0
        try:
            if self._debug_source.text is None:
                from meltygui.editor.pending_save import PendingSave
                self._debug_source.text = PendingSave.current_file_text(self._debug_source.path)
                if self._debug_source.text is None:
                    raise OSError(f'Cannot read {self._debug_source.path}')
            parts = []
            parent = Path(self._debug_source.path).parent
            while (parent / '__init__.py').is_file():
                parts.insert(0, parent.name)
                parent = parent.parent
            if package is None:
                package = '.'.join(parts)
            paths = tuple(dict.fromkeys(str(path) for path in (
                Path(self._debug_source.path).parent, parent, Path(self._project_root),
                Path(self._project_root) / 'src') if path.is_dir()))
            namespace.update(__name__='__main__', __file__=self._debug_source.path,
                             __package__=package, __builtins__=local_builtins)
            source = self._debug_source.prepare()
            self._post(execution, 'prepared', source)
            self._resume_event.wait()
            if self._stop_requested.is_set():
                raise _LocalDebugStopped()
            self._resume_event.clear()
            with _local_execution_context(self, execution, paths):
                self._install_monitoring()
                self._executing = True
                try:
                    exec(source.compiled, namespace)
                finally:
                    self._executing = False
        except _LocalDebugStopped:
            result = -signal.SIGTERM
        except SystemExit as error:
            result = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
            if error.code is not None and not isinstance(error.code, int):
                self._post(execution, 'output', str(error.code) + '\n')
        except BaseException as error:
            result = 1
            self._post(execution, 'error', str(error))
            self._post(execution, 'output', traceback.format_exc())
        finally:
            self._remove_monitoring()
            self._paused_frame = self._step_frame = None
            self._debug_thread_id = None
            self._post(execution, 'exited', result)

    def _on_debug_line(self, code, line):
        if threading.get_ident() != self._debug_thread_id or not self._executing:
            return
        if self._stop_requested.is_set():
            self._cancel_local()
        frame = None
        source = self._code_sources.get(code, self._debug_source)
        hit = line in self._breakpoints_by_path.get(source.path, set())
        if not hit and self._step_mode == 'continue':
            # LINE can only be enabled per code object. Disable each unrelated
            # location after its first visit, so a hot loop does not call Python
            # for every iteration just because its function has a breakpoint.
            self._disabled_lines = True
            return sys.monitoring.DISABLE
        if hit or self._step_mode != 'continue':
            frame = sys._getframe(1)
            while frame is not None and frame.f_code is not code:
                frame = frame.f_back
        if frame is None:
            return
        stepping = (self._step_mode == 'step_into'
                    or self._step_mode == 'step_over' and frame is self._step_frame
                    or self._step_mode == 'step_out' and frame is self._step_frame)
        if not hit and not stepping:
            return
        self._pause_local(frame)

    def _on_debug_return(self, code, instruction_offset, value):
        if threading.get_ident() != self._debug_thread_id or not self._executing:
            return
        if self._stop_requested.is_set():
            self._cancel_local()
        frame = sys._getframe(1)
        if not self.debug_enabled and code is self._debug_source.compiled:
            self._post(self.process, 'inspection', LocalFrameInspection(
                frame, self._debug_source, self._code_sources))
        if frame is self._step_frame:
            self._step_frame = frame.f_back
        # PY_YIELD is deliberately not registered: suspension is not return.

    def _pause_local(self, frame):
        self._adopt_running_source()
        self._debug_source.prepare()
        self._paused_frame = frame
        self._resume_event.clear()
        inspection = LocalFrameInspection(frame, self._debug_source, self._code_sources)
        self._post(self.process, 'paused', inspection)
        self._resume_event.wait()  # releases the GIL; UI/other threads continue
        if self._stop_requested.is_set():
            self._cancel_local()
        self._step_frame = frame.f_back if self._step_mode == 'step_out' else frame
        self._update_monitoring()
        self._paused_frame = None
        self._post(self.process, 'resumed')



# ── the tasks ───────────────────────────────────────────────────────────────

def read_tasks(root):
    """{name: {'cmd': str, 'cwd': str, 'env': {str: str}}} from the project's
    pyproject.toml; {} without the table. Entries that are not a string or a
    table with a string `cmd` are skipped."""
    from meltygui_pro.models.project_kind import manifest_data
    tasks = dict(project_tasks.projects.get(str(Path(root).resolve()), {}))
    table = manifest_data(root)
    for key in ('tool', 'melty', 'tasks'):
        table = table.get(key, {}) if isinstance(table, dict) else {}
    if not isinstance(table, dict):
        return tasks
    for name, value in table.items():
        if isinstance(value, str):
            value = {'cmd': value}
        if not isinstance(value, dict) or not isinstance(value.get('cmd'), str):
            continue
        env = value.get('env', {})
        tasks[str(name)] = {
            'cmd': value['cmd'],
            'cwd': str(value.get('cwd', '.')),
            'env': {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}}
        if isinstance(value.get('module'), str):
            tasks[str(name)]['module'] = value['module']
    return tasks


def add_module_task(path, root, save=False):
    """Reuse a module's task, or add one without replacing a user's named task."""
    path, root = Path(path).resolve(), Path(root).resolve()
    if path.suffix.lower() != '.py' or not path.is_file():
        raise ValueError(f'Not a Python module: {path}')
    relative = path.relative_to(root).as_posix()
    existing = read_tasks(root)
    name = next((name for name, task in existing.items()
                 if task.get('module') == relative), None)
    if name is None:
        base = name = f'Run {relative}'
        suffix = 2
        while name in existing:
            name = f'{base} ({suffix})'
            suffix += 1
        parts, parent = [path.stem], path.parent
        while (parent / '__init__.py').is_file():
            parts.insert(0, parent.name)
            parent = parent.parent
        task = {'cmd': 'python -m ' + shlex.quote('.'.join(parts)),
                'cwd': os.path.relpath(parent, root), 'env': {}, 'module': relative}
    else:
        task = existing[name]
    project_tasks.projects.setdefault(str(root), {})[name] = dict(task)
    if save:
        save_module_task(root, name, task)
    return name


def save_module_task(root, name, task):
    """Edit the manifest's current buffer, preserving comments and pending edits."""
    import tomlkit
    from meltygui.code.fileref import writable_file_refusal
    from meltygui.code.project_code import project_code
    from meltygui_pro.models.project_toml import apply_plan
    path = Path(root) / 'pyproject.toml'
    refusal = writable_file_refusal(path)
    if refusal:
        raise ValueError(f'Cannot save task: {refusal}')
    before = (project_code[path].text() or '') if path.exists() else ''
    document = tomlkit.parse(before)
    table = document.setdefault('tool', tomlkit.table()).setdefault(
        'melty', tomlkit.table()).setdefault('tasks', tomlkit.table())
    table[name] = dict(task)
    apply_plan({'path': path, 'before': before, 'after': tomlkit.dumps(document),
                'summary': [name], 'created': not path.exists()})


# A fresh interpreter executes the current editor text with module/package context.
# The request travels through a temporary stdin file, not command-line size limits.
_MODULE_RUNNER = '''import json, sys, types
request = json.load(sys.stdin)
sys.path[:0] = request['paths']
sys.argv = [request['path']]
main = types.ModuleType('__main__')
sys.modules['__main__'] = main
namespace = main.__dict__
namespace.update(__file__=request['path'], __package__=request['package'], __spec__=None)
exec(compile(request['text'], request['path'], 'exec'), namespace)
'''


def module_request(root, relative):
    from meltygui_pro.models.project_function_runner import function_request, project_python
    from meltygui_pro.models.project_analysis import analysis_project
    path = (Path(root) / relative).resolve()
    if not path.is_file():
        raise ValueError(f'Module no longer exists: {path}')
    request = function_request(str(path), None)
    if request['text'] is None:
        raise ValueError(f'Cannot read module: {path}')
    request['paths'] = list(dict.fromkeys([
        str(path.parent), *analysis_project(path=root).source_paths, *request['paths']]))
    return [project_python(root) or sys.executable, '-u', '-c', _MODULE_RUNNER], request


def task_environment(root):
    """The run's environment: ours, with the project's venv first on PATH
    (`VIRTUAL_ENV` set) when the project has a usable one, unbuffered
    Python, and no inherited PYTHONPATH / PYTHONHOME."""
    from meltygui_pro.models.project_analysis import analysis_project
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    env['PYTHONUNBUFFERED'] = '1'
    project = analysis_project(path=root)
    if project.environment and project.environment_status in ('Selected', 'Automatic'):
        venv = Path(project.environment)
        scripts = venv / ('Scripts' if sys.platform == 'win32' else 'bin')
        env['PATH'] = str(scripts) + os.pathsep + env.get('PATH', '')
        env['VIRTUAL_ENV'] = str(venv)
    return env


# ── running ─────────────────────────────────────────────────────────────────

_LIVE = globals().get("_LIVE", set())      # the states with a running process, stopped at exit


@atexit.register
def _stop_all():
    for state in list(_LIVE):
        state.shutdown()


# ── the menu's requests ─────────────────────────────────────────────────────

# (root, name) posted by the Run menu; the first Tasks tile drawn takes it.
_pending = globals().get("_pending")
_pending_error = globals().get("_pending_error")
_pending_debug = globals().get("_pending_debug", False)
_last = globals().get("_last")       # (root, name) of the last run started anywhere (TaskState.run)
_TILES = globals().get("_TILES", {})        # tile instance -> its draw_state, to wake them for a request


def request_run(root, name, error=None, debug=False):
    """The menu's pick: the first Tasks tile drawn runs it. A pick produces
    no frame of its own, so the tiles are marked dirty and the window woken."""
    global _pending, _pending_error, _pending_debug
    from meltygui.core.windowing.glfw_utils import request_render
    _pending = (str(root), name)
    _pending_error = error
    _pending_debug = debug
    for tile_ds in _TILES.values():
        if Melty.cache is not None and tile_ds._tile_id is not None:
            Melty.cache.invalidate_up(tile_ds._tile_id, force=True, max_depth=4)
    request_render()


def request_module_run(path, root=None, debug=False):
    """The clicked editor's module becomes a project task and a queued run."""
    from editor_settings import settings
    root = str(Path(root or project_for(path) or Path(path).parent).resolve())
    try:
        name = add_module_task(path, root, save=settings['Tasks']['save_tasks'])
    except (OSError, ValueError, TypeError) as error:
        request_run(root, f'Run {Path(path).name}', error=str(error), debug=debug)
        return
    request_run(root, name, debug=debug)


def request_rerun():
    """The chord: the last task run anywhere, again."""
    if _last is not None:
        request_run(*_last)


def pending_run():
    return _pending


# ── the tile ────────────────────────────────────────────────────────────────

def tile_project(files_view, open_files):
    """Read the explicitly injected Files selection, with a standalone fallback."""
    selection = project_selection(files_view)
    if selection is not None:
        return str(selection.selected_project) if selection.selected_project else None
    path = open_files.active_path if open_files is not None else None
    if isinstance(path, str) and not path.startswith(OpenFiles.GIT_DIFF_PREFIX):
        root = project_for(path)
        if root:
            return str(root)
    saved = project_roots()
    return str(saved[0]) if saved else None


def selected_task(state, root, tasks):
    """Restore a project's picker; migrate an older tile's saved run selection."""
    selections = state.selected_tasks
    if state.project and state.task:
        selections.setdefault(str(state.project), state.task)
    if not root:
        return None
    root = str(root)
    if selections.get(root) not in tasks:
        selections[root] = next(iter(tasks), None)
    return selections[root]


def follow_output(state, output_ds, tolerance, running=False):
    """Follow new output until the reader moves upward; range clamps aren't input."""
    max_y = output_ds._max_scroll_y
    if max_y is None:
        return
    x, y = output_ds.scroll_offset
    previous_y = state._output_scroll_y
    if (previous_y is not None and y < min(previous_y, max_y) - tolerance):
        state._follow = False
    if running and state._follow and y < max_y - tolerance:
        output_ds.scroll_offset = (x, max_y)
    state._output_scroll_y = output_ds.scroll_offset[1]


def status_text(state):
    if state.error:
        return state.error
    if state.paused:
        return 'paused'
    if state.waiting_for_input:
        return 'waiting for input'
    if state.running:
        return 'running'
    if state.exit is not None:
        return f'exit {state.exit} · {state.ended - state.started:.1f} s'
    return ''


def task_input_layout(left, top, width):
    button_width, gap, height = Melty.px(50), Melty.px(4), Melty.px(28)
    field_width = max(0, width - 2 * (button_width + gap))
    return ((left, top, field_width, height),
            (left + field_width + gap, top, button_width, height),
            (left + field_width + button_width + 2 * gap, top, button_width, height))


def draw_task_input(state, view_state, draw_state, width, unique, paint=True):
    """Task-owned stdin, shared by the Tasks tile and detached Console view."""
    from meltygui.view.text_view import draw_text
    left, top = imgui.get_cursor_screen_pos()
    field, send, eof = task_input_layout(left, top, width)
    _, view_state.input_text, input_ds = draw_text(getattr(view_state, 'input_text', ''),
        name=f'console-input##{unique}', single_line=True, width=field[2], height=field[3],
        syntax_highlight=False, autocomplete=False, wrap=False, show_header=False,
        show_widgets=False, disable_scroll=True, return_extras=True)
    if not paint:
        view_state._input_view = input_ds
    imgui.set_cursor_screen_pos(send[:2])
    if flat_button('Send', draw_state, f'console-send::{unique}',
                   width=send[2], height=send[3], paint=paint):
        text, view_state.input_text = view_state.input_text, ''
        state.submit_input(text)
        state.output = (state.output + text + '\n')[-OUTPUT_LIMIT:]
        state._changed()
    imgui.set_cursor_screen_pos(eof[:2])
    if flat_button('EOF', draw_state, f'console-eof::{unique}',
                   width=eof[2], height=eof[3], paint=paint):
        state.close_input()


def task_toolbar_layout(left, top, width, height, toolbar_left, toolbar_height):
    """Live rectangles shared by toolbar input, paint and child placement."""
    px = Melty.px
    gap, button_w, button_h = px(4), px(30), px(24)
    left += min(toolbar_left, width)
    top += max(0, height - toolbar_height)
    width = max(0, width - toolbar_left)
    button_top = top + (toolbar_height - button_h) * 0.5
    navigation_w = 2 * button_w + 6 + gap
    selector_w = min(px(280), max(0, width - navigation_w - 3 * button_w - 3 * gap))
    selector_left = left + navigation_w
    controls_left = selector_left + selector_w + gap
    return ((left, button_top),
            (selector_left, top, selector_w, toolbar_height),
            (controls_left, button_top, button_w, button_h),
            (controls_left + button_w + gap, button_top, button_w, button_h),
            (controls_left + 2 * (button_w + gap), button_top, button_w, button_h))


def task_toolbar_buttons(state, view_state):
    """The same labels and styles feed input-only bodies and live painting."""
    stop_color = (0.90, 0.20, 0.18) if state.running else (0.42, 0.42, 0.42)
    return (
        (f'\uf04b', 'run', view_state._toolbar['has_tasks'] and not state.running,
         (0.16, 0.75, 0.30), (0.24, 0.90, 0.40)),
        (f'\uf188', 'debug', view_state._toolbar['has_tasks'] and not state.running,
         (0.90, 0.55, 0.16), (1.0, 0.70, 0.25)),
        (f'\uf04d', 'stop', state.running, stop_color, stop_color),
    )


def draw_tasks_overlay(draw_state, draw_list):
    """Paint toolbar chrome and retain its shadows at the current tile bounds."""
    from meltygui.core.cache.tile_marks import add_shadow, clear_shadows
    from meltygui.hdr_color import pack_color
    from meltygui.core.runtime.toggles import Tint
    from meltygui_pro.editor.code_editor import _draw_nav_buttons
    clear_shadows(draw_state, 'task_buttons')
    state = draw_state.misc.get('task_state')
    view_state = draw_state.misc.get('_task_view_state')
    if state is None or view_state is None or view_state._toolbar is None:
        clear_shadows(draw_state, 'nav_buttons')
        return
    toolbar = view_state._toolbar
    nav, selector, run, debug, stop = task_toolbar_layout(
        draw_state.abs_left, draw_state.abs_top, draw_state.width, draw_state.height,
        toolbar['left'], view_state._toolbar_height)
    _draw_nav_buttons(draw_state, *toolbar['nav_tints'], draw_list=draw_list, pos=nav)
    if not toolbar['has_tasks']:
        x, y, width, height = selector
        draw_list.push_clip_rect(x, y, x + width, y + height, True)
        try:
            draw_list.add_text(x, y + (height - imgui.get_text_line_height()) * 0.5,
                               pack_color(*Tint.dd_text(draw_state.current_tint), 1.0),
                               toolbar['empty_text'])
        finally:
            draw_list.pop_clip_rect()
    for rect, (label, name, enabled, color, text_color) in zip((run, debug, stop), task_toolbar_buttons(state, view_state)):
        x, y, width, height = rect
        flat_button(label, draw_state if enabled else None, view_id=None,
                    width=width, height=height, pos=(x, y), layout=False,
                    draw_list=draw_list, color=color, text_color=text_color,
                    hovered=None if enabled else False, tooltip=name.capitalize())
        add_shadow(rect, corner_radius=Melty.px(6), clip=draw_state.abs_clip_rect,
                   draw_state=draw_state, group='task_buttons')
    if state.waiting_for_input:
        top = draw_state.abs_top + max(Melty.px(OUTPUT_TOP),
            draw_state.height - view_state._toolbar_height - Melty.px(32))
        _, send, eof = task_input_layout(draw_state.abs_left, top, draw_state.width)
        for label, rect in (('Send', send), ('EOF', eof)):
            flat_button(label, draw_state, view_id=None, width=rect[2], height=rect[3],
                        pos=rect[:2], layout=False, draw_list=draw_list)


def draw_tasks_overlay_background(draw_state, draw_list):
    """Keep the cached console placed at live bounds during tile replay."""
    from meltygui.core.rendering.overlay import place_overlay_view, paint_cached_view
    view_state = draw_state.misc.get('_task_view_state')
    if view_state is None:
        return
    replay = getattr(draw_state, '_blit_served_frame', None) == Melty.frame_count
    if view_state._selector_view is not None and view_state._toolbar is not None:
        _, rect, _, _, _ = task_toolbar_layout(
            draw_state.abs_left, draw_state.abs_top, draw_state.width, draw_state.height,
            view_state._toolbar['left'], view_state._toolbar_height)
        place_overlay_view(view_state._selector_view, rect, draw_state.abs_clip_rect)
        if replay:
            paint_cached_view(view_state._selector_view)
    if view_state._output_view is None:
        return
    top = Melty.px(OUTPUT_TOP)
    state = draw_state.misc.get('task_state')
    input_h = Melty.px(32) if state is not None and state.waiting_for_input else 0
    height = max(0, draw_state.height - view_state._toolbar_height - top - input_h)
    if input_h and getattr(view_state, '_input_view', None) is not None:
        rect, _, _ = task_input_layout(draw_state.abs_left, draw_state.abs_top + top + height,
                                       draw_state.width)
        place_overlay_view(view_state._input_view, rect, draw_state.abs_clip_rect)
        if replay:
            paint_cached_view(view_state._input_view)
    place_overlay_view(view_state._output_view,
                       (draw_state.abs_left, draw_state.abs_top + top,
                        draw_state.width, height),
                       draw_state.abs_clip_rect)
    if replay:
        paint_cached_view(view_state._output_view)


@render_func(multi_instance=True, tint=(0.36, 0.47, 0.42), icon='', display_name='Tasks',
             selectable=False, disable_scroll=True, show_add_delete=False, is_tree=False,
             show_bg=False, shadow=False, show_header=False, use_cache=True, tile_toolbar=True,
             draw_overlay_background=draw_tasks_overlay_background,
             draw_overlay=draw_tasks_overlay)
def draw_tasks(input_value: object, draw_state, task_state: TaskState = None,
               _task_view_state: TaskViewState = None,
               files_view: DrawState[draw_project_tree] = None,
               header_height=28.0, tile_toolbar_rect=None, file_metadata=None, **kwargs):
    """The Tasks tile: output and status above a bottom task/run/stop toolbar. `input_value` is the tile's `OpenFiles`, returned unchanged."""
    global _pending, _pending_error, _pending_debug
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.text_view import draw_text
    state = task_state
    # Adopt the old public injection slot once during live definition updates.
    retired = draw_state.misc.pop('task_view_state', None)
    if isinstance(retired, TaskViewState):
        _task_view_state = draw_state.misc['_task_view_state'] = retired
    view_state = _task_view_state
    if view_state._execution is not state.process:
        view_state._execution = state.process
        view_state._follow, view_state._output_scroll_y = True, None
    state.ensure_runtime()
    action = None
    pending_start = None
    draw_state.misc['task_state'] = state
    _TILES[kwargs.get('instance')] = draw_state
    open_files = input_value if isinstance(input_value, OpenFiles) else None
    if _pending is not None:
        # The menu's request: the first Tasks tile drawn runs it.
        pending_root, name = _pending
        _pending = None
        if _pending_error is not None:
            state.selected_tasks[pending_root] = name
            state.project, state.task, state.error = pending_root, name, _pending_error
            _pending_error = None
        else:
            pending_start = (pending_root, name, _pending_debug, file_metadata)
        _pending_debug = False

    root = tile_project(files_view, open_files)
    tasks = read_tasks(root) if root else {}
    names = list(tasks)
    choice = selected_task(state, root, tasks)

    px = Melty.px
    body_left, body_top = imgui.get_cursor_screen_pos()
    width = draw_state.content_width or (draw_state.width or 240)
    header_h, unique = px(header_height), kwargs.get('instance')
    toolbar_x = 0 if tile_toolbar_rect is None else tile_toolbar_rect[0]
    if tile_toolbar_rect is not None:
        header_h = tile_toolbar_rect[3]
    view_state._toolbar_height = header_h
    toolbar_y = max(0, (draw_state.height or 0) - header_h)
    from meltygui_pro.editor.code_editor import _draw_nav_buttons, _nav_button_tints
    back_tint, forward_tint, _ = _nav_button_tints()
    view_state._toolbar = {
        'left': toolbar_x, 'nav_tints': (back_tint, forward_tint),
        'has_tasks': bool(names),
        'empty_text': NO_TASKS if root else 'Open a project to run tasks',
    }
    nav, selector, run, debug, stop = task_toolbar_layout(
        body_left, body_top, width, draw_state.height or 0, toolbar_x, header_h)
    imgui.set_cursor_screen_pos(nav)
    _draw_nav_buttons(draw_state, back_tint, forward_tint, paint=False)
    left, top, selector_w, _ = selector
    view_state._selector_view = None
    if names and selector_w > 0:
        imgui.set_cursor_screen_pos((left, top))
        picked, choice, view_state._selector_view = draw_dropdown(
            choice, collection=names, name='task', show_header=False,
            width=selector_w, height=header_h,
            trigger_height=header_h / Melty.ui_scale, shadow=False,
            return_extras=True)
        if picked and isinstance(choice, str):
            state.selected_tasks[str(root)] = choice
            draw_state.invalidate()
    for rect, (label, name, enabled, color, text_color) in zip((run, debug, stop), task_toolbar_buttons(state, view_state)):
        x, y, button_w, button_h = rect
        imgui.set_cursor_screen_pos((x, y))
        if flat_button(f'{label}##tasks-{name}{unique}', draw_state if enabled else None,
                       f'tasks-{name}::{unique}', width=button_w, height=button_h,
                       color=color, text_color=text_color, paint=False, tooltip=name.capitalize(),
                       hovered=None if enabled else False) and enabled:
            action = name

    # Keep the output view in the render/cache lifecycle even at zero height.
    status_h = px(20)
    if toolbar_y >= status_h:
        imgui.set_cursor_screen_pos((body_left, body_top))
        output_root = state.project or root
        imgui.text(f'{Path(output_root).name if output_root else "no project"}  {status_text(state)}')
    output_top = px(OUTPUT_TOP)
    input_h = px(32) if state.waiting_for_input else 0
    output_h = max(0, toolbar_y - output_top - input_h)
    view_state._input_view = None
    if input_h:
        imgui.set_cursor_screen_pos((body_left, body_top + output_top + output_h))
        draw_task_input(state, view_state, draw_state, width, unique, paint=False)
    imgui.set_cursor_screen_pos((body_left, body_top + output_top))
    _, _, output_ds = draw_text(state.output, name=f'task-output##{unique}',
                                width=draw_state.content_width, height=output_h,
                                editable=False, syntax_highlight=False,
                                autocomplete=False, wrap=True, show_header=False,
                                show_widgets=False, use_cache=True, freeze_resize=True, shadow=False,
                                return_extras=True)
    view_state._output_view = output_ds
    # Follow the tail while running; scrolling up stops following until the next run.
    if output_ds is not None:
        follow_output(view_state, output_ds, px(2), running=state.running)
    if action == 'stop':
        state.stop()
    elif action is not None:
        state.run(root, choice, debug=action == 'debug', file_metadata=file_metadata)
    elif pending_start is not None:
        state.run(*pending_start)
    return False, input_value


@no_save('_follow', '_output_scroll_y')
class ConsoleViewState(DictConversion):
    def __init__(self):
        super().__init__()
        self.input_text = ''
        self._follow = True
        self._output_scroll_y = None


@render_func(multi_instance=True, tint=(0.24, 0.31, 0.29), show_header=False,
             display_name="Console", icon="\uf120")
def draw_console(input_value: object, debugger_state: TaskState = None,
                 _console_view_state: ConsoleViewState = None, draw_state=None):
    """An independently scrollable view of one injected task session."""
    from meltygui.view.text_view import draw_text
    retired = draw_state.misc.pop('console_view_state', None)
    if isinstance(retired, ConsoleViewState):
        _console_view_state = draw_state.misc['_console_view_state'] = retired
    debugger_state.ensure_runtime()
    command = None
    left, top = imgui.get_cursor_screen_pos()
    px = Melty.px
    if debugger_state.debug_enabled:
        # FA5 arrows describe motion through the current frame; hover gives
        # the full command name. Keep the same compact size as task controls.
        controls = ((f'\uf04b', 'continue', 'Continue'),
                    (f'\uf063', 'step_into', 'Step into'),
                    (f'\uf064', 'step_over', 'Step over'),
                    (f'\uf062', 'step_out', 'Step out'))
        for index, (label, mode, tooltip) in enumerate(controls):
            imgui.set_cursor_screen_pos((left + index * px(34), top))
            if flat_button(label, draw_state if debugger_state.paused else None,
                           f'debug-{mode}', width=px(30), height=px(24), tooltip=tooltip,
                           hovered=None if debugger_state.paused else False) and debugger_state.paused:
                command = mode
        top += px(28)
        if debugger_state._external is not None and debugger_state.running:
            imgui.set_cursor_screen_pos((left, top))
            if flat_button('Force stop', draw_state, 'force-stop', width=px(98), height=px(24)):
                debugger_state.force_stop()
            top += px(28)
    imgui.set_cursor_screen_pos((left, top))
    imgui.text(status_text(debugger_state))
    if debugger_state.waiting_for_input:
        draw_task_input(debugger_state, _console_view_state, draw_state,
                        draw_state.content_width or draw_state.width or px(240), 'session')
        imgui.set_cursor_screen_pos((left, top + px(52)))
    _, _, output_ds = draw_text(debugger_state.output, name='session-output',
        editable=False, syntax_highlight=False, autocomplete=False, wrap=True,
        show_header=False, show_widgets=False, shadow=False, return_extras=True)
    if output_ds is not None:
        # View-local follow history; the shared session never owns this pane.
        follow_output(_console_view_state, output_ds, Melty.px(2), running=debugger_state.running)
    if command is not None:
        debugger_state.resume(command)
    return False, input_value


@render_func(multi_instance=True, tint=(0.29, 0.26, 0.37), show_header=False,
             display_name="Locals", icon="\uf03a")
def draw_locals(input_value: object, debugger_state: TaskState = None):
    """Ordinary renderer dispatch over shallow captured local bindings."""
    from meltygui.core.rendering.render_dispatch import draw_any
    debugger_state.ensure_runtime()
    inspection = debugger_state.inspection
    if inspection is not None:
        choices = {f'{scope.name}:{scope.line} ({index})': identity
                   for index, (identity, scope) in enumerate(inspection.scopes.items())}
        scope = debugger_state.selected_scope
        changed, identity = draw_dropdown(debugger_state.selected_scope_id,
            collection=choices, name='Frame', show_header=False,
            display_label=f'{scope.name}:{scope.line}' if scope is not None else 'Frame')
        if changed:
            debugger_state.select_scope(identity)
        scope = debugger_state.selected_scope
        if scope is not None:
            draw_any(scope.locals, name='locals', show_header=False, editable=False)
    return False, input_value
