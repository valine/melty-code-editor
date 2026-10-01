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
process, its reader thread and the output are the session's. `start_task`
spawns the command in its own process group and a daemon thread that appends
the output and wakes the window; `stop_task` ends the group (the Stop button,
and every live run at app exit). Closing the tile does not stop a run: the
state, and the process with it, come back with the tile.

The picker follows its injected Files view's selected project, falling
back to the selected tab or saved project when unlinked. Run identity and
output stay separate from the per-project picker selection.
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
project_tasks = ProjectTasks()


@no_save('process', 'thread', 'output', 'running', 'exit', 'started', 'ended', 'error',
         '_follow', '_output_scroll_y', '_output_view', '_toolbar_height', '_toolbar', '_selector_view')
class TaskState(DictConversion):
    # Defaults for existing live instances when these transient fields are added.
    _output_scroll_y = None
    _output_view = None
    _toolbar_height = 0
    _toolbar = None
    _selector_view = None

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
        self._follow = True       # the output pane follows the tail
        self._output_scroll_y = None  # frame history, meaningful only for this run
        self._output_view = None
        self._toolbar_height = 0
        self._toolbar = None
        self._selector_view = None


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
    state.selected_tasks[str(root)] = name
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
    state._output_scroll_y = None
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


def follow_output(state, output_ds, tolerance):
    """Follow new output until the reader moves upward; range clamps aren't input."""
    max_y = output_ds._max_scroll_y
    if max_y is None:
        return
    x, y = output_ds.scroll_offset
    previous_y = state._output_scroll_y
    if (previous_y is not None and y < min(previous_y, max_y) - tolerance):
        state._follow = False
    if state.running and state._follow and y < max_y - tolerance:
        output_ds.scroll_offset = (x, max_y)
    state._output_scroll_y = output_ds.scroll_offset[1]


def status_text(state):
    if state.error:
        return state.error
    if state.running:
        return f'running {time.monotonic() - state.started:.1f} s'
    if state.exit is not None:
        return f'exit {state.exit} · {state.ended - state.started:.1f} s'
    return ''


def task_toolbar_layout(left, top, width, height, toolbar_left, toolbar_height):
    """Live rectangles shared by toolbar input, paint and child placement."""
    px = Melty.px
    gap, button_w, button_h = px(4), px(30), px(24)
    left += min(toolbar_left, width)
    top += max(0, height - toolbar_height)
    width = max(0, width - toolbar_left)
    button_top = top + (toolbar_height - button_h) * 0.5
    navigation_w = 2 * button_w + 6 + gap
    selector_w = min(px(280), max(0, width - navigation_w - 2 * button_w - 2 * gap))
    selector_left = left + navigation_w
    controls_left = selector_left + selector_w + gap
    return ((left, button_top),
            (selector_left, top, selector_w, toolbar_height),
            (controls_left, button_top, button_w, button_h),
            (controls_left + button_w + gap, button_top, button_w, button_h))


def task_toolbar_buttons(state):
    """The same labels and styles feed input-only bodies and live painting."""
    stop_color = (0.90, 0.20, 0.18) if state.running else (0.42, 0.42, 0.42)
    return (
        ('\uf04b', 'run', state._toolbar['has_tasks'] and not state.running,
         (0.16, 0.75, 0.30), (0.24, 0.90, 0.40)),
        ('\uf04d', 'stop', state.running, stop_color, stop_color),
    )


def draw_tasks_overlay(draw_state, draw_list):
    """Paint toolbar chrome and retain its shadows at the current tile bounds."""
    from meltygui.core.cache.tile_marks import add_shadow, clear_shadows
    from meltygui.hdr_color import pack_color
    from meltygui.core.runtime.toggles import Tint
    from meltygui_pro.editor.code_editor import _draw_nav_buttons
    clear_shadows(draw_state, 'task_buttons')
    state = draw_state.misc.get('task_state')
    if state is None or state._toolbar is None:
        clear_shadows(draw_state, 'nav_buttons')
        return
    toolbar = state._toolbar
    nav, selector, run, stop = task_toolbar_layout(
        draw_state.abs_left, draw_state.abs_top, draw_state.width, draw_state.height,
        toolbar['left'], state._toolbar_height)
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
    for rect, (label, name, enabled, color, text_color) in zip((run, stop), task_toolbar_buttons(state)):
        x, y, width, height = rect
        flat_button(label, draw_state if enabled else None, view_id=None,
                    width=width, height=height, pos=(x, y), layout=False,
                    draw_list=draw_list, color=color, text_color=text_color,
                    hovered=None if enabled else False)
        add_shadow(rect, corner_radius=Melty.px(6), clip=draw_state.abs_clip_rect,
                   draw_state=draw_state, group='task_buttons')


