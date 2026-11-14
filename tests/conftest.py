import pytest


@pytest.fixture(autouse=True)
def _melty_global(tmp_path_factory, monkeypatch):
    """No test writes the real global tint store (a folder that cannot hold a `.melty`)."""
    monkeypatch.setenv('MELTY_GLOBAL', str(tmp_path_factory.getbasetemp() / 'melty-global.json'))


@pytest.fixture
def tints(monkeypatch):
    """tint_model with fresh state: no watcher thread, render-thread posts run at once.
    `tints._check()` is one watcher pass; `tints.flush()` saves pending writes now."""
    from meltygui.model import tint_model
    for name, value in {'_tints': {}, '_files': {}, '_loaded': {}, '_shared': [False], '_pending': {},
                        '_state': {'watch': None, 'timer': None, 'queued': False, 'refresh': False}}.items():
        monkeypatch.setattr(tint_model, name, value)
    monkeypatch.setattr(tint_model, '_watch', lambda: None)
    monkeypatch.setattr(tint_model, '_post', lambda task: task())
    yield tint_model
    tint_model.flush()
