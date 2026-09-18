"""A generated template: the files are built here, from the inputs and from
the meltygui install this editor runs on."""
import importlib.util
import pathlib
from typing import Annotated, Literal

from project_templates import identifier

ORDER = 20

APP = '''\
#!/usr/bin/env python3
"""{name}: a Melty app.

    .venv/bin/python app.py
"""
import meltygui
from meltygui import glfw_window, imgui
from meltygui.core.core_render import render_func
from meltygui.core.conversion.dict_conversion import DictConversion


class AppState(DictConversion):
    """The window's state, injected into `render` by its annotation. It lives
    across frames and is saved between runs: declare every field in __init__."""

    def __init__(self):
        super().__init__()
        self.notes = "Hello from {name}\\n"


@glfw_window(name="{name}", app_id="{app_id}", width=900, height=700)
@render_func(use_cache=False)
def render(input_value=None, state: AppState = None):
    """Drawn every frame. Return (changed, value)."""
    imgui.text("{name}: edit render() in app.py")
    changed, state.notes = meltygui.draw_text(state.notes, name="notes", syntax_highlight=False)
    return changed, input_value
'''

README = '''\
# {name}

A Melty app: `app.py` draws one window from a `render` function and an
injected, persisted `AppState`.

    uv venv --python {python_version} .venv
    uv pip install --python .venv/bin/python -r requirements.txt
    .venv/bin/python app.py
'''

GITIGNORE = '.venv*/\n__pycache__/\n*.pyc\nimgui.ini\n'


def meltygui_requirements(extra=''):
    """requirements.txt lines for meltygui. An editable checkout (the editor's
    own development install) is referenced by path, as the editor's
    requirements.txt does; a released install by name."""
    spec = importlib.util.find_spec('meltygui')
    checkout = pathlib.Path(spec.origin).parents[1] if spec and spec.origin else None
    if checkout is None or not (checkout / 'pyproject.toml').is_file():
        return [f'meltygui{extra}']
    lines = ['# meltygui from the checkout the code editor runs on (not yet published).']
    if (checkout / 'dist' / 'release').is_dir():
        lines.append(f'--find-links {checkout / "dist" / "release"}')
    return lines + [f'-e {checkout}{extra}']


def create(name: str, python_version: str,
           torch: Annotated[Literal['None', 'Latest', '2.9', '2.8', '2.7', '2.6', '2.5'], 'Torch version'] = 'None'):
    """Melty App.

    A MeltyGUI window: a render function, a persisted state object and a
    requirements.txt, with torch for tensor views when a version is selected.
    """
    requirements = meltygui_requirements('' if torch == 'None' else '[tensor]')
    if torch == 'Latest':
        requirements.append('torch')
    elif torch != 'None':
        requirements.append(f'torch=={torch}.*')
    return {
        'app.py': APP.format(name=name, app_id=identifier(name).replace('_', '-')),
        'requirements.txt': '\n'.join(requirements) + '\n',
        'README.md': README.format(name=name, python_version=python_version),
        '.gitignore': GITIGNORE,
    }
