"""The embedded UIKit host's entry point for the shared Python editor."""
from __future__ import annotations


def create_app(config, host):
    import _melty_metal
    from meltygui.core.runtime import app
    from meltygui.core.runtime.native_app import NativeApplication
    from meltygui.core.graphics.metal_renderer import MetalRenderer

    application = NativeApplication(config, host, lambda: MetalRenderer(_melty_metal))
    app.install_native_host(application)
    # Installing the owner precedes decorators and persisted model loading.
    # This imports the same editor module used by the desktop launcher.
    import editor  # noqa: F401
    app.run()
    return application
