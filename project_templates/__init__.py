"""Project templates: one function per template.

Each sub-folder of this package is a template. Its ``__init__.py`` defines

    def create(name: str, ...) -> dict

which returns the whole project as ``{relative path: str | bytes}``; the New
Project window (new_project.py) writes that dict out as a directory. A
template can be static (``static_files``: a ``files`` folder beside it, with
``{{placeholders}}``) or build its dict with code, or both. To add a
template, add a folder with a ``create`` function. Nothing else to register.

The function is the form:

* The first docstring line is the template's title, the rest its description.
* ``name`` (the project's folder name) and ``python_version`` ('3.12') are
  filled from the window's own fields when the function declares them.
* Every other parameter is an input of the window, drawn from its annotation
  and default: ``str`` a text field, ``bool`` a checkbox, ``Literal[...]`` a
  dropdown. ``Annotated[T, 'Label']`` overrides the label made from the name.
* The first ``.py`` file of the dict is opened in the editor afterwards.
* A ``requirements.txt`` in the dict is what "Install dependencies" installs.
* ``ORDER`` (a module attribute, default 100) sorts the template list.

This package draws nothing and imports no toolkit: ``build`` and
``write_project`` are usable from a script or a test.
"""
import dataclasses
import importlib
import inspect
import os
import pathlib
import pkgutil
import re
import typing

# Filled by the window, never drawn as template inputs.
RESERVED = ('name', 'python_version')


@dataclasses.dataclass(frozen=True)
class TemplateInput:
    name: str
    label: str
    kind: str                 # 'str' | 'bool' | 'choice'
    default: object
    choices: tuple = ()


@dataclasses.dataclass(frozen=True)
class Template:
    id: str                   # the sub-folder's name
    title: str
    description: str
    create: typing.Callable
    inputs: tuple             # of TemplateInput, in parameter order
    order: int = 100


def template_inputs(create):
    """The window inputs of a template function: its non-reserved parameters."""
    hints = typing.get_type_hints(create, include_extras=True)
    inputs = []
    for parameter in inspect.signature(create).parameters.values():
        if parameter.name in RESERVED or parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(parameter.name, str)
        label = parameter.name.replace('_', ' ').capitalize()
        if typing.get_origin(annotation) is typing.Annotated:
            annotation, *extras = typing.get_args(annotation)
            label = next((extra for extra in extras if isinstance(extra, str)), label)
        default = None if parameter.default is parameter.empty else parameter.default
        if typing.get_origin(annotation) is typing.Literal:
            choices = typing.get_args(annotation)
            inputs.append(TemplateInput(parameter.name, label, 'choice',
                                        choices[0] if default is None else default, choices))
        elif annotation is bool:
            inputs.append(TemplateInput(parameter.name, label, 'bool', bool(default)))
        else:
            inputs.append(TemplateInput(parameter.name, label, 'str', '' if default is None else str(default)))
    return tuple(inputs)


def templates():
    """Every template, by ORDER then title. A folder that fails to import or has
    no ``create`` is skipped with a printed reason: one broken template must
    not take the window down."""
    found = []
    for info in pkgutil.iter_modules(__path__):
        if not info.ispkg:
            continue
        try:
            module = importlib.import_module(f'{__name__}.{info.name}')
            create = module.create
            doc = inspect.getdoc(create) or info.name
            title, _, description = doc.partition('\n')
            found.append(Template(info.name, title.strip().rstrip('.'), ' '.join(description.split()),
                                  create, template_inputs(create), getattr(module, 'ORDER', 100)))
        except Exception as error:
            print(f'project_templates: skipped {info.name}: {error!r}')
    return sorted(found, key=lambda template: (template.order, template.title.casefold()))


def build(template, name, python_version, values=None):
    """Call the template and check its dict: {relative posix path: str | bytes}."""
    context = {'name': name, 'python_version': python_version, **(values or {})}
    accepted = inspect.signature(template.create).parameters
    files = template.create(**{key: value for key, value in context.items() if key in accepted})
    if not isinstance(files, dict) or not files:
        raise ValueError(f'{template.title}: create() must return a non-empty dict of files.')
    for relative, content in files.items():
        path = pathlib.PurePosixPath(relative)
        if not isinstance(relative, str) or path.is_absolute() or '..' in path.parts or not path.parts:
            raise ValueError(f'{template.title}: {relative!r} is not a path inside the project.')
        if not isinstance(content, (str, bytes)):
            raise ValueError(f'{template.title}: {relative} must be str or bytes, not {type(content).__name__}.')
    return files


def project_destination(location, name):
    """Where the project goes, or a ValueError the window shows as-is."""
    name = name.strip()
    if not name:
        raise ValueError('Enter a project name.')
    if name in ('.', '..') or pathlib.Path(name).name != name or '\\' in name:
        raise ValueError('The name is a single folder name, for example my-project.')
    folder = pathlib.Path(location.strip() or '.').expanduser()
    if not folder.is_dir():
        raise ValueError(f'{folder} is not a folder.')
    destination = folder / name
    if os.path.lexists(destination):
        raise ValueError(f'{destination} already exists.')
    return destination


def write_project(files, destination):
    """Write a template's dict as the new directory `destination`. Never
    writes into an existing folder. Returns the first .py file (or None)."""
    destination = pathlib.Path(destination)
    destination.mkdir(parents=True)          # FileExistsError: refuse to merge
    for relative, content in files.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding='utf-8')
        if content[:2] in ('#!', b'#!'):
            path.chmod(path.stat().st_mode | 0o111)
    return next((destination / relative for relative in files if relative.endswith('.py')), None)


def static_files(template_file, **substitutions):
    """The ``files`` folder beside `template_file` (pass ``__file__``) as a
    template dict. ``{{key}}`` is replaced in paths and in UTF-8 text; files
    that are not text are kept as bytes."""
    root = pathlib.Path(template_file).parent / 'files'

    def fill(text):
        return re.sub(r'\{\{(\w+)\}\}', lambda match: str(substitutions.get(match[1], match[0])), text)

    files = {}
    for path in sorted(root.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts:
            continue
        data = path.read_bytes()
        try:
            content = fill(data.decode('utf-8'))
        except UnicodeDecodeError:
            content = data
        files[fill(path.relative_to(root).as_posix())] = content
    return files


def identifier(name):
    """`name` as a Python identifier / package name: 'My project' -> 'my_project'."""
    slug = re.sub(r'\W+', '_', name.strip()).strip('_').lower() or 'project'
    return '_' + slug if slug[0].isdigit() else slug
