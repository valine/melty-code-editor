"""Local .app launcher: build with this project's Python; sources stay editable.

    .venv/bin/python macos.py --install --desktop

The bundle uses py2app's native launcher in alias mode. App metadata belongs
in pyproject.toml; this file owns the editor's macOS packaging and file events.
"""
import argparse
import os
from pathlib import Path
import plistlib
import runpy
import shutil
import subprocess
import sys
import time
import tomllib
import unicodedata


ROOT = Path(__file__).resolve().parent
_documents = None


def metadata():
    return tomllib.loads((ROOT / 'pyproject.toml').read_text())['tool']['melty']['app']


def bundle_plist(app):
    return {
        'CFBundleName': app['name'],
        'CFBundleDisplayName': app['name'],
        'CFBundleIdentifier': app['bundle_id'],
        'CFBundleShortVersionString': app['version'],
        'CFBundleVersion': app['version'],
        'NSHighResolutionCapable': True,
        'LSApplicationCategoryType': 'public.app-category.developer-tools',
        'LSMultipleInstancesProhibited': True,
        'CFBundleDocumentTypes': [{
            'CFBundleTypeName': 'Text and source code',
            'CFBundleTypeRole': 'Editor',
            'LSHandlerRank': 'Alternate',
            'LSItemContentTypes': ['public.text', 'public.source-code', 'public.json', 'public.xml'],
        }],
    }


