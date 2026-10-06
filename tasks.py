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

One injected `TaskState` per tile stores the picker selection and delegates
execution to Pro's `ProjectExecution`. That model owns processes, worker threads,
output and debugging. Closing the tile does not stop a run: the state and its
execution come back with the tile.

The picker follows its injected Files view's selected project, falling
back to the selected tab or saved project when unlinked. Run identity and
output stay separate from the per-project picker selection.
The Run menu (editor.py) lists the same tasks and runs one in the first Tasks
tile through `request_run`.

The IDE process (thread) environment runs both Run and Debug in the IDE's
interpreter, and is the only environment on iOS.
Project imports and text console I/O are scoped to the execution thread, while
the module cache, app cwd and environment are shared. Shell tasks are refused.
"""
from meltygui_pro.models.project_execution import ProjectExecution, OUTPUT_LIMIT
from meltygui_pro.editor.execution_target import ExecutionTargetState, draw_execution_target, prepare_execution_target

import os
import shlex
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

RUN_CURRENT_FILE = 'Run Current File'
NO_TASKS = 'No tasks — right-click a Python module to Run'
OUTPUT_TOP = 24.0           # status line (20) and gap (4), logical pixels


class ProjectTasks(DictConversion):
    """Project root -> named task definitions, saved with the app's open files."""
    def __init__(self):
        super().__init__()
        self.projects = {}


# editor.py replaces this with the app session's persisted instance at startup.
project_tasks = globals().get("project_tasks", ProjectTasks())


@no_save('_follow', '_output_scroll_y', '_output_view', '_toolbar_height',
         '_toolbar', '_selector_view', '_target_view', '_execution', '_input_view')
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
        self._target_view = None
        self._execution = None
        self._input_view = None


