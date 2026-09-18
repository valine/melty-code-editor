"""File → New Project…: pick a template, fill in its inputs, create it.

The templates are the functions of `project_templates` (one sub-folder each);
the window draws the fields every project has (name, location, Python,
virtual environment) and then one input per parameter of the selected
template's function. Create runs on a background thread: build the
template's dict, write it as the new folder, make `.venv` with uv, install
`requirements.txt`. The view returns `(True, project root)` once the folder
exists; editor.py marks it as a project and opens `main_files[root]`.
"""
import pathlib
import re
import shutil
import subprocess
import sys

from meltygui import imgui, draw_file_selector
from meltygui.core.runtime.background import Background
from meltygui.hdr_color import pack_color
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.runtime.toggles import Tint
from meltygui.core.core_render import render_func
from meltygui.core.rendering.core_decoration import no_save
from meltygui.view.header_view import flat_button
from meltygui.view.dropdown_view import draw_dropdown
from meltygui.view.control_view import draw_str, draw_bool
from meltygui_pro.models.project_environments import create_environment, installed_pythons

import project_templates

ENVIRONMENT_NAME = '.venv'
# project root -> the file to open in the editor (the template's first .py), or None.
# Beside the return value because a view returns its input's type: the root, a str.
main_files = {}
# What the running job is doing, by job request number (written by the job's thread).
_stages = {}


def python_version_of(label):
    """'3.12' from an interpreter label ('Python 3.12.3'); the editor's own without one."""
    match = re.search(r'(\d+)\.(\d+)', label or '')
    return f'{match[1]}.{match[2]}' if match else f'{sys.version_info[0]}.{sys.version_info[1]}'


def install_requirements(root, environment):
    uv = shutil.which('uv')
    if not uv:
        raise ValueError('uv is not installed or is not on PATH.')
    python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    result = subprocess.run([uv, 'pip', 'install', '--python', str(python), '-r', 'requirements.txt'],
                            cwd=root, close_fds=False, capture_output=True, text=True, timeout=3600)
    if result.returncode:
        # uv states the cause first; its tree glyphs are not in the UI font.
        output = result.stderr or result.stdout or 'Installing the requirements failed.'
        raise ValueError(re.sub(r'[\u2500-\u25ff\u00d7]', '', output).strip()[:1200])


def create_project(input_value):
    """The Create job. Once the folder is written the project exists: a later
    step that fails (environment, install) is reported, not rolled back."""
    template_id, location, name, python, python_label, venv, install, values, request, tile_id = input_value

    def stage(text):
        from meltygui.core.melty import Melty
        from meltygui.core.windowing.glfw_utils import request_render
        _stages[request] = text
        Melty.cache.invalidate_up(tile_id, max_depth=5)
        request_render()

    root = main_file = None
    try:
        template = next(item for item in project_templates.templates() if item.id == template_id)
        destination = project_templates.project_destination(location, name)
        files = project_templates.build(template, name.strip(), python_version_of(python_label), dict(values))
        stage('Writing files…')
        main_file = project_templates.write_project(files, destination)
        root = str(destination)
        result = {'ok': True, 'root': root, 'open': str(main_file) if main_file else None,
                  'message': f'Created {destination}.'}
        if venv:
            stage('Creating the virtual environment…')
            created = create_environment((root, ENVIRONMENT_NAME, python, request))
            if not created['ok']:
                raise ValueError(created['message'])
            if install and 'requirements.txt' in files:
                stage('Installing dependencies (this can take a while)…')
                install_requirements(destination, destination / ENVIRONMENT_NAME)
        return result
    except Exception as error:
        return {'ok': False, 'root': root, 'open': str(main_file) if main_file else None, 'message': str(error)}
    finally:
        _stages.pop(request, None)