def install_bundle(source, destination):
    """Replace only a launcher with our bundle identity; leave other apps alone."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        previous = destination / 'Contents' / 'Info.plist'
        with (source / 'Contents' / 'Info.plist').open('rb') as stream:
            expected = plistlib.load(stream)['CFBundleIdentifier']
        if not previous.is_file():
            raise ValueError(f'Not a Melty app bundle: {destination}')
        with previous.open('rb') as stream:
            if plistlib.load(stream).get('CFBundleIdentifier') != expected:
                raise ValueError(f'A different app already exists: {destination}')
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Leave one registered copy: an installed app plus a second copy in dist
    # otherwise both appear to Launch Services as the same application.
    shutil.move(str(source), str(destination))
    return destination


def build():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='install in ~/Applications')
    parser.add_argument('--desktop', action='store_true', help='also create a Desktop shortcut')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Build the launcher on macOS.')
    if Path(sys.prefix).resolve() != (ROOT / '.venv').resolve():
        parser.error('Use .venv/bin/python so the launcher uses the editor environment.')
    from PIL import Image
    from setuptools import setup

    app = metadata()
    work = ROOT / 'build' / 'macos'
    work.mkdir(parents=True, exist_ok=True)
    icon = work / 'app.icns'
    with Image.open(ROOT / app['icon']) as image:
        image.save(icon, format='ICNS')
    # py2app's native executable gives the running process its bundle identity.
    # Alias mode leaves all three checkouts and the selected venv in place.
    setup(name=app['name'], version=app['version'], packages=[], py_modules=[],
          app=[{'script': str(Path(__file__).resolve()), 'dest_base': app['name']}],
          options={'py2app': dict(alias=True, argv_emulation=False, no_chdir=True,
                                  iconfile=str(icon), plist=bundle_plist(app))},
          script_args=['py2app', '--dist-dir', str(ROOT / 'dist' / 'macos'),
                       '--bdist-base', str(work / 'temp')])
    bundle = ROOT / 'dist' / 'macos' / (app['name'] + '.app')
    # Alias mode links resources too. Keep the icon inside the bundle so
    # cleaning the generated build directory doesn't break Finder/Dock art.
    bundled_icon = bundle / 'Contents' / 'Resources' / icon.name
    bundled_icon.unlink()
    shutil.copy2(icon, bundled_icon)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(bundle)],
                   check=True, close_fds=False)
    if args.install or args.desktop:
        bundle = install_bundle(bundle, Path.home() / 'Applications' / bundle.name)
    if args.desktop:
        shortcut = Path.home() / 'Desktop' / bundle.name
        if not shortcut.is_symlink() and shortcut.exists():
            raise ValueError(f'Desktop item already exists: {shortcut}')
        if shortcut.is_symlink():
            shortcut.unlink()
        shortcut.symlink_to(bundle, target_is_directory=True)
    print(f'Launcher ready: {bundle}')


def open_documents(open_files, paths):
    """Use the same writable-file gate as command-line opening."""
    from meltygui.code.fileref import writable_file_refusal
    opened, refused = [], []
    for value in paths:
        path = Path(value).expanduser().resolve()
        reason = writable_file_refusal(path) if path.is_file() else 'not a file'
        if reason:
            refused.append(f'{path}: {reason}')
            continue
        open_files.open_file(path)
        opened.append(str(path))
    if opened:
        open_files.jump_to_path = opened[0]
    return opened, refused


def bind_open_files(open_files):
    """The editor supplies its existing model after loading its normal session."""
    if _documents is not None:
        _documents.open_files = open_files
        _documents.dispatch()


def _install_document_events():
    # Receive Finder's file events before GLFW runs Cocoa's launch loop. A
    # launch notification re-registers after AppKit installs its own handlers.
    # Keep GLFW's delegate: it owns Cmd-Q and orderly window-close/save handling.
    from AppKit import NSApplicationWillFinishLaunchingNotification
    from Foundation import NSObject, NSAppleEventManager, NSNotificationCenter, NSURL
    import objc

    fourcc = lambda value: int.from_bytes(value.encode('ascii'), 'big')

    class MeltyEditorDocuments(NSObject):
        @objc.python_method
        def register(self):
            manager = NSAppleEventManager.sharedAppleEventManager()
            manager.setEventHandler_andSelector_forEventClass_andEventID_(
                self, 'openDocuments:withReplyEvent:', fourcc('aevt'), fourcc('odoc'))
            manager.setEventHandler_andSelector_forEventClass_andEventID_(
                self, 'reopen:withReplyEvent:', fourcc('aevt'), fourcc('rapp'))

        def willLaunch_(self, notification):
            self.register()

        def openDocuments_withReplyEvent_(self, event, reply):
            descriptors = event.paramDescriptorForKeyword_(fourcc('----'))
            for index in range(1, descriptors.numberOfItems() + 1):
                item = descriptors.descriptorAtIndex_(index).coerceToDescriptorType_(fourcc('furl'))
                if item is not None:
                    # A file-URL descriptor carries UTF-8 bytes; stringValue
                    # only handles text descriptors and returns None here.
                    url = NSURL.URLWithString_(bytes(item.data()).decode('utf-8'))
                    if url is not None and url.isFileURL():
                        path = str(url.path())
                        normalized = unicodedata.normalize('NFC', path)
                        self.pending.append(normalized if Path(normalized).exists() else path)
            self.dispatch()

        def reopen_withReplyEvent_(self, event, reply):
            self.dispatch()

        @objc.python_method
        def dispatch(self):
            if self.open_files is None:
                return
            from meltygui.core.melty import Melty
            paths, self.pending = self.pending, []
            model = self.open_files

            def apply():
                from meltygui.core.windowing.surface import Surface
                from meltygui import window_api as glfw
                _, refused = open_documents(model, paths)
                for surface in Surface.all:
                    if not surface.closed and surface.parent is None:
                        glfw.restore_window(surface.window)
                        glfw.show_window(surface.window)
                        glfw.focus_window(surface.window)
                        surface.request_frame()
                        break
                if refused:
                    from AppKit import NSAlert
                    alert = NSAlert.alloc().init()
                    alert.setMessageText_('Could not open file')
                    alert.setInformativeText_('\n'.join(refused))
                    alert.runModal()
            Melty.post_to_render(apply)

    handler = MeltyEditorDocuments.alloc().init()
    handler.pending, handler.open_files = [], None
    handler.register()
    NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        handler, 'willLaunch:', NSApplicationWillFinishLaunchingNotification, None)
    return handler


def launch():
    global _documents
    app = metadata()
    # Finder doesn't load shell startup files. Add the known tool locations
    # directly, keeping project Python and Homebrew/uv available to task runners.
    python = ROOT / '.venv' / 'bin' / 'python'
    sys.executable = str(python)
    # Alias mode adds the venv's site-packages but leaves the embedded
    # interpreter's base prefix. Environment discovery must see our venv too.
    sys.prefix = sys.exec_prefix = str(ROOT / '.venv')
    locations = [str(python.parent), str(Path.home() / '.local' / 'bin'),
                 '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin']
    os.environ['PATH'] = os.pathsep.join(dict.fromkeys(locations + os.environ.get('PATH', '').split(os.pathsep)))
    os.environ['MELTY_T0'] = str(time.time())
    log = Path(os.environ.get('MELTY_LAUNCH_LOG') or
               Path.home() / 'Library' / 'Logs' / app['name'] / 'launch.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists():
        log.replace(log.with_name('previous.log'))
    with log.open('w') as stream:
        os.dup2(stream.fileno(), 1)
        os.dup2(stream.fileno(), 2)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(line_buffering=True)
    print(f'{app["name"]}: {ROOT} (Python {sys.version.split()[0]})')
    print(f'Interpreter: {sys.executable}; environment: {sys.prefix}')
    import glfw
    glfw.init_hint(glfw.COCOA_CHDIR_RESOURCES, False)
    _documents = _install_document_events()
    os.chdir(ROOT)
    entry = ROOT / app['entry']
    sys.argv[0] = str(entry)
    runpy.run_path(str(entry), run_name='__main__')


if __name__ == '__main__':
    if getattr(sys, 'frozen', False):
        # Keep one canonical module for the event handler while editor.py
        # becomes __main__ (its persisted classes keep their usual identity).
        import macos
        macos.launch()
    else:
        build()