class TaskState(ProjectExecution):
    """The Tasks tile's saved selection over a reusable project execution."""
    def __init__(self):
        super().__init__()
        self.selected_tasks = {}

    def on_tile_layout_event(self, event):
        """Layout policy: a hidden/replaced tile keeps its execution alive."""

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
        task = read_tasks(root).get(name)
        self.selected_tasks[str(root)] = name
        if task is None:
            self.task, self.project = name, str(root)
            self.error = f'No task {name!r} in {Path(root).name}'
            self._changed()
            return
        self.execute(root, task, name, debug, file_metadata, source_snapshot)

    def run_current_file(self, editor_view, *, debug=False, file_metadata=None):
        """Resolve the linked editor at click time and reuse its normal module task."""
        editor = editor_view.misc.get('file_editor_state') if editor_view is not None else None
        path = editor.selected_path if editor is not None else None
        if not path or editor.version != 'current':
            self.error = 'Select a current Python file in the linked File Editor.'
            self._changed()
            return
        root = current_file_project(editor_view)
        snapshot = None
        value = editor._file
        if value is not None and str(value.repo.root / value.path) == path and value.version == 'current':
            snapshot = value.source_snapshot()
        self.run_module(path, root, debug=debug, file_metadata=file_metadata,
                        source_snapshot=snapshot)
        # Keep the dynamic option selected; the concrete task is still available
        # in the list and remains the run identity for Rerun last task.
        self.selected_tasks[root] = RUN_CURRENT_FILE

    def _execution_started(self):
        global _last
        _last = (self.project, self.task)


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
        command = ('python -m ' + shlex.quote('.'.join(parts)) if len(parts) > 1
                   else 'python ' + shlex.quote(path.name))
        task = {'cmd': command,
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

def initialize_editor_link(tile):
    """Adopt Auto once for existing and new Tasks tiles; preserve later Unlinked."""
    from meltygui.core.layout.tile_links import AUTO
    from meltygui.state.view_reference import view_identifier
    if getattr(tile, 'tasks_editor_link_initialized', False):
        return
    key = view_identifier(draw_tasks)
    bindings = dict(tile.links.get(key, {}))
    bindings.setdefault('file_editor_view', AUTO)
    tile.links = {**tile.links, key: bindings}
    tile.tasks_editor_link_initialized = True


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


def current_file_project(editor_view):
    """Use the linked editor's project for both its execution target and run."""
    editor = editor_view.misc.get('file_editor_state') if editor_view is not None else None
    if editor is None or not editor.selected_path:
        return None
    source = (editor_view._kwargs or {}).get('files_view')
    selection = project_selection(source)
    root = selection.selected_project if selection is not None else None
    return str(Path(root or project_for(editor.selected_path) or Path(editor.selected_path).parent).resolve())


def task_choices(root):
    """The dynamic default precedes the project's persisted task definitions."""
    return {RUN_CURRENT_FILE: None, **(read_tasks(root) if root else {})}


def selected_task(state, root, tasks):
    """Restore a project's picker; migrate an older tile's saved run selection."""
    selections = state.selected_tasks
    if state.project and state.task:
        selections.setdefault(str(state.project), state.task)
    root = str(root or '')
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


def task_toolbar_layout(left, top, width, height, toolbar_left, toolbar_height, selector_width, target_width):
    """Live rectangles shared by toolbar input, paint and child placement."""
    px = Melty.px
    gap, button_w, button_h = px(4), px(30), px(24)
    left += min(toolbar_left, width)
    top += max(0, height - toolbar_height)
    width = max(0, width - toolbar_left)
    button_top = top + (toolbar_height - button_h) * 0.5
    available = max(0, width - 3 * button_w - 4 * gap)
    total = max(1, selector_width + target_width)
    scale = min(1, available / total)
    selector_w, target_w = selector_width * scale, target_width * scale
    selector_left = left
    target_left = selector_left + selector_w + gap
    controls_left = target_left + target_w + gap
    return ((selector_left, top, selector_w, toolbar_height),
            (target_left, top, target_w, toolbar_height),
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
    clear_shadows(draw_state, 'task_buttons')
    state = draw_state.misc.get('task_state')
    view_state = draw_state.misc.get('_task_view_state')
    if state is None or view_state is None or view_state._toolbar is None:
        return
    toolbar = view_state._toolbar
    selector, target, run, debug, stop = task_toolbar_layout(
        draw_state.abs_left, draw_state.abs_top, draw_state.width, draw_state.height,
        toolbar['left'], view_state._toolbar_height, *toolbar['picker_widths'])
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
    if view_state._toolbar is not None:
        selector, target, _, _, _ = task_toolbar_layout(
            draw_state.abs_left, draw_state.abs_top, draw_state.width, draw_state.height,
            view_state._toolbar['left'], view_state._toolbar_height, *view_state._toolbar['picker_widths'])
        for view, rect in ((view_state._selector_view, selector), (view_state._target_view, target)):
            if view is not None:
                place_overlay_view(view, rect, draw_state.abs_clip_rect)
                if replay:
                    paint_cached_view(view)
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


# File Editor consumes TaskState; defer its import and source annotation so both
# modules can be imported first, with links resolved after module initialization.
import file_editor


@render_func(multi_instance=True, tint=(0.36, 0.47, 0.42), icon='', display_name='Tasks',
             selectable=False, disable_scroll=True, show_add_delete=False, is_tree=False,
             show_bg=False, shadow=False, show_header=False, use_cache=True, tile_toolbar=True,
             draw_overlay_background=draw_tasks_overlay_background,
             draw_overlay=draw_tasks_overlay)
def draw_tasks(input_value: object, draw_state, task_state: TaskState = None,
               _task_view_state: TaskViewState = None,
               execution_target_state: ExecutionTargetState = None,
               files_view: DrawState[draw_project_tree] = None,
               file_editor_view: "DrawState[file_editor.draw_file_editor]" = None,
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
    tasks = task_choices(root)
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
    target_root = current_file_project(file_editor_view) if choice == RUN_CURRENT_FILE else root
    selected_target = state.selected_targets.get(str(target_root), '')
    prepared_target = prepare_execution_target(selected_target, str(target_root), execution_target_state, draw_state) if target_root else None
    picker_widths = (imgui.calc_text_size(choice if names else (NO_TASKS if root else 'Open a project to run tasks')).x + 30,
                     imgui.calc_text_size(prepared_target[2]).x + 30 if prepared_target else 0)
    view_state._toolbar = {
        'left': toolbar_x, 'picker_widths': picker_widths,
        'has_tasks': bool(names),
        'empty_text': NO_TASKS if root else 'Open a project to run tasks',
    }
    selector, target, run, debug, stop = task_toolbar_layout(
        body_left, body_top, width, draw_state.height or 0, toolbar_x, header_h, *picker_widths)
    left, top, selector_w, _ = selector
    view_state._selector_view = None
    if names and selector_w > 0:
        imgui.set_cursor_screen_pos((left, top))
        picked, choice, view_state._selector_view = draw_dropdown(
            choice, collection=names, name='task', show_header=False, trigger_caret=('', ''),
            width=selector_w, height=header_h,
            trigger_height=header_h / Melty.ui_scale, shadow=False,
            return_extras=True)
        if picked and isinstance(choice, str):
            state.selected_tasks[str(root or '')] = choice
            draw_state.invalidate()
    view_state._target_view = None
    if target_root and target[2] > 0:
        state.ensure_runtime()
        imgui.set_cursor_screen_pos(target[:2])
        picked, selected, view_state._target_view = draw_execution_target(
            state.selected_targets.get(str(target_root), ''), str(target_root), execution_target_state, draw_state,
            width=target[2], height=header_h, trigger_height=header_h / Melty.ui_scale,
            prepared=prepared_target, file_metadata=file_metadata, trigger_caret=('', ''))
        if picked:
            state.selected_targets[str(target_root)] = selected
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
    # Read-only draw_text retains selection/copy without opening the touch keyboard.
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
        if choice == RUN_CURRENT_FILE:
            state.run_current_file(file_editor_view, debug=action == 'debug', file_metadata=file_metadata)
        else:
            state.run(root, choice, debug=action == 'debug', file_metadata=file_metadata)
    elif pending_start is not None:
        if pending_start[1] == RUN_CURRENT_FILE:
            state.run_current_file(file_editor_view, debug=pending_start[2], file_metadata=file_metadata)
        else:
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
