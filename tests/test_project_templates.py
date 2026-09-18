"""project_templates draws nothing: run with `.venv/bin/python -m pytest tests`."""
import ast
import pathlib
import sys
from typing import Annotated, Literal

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import project_templates as pt


def by_id():
    return {template.id: template for template in pt.templates()}


def test_every_sub_folder_is_a_template_in_order():
    assert [template.id for template in pt.templates()] == ['python_basic', 'melty_app']
    assert by_id()['melty_app'].title == 'Melty App'


def test_parameters_are_the_inputs():
    def create(name: str, python_version: str, port: str = '8000', docker: bool = True,
               kind: Annotated[Literal['api', 'cli'], 'Project kind'] = 'cli'):
        """Demo."""
    inputs = pt.template_inputs(create)
    assert [(item.name, item.label, item.kind, item.default) for item in inputs] == [
        ('port', 'Port', 'str', '8000'), ('docker', 'Docker', 'bool', True),
        ('kind', 'Project kind', 'choice', 'cli')]
    assert inputs[2].choices == ('api', 'cli')


def test_python_project_is_static_files_with_placeholders(tmp_path):
    template = by_id()['python_basic']
    files = pt.build(template, 'My demo', '3.12')
    assert 'tests/test_main.py' not in files
    assert 'name = "my-demo"' in files['pyproject.toml'] and '>=3.12' in files['pyproject.toml']
    files = pt.build(template, 'My demo', '3.12', {'tests': True})
    main_file = pt.write_project(files, tmp_path / 'My demo')
    assert main_file == tmp_path / 'My demo' / 'main.py'
    assert (tmp_path / 'My demo' / 'tests' / 'test_main.py').is_file()
    with pytest.raises(FileExistsError):
        pt.write_project(files, tmp_path / 'My demo')


def test_melty_app_is_generated_and_valid_python():
    template = by_id()['melty_app']
    assert [item.name for item in template.inputs] == ['torch']
    files = pt.build(template, 'viewer', '3.12', {'torch': '2.8'})
    assert next(iter(files)) == 'app.py'
    ast.parse(files['app.py'])
    assert 'app_id="viewer"' in files['app.py'] and 'class AppState(DictConversion)' in files['app.py']
    assert 'torch==2.8.*' in files['requirements.txt'] and '[tensor]' in files['requirements.txt']
    assert 'torch' not in pt.build(template, 'viewer', '3.12')['requirements.txt']


def test_build_rejects_paths_outside_the_project():
    bad = pt.Template('bad', 'Bad', '', lambda name: {'../evil.py': ''}, ())
    with pytest.raises(ValueError):
        pt.build(bad, 'x', '3.12')


def test_destination(tmp_path):
    assert pt.project_destination(str(tmp_path), ' demo ') == tmp_path / 'demo'
    (tmp_path / 'demo').mkdir()
    for name in ('', 'a/b', '..', 'demo'):
        with pytest.raises(ValueError):
            pt.project_destination(str(tmp_path), name)
