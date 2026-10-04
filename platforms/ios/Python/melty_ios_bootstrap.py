"""Native host lifecycle. Does not import the desktop GLFW/OpenGL entry point."""
from __future__ import annotations

import importlib
import io
import os
from pathlib import Path
import sys

APP_ID = "melty-code-editor"
_app = None
_host = None


class Host:
    """The only native services the Python application needs during bootstrap."""

    def __init__(self, native):
        self._native = native

    def request_frame(self):
        """Wake display pacing; safe to call from a Python worker thread."""
        self._native.request_frame()

    def set_keyboard_visible(self, visible):
        self._native.set_keyboard_visible(bool(visible))


class _Log(io.TextIOBase):
    def __init__(self, native):
        self._native = native

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, text):
        if text:
            self._native.write_log(text)
        return len(text)

    def flush(self):
        pass


def initialize(config):
    """Create the application once on the native render thread.

    The packaged entry module must export create_app(config, host), returning
    an object with frame(info, events), suspend(), resume(), and close().
    """
    global _app, _host
    if _app is not None:
        raise RuntimeError("The iOS application is already initialized")
    if config.get("app_id") != APP_ID:
        raise ValueError(f"app_id must remain {APP_ID!r} for session restoration")
    import _melty_ios

    sys.stdout = sys.stderr = _Log(_melty_ios)
    _host = Host(_melty_ios)
    for key in ("documents", "workspace", "application_support", "cache"):
        Path(config[key]).mkdir(parents=True, exist_ok=True)
    os.chdir(config["workspace"])
    if not config["renderer_available"]:
        raise RuntimeError(
            "The iOS host and embedded Python started, but MeltyMetalRenderer "
            "is not linked. The existing GLFW/OpenGL editor cannot render here. "
            "Link the Metal adapter and package a melty_ios_app entry module."
        )
    entry = importlib.import_module(config.get("entry_module", "melty_ios_app"))
    app = entry.create_app(dict(config), _host)
    for name in ("frame", "suspend", "resume", "close"):
        if not callable(getattr(app, name, None)):
            raise TypeError(f"The iOS application must implement {name}()")
    _app = app
    print(f"{APP_ID}: embedded CPython {sys.version.split()[0]} initialized")


def frame(info, events):
    """True requests another frame; False puts the display link to sleep."""
    if _app is None:
        raise RuntimeError("The iOS application has not initialized")
    return bool(_app.frame(info, events))


def suspend():
    if _app is not None:
        _app.suspend()


def resume():
    if _app is not None:
        _app.resume()


def close():
    global _app
    if _app is not None:
        _app.close()
        _app = None
