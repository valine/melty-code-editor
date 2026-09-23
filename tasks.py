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

One injected `TaskState` per tile: the selected task name persists; the
process, its reader thread and the output are the session's. `start_task`
spawns the command in its own process group and a daemon thread that appends
the output and wakes the window; `stop_task` ends the group (the Stop button,
and every live run at app exit). Closing the tile does not stop a run: the
state, and the process with it, come back with the tile.

Before its first run the tile uses the nearest code editor's project, else
the selected tab's, else the first saved project. After a request it retains
that run's project so the label, task selection and output agree.
The Run menu (editor.py) lists the same tasks and runs one in the first Tasks
tile through `request_run`.
"""
import atexit
import codecs
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import tempfile
from contextlib import ExitStack
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
from project_tree import target_editor

OUTPUT_LIMIT = 200_000       # chars of output kept, the console's limit
KILL_AFTER = 3.0             # seconds between SIGTERM and SIGKILL
NO_TASKS = 'No tasks — right-click a Python module to Run'


class ProjectTasks(DictConversion):
    """Project root -> named task definitions, saved with the app's open files."""
    def __init__(self):
        super().__init__()
        self.projects = {}


# editor.py replaces this with the app session's persisted instance at startup.
project_tasks = ProjectTasks()


@no_save('process', 'thread', 'output', 'running', 'exit', 'started', 'ended', 'error', '_follow')
class TaskState(DictConversion):
    def __init__(self):
        super().__init__()
        self.task = None          # the selected task name (persists)
        self.project = None       # project of the selected/run task (persists)
        self.process = None       # subprocess.Popen while running
        self.thread = None        # the reader thread
        self.output = ''          # what the tile shows, the last OUTPUT_LIMIT chars
        self.running = False
        self.exit = None          # exit code of the last run
        self.started = 0.0        # time.monotonic() of the last start
        self.ended = 0.0
        self.error = None         # why the last start was refused
        self._follow = True       # the output pane follows the tail


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

_LIVE = set()      # the states with a running process, stopped at exit


def start_task(state, root, name):
    """Run task `name` of project `root` on `state`; a running one is stopped
    first. A refusal (unknown task, bad cwd, spawn failure) lands in
    `state.error`, not in an exception."""
    from meltygui.core.windowing.glfw_utils import request_render
    if state.running:
        stop_task(state)
        if state.thread is not None:
            state.thread.join(KILL_AFTER + 1.0)
    task = read_tasks(root).get(name)
    state.task = name
    state.project = str(root)
    state.error = None
    if task is None:
        state.error = f'No task {name!r} in {Path(root).name}'
        return
    cwd = Path(root) / task['cwd']
    if not cwd.is_dir():
        state.error = f'cwd is not a folder: {cwd}'
        return
    shell = ['cmd', '/c'] if sys.platform == 'win32' else ['sh', '-c']
    state.output = f'$ {task["cmd"]}\n'
    state.exit = None
    state.started, state.ended = time.monotonic(), 0.0
    state._follow = True
    try:
        env = task_environment(root)
        env.update(task['env'])
        with ExitStack() as stack:
            command, stdin = shell + [task['cmd']], subprocess.DEVNULL
            if 'module' in task:
                command, request = module_request(root, task['module'])
                stdin = stack.enter_context(tempfile.TemporaryFile())
                stdin.write(json.dumps(request).encode('utf-8'))
                stdin.seek(0)
            process = subprocess.Popen(
                command, cwd=str(cwd), env=env, stdin=stdin,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=False,
                start_new_session=True)
    except (OSError, ValueError) as error:
        state.error = str(error)
        return
    global _last
    state.process, state.running = process, True
    _last = (str(root), name)
    _LIVE.add(state)
    state.thread = threading.Thread(target=_read_output, args=(state, process),
                                    name='melty-task', daemon=True)
    state.thread.start()
    request_render()


def _read_output(state, process):
    """The reader thread: chunks into `state.output`, then the exit code.
    Only plain fields are written; the window is woken, never drawn."""
    from meltygui.core.windowing.glfw_utils import request_render
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    fd = process.stdout.fileno()
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        text = decoder.decode(chunk, final=not chunk)
        if text:
            state.output = (state.output + text)[-OUTPUT_LIMIT:]
            request_render()
        if not chunk:
            break
    process.stdout.close()
    state.exit = process.wait()
    state.ended = time.monotonic()
    state.running = False
    _LIVE.discard(state)
    request_render()


def stop_task(state):
    """End the run: SIGTERM to the process group, SIGKILL KILL_AFTER seconds
    later if it is still there. No-op without a live process."""
    process = state.process
    if process is None or process.poll() is not None:
        return
    try:
        if sys.platform == 'win32':
            process.terminate()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return

    def kill():
        if process.poll() is None:
            try:
                if sys.platform == 'win32':
                    process.kill()
                else:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
    threading.Timer(KILL_AFTER, kill).start()


@atexit.register
def _stop_all():
    for state in list(_LIVE):
        stop_task(state)


# ── the menu's requests ─────────────────────────────────────────────────────

# (root, name) posted by the Run menu; the first Tasks tile drawn takes it.
_pending = None
_pending_error = None
_last = None       # (root, name) of the last run started anywhere (start_task)
_TILES = {}        # tile instance -> its draw_state, to wake them for a request


def request_run(root, name, error=None):
    """The menu's pick: the first Tasks tile drawn runs it. A pick produces
    no frame of its own, so the tiles are marked dirty and the window woken."""
    global _pending, _pending_error
    from meltygui.core.windowing.glfw_utils import request_render
    _pending = (str(root), name)
    _pending_error = error
    for tile_ds in _TILES.values():
        if Melty.cache is not None and tile_ds._tile_id is not None:
            Melty.cache.invalidate_up(tile_ds._tile_id, force=True, max_depth=4)
    request_render()


