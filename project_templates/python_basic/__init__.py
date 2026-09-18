"""A static template: the ``files`` folder, with {{placeholders}} filled in."""
from project_templates import identifier, static_files

ORDER = 10


def create(name: str, python_version: str, tests: bool = False):
    """Python Project.

    A main.py, a pyproject.toml and a README; optionally a tests folder.
    """
    files = static_files(__file__, name=name, distribution=identifier(name).replace("_", "-"), python_version=python_version)
    if not tests:
        files = {path: content for path, content in files.items() if not path.startswith('tests/')}
    return files
