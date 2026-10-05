#!/usr/bin/env python3
"""Assemble the editor's locked CPython 3.13 iPhone package set.

Run with build/dependencies/tools/bin/python after the native build recipes.
Downloads are hash pinned in dependencies.json. Local packages are built as
ordinary wheels; no desktop virtualenv, editable installs or .pth files ship.
"""
from __future__ import annotations

import argparse
import email
import hashlib
from importlib.metadata import distributions
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from prepare_bundle import validate_device_binary

ROOT = Path(__file__).resolve().parent
EDITOR = ROOT.parents[1]


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(args, *, env=None):
    subprocess.run(list(map(str, args)), check=True, close_fds=False, env=env)


def download(pin, output, offline):
    path = output / pin['url'].rsplit('/', 1)[-1]
    if not path.exists():
        if offline:
            raise FileNotFoundError(f'Missing offline input: {path}')
        temporary = path.with_suffix('.download')
        try:
            with urllib.request.urlopen(pin['url'], timeout=60) as source, temporary.open('wb') as target:
                shutil.copyfileobj(source, target)
            if digest(temporary) != pin['sha256']:
                raise ValueError(f'Wrong download hash: {path.name}')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    if digest(path) != pin['sha256']:
        raise ValueError(f'Wrong cached hash: {path}')
    return path


def pure_wheel(pin, archive, output):
    if pin['kind'] == 'wheel':
        return archive
    wheels = output / 'wheels'
    wheels.mkdir(exist_ok=True)
    candidates = list(wheels.glob(pin['name'].replace('-', '_') + '-*.whl'))
    if candidates:
        wheel, = candidates
        return wheel
    with tarfile.open(archive) as source:
        source.extractall(output, filter='data')
        directory = output / PurePosixPath(source.getnames()[0]).parts[0]
    env = dict(os.environ)
    if pin['name'] == 'pyyaml-ft':
        # Upstream supports the same YAML API without its optional C speedup.
        env['PYYAML_FORCE_LIBYAML'] = '0'
    elif pin['name'] == 'watchdog':
        # Its upstream polling observer is selected automatically on iOS.
        setup = directory / 'setup.py'
        old = 'is_macos = sys.platform == "darwin" and not machine().lower().startswith(_apple_devices)'
        contents = setup.read_text()
        if old not in contents:
            raise ValueError('Pinned Watchdog build configuration changed')
        setup.write_text(contents.replace(old, 'is_macos = False  # iOS: no FSEvents extension'))
    else:
        raise ValueError(f'No portable build recipe for {pin["name"]}')
    runner = "import os,runpy,sys; os.chdir(sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path('setup.py',run_name='__main__')"
    run([sys.executable, '-c', runner, directory, 'setup.py', 'bdist_wheel', '--dist-dir', wheels], env=env)
    wheel, = wheels.glob(pin['name'].replace('-', '_') + '-*.whl')
    return wheel


def install_wheel(wheel, destination):
    """Install resources plus metadata, refusing code from another OS/CPU."""
    with zipfile.ZipFile(wheel) as archive:
        metadata_path, = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        metadata = email.message_from_bytes(archive.read(metadata_path))
        for member in archive.infolist():
            if member.is_dir():
                continue
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or '..' in relative.parts or '\\' in member.filename or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError(f'Unsafe wheel path: {member.filename}')
            if relative.parts[0].endswith('.data'):
                if relative.parts[1] not in ('purelib', 'platlib'):
                    continue  # Installed command scripts and build headers are not app resources.
                relative = PurePosixPath(*relative.parts[2:])
            if relative.suffix in ('.pth', '.dylib', '.dll', '.pyd'):
                raise ValueError(f'Unsupported wheel resource: {member.filename}')
            target = destination / relative
            if target.exists():
                raise ValueError(f'Conflicting package resource: {relative}')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
            if target.suffix == '.so':
                validate_device_binary(target)
    return dict(name=metadata['Name'], version=metadata['Version'], wheel=str(wheel), sha256=digest(wheel))


