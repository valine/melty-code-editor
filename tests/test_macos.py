"""Launcher installation and Finder events, without opening editor windows."""
from pathlib import Path
import plistlib
import sys
from types import SimpleNamespace

import pytest

import macos


def app_bundle(path, identifier):
    contents = path / 'Contents'
    contents.mkdir(parents=True)
    (contents / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': identifier}))
    return path


def test_install_replaces_our_app_but_preserves_other_apps(tmp_path):
    source = app_bundle(tmp_path / 'dist' / 'Editor.app', 'org.meltygui.code-editor')
    destination = app_bundle(tmp_path / 'Applications' / 'Editor.app', 'another.app')
    marker = destination / 'Contents' / 'keep.txt'
    marker.write_text('existing app')
    with pytest.raises(ValueError, match='different app'):
        macos.install_bundle(source, destination)
    assert marker.read_text() == 'existing app'
    (destination / 'Contents' / 'Info.plist').write_bytes(
        (source / 'Contents' / 'Info.plist').read_bytes())
    (source / 'Contents' / 'python').symlink_to(sys.executable)
    macos.install_bundle(source, destination)
    assert not source.exists()  # one installed identity, no duplicate in dist
    assert not marker.exists()
    assert (destination / 'Contents' / 'python').is_symlink()


def test_document_opening_uses_the_existing_file_gate(tmp_path):
    writable = tmp_path / 'editable.py'
    readonly = tmp_path / 'readonly.py'
    library = tmp_path / 'site-packages' / 'library.py'
    library.parent.mkdir()
    for path in (writable, readonly, library):
        path.write_text('value = 1\n')
    readonly.chmod(0o444)
    received = []
    model = SimpleNamespace(open_file=received.append, jump_to_path=None)
    paths = [writable, readonly, library, tmp_path, tmp_path / 'missing.py']
    opened, refused = macos.open_documents(model, paths)
    assert received == [writable.resolve()]
    assert opened == [str(writable.resolve())]
    assert model.jump_to_path == opened[0]
    assert len(refused) == 4
    assert not (tmp_path / 'missing.py').exists()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS Apple Events')
def test_finder_events_queue_until_the_model_is_ready_then_use_the_render_thread(tmp_path, monkeypatch):
    from Foundation import NSAppleEventDescriptor, NSAppleEventManager, NSNotificationCenter, NSURL
    from meltygui.core.melty import Melty
    from meltygui.core.windowing.surface import Surface

    queued, received = [], []
    monkeypatch.setattr(Melty, 'post_to_render', queued.append)
    monkeypatch.setattr(Surface, 'all', [])
    handler = macos._install_document_events()
    monkeypatch.setattr(macos, '_documents', handler)
    fourcc = lambda value: int.from_bytes(value.encode('ascii'), 'big')

    def send(name):
        path = (tmp_path / name).resolve()
        path.write_text('value = 1\n')
        files = NSAppleEventDescriptor.listDescriptor()
        files.insertDescriptor_atIndex_(
            NSAppleEventDescriptor.descriptorWithFileURL_(NSURL.fileURLWithPath_(str(path))), 1)
        event = NSAppleEventDescriptor.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_(
            fourcc('aevt'), fourcc('odoc'), None, -1, 0)
        event.setParamDescriptor_forKeyword_(files, fourcc('----'))
        handler.openDocuments_withReplyEvent_(event, None)
        return path

    try:
        first = send('first file ü.py')
        assert handler.pending == [str(first)]
        assert queued == []
        model = SimpleNamespace(open_file=received.append, jump_to_path=None)
        macos.bind_open_files(model)
        assert received == []  # Cocoa callbacks cannot mutate the render model
        queued.pop(0)()
        assert received == [first]
        assert model.jump_to_path == str(first)
        assert handler.pending == []

        second = send('second file.py')
        assert received == [first]
        queued.pop(0)()
        assert received == [first, second]
        assert model.jump_to_path == str(second)
        handler.reopen_withReplyEvent_(None, None)
        queued.pop(0)()
        assert received == [first, second]  # reopening focuses without adding tabs
    finally:
        NSNotificationCenter.defaultCenter().removeObserver_(handler)
        manager = NSAppleEventManager.sharedAppleEventManager()
        for kind in ('odoc', 'rapp'):
            manager.removeEventHandlerForEventClass_andEventID_(fourcc('aevt'), fourcc(kind))