@no_save('job', 'pythons', 'python_error', 'scanning', 'message', 'browse_request', 'seeded', 'created', 'last_frame')
class NewProjectState(DictConversion):
    def __init__(self):
        super().__init__()
        self.template = ''
        self.name = 'my-project'
        self.location = ''
        self.python = ''
        self.venv = True
        self.install = True
        # Template inputs, '<template>.<parameter>' -> value: each template keeps its own.
        self.values = {}
        self.job = None
        self.request = 0
        self.message = None
        self.pythons = None
        self.python_error = None
        self.scanning = True
        self.scan_request = 0
        self.browse_request = 0
        self.seeded = False
        # The project a job wrote before a later step failed: the window shows what failed.
        self.created = None
        self.last_frame = 0


@render_func(tint=(0.22, 0.38, 0.44), selectable=False, show_bg=True)
def draw_new_project(input_value: str, draw_state, project_state: NewProjectState = None):
    """`input_value` is the default location (a folder). Returns (True, project
    root) on the frame the project's folder was written, else (False, input_value)."""
    state = project_state
    created = None
    browse = False
    available = project_templates.templates()
    from meltygui.core.melty import Melty
    if Melty.frame_count - state.last_frame > 2 and state.job is None:
        state.message = state.created = None          # reopened: a fresh form
    state.last_frame = Melty.frame_count
    if not state.seeded:
        # Once per run, not per frame: a half-typed location is not a folder either.
        state.seeded = True
        if not state.location or not pathlib.Path(state.location).expanduser().is_dir():
            state.location = input_value or str(pathlib.Path.home())
    template = next((item for item in available if item.id == state.template), available[0] if available else None)

    def process_jobs():
        nonlocal created
        if state.scanning:
            result = Background.run(installed_pythons, user_id=f'new-project-python:{draw_state.id}',
                                    func_kwargs={'input_value': state.scan_request},
                                    invalidate_id=draw_state._tile_id, debounce=None)
            if isinstance(result, dict):
                state.pythons = result['choices']
                state.python_error = result['error']
                state.scanning = False
                if state.python not in state.pythons.values():
                    current = str(pathlib.Path(sys.executable).resolve())
                    state.python = current if current in state.pythons.values() else next(iter(state.pythons.values()), '')
        if state.job is not None:
            result = Background.run(create_project, user_id=f'new-project:{draw_state.id}',
                                    func_kwargs={'input_value': state.job},
                                    invalidate_id=draw_state._tile_id, debounce=None)
            if isinstance(result, dict):
                state.job = None
                state.message = result['message']
                if result['root']:
                    created = result['root']
                    main_files[created] = result['open']
                if result['ok']:
                    state.message = None
                    draw_state.closed = True
                elif result['root']:
                    state.created = result['root']

    process_jobs()
    left, top = imgui.get_cursor_screen_pos()
    left += 24
    top += 20
    width = draw_state.content_width - 48
    row = 28
    draw_list = imgui.get_window_draw_list()
    color = pack_color(*Tint.dd_text(), 1)
    muted = pack_color(*Tint.dd_text(), 0.65)

    def label(text, subdued=False, x=0.0, advance=True):
        nonlocal top
        draw_list.push_clip_rect(left, top, left + width, top + 24, True)
        draw_list.add_text(left + x, top + (5 if not advance else 0), muted if subdued else color, text)
        draw_list.pop_clip_rect()
        if advance:
            top += 24

    def wrapped(text, subdued=True):
        line = ''
        for word in text.split():
            if line and imgui.calc_text_size(line + ' ' + word)[0] > width:
                label(line, subdued)
                line = word
            else:
                line = f'{line} {word}'.strip()
        if line:
            label(line, subdued)

    def button(text, identity, x, y, button_width):
        imgui.set_cursor_screen_pos((x, y))
        return flat_button(text, draw_state, view_id=identity, width=button_width,
                           height=row) and state.job is None

    def text_field(value, identity, field_width):
        imgui.set_cursor_screen_pos((left, top))
        edited, new = draw_str(value, name=identity, width=field_width, height=row, show_header=False)
        return new if edited else value

    def dropdown(value, choices, identity, field_width):
        imgui.set_cursor_screen_pos((left, top))
        picked, new = draw_dropdown(value, collection=choices, name=identity, show_header=False,
                                    width=field_width, trigger_height=row, shadow=False)
        return new if picked else value

    def checkbox(value, text, identity):
        nonlocal top
        # draw_bool right-aligns its box in a cell of at least 83 px: start the
        # cell early so the box itself sits on the form's left edge.
        imgui.set_cursor_screen_pos((left - 53, top))
        toggled, new = draw_bool(bool(value), name=identity, width=83, height=row - 4, show_header=False)
        label(text, x=40, advance=False)
        top += row + 6
        return new if toggled else value

    if template is None:
        label('No project templates found.')
        label(str(pathlib.Path(project_templates.__file__).parent), True)
    elif state.job is not None:
        label(f'Creating {state.job[2]}…')
        label(_stages.get(state.job[8], 'Starting…'), True)
    elif state.created:
        label('Created ' + state.created)
        label('The project is open in the editor, but a later step failed:', True)
        top += 8
        for line in (state.message or '').splitlines()[:16]:
            wrapped(line)
        top += 12
        if button('Close', 'close-result', left + width - 148, top, 148):
            state.message = state.created = None
            draw_state.closed = True
        top += row + 10
    else:
        label('Template')
        state.template = dropdown(template.id, {item.title: item.id for item in available}, 'template', width)
        top += row + 8
        wrapped(template.description)
        top += 12

        label('Name')
        state.name = text_field(state.name, 'project-name', width).replace('\n', '')
        top += row + 12
        label('Location')
        state.location = text_field(state.location, 'project-location', width - 112).replace('\n', '')
        if button('Browse…', 'browse-location', left + width - 100, top, 100):
            state.browse_request += 1
            browse = True
        top += row + 8
        try:
            problem = None
            label(str(project_templates.project_destination(state.location, state.name)), True)
        except ValueError as error:
            problem = str(error)
            label(problem, True)
        top += 12

        label('Python')
        if state.scanning:
            label('Finding installed interpreters…', True)
        elif state.pythons:
            state.python = dropdown(state.python, state.pythons, 'python-version', width - 112)
            if button('Refresh', 'refresh-python', left + width - 100, top, 100):
                state.scan_request += 1
                state.scanning = True
            top += row + 8
            label(state.python, True)
        else:
            label(state.python_error or 'No installed Python interpreters found.', True)
            if button('Try again', 'retry-python', left, top, 100):
                state.scan_request += 1
                state.scanning = True
            top += row + 8
        top += 8
        state.venv = checkbox(state.venv, f'Create a virtual environment ({ENVIRONMENT_NAME})', 'create-venv')
        if state.venv:
            state.install = checkbox(state.install, 'Install dependencies (requirements.txt)', 'install-dependencies')

        values = {}
        for item in template.inputs:
            key = f'{template.id}.{item.name}'
            value = state.values.get(key, item.default)
            if item.kind == 'bool':
                value = checkbox(value, item.label, key)
            else:
                top += 4
                label(item.label)
                if item.kind == 'choice':
                    if value not in item.choices:
                        value = item.default
                    value = dropdown(value, {str(choice): choice for choice in item.choices}, key, width)
                else:
                    value = text_field(str(value), key, width).replace('\n', '')
                top += row + 8
            state.values[key] = values[item.name] = value

        top += 10
        python_label = next((text for text, path in (state.pythons or {}).items() if path == state.python), '')
        ready = problem is None and (not state.venv or (not state.scanning and state.python and state.pythons))
        if ready and button('Create', 'create', left + width - 148, top, 148):
            state.request += 1
            state.message = None
            state.job = (template.id, state.location, state.name, state.python, python_label, state.venv,
                         state.venv and state.install, tuple(values.items()), state.request, draw_state._tile_id)
        top += row + 10
    if state.message and not state.created:
        top += 8
        for line in state.message.splitlines():
            wrapped(line)
    imgui.set_cursor_screen_pos((left, top))
    imgui.dummy(width, 20)

    picked, folder = draw_file_selector(
        state.location, name='Choose project location', glfw_window=True,
        open_requested=browse, choose_folder=True, window_size=(720, 640),
        browse=(state.location, state.browse_request))
    if picked:
        state.location = folder
    process_jobs()
    return created is not None, created if created is not None else input_value