def validate_dependencies(packages):
    installed = {canonicalize_name(d.metadata['Name']): d for d in distributions(path=[str(packages)])}
    environment = default_environment() | dict(sys_platform='ios', platform_system='iOS', platform_machine='arm64',
                                               platform_release='17.0', platform_version='17.0', os_name='posix',
                                               implementation_name='cpython', platform_python_implementation='CPython',
                                               python_version='3.13', python_full_version='3.13.14',
                                               implementation_version='3.13.14', extra='')
    for name, distribution in installed.items():
        supported = distribution.metadata.get('Requires-Python')
        if supported and not SpecifierSet(supported).contains(environment['python_full_version']):
            raise ValueError(f'{name} requires Python {supported}; bundled runtime is {environment["python_full_version"]}')
    pending = [(name, '') for name in installed]
    seen = set()
    while pending:
        name, extra = pending.pop()
        if (name, extra) in seen:
            continue
        seen.add((name, extra))
        for text in installed[name].requires or ():
            requirement = Requirement(text)
            if requirement.marker and not requirement.marker.evaluate(environment | {'extra': extra}):
                continue
            key = canonicalize_name(requirement.name)
            if key not in installed or not requirement.specifier.contains(installed[key].version):
                raise ValueError(f'{name} requires {requirement}; compatible dependency absent from bundle')
            pending.extend((key, value) for value in requirement.extras)
    return installed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/editor-bundle')
    parser.add_argument('--wheel-dir', type=Path, action='append', default=[])
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--device-check', action='store_true', help='Include the isolated device smoke entry point')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    pure = ROOT / 'build/dependencies/pure'
    pure.mkdir(parents=True, exist_ok=True)
    wheels = []
    for pin in json.loads((ROOT / 'dependencies.json').read_text())['packages']:
        wheels.append(pure_wheel(pin, download(pin, pure, args.offline), pure))
    local = output / 'wheels'
    local.mkdir(exist_ok=True)
    for source in (EDITOR.parent / 'meltygui', EDITOR.parent / 'meltygui-pro'):
        run([sys.executable, '-m', 'build', '--wheel', '--no-isolation', '--outdir', local, source])
    wheels.extend(sorted(local.glob('*.whl')))
    directories = args.wheel_dir or [ROOT / 'build/binding', ROOT / 'build/dependencies/numeric/wheelhouse',
                                    ROOT / 'build/dependencies/platform/wheels', ROOT / 'build/dependencies/rust/wheelhouse',
                                    ROOT / 'build/dependencies/crypto/wheelhouse']
    for directory in directories:
        files = sorted(directory.glob('*.whl'))
        if not files:
            raise ValueError(f'Native build must finish first: no wheels in {directory}')
        wheels.extend(files)
    with tempfile.TemporaryDirectory(prefix='staging-', dir=output) as temporary:
        packages = Path(temporary) / 'packages'
        packages.mkdir()
        manifest = [install_wheel(wheel, packages) for wheel in wheels]
        installed = validate_dependencies(packages)
        app = Path(temporary) / 'app'
        app.mkdir()
        for source in EDITOR.glob('*.py'):
            if not source.name.startswith('profile_'):
                shutil.copy2(source, app / source.name)
        shutil.copytree(EDITOR / 'project_templates', app / 'project_templates',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        if args.device_check:
            shutil.copy2(ROOT / 'tests/device_dependency_check.py', app / 'device_dependency_check.py')
        for name in ('app', 'packages'):
            destination = output / name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.move(str(Path(temporary) / name), destination)
    (output / 'manifest.json').write_text(json.dumps({'schema': 1, 'packages': manifest}, indent=2) + '\n')
    print(f'Staged {len(installed)} distributions; complete iOS dependency closure verified: {output}')


if __name__ == '__main__':
    main()