def request_module_run(path, root=None):
    """The clicked editor's module becomes a project task and a queued run."""
    from editor_settings import settings
    root = str(Path(root or project_for(path) or Path(path).parent).resolve())
    try:
        name = add_module_task(path, root, save=settings['Tasks']['save_tasks'])
    except (OSError, ValueError, TypeError) as error:
        request_run(root, f'Run {Path(path).name}', error=str(error))
        return
    request_run(root, name)


def request_rerun():
    """The chord: the last task run anywhere, again."""
    if _last is not None:
        request_run(*_last)


def pending_run():
    return _pending


# ── the tile ────────────────────────────────────────────────────────────────

def tile_project(draw_state, open_files):
    """The nearest code editor's selected project, else the selected tab's,
    else the first saved project; None with nothing to go on."""
    editor = target_editor(draw_state)
    if editor is not None:
        project_state = editor.misc.get('project_state')
        folder = getattr(project_state, 'selected_project', None)
        if folder and Path(folder).is_dir():
            return str(folder)
    path = open_files.active_path if open_files is not None else None
    if isinstance(path, str) and not path.startswith(OpenFiles.GIT_DIFF_PREFIX):
        root = project_for(path)
        if root:
            return str(root)
    saved = project_roots()
    return str(saved[0]) if saved else None


def status_text(state):
    if state.error:
        return state.error
    if state.running:
        return f'running {time.monotonic() - state.started:.1f} s'
    if state.exit is not None:
        return f'exit {state.exit} · {state.ended - state.started:.1f} s'
    return ''


@render_func(multi_instance=True, tint=(0.36, 0.47, 0.42), icon='', display_name='Tasks',
             selectable=False, disable_scroll=True, show_add_delete=False, is_tree=False,
             show_bg=False, shadow=False, show_header=False, use_cache=False)
def draw_tasks(input_value: object, draw_state, task_state: TaskState = None,
               header_height=30.0, **kwargs):
    """The Tasks tile: project name, task dropdown, Run / Stop, status, and
    the output. `input_value` is the tile's `OpenFiles`, returned unchanged."""
    global _pending, _pending_error
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.text_view import draw_text
    state = task_state
    _TILES[kwargs.get('instance')] = draw_state
    open_files = input_value if isinstance(input_value, OpenFiles) else None
    if _pending is not None:
        # The menu's request: the first Tasks tile drawn runs it.
        pending_root, name = _pending
        _pending = None
        if _pending_error is not None:
            state.project, state.task, state.error = pending_root, name, _pending_error
            _pending_error = None
        else:
            start_task(state, pending_root, name)

    root = getattr(state, 'project', None) or tile_project(draw_state, open_files)
    tasks = read_tasks(root) if root else {}
    names = list(tasks)
    if state.task not in tasks and names and not state.running:
        state.task = names[0]

    px = Melty.px
    left, top = imgui.get_cursor_screen_pos()
    width = draw_state.content_width or (draw_state.width or 240)
    gap, header_h, unique = px(4), px(header_height), kwargs.get('instance')
    button_w = px(64)
    project_label = Path(root).name if root else 'no project'
    imgui.set_cursor_screen_pos((left, top + (header_h - imgui.get_text_line_height()) * 0.5))
    imgui.text(project_label)
    label_w = imgui.calc_text_size(project_label)[0] + gap * 2
    imgui.set_cursor_screen_pos((left + label_w, top))
    if names:
        picked, choice = draw_dropdown(state.task, collection=names, name='task', show_header=False,
                                       width=max(px(80), width - label_w - button_w - 2 * gap),
                                       trigger_height=header_height, shadow=False)
        if picked and isinstance(choice, str):
            state.task = choice
            draw_state.invalidate()
    else:
        imgui.set_cursor_screen_pos((left + label_w, top + (header_h - imgui.get_text_line_height()) * 0.5))
        imgui.text(NO_TASKS if root else 'Open a project to run tasks')
    imgui.set_cursor_screen_pos((left + width - button_w, top + (header_h - px(24)) * 0.5))
    if state.running:
        if flat_button(f'Stop##tasks-stop{unique}', draw_state, f'tasks-stop::{unique}',
                       width=button_w, height=24):
            stop_task(state)
    elif names and flat_button(f'Run##tasks-run{unique}', draw_state, f'tasks-run::{unique}',
                               width=button_w, height=24):
        start_task(state, root, state.task)

    status_h = px(20)
    imgui.set_cursor_screen_pos((left, top + header_h + gap))
    imgui.text(status_text(state))
    imgui.set_cursor_screen_pos((left, top + header_h + gap + status_h))
    output_h = max(px(40), (draw_state.height or 0) - header_h - status_h - 2 * gap - px(4))
    _, _, output_ds = draw_text(state.output, name=f'task-output##{unique}', width=width,
                                height=output_h, editable=False, syntax_highlight=False,
                                autocomplete=False, wrap=True, show_header=False,
                                show_widgets=False, use_cache=False, return_extras=True)
    # Follow the tail while running; scrolling up stops following until the next run.
    if output_ds is not None:
        max_y = getattr(output_ds, '_max_scroll_y', None)
        if max_y is not None:
            at_end = output_ds.scroll_offset[1] >= max_y - px(2)
            if state.running and state._follow and not at_end:
                output_ds.scroll_offset = (output_ds.scroll_offset[0], max_y)
            elif getattr(output_ds, 'scrolled', False) and not at_end:
                state._follow = False
    if state.running:
        draw_state.invalidate()
        request_render()
    return False, input_value