def draw_tasks_overlay_background(draw_state, draw_list):
    """Keep the cached console placed at live bounds during tile replay."""
    from meltygui.core.rendering.overlay import place_overlay_view, paint_cached_view
    state = draw_state.misc.get('task_state')
    if state is None:
        return
    replay = getattr(draw_state, '_blit_served_frame', None) == Melty.frame_count
    if state._selector_view is not None and state._toolbar is not None:
        _, rect, _, _ = task_toolbar_layout(
            draw_state.abs_left, draw_state.abs_top, draw_state.width, draw_state.height,
            state._toolbar['left'], state._toolbar_height)
        place_overlay_view(state._selector_view, rect, draw_state.abs_clip_rect)
        if replay:
            paint_cached_view(state._selector_view)
    if state._output_view is None:
        return
    top = Melty.px(OUTPUT_TOP)
    height = max(0, draw_state.height - state._toolbar_height - top)
    place_overlay_view(state._output_view,
                       (draw_state.abs_left, draw_state.abs_top + top,
                        draw_state.width, height),
                       draw_state.abs_clip_rect)
    if replay:
        paint_cached_view(state._output_view)


@render_func(multi_instance=True, tint=(0.36, 0.47, 0.42), icon='', display_name='Tasks',
             selectable=False, disable_scroll=True, show_add_delete=False, is_tree=False,
             show_bg=False, shadow=False, show_header=False, use_cache=False, tile_toolbar=True,
             draw_overlay_background=draw_tasks_overlay_background,
             draw_overlay=draw_tasks_overlay)
def draw_tasks(input_value: object, draw_state, task_state: TaskState = None,
               files_view: DrawState[draw_project_tree] = None,
               header_height=28.0, tile_toolbar_rect=None, **kwargs):
    """The Tasks tile: output and status above a bottom task/run/stop toolbar. `input_value` is the tile's `OpenFiles`, returned unchanged."""
    global _pending, _pending_error
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.text_view import draw_text
    state = task_state
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
            start_task(state, pending_root, name)

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
    state._toolbar_height = header_h
    toolbar_y = max(0, (draw_state.height or 0) - header_h)
    from meltygui_pro.editor.code_editor import _draw_nav_buttons, _nav_button_tints
    back_tint, forward_tint, _ = _nav_button_tints()
    state._toolbar = {
        'left': toolbar_x, 'nav_tints': (back_tint, forward_tint),
        'has_tasks': bool(names),
        'empty_text': NO_TASKS if root else 'Open a project to run tasks',
    }
    nav, selector, run, stop = task_toolbar_layout(
        body_left, body_top, width, draw_state.height or 0, toolbar_x, header_h)
    imgui.set_cursor_screen_pos(nav)
    _draw_nav_buttons(draw_state, back_tint, forward_tint, paint=False)
    left, top, selector_w, _ = selector
    state._selector_view = None
    if names and selector_w > 0:
        imgui.set_cursor_screen_pos((left, top))
        picked, choice, state._selector_view = draw_dropdown(
            choice, collection=names, name='task', show_header=False,
            width=selector_w, height=header_h,
            trigger_height=header_h / Melty.ui_scale, shadow=False,
            return_extras=True)
        if picked and isinstance(choice, str):
            state.selected_tasks[str(root)] = choice
            draw_state.invalidate()
    for rect, (label, name, enabled, color, text_color) in zip((run, stop), task_toolbar_buttons(state)):
        x, y, button_w, button_h = rect
        imgui.set_cursor_screen_pos((x, y))
        if flat_button(f'{label}##tasks-{name}{unique}', draw_state if enabled else None,
                       f'tasks-{name}::{unique}', width=button_w, height=button_h,
                       color=color, text_color=text_color, paint=False,
                       hovered=None if enabled else False) and enabled:
            if name == 'run':
                start_task(state, root, choice)
            else:
                stop_task(state)

    # Keep the output view in the render/cache lifecycle even at zero height.
    status_h = px(20)
    if toolbar_y >= status_h:
        imgui.set_cursor_screen_pos((body_left, body_top))
        output_root = state.project or root
        imgui.text(f'{Path(output_root).name if output_root else "no project"}  {status_text(state)}')
    output_top = px(OUTPUT_TOP)
    output_h = max(0, toolbar_y - output_top)
    imgui.set_cursor_screen_pos((body_left, body_top + output_top))
    _, _, output_ds = draw_text(state.output, name=f'task-output##{unique}',
                                width=draw_state.content_width, height=output_h,
                                editable=False, syntax_highlight=False,
                                autocomplete=False, wrap=True, show_header=False,
                                show_widgets=False, use_cache=True, freeze_resize=True, shadow=False,
                                return_extras=True)
    state._output_view = output_ds
    # Follow the tail while running; scrolling up stops following until the next run.
    if output_ds is not None:
        follow_output(state, output_ds, px(2))
    if state.running:
        draw_state.invalidate()
        request_render()
    return False, input_value
